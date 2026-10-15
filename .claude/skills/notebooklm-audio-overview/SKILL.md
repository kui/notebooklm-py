---
name: notebooklm-audio-overview
description: 渡されたURL・YouTubeリンク・文章をソースにNotebookLMのノートブックを新規作成し、音声概要（ポッドキャスト風Audio Overview）の生成を開始する。容量不足なら後で再開できるようジョブとして残す。既存の音声の無いノートブック（スキルの外で作ったものも）に音声概要を作る。音声概要の再生状況（未再生・途中・再生済み）の一覧も出す。「このURLで音声概要を作って」「この記事をNotebookLMでポッドキャストにして」「この文章から音声解説を作成」「音声概要のキューを処理して」「未再生の音声概要は？」「聞いていない音声概要を一覧して」「音声の無いノートブックにも音声概要を作って」などのリクエストで使用する。
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

- 先にジョブ台帳（`jobs/<job_id>.json`）にジョブを作り、そのジョブだけを処理する。残っている未完了ジョブ（容量不足などで保留中のもの）には
  触れない。それらを処理するのは「キューを処理して」と明示的に頼まれたとき（`--spec` なしの実行）だけ。
- ノートブック作成 → ソース追加 → 処理完了待ち（最大5分）→ 音声概要の生成開始。生成の完了は待たず、ダウンロードもしない。
- 実行のはじめに、生成を始めたジョブ（`started`）の結果を確かめる。生成に失敗していたら `generation_failed` にする。
  自動では再試行しない。「失敗したものを再試行して」と明示的に頼まれたとき（`--retry`）だけ、NotebookLM の「再試行」と同じ操作でやり直す。
- 各工程の前に「もう済んでいないか」を確かめるので、何度実行しても二重には作らない。複数のセッションから同時に実行してよい。
- 容量不足ならジョブを `waiting_quota` で残し、同じ実行の残りのジョブも生成は試さない
  （容量制限の応答なのに受け付けられた場合も、残りのジョブは試さない）。
- ノートブック数の上限に当たったら、音声概要を聞き終えたもの → 音声の無いものの順に古いノートブックを10件削除して
  作り直す（`tools/notebook_cleanup.py`）。未再生・途中のものは消さない。消せるものが無ければ `waiting_notebook_slot` で保留にする。
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
入力が「失敗したものを再試行して」なら、`--spec` を付けずに `--retry` を付けて実行する（生成に失敗したジョブを保留中のジョブに戻して処理する）。
新しいソースを渡されただけのときは、保留中・失敗のジョブを処理しない（`--retry` も付けない。キューの処理は頼まれたときだけ）。
入力が「音声の無いノートブックにも音声概要を作って」なら、`--spec` を付けずに `--adopt` を付けて実行する。
自分のノートブックのうち音声概要が無い・生成に失敗したものを既定のオプションでジョブに登録し、生成を始める
（ソースの無いもの、台帳に既にあるものは登録しない）。

## 2. 実行

spec をスクラッチパッドに JSON で書き、Bash で実行する（タイムアウトは 600000 ミリ秒にする。
ソースの処理待ちとログインで数分かかることがある）。

```
uv run --quiet --script tools/audio_overview.py --spec @<spec.json のパス>
```

spec の形: `{"sources": [...], "options": {...}}`（options は指定したキーだけ）。

出力は stdout に1行の JSON（stderr は進み具合）:

- `jobs`: 処理したジョブごとの結果。主なキー:
  `job_id` / `outcome` / `state` / `notebook_id` / `notebook_url` / `notebook_title` / `source_report` / `task_id` / `audio` / `error`、
  （`notebook_url` はノートブックが作成済みなら、生成の成否にかかわらず入る）
  あれば `possible_orphan_notebook_at` / `previous_create_unconfirmed` / `deleted_notebooks`
- `outcome`: `started`（生成開始を確認）/ `waiting_quota`（容量不足で保留）/ `waiting_notebook_slot`（ノートブック上限で保留）/ `failed`（打ち切り）/
  `error`（今回は失敗。3回目で `failed` になる）/ `lease_lost`（他の実行が引き継いだ）/ `not_processed`（接続できず未着手）
- `fatal`: 全体を止めた原因（接続失敗、認証の復旧失敗など）。無ければ `null`
- `adopted`（`--adopt` のときだけ）: 既存ノートブックごとに `result` が `adopted`（登録した）/ `already_tracked`（台帳に既にある）/
  `skipped_no_sources`（ソースが無い）。登録したジョブの結果は `jobs` に入る
- `checked`: 生成を始めたジョブの確認結果。`result` が `completed`（完成）/ `generation_failed`（生成に失敗していた。再試行は頼まれたときだけ）/
  `deleted`（画面で音声概要が消されていた）/ `notebook_missing`（ノートブックが無い）
- `requeued`（`--retry` のときだけ）: 再試行に回したジョブ id の一覧
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
  後で「キューを処理して」と頼めば再開。自動では再開しない）／`waiting_notebook_slot`（ノートブック数の上限で、消してよい＝聞き終えた
  ノートブックが無いので保留。どれかを聞き終えてから「キューを処理して」と頼めば再開）／`failed`・`error`（理由）
- `lease_lost` のジョブ、空のノートブックが残った可能性（`possible_orphan_notebook_at` / `previous_create_unconfirmed`）があれば
- ノートブック上限で古いノートブックを削除した場合は、その一覧（`deleted_notebooks` のタイトルと id）
- `checked` のうち `generation_failed`・`deleted` のもの（生成失敗を検知した／削除されていた）。`completed` は件数だけ。
  生成失敗があれば「失敗したものを再試行して」と頼めば再試行する旨を添える
- `open_jobs` が1以上なら、その件数（「キューを処理して」と頼めば処理する旨を添える）
- 完成は NotebookLM の画面で確認する旨（ノートブックの Studio パネル）

報告の最後に、`notebook_url` があるジョブすべてについて、ノートブックの URL を1行ずつ出す
（生成が `started` 以外でも出す。`notebook_url` が無いジョブ＝ノートブック未作成のものは出さない）:

```
ノートブック: <notebook_title>
<notebook_url>
```

## 再生状況の一覧（「未再生の音声概要は？」など）

ソースが無く、再生状況を聞かれた場合はノートブックを作らず、次を実行する（タイムアウト 600000 ミリ秒）。

```
uv run --quiet --script tools/audio_playback.py --status unplayed,in_progress
```

再生状況は NotebookLM がサーバに持つ再生位置から判定する（PC でもスマートフォンでも、再生すれば反映される。手で記録しない）。
出力は1行の JSON: `counts`（状態ごとの件数）と `notebooks`（作成日の新しい順。`title` / `url` / `status` /
`audios[].position_seconds` / `audios[].duration_seconds`）。全件が欲しいと言われたら `--status` を付けない。

報告は日本語で、件数のあとに未再生・途中（生成失敗 `failed` を聞かれたらそれも）のノートブックを1件ずつ（途中のものは「12分/20分」のように位置も）出し、
URL を添える。
