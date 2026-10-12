# /// script
# requires-python = ">=3.11"
# dependencies = ["notebooklm-py"]
# ///
"""ノートブック数の上限に達したときに、古いノートブックをまとめて削除する。

音声概要スキルで notebook_create が NOTEBOOK_LIMIT を返したときに呼ぶ。
削除対象の選び方をエージェントの判断に任せず、ここで決める:

- 自分が所有するノートブック（is_owner が true）だけを対象にする（共有されただけのものは消さない）。
- 未完了ジョブ（jobs/*.json で state が started / failed 以外）が使っている notebook_id は除く。
- 残りを created_at の古い順に並べ、先頭から --count 件（既定 10）を削除する。

同時実行への備え:
- jobs/.cleanup.lock のプロセス間ロックの中で、一覧の取得から削除までを行う。
- 直近 COOLDOWN_SECONDS 以内に他の実行が掃除を済ませていれば、何も消さずに
  {"status": "recently_cleaned"} を返す（複数のセッションが同時に上限に当たっても、二重に消さない）。

NotebookLM の操作は notebooklm-py のライブラリ（NotebookLMClient）を直接使う。
audio_overview.py からは、認証切れの復旧付きでクライアントを渡す関数（NotebookLM.do）を渡して run_with() を使う。

使い方（出力は stdout に1行の JSON）:
  notebook_cleanup.py [--count N] [--dry-run]
終了コード: 0 成功（recently_cleaned を含む）/ 1 失敗
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

from notebooklm import NotebookLMClient
from notebooklm.exceptions import NotebookLMError
from notebooklm.types import Notebook

ROOT = Path(__file__).resolve().parent.parent

# op(client) を実行する関数。audio_overview.py の NotebookLM.do（認証切れの復旧付き）など。
Do = Callable[[Callable[[NotebookLMClient], Awaitable[Any]]], Awaitable[Any]]
JOBS_DIR = Path(os.environ.get("AUDIO_JOBS_DIR", ROOT / "jobs"))
LOCK_PATH = JOBS_DIR / ".cleanup.lock"
STATE_PATH = JOBS_DIR / ".cleanup.json"
COOLDOWN_SECONDS = 5 * 60
LOCK_TIMEOUT = 300.0
TERMINAL = {"started", "failed"}

IS_WINDOWS = os.name == "nt"


@contextlib.contextmanager
def cleanup_lock() -> Iterator[None]:
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
                    raise TimeoutError(f"could not lock {LOCK_PATH}") from None
                time.sleep(0.2)
        try:
            yield
        finally:
            if IS_WINDOWS:
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


def protected_notebook_ids() -> set[str]:
    """未完了ジョブが使っている notebook_id。"""
    ids = set()
    for path in JOBS_DIR.glob("*.json"):
        if path.name.startswith("."):
            continue
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if job.get("state") not in TERMINAL and job.get("notebook_id"):
            ids.add(job["notebook_id"])
    return ids


def last_cleanup_age() -> float | None:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return time.time() - float(state["at_epoch"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def record_cleanup(deleted: list[dict]) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {"at_epoch": time.time(), "at": datetime.now(UTC).isoformat(timespec="seconds"), "deleted": deleted},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, STATE_PATH)


def pick(notebooks: list[Notebook], protected: set[str], count: int) -> list[Notebook]:
    candidates = [nb for nb in notebooks if nb.is_owner and nb.id not in protected and nb.created_at]
    candidates.sort(key=lambda nb: nb.created_at)
    return candidates[:count]


async def run(count: int, dry_run: bool) -> dict:
    async with NotebookLMClient.from_storage() as client:

        async def do(op: Callable[[NotebookLMClient], Awaitable[Any]]) -> Any:
            return await op(client)

        return await run_with(do, count, dry_run)


async def run_with(do: Do, count: int, dry_run: bool) -> dict:
    with cleanup_lock():
        age = last_cleanup_age()
        if not dry_run and age is not None and age < COOLDOWN_SECONDS:
            return {"status": "recently_cleaned", "seconds_ago": round(age)}

        notebooks = await do(lambda c: c.notebooks.list())
        protected = protected_notebook_ids()
        targets = pick(notebooks, protected, count)
        summary = [
            {"id": nb.id, "title": nb.title, "created_at": nb.created_at.isoformat()} for nb in targets
        ]
        base = {"total_notebooks": len(notebooks), "protected": sorted(protected)}
        if dry_run:
            return {"status": "dry_run", **base, "would_delete": summary}

        deleted: list[dict] = []
        errors: list[dict] = []
        for nb in summary:
            try:
                await do(lambda c, nb_id=nb["id"]: c.notebooks.delete(nb_id))
                deleted.append(nb)
            except NotebookLMError as exc:
                errors.append({**nb, "error": f"{type(exc).__name__}: {exc}"})
        if deleted:
            record_cleanup(deleted)
        status = "deleted" if deleted else "nothing_deleted"
        return {"status": status, **base, "deleted": deleted, "errors": errors}


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        out = asyncio.run(run(args.count, args.dry_run))
    except Exception as exc:  # noqa: BLE001 - 呼び出し側には JSON で返す
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["status"] in {"deleted", "recently_cleaned", "dry_run"} else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
