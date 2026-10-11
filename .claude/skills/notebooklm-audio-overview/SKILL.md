---
name: notebooklm-audio-overview
description: 渡されたURL・YouTubeリンク・文章をソースにNotebookLMのノートブックを新規作成し、音声概要（ポッドキャスト風Audio Overview）の生成を開始する。容量不足ならローカルキューに積む。「このURLで音声概要を作って」「この記事をNotebookLMでポッドキャストにして」「この文章から音声解説を作成」「音声概要のキューを処理して」などのリクエストで使用する。
argument-hint: <URL・YouTubeリンク・文章（複数可）> [追加の指示]
---

# NotebookLM 音声概要作成

ユーザーが渡したソース（URL / YouTube / テキスト）でノートブックを作り、音声概要の生成を開始する。
`notebooklm` MCPサーバ（`.mcp.json`）のツールを使う。ツールが未ロードなら ToolSearch で
`select:mcp__notebooklm__notebook_create,mcp__notebooklm__source_add,mcp__notebooklm__source_wait,mcp__notebooklm__studio_generate,mcp__notebooklm__studio_status`
をまとめて読み込む。

入力: $ARGUMENTS

（スラッシュコマンドで起動された場合は上の入力を、会話から起動された場合はユーザーの発言をソースとして扱う。
URLだけが渡されたら、確認せずに既定値でそのまま最後まで進める。）

方針:

- **ダウンロードしない。** `studio_download` は呼ばない。音声は NotebookLM 上で聞く。
- **生成完了を待たない。** 生成が「キューに積まれた／開始された」ことを確認したら終了する。
- 音声概要の生成は NotebookLM の利用枠（usage）を消費する。**枠が足りないときはローカルキューに積む**（下の「キュー」参照）。

## 0. キューの先行処理

キューファイル `<作業ディレクトリ>/queue/audio-overview.jsonl` があり、1行以上残っていれば、新しい入力より先に
古い順に「4. 音声概要の生成開始」を試す。

- 開始を確認できた行はファイルから削除する。
- 容量不足（下の判定）になったらそこで打ち切る。残りの行と新しい入力はキューに残る／積む。
- ノートブックが見つからない（`NOT_FOUND`）行は削除し、報告に含める。

入力が「キューを処理して」だけなら、この手順と報告だけ行って終了する。

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

すべて追加したら `source_wait(notebook=id, timeout=300)` で処理完了を待つ（ソースが READY でないと生成を開始できないため、ここは待つ）。

- `failed` / `not_found` があればユーザーに該当ソースを伝える。READYのソースが1つ以上あれば続行する。
- 全滅なら生成に進まず報告して終了する。
- READYでも `warning`（本文が薄い＝リンク切れ・ペイウォール等の疑い）が付いたら報告に含める。

## 4. 音声概要の生成開始

キューの先行処理で容量不足が出ていたら、呼ばずに直接キューに積む。そうでなければ:

`studio_generate(notebook=id, artifact_type="audio", audio_format=..., audio_length=..., language=..., instructions=...)`

結果を次のように判定する。

- **開始できた**: `task_id` があり `status` が `pending` / `in_progress`（まれに `completed`）。
  続けて `studio_status(notebook=id, task_id=...)` を **1回だけ** 呼び、`pending`（NotebookLM 側のキューに積まれた）
  か `in_progress`（作成開始）であることを確認して終了する。ポーリングしない。
  この時点で `failed` なら下の容量不足／その他の失敗として扱う。
- **容量不足**: エラーが `RATE_LIMITED`、または `status` が `failed` でエラー文が利用上限・クォータ・rate limit
  を示している場合。→ キューに積む。
- **その他の失敗**: 内容を報告する。`retriable=true` なら `studio_generate` を1回だけやり直し、それでも失敗なら報告して終える（キューには積まない）。

## キュー

容量不足のときは `<作業ディレクトリ>/queue/audio-overview.jsonl` に1行追記する（ディレクトリがなければ作る）。
ノートブックとソースは作成済みなので、再実行に必要な生成パラメータだけを残す。

```json
{"queued_at": "<ISO8601>", "notebook_id": "...", "notebook_title": "...", "audio_format": "deep-dive", "audio_length": "long", "language": "ja", "instructions": "..."}
```

キューは次回このスキルを使ったとき（0. の手順）、またはユーザーが「音声概要のキューを処理して」と頼んだときに消化する。
NotebookLM の利用枠は時間が経つと回復するので、報告では「後で『キューを処理して』と頼めば再開できる」と伝える。

## 5. 報告

日本語で簡潔に:

- ノートブック名と id
- 追加できたソース数／失敗したソース（あれば理由）
- 音声概要の形式・長さ・言語
- 生成の状態: `pending`（NotebookLM のキュー待ち）／`in_progress`（作成中）／ローカルキューに積んだ（容量不足）
- 先行処理したキューの結果と、キューの残り件数（あれば）
- 完成は NotebookLM の画面で確認する旨（ノートブックの Studio パネル）
