# /// script
# requires-python = ">=3.11"
# ///
"""音声概要スキルのジョブ記録（同時実行しても壊れない作業台帳）。

1ジョブ = jobs/<job_id>.json。スキルは NotebookLM を操作する前後でここに
「どこまで進んだか」を書き、中断・失敗・容量不足から安全に再開できるようにする。

同時実行への備え:
- すべての読み書きは jobs/.lock のプロセス間ロックの中で行う（読み→変更→書きが割り込まれない）。
- ファイルは一時ファイル + os.replace で丸ごと置き換える（途中までしか書かれない状態を作らない）。
- ジョブには「リース」（担当トークンと期限）を付ける。担当中のジョブは他の実行が取れない。
  更新のたびにトークンを照合し、期限切れで他の実行に奪われていたら更新を拒否する（exit 3）。
  期限切れのリースは、担当していた実行が落ちたものとみなして他の実行が引き継げる。

使い方（出力はすべて JSON。stdout に1行）:
  new     --spec JSON            ジョブを作って担当する。spec は {"sources": [...], "options": {...}}
  claim   [--job ID] [--exclude ID,...]
                                 担当できるジョブ（未完了・リース切れ）を古い順に1つ担当する
  update  ID --token T --patch JSON [--event TEXT]   担当中のジョブを更新しリースを延長する
  release ID --token T [--patch JSON] [--event TEXT] [--error TEXT]
                                 更新して担当を外す。--error は容量不足以外の失敗で、
                                 MAX_ERRORS 回目で state=failed にして打ち切る
  list    [--all]                ジョブ一覧（既定は未完了のみ）
  show    ID                     ジョブ1件
JSON 引数は文字列か、@ファイルパス で渡す。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

JOBS_DIR = Path(os.environ.get("AUDIO_JOBS_DIR", Path(__file__).resolve().parent.parent / "jobs"))
LOCK_PATH = JOBS_DIR / ".lock"
LEASE_SECONDS = float(os.environ.get("AUDIO_JOBS_LEASE_SECONDS", str(30 * 60)))
LOCK_TIMEOUT = 30.0
MAX_ERRORS = 3

# 状態の流れ:
#   new -> creating_notebook -> notebook_created -> sources_added -> sources_ready
#       -> generate_requested -> started
#   どこからでも: waiting_quota（容量不足。後で再開） / failed（打ち切り）
STATES = {
    "new",
    "creating_notebook",
    "notebook_created",
    "sources_added",
    "sources_ready",
    "generate_requested",
    "waiting_quota",
    "started",
    "failed",
}
TERMINAL = {"started", "failed"}

IS_WINDOWS = os.name == "nt"


class LeaseLost(Exception):
    pass


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextlib.contextmanager
def store_lock() -> Iterator[None]:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    f = open(LOCK_PATH, "a+b")  # noqa: SIM115 - ロック中は開いたままにする
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                if IS_WINDOWS:
                    import msvcrt

                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"lock timeout: {LOCK_PATH}") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            if IS_WINDOWS:
                import msvcrt

                f.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


def job_path(job_id: str) -> Path:
    if not job_id or any(c in job_id for c in "/\\.") or job_id.startswith("-"):
        raise ValueError(f"invalid job id: {job_id!r}")
    return JOBS_DIR / f"{job_id}.json"


def load(job_id: str) -> dict:
    return json.loads(job_path(job_id).read_text(encoding="utf-8"))


def save(job: dict) -> None:
    path = job_path(job["id"])
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    # Windows ではウイルス対策ソフト等が一瞬ファイルを掴んで置き換えに失敗することがある。
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def all_jobs() -> list[dict]:
    jobs = []
    for p in JOBS_DIR.glob("*.json"):
        with contextlib.suppress(OSError, ValueError):
            jobs.append(json.loads(p.read_text(encoding="utf-8")))
    return sorted(jobs, key=lambda j: (j.get("created_at", ""), j.get("id", "")))


def lease_active(job: dict) -> bool:
    lease = job.get("lease")
    return bool(lease) and lease.get("expires_at", 0) > time.time()


def take_lease(job: dict) -> str:
    token = secrets.token_hex(8)
    job["lease"] = {"token": token, "expires_at": time.time() + LEASE_SECONDS}
    return token


def check_lease(job: dict, token: str) -> None:
    lease = job.get("lease") or {}
    # 期限切れでも、まだ誰にも引き継がれていなければ（トークンが一致すれば）続行してよい。
    if lease.get("token") != token:
        raise LeaseLost(f"job {job['id']} is not held by this token")


def add_event(job: dict, event: str | None, state_before: str | None) -> None:
    entry = {"at": now_iso(), "state": job.get("state")}
    if state_before != job.get("state"):
        entry["from"] = state_before
    if event:
        entry["event"] = event
    job.setdefault("history", []).append(entry)


def apply_patch(job: dict, patch: dict) -> None:
    for key in ("id", "created_at", "lease", "history"):
        if key in patch:
            raise ValueError(f"cannot patch {key!r}")
    state = patch.get("state")
    if state is not None and state not in STATES:
        raise ValueError(f"unknown state {state!r}; one of {sorted(STATES)}")
    for key, value in patch.items():
        if value is None:
            job.pop(key, None)
        else:
            job[key] = value
    job["updated_at"] = now_iso()


def public(job: dict) -> dict:
    """出力用。トークンは担当者にだけ返すので、ジョブ本体の表示からは外す。"""
    out = dict(job)
    lease = out.pop("lease", None)
    out["leased"] = bool(lease) and lease.get("expires_at", 0) > time.time()
    return out


# --- コマンド ----------------------------------------------------------------------


def cmd_new(spec: dict) -> dict:
    if not isinstance(spec.get("sources"), list) or not spec["sources"]:
        raise ValueError("spec.sources must be a non-empty list")
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    job = {
        "id": job_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "state": "new",
        "sources": spec["sources"],
        "options": spec.get("options", {}),
    }
    with store_lock():
        token = take_lease(job)
        add_event(job, "created", None)
        save(job)
    return {"job_id": job_id, "token": token, "job": public(job)}


def cmd_claim(job_id: str | None, exclude: set[str] | None = None) -> dict:
    exclude = exclude or set()
    with store_lock():
        candidates = [load(job_id)] if job_id else all_jobs()
        for job in candidates:
            if job.get("state") in TERMINAL or lease_active(job) or job["id"] in exclude:
                continue
            had_lease = bool(job.get("lease"))
            token = take_lease(job)
            add_event(job, "claimed (previous lease expired)" if had_lease else "claimed", job["state"])
            save(job)
            return {"job_id": job["id"], "token": token, "job": public(job)}
    if job_id:
        job = load(job_id)
        reason = "terminal" if job.get("state") in TERMINAL else "leased by another run"
        return {"job_id": None, "reason": reason, "job": public(job)}
    return {"job_id": None}


def cmd_update(
    job_id: str, token: str, patch: dict, event: str | None, release: bool, error: str | None = None
) -> dict:
    with store_lock():
        job = load(job_id)
        check_lease(job, token)
        before = job.get("state")
        apply_patch(job, patch)
        if error:
            # 容量不足以外の失敗。MAX_ERRORS 回で打ち切り、以後は再開対象にしない。
            job["error_count"] = job.get("error_count", 0) + 1
            job["last_error"] = error
            if job["error_count"] >= MAX_ERRORS:
                job["state"] = "failed"
            event = f"{event}: {error}" if event else f"error: {error}"
        if release or job.get("state") in TERMINAL:
            job.pop("lease", None)
        else:
            job["lease"]["expires_at"] = time.time() + LEASE_SECONDS
        add_event(job, event, before)
        save(job)
    return {"job_id": job_id, "job": public(job)}


def cmd_list(include_all: bool) -> dict:
    with store_lock():
        jobs = all_jobs()
    rows = []
    for job in jobs:
        if not include_all and job.get("state") in TERMINAL:
            continue
        rows.append(
            {
                "id": job["id"],
                "state": job.get("state"),
                "leased": lease_active(job),
                "notebook_id": job.get("notebook_id"),
                "notebook_title": job.get("notebook_title"),
                "created_at": job.get("created_at"),
                "updated_at": job.get("updated_at"),
            }
        )
    return {"jobs": rows}


def parse_json_arg(value: str | None) -> dict:
    if value is None:
        return {}
    if value.startswith("@"):
        value = Path(value[1:]).read_text(encoding="utf-8")
    data = json.loads(value)
    if not isinstance(data, dict):
        raise ValueError("JSON argument must be an object")
    return data


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="audio_jobs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new")
    p.add_argument("--spec", required=True)
    p = sub.add_parser("claim")
    p.add_argument("--job")
    p.add_argument("--exclude", default="", help="取らないジョブ id（カンマ区切り）")
    for name in ("update", "release"):
        p = sub.add_parser(name)
        p.add_argument("job_id")
        p.add_argument("--token", required=True)
        p.add_argument("--patch")
        p.add_argument("--event")
        p.add_argument("--error")
    p = sub.add_parser("list")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("show")
    p.add_argument("job_id")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "new":
            out = cmd_new(parse_json_arg(args.spec))
        elif args.cmd == "claim":
            out = cmd_claim(args.job, {x for x in args.exclude.split(",") if x})
        elif args.cmd in ("update", "release"):
            out = cmd_update(
                args.job_id,
                args.token,
                parse_json_arg(args.patch),
                args.event,
                release=args.cmd == "release",
                error=args.error,
            )
        elif args.cmd == "list":
            out = cmd_list(args.all)
        else:
            with store_lock():
                out = {"job": public(load(args.job_id))}
    except LeaseLost as e:
        print(json.dumps({"error": "lease_lost", "message": str(e)}, ensure_ascii=False))
        return 3
    except (ValueError, OSError, TimeoutError) as e:
        print(json.dumps({"error": type(e).__name__, "message": str(e)}, ensure_ascii=False))
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
