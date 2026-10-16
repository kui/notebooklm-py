# notebooklm-py ワークスペース

[notebooklm-py](https://pypi.org/project/notebooklm-py/) の MCP サーバを Claude Code から使い、
URL や文章から NotebookLM のノートブックと音声概要を作るための作業ディレクトリ。

## 前提

- [uv](https://docs.astral.sh/uv/)（`uv` / `uvx` コマンド）がインストールされていること
- NotebookLM を使える Google アカウント

## セットアップ

### 1. NotebookLM にログインする（必須）

MCP サーバは notebooklm-py に保存されたログイン情報で NotebookLM にアクセスする。
**最初に一度、次のコマンドでログインしておくこと。** ブラウザが開くので Google アカウントでサインインする。

```bash
uvx --from "notebooklm-py[mcp,browser]" notebooklm login
```

ログインは Playwright の Chromium で行うため、`browser` extra が必要（`[mcp]` だけだと「Playwright not installed」で止まる）。
Chromium が未導入なら先に次を実行する。

```bash
uvx --from "notebooklm-py[mcp,browser]" playwright install chromium
```

MCP のツールが `unauthenticated` を返すようになったら（`UNEXPECTED` を返し、ログに `CSRF token not found` が出ている場合も同じ）、
ログインし直してから Claude Code で `notebooklm` MCP サーバを再接続する（`/mcp` から Reconnect、またはセッションを開き直す）。

```bash
uvx --from "notebooklm-py[mcp,browser]" notebooklm login
```

- Chromium のプロファイルに Google のログインが残っていれば、サインインし直さずに認証情報だけ更新される。
- MCP サーバは起動時に読んだ Cookie をメモリに持ち続け、`login` で書き換わったファイルを読み直さないことがある。
  そのため `login` だけでは復旧せず、再接続が要る。
- MCP サーバの Cookie ローテーション（`RotateCookies`）が 200 を返していても、Google 側でセッションが無効になることがある（原因は未特定）。
  2026-10-12 には、`login` から約2.5時間で、10分ごとのローテーションが成功し続けているのに無効になった。
  MCP サーバを1プロセスにまとめていても起きたので、数時間ごとに `login` が要るものとして扱う。
- 音声概要のスクリプト（`tools/audio_overview.py`）は MCP を使わないので、この手順は要らない（自分でログインし直して続ける）。

確認は `uvx --from "notebooklm-py[mcp]" notebooklm auth check --test --passive`（Cookie の更新や書き込みをしない）。

### 2. Claude Code を起動する

このディレクトリで Claude Code を開くと、[.mcp.json](.mcp.json) の `notebooklm` MCP サーバが読み込まれる。
初回はプロジェクトの MCP サーバを有効にするか聞かれるので許可する。

MCP サーバ（`notebooklm-mcp`、stdio）はセッションごとに1つ起動する。複数のセッションで同時に動いても、
Cookie の更新はライブラリがプロセス間ロックと差分保存で調停する（notebooklm-py 0.8.5 で確認）。

## 音声概要スキル

[.claude/skills/notebooklm-audio-overview](.claude/skills/notebooklm-audio-overview/SKILL.md)

```
/notebooklm-audio-overview https://example.com/article
```

URL・YouTube リンク・文章を渡すと、ノートブックを作成してソースを追加し、音声概要の生成を開始する。
生成の完了は待たず、ダウンロードもしない。完成した音声は NotebookLM の画面で聞く。
複数のソースや追加の指示も渡せる。

```
/notebooklm-audio-overview https://example.com/a https://youtu.be/xxxx Kubernetes の運用面を中心に
```

既定値:

| 項目 | 既定 |
|---|---|
| 形式 | deep-dive |
| 長さ | long |
| 言語 | ja |
| 指示 | ITエンジニアを聞き手に想定（基礎説明は省き、仕組み・トレードオフ・実務上の注意点を深掘り） |

ノートブック名は付けずに作成し、NotebookLM がソースの内容から自動で命名する。

工程はすべて [tools/audio_overview.py](tools/audio_overview.py) が行い、スキル（エージェント）は入力の解釈と結果の報告だけを受け持つ。
スクリプトは MCP を使わず、notebooklm-py のライブラリ（`NotebookLMClient`）を直接使う。
実行のたびにその時点の Cookie を読み込み、認証が切れていれば `notebooklm login` で取り直して開き直す。
Claude Code を使わずに直接実行してもよい。

```bash
uv run --quiet --script tools/audio_overview.py --spec '{"sources": [{"type": "url", "url": "https://example.com/article"}]}'
uv run --quiet --script tools/audio_overview.py   # 残っているジョブ（保留中のもの）を処理する
uv run --quiet --script tools/audio_overview.py --retry   # 生成に失敗したジョブも再試行する
uv run --quiet --script tools/audio_overview.py --adopt   # 音声の無い既存ノートブック（スキルの外で作ったものも）にも作る
```

音声概要の生成は NotebookLM の使用量の枠を消費する（チャットなどと共有で、5時間ごとに回復し、別に週の上限がある。
音声概要1本で5時間枠の4割ほどを使う）。生成を依頼する前に使用量を見て、枠が足りなければ NotebookLM の「後で生成」で予約し、
枠が回復すると NotebookLM 側で生成される（予約は数本まで）。予約枠も埋まっていれば依頼せずにジョブを保留にし、
「音声概要のキューを処理して」と頼んだときに古い順に再開する（オプション `defer` を `false` にすると予約せずに保留にする）。新しいソースで音声概要を頼んだときは
そのジョブだけを処理し、保留中のジョブには触れない。

ノートブック数が上限に達して作成できないときは、[tools/notebook_cleanup.py](tools/notebook_cleanup.py) が
自分の所有するノートブックのうち、音声概要を聞き終えたもの → 完成した音声概要が無いものの順に、
それぞれ作成日の古い順で10件削除してから作り直す（確認なし・取り消し不可）。
未再生・途中まで再生・生成中のノートブック、未完了ジョブが使っているノートブック、共有されただけのノートブックは消さない。
消せるものが無ければジョブを `waiting_notebook_slot` で保留にし、次回の実行で再開する（聞き終えれば空く）。
複数の実行が同時に上限に当たっても、5分以内に掃除済みなら消さずにやり直すだけにする。
何が消えるかは次で事前に確認できる。

```bash
uv run --quiet --script tools/notebook_cleanup.py --dry-run
```

### 再生状況

NotebookLM は音声概要ごとの再生位置をサーバに持っている（Web でもスマートフォンのアプリでも、再生すると更新される）。
[tools/audio_playback.py](tools/audio_playback.py) はこれを読み、ノートブックごとに
未再生（`unplayed`）／途中（`in_progress`）／再生済み（`played`、95% 以上か残り30秒以内）／生成中（`generating`）／
生成失敗（`failed`）／音声なし（`no_audio`）に分ける。再生済みの記録を手で付ける必要はない。

```bash
uv run --quiet --script tools/audio_playback.py --status unplayed,in_progress
```

### 同時実行とやり直し

スキルは複数のセッションで同時に実行してもよい。進み具合は [tools/audio_jobs.py](tools/audio_jobs.py) が
`jobs/<job_id>.json` に1ジョブ1ファイルで記録する（プロセス間ロックの中で読み書きし、ファイルは丸ごと置き換える）。

- 各ジョブには担当中の実行を示す期限付きの担当権（30分）があり、担当中のジョブを他の実行が処理することはない。
  担当していた実行が落ちても、期限が切れれば次の実行が引き継ぐ。
- NotebookLM を操作する前に「もう済んでいないか」を確かめる（ソースは URL・題名で、音声概要は Studio の一覧で照合）。
  応答が失われて結果が分からないときも、確認してからやり直すので二重に作らない。
- 容量不足以外のエラーは3回で打ち切る。
- 生成を始めた後の失敗（NotebookLM の画面に「音声解説を生成できませんでした」と出る状態）は、次の実行のはじめに検知して
  `generation_failed` にする。自動では再試行せず、「失敗したものを再試行して」と頼んだとき（`--retry`）だけやり直す。
  画面で音声概要を削除したものは作り直さない。

```bash
uv run --quiet --script tools/audio_jobs.py list --all
```

スラッシュコマンドを使わず「この URL で音声概要を作って」と頼んでも同じスキルが使われる。
