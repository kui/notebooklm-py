# /// script
# requires-python = ">=3.11"
# dependencies = ["notebooklm-py"]
# ///
"""音声概要の再生状況（未再生／途中／再生済み）を NotebookLM から取得する。

NotebookLM は音声概要ごとにユーザー単位の再生位置（playback_position）をサーバに持っている。
Web でもスマートフォンのアプリでも、再生するとこの位置が更新されるので、
どの端末で聞いたかに関係なく、手で記録しなくても再生済みかどうかが分かる。

ノートブックごとの判定（自分が所有するノートブックだけ）:
- played       完成した音声概要をすべて最後まで（PLAYED_RATIO 以上か、残り PLAYED_TAIL_SECONDS 以内）再生した
- in_progress  途中まで再生した音声概要がある
- unplayed     完成しているが、再生していない音声概要がある
- generating   音声概要を生成中（完成したものはまだ無い）
- scheduled    「後で生成」で予約中（使用量の枠が回復すると NotebookLM が生成する）
- failed       完成した音声概要が無く、生成に失敗したものがある（NotebookLM の画面で「再試行」が出る状態）
- no_audio     音声概要が無い
- unknown      一覧を取れなかった（消してよいか判断できないので、消さない側に倒す）

notebook_cleanup.py はこの判定を使い、played・no_audio・failed のノートブックだけを削除する。

使い方（結果は stdout に1行の JSON）:
  audio_playback.py [--status unplayed,in_progress,...]   指定した状態のノートブックだけを出す（既定はすべて）
終了コード: 0 成功 / 1 失敗
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from notebooklm import NotebookLMClient
from notebooklm.exceptions import NotebookLMError
from notebooklm.types import Artifact, ArtifactType, AudioArtifactUserState, Notebook

sys.path.insert(0, str(Path(__file__).resolve().parent))

# op(client) を実行する関数。audio_overview.py の NotebookLM.do（認証切れの復旧付き）など。
Do = Callable[[Callable[[NotebookLMClient], Awaitable[Any]]], Awaitable[Any]]

# 再生位置は終わり際で止まることがあるので、ここまで聞けば再生済みとみなす。
PLAYED_RATIO = 0.95
PLAYED_TAIL_SECONDS = 30.0
# ノートブックごとの音声一覧を同時に取りに行く数。
CONCURRENCY = 8

STATUSES = ("unplayed", "in_progress", "generating", "scheduled", "unknown", "failed", "no_audio", "played")
# 削除してよい状態（notebook_cleanup.py が使う）。
DELETABLE = {"played", "no_audio", "failed"}
# 「後で生成」で予約された音声概要の状態コード。ライブラリの ArtifactStatus には無い（status_str は "unknown" になる）。
SCHEDULED_STATUS = 7


def is_scheduled(a: Artifact) -> bool:
    return a.status == SCHEDULED_STATUS


def audio_entry(a: Artifact) -> dict:
    """完成した音声概要1件の再生状況。"""
    duration = a.duration_seconds
    position = a.user_state.playback_position_seconds if isinstance(a.user_state, AudioArtifactUserState) else None
    if position is None or position <= 0:
        status = "unplayed"
    elif duration and (position >= duration * PLAYED_RATIO or duration - position <= PLAYED_TAIL_SECONDS):
        status = "played"
    else:
        status = "in_progress"
    return {
        "artifact_id": a.id,
        "title": a.title,
        "status": status,
        "position_seconds": round(position, 1) if position is not None else None,
        "duration_seconds": round(duration, 1) if duration else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def notebook_status(audios: list[Artifact]) -> tuple[str, list[dict]]:
    done = [audio_entry(a) for a in audios if a.is_completed]
    if not done:
        if any(a.is_processing or a.is_pending for a in audios):
            return "generating", []
        if any(is_scheduled(a) for a in audios):
            return "scheduled", []
        return ("failed" if any(a.is_failed for a in audios) else "no_audio"), []
    kinds = {d["status"] for d in done}
    if kinds == {"played"}:
        return "played", done
    return ("in_progress" if "in_progress" in kinds else "unplayed"), done


async def report_for(do: Do, notebooks: list[Notebook]) -> list[dict]:
    """自分が所有するノートブックごとの再生状況。作成日の新しい順。"""
    # audio_overview.py は notebook_cleanup.py 経由でこのモジュールを読み込むので、使う時に読み込む。
    from audio_overview import notebook_url

    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(client: NotebookLMClient, nb: Notebook) -> dict:
        entry: dict[str, Any] = {
            "notebook_id": nb.id,
            "title": nb.title,
            "url": notebook_url(nb.id),
            "created_at": nb.created_at.isoformat() if nb.created_at else None,
            "sources_count": nb.sources_count,
        }
        try:
            async with sem:
                audios = await client.artifacts.list(nb.id, ArtifactType.AUDIO)
        except NotebookLMError as e:
            return {**entry, "status": "unknown", "error": f"{type(e).__name__}: {e}"}
        status, items = notebook_status(audios)
        return {**entry, "status": status, "audios": items}

    owned = [nb for nb in notebooks if nb.is_owner]
    owned.sort(key=lambda nb: nb.created_at.timestamp() if nb.created_at else 0, reverse=True)
    return await do(lambda c: asyncio.gather(*(one(c, nb) for nb in owned)))


async def report(do: Do) -> list[dict]:
    notebooks = await do(lambda c: c.notebooks.list())
    return await report_for(do, notebooks)


def summarize(entries: list[dict]) -> dict:
    return {s: sum(1 for e in entries if e["status"] == s) for s in STATUSES}


async def run(statuses: set[str] | None) -> dict:
    # 認証切れの復旧（notebooklm login）は audio_overview.py のものを使う。
    from audio_overview import NotebookLM

    nlm = NotebookLM()
    try:
        entries = await report(nlm.do)
    finally:
        await nlm.close()
    shown = [e for e in entries if statuses is None or e["status"] in statuses]
    return {"counts": summarize(entries), "notebooks": shown}


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", help="カンマ区切り: " + ",".join(STATUSES))
    args = parser.parse_args(argv)
    statuses = None
    if args.status:
        statuses = {s.strip() for s in args.status.split(",") if s.strip()}
        if unknown := statuses - set(STATUSES):
            parser.error(f"unknown status: {sorted(unknown)}")
    try:
        out = asyncio.run(run(statuses))
    except Exception as exc:  # noqa: BLE001 - 呼び出し側には JSON で返す
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
