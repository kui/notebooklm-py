# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.9,<2"]
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

NotebookLM には共有 MCP サーバのラッパー（tools/notebooklm_mcp_shared.py）に stdio で接続する。
本体を別に立てると Cookie を独立にローテーションしてしまうため。ラッパーは接続中のクライアントとして
登録されるので、Claude Code のセッションが無くても本体が起動する。

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
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
import audio_jobs as jobs  # noqa: E402
import notebook_cleanup  # noqa: E402

SHARED = TOOLS / "notebooklm_mcp_shared.py"
SERVER_LOG = (
    Path(os.environ.get("NOTEBOOKLM_SHARED_MCP_STATE", Path.home() / ".notebooklm" / "shared-mcp"))
    / "server.log"
)
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
ERROR_RE = re.compile(r"^(?P<code>[A-Z_]+): (?P<rest>.*)$", re.S)


def progress(msg: str) -> None:
    print(f"[audio_overview] {msg}", file=sys.stderr, flush=True)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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


# --- MCP 呼び出し ------------------------------------------------------------------


class ToolErr(Exception):
    def __init__(self, tool: str, text: str):
        super().__init__(f"{tool}: {text}")
        m = ERROR_RE.match(text.strip())
        self.code = m["code"] if m else "UNKNOWN"
        self.text = text
        self.retriable = "retriable=true" in text
        self.unconfirmed = "unconfirmed=true" in text

    @property
    def uncertain(self) -> bool:
        """依頼が通ったかどうか分からない失敗。"""
        return self.unconfirmed or self.retriable or self.code in {"TIMEOUT", "NETWORK"}

    @property
    def is_auth(self) -> bool:
        """認証切れ。形は3通りある:
        - コード AUTH
        - コード RPC で本文に "(unauthenticated)"（RPC の中身が rpc_code=16。HTTP は 200 なのでライブラリの自動更新が動かない）
        - コード UNEXPECTED で server.log に CSRF token not found（未ログイン用ページが返った）
        """
        if self.code == "AUTH" or "unauthenticated" in self.text.lower():
            return True
        return self.code == "UNEXPECTED" and csrf_failure_logged()


class AuthFailed(Exception):
    pass


class LeaseLost(Exception):
    pass


def csrf_failure_logged() -> bool:
    """UNEXPECTED の原因が Cookie 切れ（未ログイン用ページで CSRF トークンが無い）か。"""
    try:
        with SERVER_LOG.open("rb") as f:
            f.seek(max(0, SERVER_LOG.stat().st_size - 4000))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    return "CSRF token not found" in tail


