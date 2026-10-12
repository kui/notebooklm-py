---
name: notebooklm-audio-overview
description: 渡されたURL・YouTubeリンク・文章をソースにNotebookLMのノートブックを新規作成し、音声概要（ポッドキャスト風Audio Overview）の生成を開始する。容量不足なら後で再開できるようジョブとして残す。「このURLで音声概要を作って」「この記事をNotebookLMでポッドキャストにして」「この文章から音声解説を作成」「音声概要のキューを処理して」などのリクエストで使用する。
argument-hint: <URL・YouTubeリンク・文章（複数可）> [追加の指示]
---

# NotebookLM 音声概要作成

ユーザーが渡したソース（URL / YouTube / テキスト）でノートブックを作り、音声概要の生成を開始する。
工程はすべて `tools/audio_overview.py` が行う。エージェントがやるのは **入力の解釈** と **結果の報告** だけ。
NotebookLM の MCP ツールを直接呼んで工程を進めない（二重作成の防止や台帳の更新はスクリプトが受け持つ）。

入力: $ARGUMENTS

（スラッシュコマンドで起動された場合は上の入力を、会話から起動された場合はユーザーの発言をソースとして扱う。
URLだけが渡されたら、確認せずに既定値でそのまま最後まで進める。）

スクリプトがやること（参考。手で再現しない）:

- 先にジョブ台帳（`jobs/<job_id>.json`）にジョブを作り、残っている未完了ジョブを古い順に処理してから新しいジョブを処理する。
- ノートブック作成 → ソース追加 → 処理完了待ち（最大5分）→ 音声概要の生成開始。生成の完了は待たず、ダウンロードもしない。
- 各工程の前に「もう済んでいないか」を確かめるので、何度実行しても二重には作らない。複数のセッションから同時に実行してよい。
- 容量不足ならジョブを `waiting_quota` で残し、同じ実行の残りのジョブも生成は試さない。
- ノートブック数の上限に当たったら、古いノートブックを10件削除して作り直す（`tools/notebook_cleanup.py`）。
- 認証切れ（`AuthError`、`rpc_code=16` の `RPCError`、開くときの `CSRF token not found`）は、
  `notebooklm login` で取り直して1回だけ自動復旧を試みる。NotebookLM の操作はライブラリを直接使い、MCP は使わない。

## 1. 入力の解釈

入力からソースを分類する。

- `https://www.youtube.com/...` / `https://youtu.be/...` → `{"type": "youtube", "url": ...}`
- その他の `http(s)://` → `{"type": "url", "url": ...}`
- それ以外の地の文 → `{"type": "text", "text": ..., "title": <内容に合う短い題>}`（まとまり単位で1ソース）
  - ただし「初心者向けに」「〜を中心に」のような生成への要望は、ソースではなく下のオプションとして扱う。

オプション（指定が無いものは書かない。スクリプトが既定値を入れる。確認のために止まらない）:

| キー | 既定 | 指定するとき |
|---|---|---|
| `title` | 空（NotebookLM が内容から自動命名） | ユーザーが名前を指定した場合 |
| `audio_format` | `deep-dive` | brief / critique / debate を指定された場合 |
| `audio_length` | `long` | short / default を指定された場合 |
| `language` | `ja` | 他言語を指定された場合 |
| `audience` | 「聞き手はITエンジニアを想定してください。」 | 聞き手を変える指定（例:「経営層向けに」）。既定プロンプトの1行目を置き換える1文を書く |
| `extra_instructions` | なし | その他の要望（例:「セキュリティを中心に」）。既定プロンプトの末尾に追記される |

既定プロンプトはITエンジニア向けに、仕組み・設計判断・トレードオフ・実務上の注意を深掘りし、
推測は推測と分かるように話す内容（全文は `tools/audio_overview.py` の `DEFAULT_INSTRUCTIONS`）。

入力が「キューを処理して」だけなら、ソースは無いので 2. で `--spec` を付けずに実行する。

## 2. 実行

spec をスクラッチパッドに JSON で書き、Bash で実行する（タイムアウトは 600000 ミリ秒にする。
ソースの処理待ちとログインで数分かかることがある）。

```
uv run --quiet --script tools/audio_overview.py --spec @<spec.json のパス>
```

spec の形: `{"sources": [...], "options": {...}}`（options は指定したキーだけ）。

出力は stdout に1行の JSON（stderr は進み具合）:

- `jobs`: 処理したジョブごとの結果。主なキー:
  `job_id` / `outcome` / `state` / `notebook_id` / `notebook_title` / `source_report` / `task_id` / `audio` / `error`、
  あれば `possible_orphan_notebook_at` / `previous_create_unconfirmed` / `deleted_notebooks`
- `outcome`: `started`（生成開始を確認）/ `waiting_quota`（容量不足で保留）/ `failed`（打ち切り）/
  `error`（今回は失敗。3回目で `failed` になる）/ `lease_lost`（他の実行が引き継いだ）/ `not_processed`（接続できず未着手）
- `fatal`: 全体を止めた原因（接続失敗、認証の復旧失敗など）。無ければ `null`
- `open_jobs`: 未完了で残っているジョブの数

スクリプトが `fatal` を返したり異常終了したりしても、ジョブは台帳に残っている。原因を報告し、
直せるもの（例: 認証の復旧に失敗してサインインが要る）はユーザーに伝える。工程を MCP ツールで手作業で続けない。

## 3. 報告

日本語で簡潔に、処理したジョブ（残っていたジョブの分も含む）ごとに:

- ノートブック名と id、ジョブ id
- 追加できたソース数／失敗したソース（`source_report` の `failed` / `not_found` / `timed_out` / `add_errors`、
  本文が薄い疑いの `warning`）
- 音声概要の形式・長さ・言語
- 生成の状態: `started`（NotebookLM 側で待機中／作成中）／`waiting_quota`（容量不足で保留。
  後で「キューを処理して」と頼めば再開）／`failed`・`error`（理由）
- `lease_lost` のジョブ、空のノートブックが残った可能性（`possible_orphan_notebook_at` / `previous_create_unconfirmed`）があれば
- ノートブック上限で古いノートブックを削除した場合は、その一覧（`deleted_notebooks` のタイトルと id）
- `open_jobs` が1以上なら、その件数
- 完成は NotebookLM の画面で確認する旨（ノートブックの Studio パネル）
