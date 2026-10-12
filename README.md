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

共有サーバは起動のたびに認証を確かめ、切れていれば `notebooklm login` を自動で実行してから本体を起動し直す（下の「MCP サーバの共有」）。
使っている途中で切れて、MCP のツールが `unauthenticated` を返すようになったら（`UNEXPECTED` を返し、`server.log` に
`CSRF token not found` が出ている場合も同じ）、ログインし直してから共有サーバを再起動する。

```bash
uvx --from "notebooklm-py[mcp,browser]" notebooklm login
uv run --quiet --script tools/notebooklm_mcp_shared.py --restart-server
```

- Chromium のプロファイルに Google のログインが残っていれば、サインインし直さずに認証情報だけ更新される。
- 本体は起動時に読んだ Cookie をメモリに持ち続け、`login` で書き換わったファイルを読み直さない。
  そのため `login` だけでは復旧せず、再起動が要る。
- 本体はセッションを持たない（stateless HTTP）ので、再起動しても接続中のセッションはそのまま使える（Reconnect 不要）。
- 本体の Cookie ローテーション（`RotateCookies`）が 200 を返していても、Google 側でセッションが無効になることがある（原因は未特定）。

確認は `uvx --from "notebooklm-py[mcp]" notebooklm auth check --test --passive`（Cookie の更新や書き込みをしない）。

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
- 本体は `FASTMCP_STATELESS_HTTP=true` で起動する（MCP のセッションを持たない）。ツールはどれも呼び出しごとに完結するので失うものはなく、本体を再起動してもクライアントは切れない。
- 本体を起動する前に `notebooklm auth check --test` で認証を確かめる。本体が止まっている間（PC の停止中など）は Cookie の更新が走らず、
  切れていることがあるため。失敗したら本体を先に起動してから（MCP の接続を待たせないため）`notebooklm login` を実行し、成功したら本体を起動し直す。
  Chromium のプロファイルに Google のログインが残っていれば操作なしで10秒ほどで終わる。残っていなければサインイン画面が開き、120秒待つ。
  Cookie が切れても NotebookLM はログイン画面へリダイレクトせず未ログイン用のページを返すことがあり、ライブラリ（0.8.4）はこれを
  認証切れと判定しないため、ライブラリ自身の自動復旧（`NOTEBOOKLM_HEADLESS_REAUTH` など）は効かない。
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

ノートブック数が上限に達して作成できないときは、[tools/notebook_cleanup.py](tools/notebook_cleanup.py) が
自分の所有するノートブックを作成日の古い順に10件削除してから作り直す（確認なし・取り消し不可）。
未完了ジョブが使っているノートブックと、共有されただけのノートブックは消さない。
複数の実行が同時に上限に当たっても、5分以内に掃除済みなら消さずにやり直すだけにする。
何が消えるかは次で事前に確認できる。

```bash
uv run --quiet --script tools/notebook_cleanup.py --dry-run
```

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