def run_quiet(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    kwargs: dict = {"capture_output": True, "timeout": timeout, "stdin": subprocess.DEVNULL, "cwd": ROOT}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


def shared_cmd(*args: str) -> list[str]:
    return ["uv", "run", "--quiet", "--script", str(SHARED), *args]


def recover_auth() -> str | None:
    """ログインし直して本体を再起動する。失敗したらその理由を返す。"""
    progress("auth error; running notebooklm login")
    try:
        login = run_quiet(LOGIN_CMD, 200)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"login failed: {e!r}"
    if login.returncode != 0:
        return "login failed: " + (login.stdout + login.stderr).decode("utf-8", "replace").strip()[-300:]
    # 本体は起動時の Cookie を持ち続けるので、ログインだけでは直らない。
    run_quiet(shared_cmd("--restart-server"), 60)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        time.sleep(2)
        status = run_quiet(shared_cmd("--status"), 30)
        if b'"listening": true' in status.stdout:
            return None
    return "server did not come back after restart"


class NotebookLM:
    def __init__(self, session: ClientSession):
        self.session = session
        self.auth_recovered = False

    async def _call_once(self, name: str, args: dict, timeout: float) -> dict:
        result = await self.session.call_tool(name, args, read_timeout_seconds=timedelta(seconds=timeout))
        text = "".join(getattr(c, "text", "") for c in result.content)
        if result.isError:
            raise ToolErr(name, text)
        if result.structuredContent is not None:
            data = result.structuredContent
            return data.get("result", data) if set(data) == {"result"} else data
        return json.loads(text)

    async def call(self, name: str, args: dict, timeout: float = 120) -> dict:
        try:
            return await self._call_once(name, args, timeout)
        except ToolErr as e:
            if not e.is_auth:
                raise
            # 認証エラーはクライアントを開く前か RPC が拒否された段階で起きるので、依頼は通っていない
            # （unconfirmed=true が付いていても、サーバが unauthenticated で拒否しているので作られていない）。
            if self.auth_recovered:
                raise AuthFailed(str(e)) from e
            self.auth_recovered = True
            reason = await asyncio.to_thread(recover_auth)
            if reason:
                raise AuthFailed(reason) from e
            progress("auth recovered; retrying")
            try:
                return await self._call_once(name, args, timeout)
            except ToolErr as e2:
                if e2.is_auth:
                    raise AuthFailed(str(e2)) from e2
                raise


# --- ジョブの処理 -------------------------------------------------------------------


def same_source(want: dict, have: dict) -> bool:
    if want["type"] == "text":
        return have.get("title") == want.get("title")
    a, b = want.get("url") or "", have.get("url") or ""
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
            self._release_quietly(error=f"{type(e).__name__}: {e}")
            self.report["error"] = f"{type(e).__name__}: {e}"
            outcome = "error"
        final = self._load()
        self.report.update(
            outcome=outcome,
            state=final.get("state"),
            notebook_id=final.get("notebook_id"),
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
        retried_unconfirmed = cleaned = False
        while True:
            try:
                nb = await self.nlm.call("notebook_create", {"title": self.opts["title"]})
                break
            except ToolErr as e:
                if e.unconfirmed and not retried_unconfirmed:
                    retried_unconfirmed = True
                    self._update({"possible_orphan_notebook_at": now_iso()}, "notebook_create unconfirmed")
                    continue
                if e.code == "NOTEBOOK_LIMIT" and not cleaned:
                    cleaned = True
                    progress("notebook limit; cleaning up old notebooks")
                    result = await notebook_cleanup.run(10, False)
                    if result.get("status") == "deleted":
                        self._update({"deleted_notebooks": result["deleted"]}, "notebook limit cleanup")
                    elif result.get("status") != "recently_cleaned":
                        return self._fail(f"{e} / cleanup: {json.dumps(result, ensure_ascii=False)}")
                    continue
                return self._fail(str(e))
        self._update({"state": "notebook_created", "notebook_id": nb["notebook_id"]})
        return None

    # B. ソース追加 ---------------------------------------------------------------
    async def _existing_sources(self) -> list[dict]:
        out = await self.nlm.call("source_list", {"notebook": self.job["notebook_id"], "limit": 200})
        return out.get("sources", [])

    async def step_b(self) -> str | None:
        nb = self.job["notebook_id"]
        wanted = self.job["sources"]
        errors: list[dict] = []
        for attempt in range(2):
            existing = await self._existing_sources()
            missing = [w for w in wanted if not any(same_source(w, h) for h in existing)]
            if not missing:
                break
            uncertain = False
            urls = [w for w in missing if w["type"] != "text"]
            texts = [w for w in missing if w["type"] == "text"]
            calls: list[tuple[list[dict], dict]] = []
            if len(urls) > 1:
                calls.append((urls, {"notebook": nb, "urls": [w["url"] for w in urls]}))
            elif urls:
                calls.append((urls, {"notebook": nb, "source_type": urls[0]["type"], "url": urls[0]["url"]}))
            for w in texts:
                calls.append(([w], {"notebook": nb, "source_type": "text", "text": w["text"], "title": w["title"]}))
            for targets, args in calls:
                try:
                    res = await self.nlm.call("source_add", args, timeout=180)
                except ToolErr as e:
                    if e.uncertain:
                        uncertain = True
                    else:
                        errors.extend({"source": t.get("url") or t.get("title"), "error": str(e)} for t in targets)
                    continue
                for item in res.get("results") or []:
                    if item.get("commit_state") not in (None, "confirmed"):
                        uncertain = uncertain or item.get("commit_state") == "unconfirmed"
                        if item.get("error"):
                            errors.append({"source": item.get("url"), "error": item["error"]})
            if not uncertain:
                break
            progress("source_add result unconfirmed; re-checking")
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
    async def _notebook_title(self, nb: str) -> str:
        offset = 0
        while True:
            page = await self.nlm.call("notebook_list", {"limit": 50, "offset": offset})
            for item in page.get("notebooks", []):
                if item.get("id") == nb:
                    return item.get("title", "")
            if not page.get("has_more"):
                return ""
            offset += 50

    async def step_c(self) -> str | None:
        nb = self.job["notebook_id"]
        res = await self.nlm.call(
            "source_wait", {"notebook": nb, "timeout": SOURCE_WAIT_SECONDS}, timeout=SOURCE_WAIT_SECONDS + 120
        )
        report: dict[str, Any] = {"ready": res.get("ready_count", len(res.get("ready", [])))}
        for bucket in ("failed", "not_found", "timed_out"):
            if res.get(bucket):
                report[bucket] = res[bucket]
        warnings = [{"title": s.get("title"), "warning": s["warning"]} for s in res.get("ready", []) if s.get("warning")]
        if warnings:
            report["warning"] = warnings
        if self.job.get("source_add_errors"):
            report["add_errors"] = self.job["source_add_errors"]
        if report["ready"] > 0:
            title = await self._notebook_title(nb)
            self._update({"state": "sources_ready", "notebook_title": title, "source_report": report})
            return None
        if res.get("timed_out"):
            return self._fail("source_wait timeout", {"source_report": report})
        self._update({"state": "failed", "source_report": report}, "no ready sources", release=True)
        self.report["error"] = "no ready sources"
        return "failed"

    # D. 生成開始 -----------------------------------------------------------------
    async def _audio_items(self) -> list[dict]:
        out = await self.nlm.call(
            "studio_list", {"notebook": self.job["notebook_id"], "kind": "audio", "detail": "compact"}
        )
        return out.get("items", [])

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
        for attempt in range(2):
            items = await self._audio_items()
            active = next((i for i in items if i.get("status_label") in ACTIVE), None)
            if active:
                return self._started(active["id"], "already exists")
            failed = next((i for i in items if i.get("status_label") == "failed"), None)
            self._update({"state": "generate_requested"})
            try:
                if failed:
                    res = await self.nlm.call("studio_retry", {"notebook": nb, "artifact": failed["id"]})
                else:
                    res = await self.nlm.call(
                        "studio_generate",
                        {
                            "notebook": nb,
                            "artifact_type": "audio",
                            **{k: self.opts[k] for k in ("audio_format", "audio_length", "language", "instructions")},
                        },
                    )
            except ToolErr as e:
                if e.code == "RATE_LIMITED":
                    # rejected でも実際には受け付けられていることがある。
                    items = await self._audio_items()
                    active = next((i for i in items if i.get("status_label") in ACTIVE), None)
                    if active:
                        return self._started(active["id"], "accepted despite RATE_LIMITED")
                    return self._waiting_quota(e.text[:200])
                if e.uncertain and attempt == 0:
                    progress("generate result unconfirmed; re-checking")
                    continue
                return self._fail(str(e))
            task_id, status = res.get("task_id"), res.get("status")
            if task_id and status in ACTIVE:
                st = await self.nlm.call("studio_status", {"notebook": nb, "task_id": task_id})
                status = st.get("status")
                if status in ACTIVE:
                    return self._started(task_id)
                res = st
            error = str(res.get("error") or res.get("error_code") or f"status={status}")
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
    params = StdioServerParameters(command="uv", args=["run", "--quiet", "--script", str(SHARED)], cwd=str(ROOT))
    try:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            runner = Runner(NotebookLM(session))
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
    except Exception as e:  # noqa: BLE001 - 接続できなかった等。ジョブは台帳に残っている
        fatal = f"{type(e).__name__}: {e}"
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
        print(json.dumps({"fatal": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
