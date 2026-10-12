# /// script
# requires-python = ">=3.11"
# dependencies = ["notebooklm-py"]
# ///
"""音声概要スキルの工程（ジョブ台帳に沿ったノートブック作成〜生成開始）をまとめて実行する。

エージェントがやるのは入力の解釈（ソースとオプションの決定）と結果の報告だけにして、
NotebookLM の操作・二重作成の防止・台帳の更新・エラー時の分岐はここで決める。

流れ:
1. --spec があれば、NotebookLM を触る前にジョブを作る（途中で落ちても台帳から再開できるように）。
2. 残っているジョブ（未完了で、他の実行が担当していないもの）を古い順に処理する。
3. 1. のジョブを処理する。
各ジョブは state に応じて A（ノートブック作成）→ B（ソース追加）→ C（処理待ち）→ D（生成開始）の
途中から再開する。各手順は「もう済んでいないか」を NotebookLM で確かめてから操作する。

NotebookLM の操作は notebooklm-py のライブラリ（NotebookLMClient）を直接使う。MCP は使わない。
実行のたびにその時点の Cookie を読み込み、終われば消えるので、古い Cookie を持ち続けることがない。
認証が切れていたら（ログインから数時間で切れる）、notebooklm login で取り直して開き直す。

使い方（結果は stdout に1行の JSON、進み具合は stderr）:
  audio_overview.py --spec '<JSON>'|@path   新しいジョブを作り、残りのジョブと合わせて処理する
  audio_overview.py                         残っているジョブだけを処理する
spec: {"sources": [{"type": "url"|"youtube", "url": ...} | {"type": "text", "text": ..., "title": ...}],
       "options": {"title", "audio_format", "audio_length", "language",
                   "instructions"（既定プロンプトを丸ごと置き換え）,
                   "audience"（既定プロンプトの1行目＝聞き手を置き換え）,
                   "extra_instructions"（既定プロンプトの末尾に追記）}}
終了コード: 0 結果を出力できた / 1 それ以外
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from notebooklm import NotebookLMClient
from notebooklm.exceptions import (
    AuthError,
    AuthExtractionError,
    NetworkError,
    NotebookLimitError,
    NotebookLMError,
    RateLimitError,
    RPCError,
    ServerError,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
)
from notebooklm.outcomes import CommitState
from notebooklm.types import Artifact, ArtifactType, AudioFormat, AudioLength, Source, SourceType

try:
    # NOTEBOOKLM_BASE_URL を反映した NotebookLM の URL。非公開モジュールなので無ければ既定のホストを使う。
    from notebooklm._env import get_base_url
except ImportError:  # pragma: no cover

    def get_base_url() -> str:
        return "https://notebook.google.com"


try:
    # 本文が薄いソースの判定（MCP の source_wait が付ける warning と同じもの）。非公開モジュールなので無ければ省く。
    from notebooklm._app.content_sanity import text_content_warning
except ImportError:  # pragma: no cover
    text_content_warning = None

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
import audio_jobs as jobs  # noqa: E402
import notebook_cleanup  # noqa: E402

LOGIN_CMD = [
    "uvx",
    "--from",
    "notebooklm-py[mcp,browser]",
    "notebooklm",
    "login",
    "--browser-timeout",
    "120",
]
SOURCE_WAIT_SECONDS = 300
DEFAULT_OPTIONS = {
    "title": "",
    "audio_format": "deep-dive",
    "audio_length": "long",
    "language": "ja",
}
DEFAULT_INSTRUCTIONS = [
    "聞き手はITエンジニアを想定してください。",
    "一般的なIT用語や基礎概念の説明は省き、技術的な仕組み、設計上の判断とトレードオフ、既存技術との違い、"
    "実務での使いどころや注意点を深掘りしてください。",
    "具体的な数値・事例・コード上の要点がソースにあれば積極的に取り上げてください。",
    "ソースに書かれていない推測や意見を述べるときは、そうと分かるように話してください。",
]
ACTIVE = {"pending", "in_progress", "completed"}
# 生成が failed になったときのエラー文にだけ使うので、広めに拾う。
QUOTA_RE = re.compile(r"quota|limit|上限|exhaust", re.I)
YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([\w-]{11})")

T = TypeVar("T")


def progress(msg: str) -> None:
    print(f"[audio_overview] {msg}", file=sys.stderr, flush=True)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def notebook_url(notebook_id: str) -> str:
    return f"{get_base_url()}/notebook/{notebook_id}"


def build_options(options: dict) -> dict:
    out = {**DEFAULT_OPTIONS, **{k: v for k, v in options.items() if k in DEFAULT_OPTIONS and v is not None}}
    if options.get("instructions"):
        out["instructions"] = options["instructions"]
    else:
        lines = list(DEFAULT_INSTRUCTIONS)
        if options.get("audience"):
            lines[0] = options["audience"]
        if options.get("extra_instructions"):
            lines.append(options["extra_instructions"])
        out["instructions"] = "\n".join(lines)
    return out


# --- NotebookLM 呼び出し ------------------------------------------------------------


class AuthFailed(Exception):
    pass


class LeaseLost(Exception):
    pass


def is_auth_error(exc: BaseException) -> bool:
    """認証切れか。形は3通りある:
    - AuthError / AuthExtractionError
    - RPCError で rpc_code=16（UNAUTHENTICATED。HTTP は 200 なのでライブラリの自動更新が動かない）
    - クライアントを開くときの ValueError「CSRF token not found」（Cookie が切れて未ログイン用ページが返った）
    """
    if isinstance(exc, (AuthError, AuthExtractionError)):
        return True
    if isinstance(exc, RPCError) and str(exc.rpc_code) == "16":
        return True
    text = str(exc)
    return isinstance(exc, ValueError) and ("CSRF token not found" in text or "Authentication expired" in text)


def is_uncertain(exc: BaseException) -> bool:
    """依頼が通ったかどうか分からない失敗（応答が失われた・タイムアウト・一時的なサーバエラー）。"""
    return bool(getattr(exc, "unconfirmed", False)) or isinstance(exc, (NetworkError, ServerError))


def error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def run_quiet(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    kwargs: dict = {"capture_output": True, "timeout": timeout, "stdin": subprocess.DEVNULL, "cwd": ROOT}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


def login() -> str | None:
    """ブラウザプロファイルのログインから Cookie を取り直す。失敗したらその理由を返す。"""
    progress("auth error; running notebooklm login")
    try:
        proc = run_quiet(LOGIN_CMD, 200)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"login failed: {e!r}"
    if proc.returncode != 0:
        return "login failed: " + (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()[-300:]
    return None


class NotebookLM:
    """NotebookLMClient を持ち、認証が切れたらログインして開き直し、1回だけやり直す。

    開くたびにその時点の storage_state.json を読み込む。開くとき（CSRF トークンの取得）にも
    認証切れは起きるので、開くことも do() の中で行う。
    """

    def __init__(self) -> None:
        self.stack: AsyncExitStack | None = None
        self.client: NotebookLMClient | None = None
        self.auth_recovered = False

    async def _open(self) -> NotebookLMClient:
        if self.client is None:
            stack = AsyncExitStack()
            try:
                self.client = await stack.enter_async_context(NotebookLMClient.from_storage())
            except BaseException:
                await stack.aclose()
                raise
            self.stack = stack
        return self.client

    async def close(self) -> None:
        if self.stack is not None:
            await self.stack.aclose()
        self.stack = self.client = None

    async def do(self, op: Callable[[NotebookLMClient], Awaitable[T]]) -> T:
        try:
            return await op(await self._open())
        except Exception as e:
            if not is_auth_error(e):
                raise
            # 認証エラーはクライアントを開く前か RPC が拒否された段階で起きるので、依頼は通っていない
            # （unconfirmed が付いていても、サーバが UNAUTHENTICATED で拒否しているので作られていない）。
            if self.auth_recovered:
                raise AuthFailed(str(e)) from e
            self.auth_recovered = True
            reason = await asyncio.to_thread(login)
            if reason:
                raise AuthFailed(reason) from e
            await self.close()
            progress("auth recovered; retrying")
            try:
                return await op(await self._open())
            except Exception as e2:
                if is_auth_error(e2):
                    raise AuthFailed(str(e2)) from e2
                raise


# --- ジョブの処理 -------------------------------------------------------------------


def same_source(want: dict, have: Source) -> bool:
    if want["type"] == "text":
        return have.title == want.get("title")
    a, b = want.get("url") or "", have.url or ""
    if a == b:
        return True
    ya, yb = YOUTUBE_ID_RE.search(a), YOUTUBE_ID_RE.search(b)
    return bool(ya and yb and ya[1] == yb[1])


class Runner:
    def __init__(self, nlm: NotebookLM):
        self.nlm = nlm
        self.quota_blocked = False

    async def process(self, job_id: str, token: str, job: dict) -> dict:
        self.job_id, self.token, self.job = job_id, token, job
        self.opts = build_options(job.get("options", {}))
        self.report: dict[str, Any] = {"job_id": job_id, "resumed_from": job.get("state")}
        progress(f"job {job_id}: state={job.get('state')}")
        try:
            outcome = await self._run()
        except LeaseLost:
            outcome = "lease_lost"
        except AuthFailed as e:
            self._release_quietly(error=f"unauthenticated: {e}")
            self.report["error"] = f"unauthenticated: {e}"
            outcome = "error"
        except Exception as e:  # noqa: BLE001 - 想定外の失敗もジョブに記録して次へ進む
            self._release_quietly(error=error_text(e))
            self.report["error"] = error_text(e)
            outcome = "error"
        final = self._load()
        self.report.update(
            outcome=outcome,
            state=final.get("state"),
            notebook_id=final.get("notebook_id"),
            # ノートブックができていれば、生成の成否にかかわらず開けるように URL を返す。
            notebook_url=notebook_url(final["notebook_id"]) if final.get("notebook_id") else None,
            notebook_title=final.get("notebook_title"),
            source_report=final.get("source_report"),
            task_id=final.get("task_id"),
            audio={k: self.opts[k] for k in ("audio_format", "audio_length", "language")},
        )
        for key in ("possible_orphan_notebook_at", "deleted_notebooks", "error_count", "last_error"):
            if final.get(key):
                self.report[key] = final[key]
        progress(f"job {job_id}: {outcome} (state={final.get('state')})")
        return self.report

    def _load(self) -> dict:
        with jobs.store_lock():
            return jobs.public(jobs.load(self.job_id))

    def _update(self, patch: dict | None = None, event: str | None = None, *, release: bool = False,
                error: str | None = None) -> dict:
        try:
            out = jobs.cmd_update(self.job_id, self.token, patch or {}, event, release=release, error=error)
        except jobs.LeaseLost as e:
            raise LeaseLost(str(e)) from e
        self.job = out["job"]
        return self.job

    def _release_quietly(self, error: str) -> None:
        try:
            self._update(release=True, error=error)
        except LeaseLost:
            pass

    def _fail(self, error: str, patch: dict | None = None) -> str:
        self._update(patch, release=True, error=error)
        self.report["error"] = error
        return "error" if self.job.get("state") != "failed" else "failed"

    async def _run(self) -> str:
        while True:
            state = self.job.get("state")
            if state in ("new", "creating_notebook"):
                step = self.step_a
            elif state == "notebook_created":
                step = self.step_b
            elif state == "sources_added":
                step = self.step_c
            elif state in ("sources_ready", "generate_requested", "waiting_quota"):
                step = self.step_d
            elif state in jobs.TERMINAL:
                self._update(release=True)
                return state
            else:
                return self._fail(f"unknown state {state!r}")
            outcome = await step()
            if outcome is not None:
                return outcome

    # A. ノートブック作成 ---------------------------------------------------------
    async def step_a(self) -> str | None:
        if self.job.get("state") == "creating_notebook":
            # 前回の作成が通っていた可能性がある（空タイトルなので照合できない）。
            self.report["previous_create_unconfirmed"] = True
        self._update({"state": "creating_notebook"})
        title = self.opts["title"]
        retried_unconfirmed = cleaned = False
        while True:
            try:
                nb = await self.nlm.do(lambda c: c.notebooks.create(title))
                break
            except NotebookLimitError as e:
                if cleaned:
                    return self._fail(error_text(e))
                cleaned = True
                progress("notebook limit; cleaning up old notebooks")
                result = await notebook_cleanup.run_with(self.nlm.do, 10, False)
                if result.get("status") == "deleted":
                    self._update({"deleted_notebooks": result["deleted"]}, "notebook limit cleanup")
                elif result.get("status") != "recently_cleaned":
                    return self._fail(f"{error_text(e)} / cleanup: {json.dumps(result, ensure_ascii=False)}")
            except NotebookLMError as e:
                if is_uncertain(e) and not retried_unconfirmed:
                    retried_unconfirmed = True
                    self._update({"possible_orphan_notebook_at": now_iso()}, "notebook create unconfirmed")
                    continue
                return self._fail(error_text(e))
        self._update({"state": "notebook_created", "notebook_id": nb.id})
        return None

    # B. ソース追加 ---------------------------------------------------------------
    async def _existing_sources(self) -> list[Source]:
        nb = self.job["notebook_id"]
        return await self.nlm.do(lambda c: c.sources.list(nb))

    async def _add(self, targets: list[dict]) -> tuple[bool, list[dict]]:
        """ソースを追加する。(結果不明があったか, 失敗したソース) を返す。"""
        nb = self.job["notebook_id"]
        uncertain = False
        errors: list[dict] = []
        urls = [t for t in targets if t["type"] != "text"]
        singles = [t for t in targets if t["type"] == "text"]
        if len(urls) > 1:
            try:
                outcomes = await self.nlm.do(lambda c: c.sources.add_urls_batch(nb, [t["url"] for t in urls]))
            except NotebookLMError as e:
                if is_uncertain(e):
                    return True, errors
                return False, [{"source": t["url"], "error": error_text(e)} for t in urls]
            for o in outcomes:
                state = o.outcome.commit_state if o.outcome else CommitState.UNKNOWN
                if state == CommitState.CONFIRMED:
                    continue
                if state == CommitState.UNKNOWN:
                    uncertain = True
                elif o.error is not None:
                    errors.append({"source": o.url, "error": error_text(o.error)})
        else:
            singles = urls + singles
        for t in singles:
            if t["type"] == "text":
                op = lambda c, t=t: c.sources.add_text(nb, t["title"], t["text"])  # noqa: E731
            else:
                op = lambda c, t=t: c.sources.add_url(nb, t["url"])  # noqa: E731
            try:
                await self.nlm.do(op)
            except NotebookLMError as e:
                if is_uncertain(e):
                    uncertain = True
                else:
                    errors.append({"source": t.get("url") or t.get("title"), "error": error_text(e)})
        return uncertain, errors

    async def step_b(self) -> str | None:
        wanted = self.job["sources"]
        errors: list[dict] = []
        for attempt in range(2):
            existing = await self._existing_sources()
            missing = [w for w in wanted if not any(same_source(w, h) for h in existing)]
            if not missing:
                break
            uncertain, errs = await self._add(missing)
            errors.extend(errs)
            if not uncertain:
                break
            progress("source add result unconfirmed; re-checking")
        existing = await self._existing_sources()
        present = [w for w in wanted if any(same_source(w, h) for h in existing)]
        if not present:
            return self._fail("no source could be added: " + json.dumps(errors, ensure_ascii=False))
        patch: dict[str, Any] = {"state": "sources_added"}
        if errors:
            patch["source_add_errors"] = errors
        self._update(patch)
        return None

    # C. ソース処理の完了待ち ------------------------------------------------------
    async def _thin_warning(self, src: Source) -> str | None:
        """本文が薄い（リンク切れ・ペイウォール・ボット対策ページ等の疑い）web ページなら警告文を返す。"""
        if text_content_warning is None or src.kind != SourceType.WEB_PAGE:
            return None
        nb = self.job["notebook_id"]
        try:
            full = await asyncio.wait_for(self.nlm.do(lambda c: c.sources.get_fulltext(nb, src.id)), 30)
            return text_content_warning(full.content or "", char_count=full.char_count)
        except AuthFailed:
            raise
        except Exception:  # noqa: BLE001 - 判定できなくても待ちは止めない
            return None

    async def step_c(self) -> str | None:
        nb = self.job["notebook_id"]
        ids = [s.id for s in await self._existing_sources()]
        results = await self.nlm.do(lambda c: c.sources.wait_all_until_ready(nb, ids, timeout=SOURCE_WAIT_SECONDS))
        ready = [r for r in results if isinstance(r, Source)]
        report: dict[str, Any] = {"ready": len(ready)}
        buckets = (
            ("failed", SourceProcessingError),
            ("not_found", SourceNotFoundError),
            ("timed_out", SourceTimeoutError),
        )
        for bucket, kind in buckets:
            hits = [{"source_id": sid, "error": str(r)} for sid, r in zip(ids, results, strict=True) if isinstance(r, kind)]
            if hits:
                report[bucket] = hits
        warnings = []
        for src in ready:
            if w := await self._thin_warning(src):
                warnings.append({"title": src.title, "warning": w})
        if warnings:
            report["warning"] = warnings
        if self.job.get("source_add_errors"):
            report["add_errors"] = self.job["source_add_errors"]
        if ready:
            notebook = await self.nlm.do(lambda c: c.notebooks.get(nb))
            self._update({"state": "sources_ready", "notebook_title": notebook.title, "source_report": report})
            return None
        if report.get("timed_out"):
            return self._fail("source wait timeout", {"source_report": report})
        self._update({"state": "failed", "source_report": report}, "no ready sources", release=True)
        self.report["error"] = "no ready sources"
        return "failed"

    # D. 生成開始 -----------------------------------------------------------------
    async def _audio_items(self) -> list[Artifact]:
        nb = self.job["notebook_id"]
        return await self.nlm.do(lambda c: c.artifacts.list(nb, ArtifactType.AUDIO))

    def _started(self, task_id: str, event: str | None = None) -> str:
        self._update({"state": "started", "task_id": task_id}, event, release=True)
        return "started"

    def _waiting_quota(self, event: str) -> str:
        self.quota_blocked = True
        self._update({"state": "waiting_quota"}, event, release=True)
        return "waiting_quota"

    async def step_d(self) -> str | None:
        if self.quota_blocked:
            self._update({"state": "waiting_quota"}, "quota exhausted earlier in this run", release=True)
            return "waiting_quota"
        nb = self.job["notebook_id"]
        o = self.opts
        for attempt in range(2):
            items = await self._audio_items()
            active = next((a for a in items if a.status_str in ACTIVE), None)
            if active:
                return self._started(active.id, "already exists")
            failed = next((a for a in items if a.is_failed), None)
            self._update({"state": "generate_requested"})
            try:
                if failed:
                    res = await self.nlm.do(lambda c: c.artifacts.retry_failed(nb, failed.id))
                else:
                    res = await self.nlm.do(
                        lambda c: c.artifacts.generate_audio(
                            nb,
                            language=o["language"],
                            instructions=o["instructions"],
                            audio_format=AudioFormat[o["audio_format"].upper().replace("-", "_")],
                            audio_length=AudioLength[o["audio_length"].upper()],
                        )
                    )
            except RateLimitError as e:
                # 拒否されたように見えても実際には受け付けられていることがある。
                items = await self._audio_items()
                active = next((a for a in items if a.status_str in ACTIVE), None)
                if active:
                    return self._started(active.id, "accepted despite RateLimitError")
                return self._waiting_quota(error_text(e)[:200])
            except NotebookLMError as e:
                if is_uncertain(e) and attempt == 0:
                    progress("generate result unconfirmed; re-checking")
                    continue
                return self._fail(error_text(e))
            task_id, status = res.task_id, str(res.status)
            if task_id and status in ACTIVE:
                st = await self.nlm.do(lambda c: c.artifacts.poll_status(nb, task_id))
                if str(st.status) in ACTIVE:
                    return self._started(task_id)
                res, status = st, str(st.status)
            error = str(res.error or res.error_code or f"status={status}")
            if QUOTA_RE.search(error):
                return self._waiting_quota(error[:200])
            return self._fail(f"generation failed: {error}")
        return self._fail("generate result unconfirmed twice")


# --- エントリポイント -----------------------------------------------------------------


async def run(spec: dict | None) -> dict:
    new = None
    if spec is not None:
        # 実際に使う値（既定プロンプトを当てはめた後）を台帳に残す。
        new = jobs.cmd_new({**spec, "options": build_options(spec.get("options", {}))})
        progress(f"created job {new['job_id']}")
    results: list[dict] = []
    fatal = None
    nlm = NotebookLM()
    try:
        runner = Runner(nlm)
        handled: set[str] = {new["job_id"]} if new else set()
        while True:
            claimed = jobs.cmd_claim(None, handled)
            if not claimed["job_id"]:
                break
            handled.add(claimed["job_id"])
            results.append(await runner.process(claimed["job_id"], claimed["token"], claimed["job"]))
            if results[-1].get("error", "").startswith("unauthenticated"):
                fatal = "unauthenticated"
                break
        if new and not fatal:
            results.append(await runner.process(new["job_id"], new["token"], new["job"]))
    except Exception as e:  # noqa: BLE001 - 想定外の失敗。ジョブは台帳に残っている
        fatal = error_text(e)
    finally:
        await nlm.close()
    if new and not any(r["job_id"] == new["job_id"] for r in results):
        # 処理できなかった新しいジョブは担当だけ外し、次回の実行で拾えるようにする。
        try:
            jobs.cmd_update(new["job_id"], new["token"], {}, "not processed: " + str(fatal), release=True)
        except jobs.LeaseLost:
            pass
        results.append({"job_id": new["job_id"], "outcome": "not_processed", "state": "new"})
    open_jobs = jobs.cmd_list(False)["jobs"]
    return {"jobs": results, "fatal": fatal, "open_jobs": len(open_jobs)}


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec")
    args = ap.parse_args(argv)
    try:
        spec = jobs.parse_json_arg(args.spec) if args.spec else None
        out = asyncio.run(run(spec))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"fatal": error_text(e)}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
