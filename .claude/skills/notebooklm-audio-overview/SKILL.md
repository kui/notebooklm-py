---
name: notebooklm-audio-overview
description: 渡されたURL・YouTubeリンク・文章をソースにNotebookLMのノートブックを新規作成し、音声概要（ポッドキャスト風Audio Overview）を生成する。「このURLで音声概要を作って」「この記事をNotebookLMでポッドキャストにして」「この文章から音声解説を作成」などのリクエストで使用する。
argument-hint: <URL・YouTubeリンク・文章（複数可）> [追加の指示]
---

# NotebookLM 音声概要作成

ユーザーが渡したソース（URL / YouTube / テキスト）でノートブックを作り、音声概要を生成する。
`notebooklm` MCPサーバ（`.mcp.json`）のツールを使う。ツールが未ロードなら ToolSearch で
`select:mcp__notebooklm__notebook_create,mcp__notebooklm__source_add,mcp__notebooklm__source_wait,mcp__notebooklm__studio_generate,mcp__notebooklm__studio_status,mcp__notebooklm__studio_download`
をまとめて読み込む。

入力: $ARGUMENTS

（スラッシュコマンドで起動された場合は上の入力を、会話から起動された場合はユーザーの発言をソースとして扱う。
URLだけが渡されたら、確認せずに既定値でそのまま最後まで進める。）

## 1. 入力の整理

入力からソースを分類する。

- `https://www.youtube.com/...` / `https://youtu.be/...` → `youtube`
- その他の `http(s)://` → `url`
- それ以外の地の文 → `text`（まとまり単位で1ソース。複数の文章があれば別ソースにする）
  - ただし「初心者向けに」「〜を中心に」のような生成への要望は、ソースではなく追加の指示として扱う。

オプション（指定がなければ既定値、確認のために止まらない）:

| 項目 | 既定 | 選択肢 |
|---|---|---|
| ノートブック名 | 内容から短く自動命名（例: `音声概要: <主題>`） | 任意 |
| 形式 `audio_format` | `deep-dive` | deep-dive / brief / critique / debate |
| 長さ `audio_length` | `long` | short / default / long |
| 言語 `language` | `ja` | ユーザーが他言語を指定した場合のみ変更 |
| 指示 `instructions` | 下の既定プロンプト | ユーザーの要望があれば既定プロンプトの末尾に追記する。聞き手を変える指定（例:「経営層向けに」）なら1行目を置き換える |
| ダウンロード | する（`downloads/` に保存） | ユーザーが不要と言えばしない |

既定プロンプト（`instructions`）:

```
聞き手はITエンジニアを想定してください。
一般的なIT用語や基礎概念の説明は省き、技術的な仕組み、設計上の判断とトレードオフ、既存技術との違い、実務での使いどころや注意点を深掘りしてください。
具体的な数値・事例・コード上の要点がソースにあれば積極的に取り上げてください。
ソースに書かれていない推測や意見を述べるときは、そうと分かるように話してください。
```

## 2. ノートブック作成

`notebook_create(title=...)` を呼び、返った notebook id を以降で使う（名前ではなくidを使う）。

## 3. ソース追加

- URL が複数: `source_add(notebook=id, urls=[...])` で一括追加（YouTube も混在可）。
- URL が1つ: `source_add(notebook=id, source_type="url"|"youtube", url=...)`。
- テキスト: `source_add(notebook=id, source_type="text", text=..., title=<内容に合う短い題>)`。

すべて追加したら `source_wait(notebook=id, timeout=300)` で処理完了を待つ。

- `failed` / `not_found` があればユーザーに該当ソースを伝える。READYのソースが1つ以上あれば続行する。
- 全滅なら生成に進まず報告して終了する。
- READYでも `warning`（本文が薄い＝リンク切れ・ペイウォール等の疑い）が付いたら報告に含める。

## 4. 音声概要の生成

`studio_generate(notebook=id, artifact_type="audio", audio_format=..., audio_length=..., language=..., instructions=...)`

返った `task_id` で `studio_status(notebook=id, task_id=...)` をポーリングする。生成には通常 5〜15分かかる。

- ポーリングは短間隔で連打しない。Bash の `sleep` は使えないので、Monitor で待つか、
  ユーザーに「生成中」と伝えたうえで間隔をあけて確認する。
- `is_complete` かつ `media_ready` で完了。`error` が返ったら内容を報告し、`retriable=true` なら `studio_retry` を1回だけ試す。

## 5. ダウンロード（既定でする）

`studio_download(notebook=id, artifact_type="audio", path="<作業ディレクトリ>/downloads/<ノートブック名を安全なファイル名にしたもの>.mp4")`

保存できたら SendUserFile で音声ファイルを送る。

## 6. 報告

日本語で簡潔に:

- ノートブック名と id
- 追加できたソース数／失敗したソース（あれば理由）
- 音声概要の形式・長さ・言語
- 保存先ファイル（リンク形式）、または生成途中ならその状況と再確認の方法
