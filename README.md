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

ログイン情報の期限が切れて MCP のツールが認証エラーを返すようになったら、もう一度 `login` を実行する。
Chromium のプロファイルに Google のログインが残っていれば、サインインし直さずに認証情報だけ更新される。
MCP サーバは Cookie が無効になると保存済みの認証情報を読み直すので、通常はこれで復旧する。
それでも認証エラーが続くときは、共有サーバを再起動する（接続中の各セッションは `/mcp` から `notebooklm` を Reconnect する必要がある）。

```bash
uv run --quiet --script tools/notebooklm_mcp_shared.py --restart-server
```

### 2. Claude Code を起動する

このディレクトリで Claude Code を開くと、[.mcp.json](.mcp.json) の `notebooklm` MCP サーバが読み込まれる。
初回はプロジェクトの MCP サーバを有効にするか聞かれるので許可する。

## MCP サーバの共有（シングルトン）

Claude Code は stdio の MCP サーバをセッションごとに起動するため、notebooklm-mcp を直接起動すると
セッションの数だけ本体が立ち上がり、それぞれが Google の Cookie を独立に更新してしまう。
そこで [.mcp.json](.mcp.json) からは [tools/notebooklm_mcp_shared.py](tools/notebooklm_mcp_shared.py) を起動し、
本体（`notebooklm-mcp --transport http`、`127.0.0.1:9420`）を全セッションで1つだけ動かす。

- 最初のセッションが接続したときに本体を起動し、最後のセッションが終わって30秒経つと止める（常駐しない）。
- 各セッションのラッパーは [mcp-proxy](https://pypi.org/project/mcp-proxy/) で stdio と HTTP を中継するだけで、Google とは通信しない。
- 状態とログは `~/.notebooklm/shared-mcp/`（`shim.log`、`server.log`）。

```bash
uv run --quiet --script tools/notebooklm_mcp_shared.py --status
```

注意: 本体は HTTP 経由の「リモート接続」扱いになるため、`studio_download` はファイルを保存せず署名付き URL を返し、
ローカルファイルをソースに追加するにはアップロード手順が要る（音声概要スキルはどちらも使わない）。

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

音声概要の生成は NotebookLM の利用枠を消費する。枠が足りないときはジョブを保留にし、
次回スキルを使ったとき、または「音声概要のキューを処理して」と頼んだときに古い順に再開する。

### 同時実行とやり直し

スキルは複数のセッションで同時に実行してもよい。進み具合は [tools/audio_jobs.py](tools/audio_jobs.py) が
`jobs/<job_id>.json` に1ジョブ1ファイルで記録する（プロセス間ロックの中で読み書きし、ファイルは丸ごと置き換える）。

- 各ジョブには担当中の実行を示す期限付きの担当権（30分）があり、担当中のジョブを他の実行が処理することはない。
  担当していた実行が落ちても、期限が切れれば次の実行が引き継ぐ。
- NotebookLM を操作する前に「もう済んでいないか」を確かめる（ソースは URL・題名で、音声概要は Studio の一覧で照合）。
  応答が失われて結果が分からないときも、確認してからやり直すので二重に作らない。
- 容量不足以外のエラーは3回で打ち切る。

```bash
uv run --quiet --script tools/audio_jobs.py list --all
```

スラッシュコマンドを使わず「この URL で音声概要を作って」と頼んでも同じスキルが使われる。
