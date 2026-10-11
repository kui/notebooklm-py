# notebooklm-py ワークスペース

[notebooklm-py](https://pypi.org/project/notebooklm-py/) の MCP サーバを Claude Code から使い、
URL や文章から NotebookLM のノートブックと音声概要を作るための作業ディレクトリ。

## 前提

- [uv](https://docs.astral.sh/uv/)（`uvx` コマンド）がインストールされていること
- NotebookLM を使える Google アカウント

## セットアップ

### 1. NotebookLM にログインする（必須）

MCP サーバは notebooklm-py に保存されたログイン情報で NotebookLM にアクセスする。
**最初に一度、次のコマンドでログインしておくこと。** ブラウザが開くので Google アカウントでサインインする。

```bash
uvx --from "notebooklm-py[mcp]" notebooklm login
```

ログイン情報の期限が切れて MCP のツールが認証エラーを返すようになったら、もう一度同じコマンドを実行する。

### 2. Claude Code を起動する

このディレクトリで Claude Code を開くと、[.mcp.json](.mcp.json) の `notebooklm` MCP サーバが読み込まれる。
初回はプロジェクトの MCP サーバを有効にするか聞かれるので許可する。

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

音声概要の生成は NotebookLM の利用枠を消費する。枠が足りないときは `queue/audio-overview.jsonl` に積み、
次回スキルを使ったとき、または「音声概要のキューを処理して」と頼んだときに古い順に生成を開始する。

スラッシュコマンドを使わず「この URL で音声概要を作って」と頼んでも同じスキルが使われる。
