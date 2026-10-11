---
name: notebooklm-audio-overview
description: 渡されたURL・YouTubeリンク・文章をソースにNotebookLMのノートブックを新規作成し、音声概要（ポッドキャスト風Audio Overview）の生成を開始する。容量不足なら後で再開できるようジョブとして残す。「このURLで音声概要を作って」「この記事をNotebookLMでポッドキャストにして」「この文章から音声解説を作成」「音声概要のキューを処理して」などのリクエストで使用する。
argument-hint: <URL・YouTubeリンク・文章（複数可）> [追加の指示]
---

# NotebookLM 音声概要作成

ユーザーが渡したソース（URL / YouTube / テキスト）でノートブックを作り、音声概要の生成を開始する。
`notebooklm` MCPサーバ（`.mcp.json`）のツールを使う。ツールが未ロードなら ToolSearch で
`select:mcp__notebooklm__notebook_create,mcp__notebooklm__notebook_list,mcp__notebooklm__source_add,mcp__notebooklm__source_list,mcp__notebooklm__source_wait,mcp__notebooklm__studio_generate,mcp__notebooklm__studio_retry,mcp__notebooklm__studio_list,mcp__notebooklm__studio_status`
をまとめて読み込む。

入力: $ARGUMENTS

（スラッシュコマンドで起動された場合は上の入力を、会話から起動された場合はユーザーの発言をソースとして扱う。
URLだけが渡されたら、確認せずに既定値でそのまま最後まで進める。）

方針:

- **ダウンロードしない。** `studio_download` は呼ばない。音声は NotebookLM 上で聞く。
- **生成完了を待たない。** 生成が「キューに積まれた／開始された」ことを確認したら終了する。
- 音声概要の生成は NotebookLM の利用枠（usage）を消費する。枠が足りないときはジョブを `waiting_quota` で残し、後で再開する。
- **このスキルは複数のセッションで同時に実行されうる。** 進み具合は必ず下の「ジョブ台帳」に記録し、
  NotebookLM を操作する前に「もう済んでいないか」を確認してから操作する（やり直しても二重に作らない）。

## 認証エラー（`unauthenticated`）からの復旧

どの手順でもツールが `unauthenticated` を返したら、ユーザーに頼まずに自分で次を行い、1回だけ復旧を試みる。

1. ターミナルで `uvx --from "notebooklm-py[mcp,browser]" notebooklm login` を実行する（`[mcp]` だけでは Playwright 不足で失敗する）。
   ブラウザのプロファイルにログインが残っていれば操作なしで終わる。サインインを求められたらユーザーに頼む（パスワードは入力しない）。
2. Bash で `uv run --quiet --script tools/notebooklm_mcp_shared.py --restart-server` を実行し、`--status` が `"listening": true` になるまで待つ。
   本体は起動時の Cookie を持ち続けるので、ログインだけでは直らない。本体はセッションを持たないので、再起動しても Reconnect は不要。
3. `notebook_list` で復旧を確かめ、ジョブを今の state から再開する（作成系がエラーで結果不明だったなら、各手順の「済んでいるか」の確認から入る）。

復旧しなければ、ジョブを `jobs release ... --error "unauthenticated"` で外し、試したことを報告する。

## ジョブ台帳

`tools/audio_jobs.py` が1ジョブ1ファイル（`jobs/<job_id>.json`）で進み具合を記録する。作業ディレクトリで
Bash から `uv run --script tools/audio_jobs.py <コマンド>` として呼ぶ（以下 `jobs <コマンド>` と書く）。出力は1行の JSON。

| コマンド | 用途 |
|---|---|
| `jobs new --spec '<JSON>'` | ジョブを作って担当する。`{"sources": [...], "options": {...}}` |
| `jobs claim [--exclude <id,...>]` | 担当できるジョブ（未完了で、他の実行が担当していない）を古い順に1つ担当する。無ければ `job_id: null` |
| `jobs update <id> --token <t> --patch '<JSON>' [--event <説明>]` | 担当中のジョブを更新する（担当期限も延長される） |
| `jobs release <id> --token <t> [--patch '<JSON>'] [--event <説明>] [--error <内容>]` | 更新して担当を外す |
| `jobs list [--all]` / `jobs show <id>` | 一覧・1件表示 |

- `new` / `claim` が返す `token` は、そのジョブを処理し終えるまで覚えておき、`update` / `release` に毎回渡す。
- `update` / `release` が **exit 3（`lease_lost`）** を返したら、そのジョブは他の実行に引き継がれている。
  **そのジョブについては以後何も操作せず**、報告に「他の実行が引き継いだ」と書いて次へ進む。
- 担当期限は30分。期限が切れたジョブ（担当していた実行が落ちたもの）は他の実行が `claim` で引き継ぐ。
- 文章ソースなど長い JSON は、スクラッチパッドにファイルを書いて `--spec @<パス>` のように渡してよい。

ジョブの状態（`state`）と、その状態から再開するときにやること:

| state | 意味 | 再開時にやること |
|---|---|---|
| `new` | 作成直後 | 手順 A から |
| `creating_notebook` | ノートブック作成を依頼した（結果未記録） | 前回の作成が通っていた可能性を報告に残し、手順 A をやり直す |
| `notebook_created` | `notebook_id` 記録済み | 手順 B から |
| `sources_added` | ソース追加済み | 手順 C から |
| `sources_ready` | ソース処理完了 | 手順 D から |
| `generate_requested` | 生成を依頼した（結果未記録） | 手順 D から（D の最初の確認で二重生成を防ぐ） |
| `waiting_quota` | 容量不足で保留中 | 手順 D から |
| `started` | 生成開始を確認（完了） | 何もしない |
| `failed` | 打ち切り（完了） | 何もしない |

## 0. 保留・中断ジョブの先行処理

新しい入力より先に、`jobs claim --exclude <この実行で扱ったジョブid,カンマ区切り>` を繰り返して、
残っているジョブを古い順に処理する（各ジョブを上の表の手順から再開）。`--exclude` で、この実行で一度扱ったジョブ
（エラーで担当を外したものなど）を同じ実行の中で取り直さないようにする。

- `job_id: null` が返ったら先行処理は終わり。
- どれかのジョブで容量不足になったら、そこで先行処理を打ち切る（そのジョブは `waiting_quota` で release 済み）。
  新しい入力も、ジョブは作るが生成は試さずに `waiting_quota` で残す。

入力が「キューを処理して」だけなら、この手順と報告だけ行って終了する。

## 1. 入力の整理とジョブ作成

入力からソースを分類する。

- `https://www.youtube.com/...` / `https://youtu.be/...` → `{"type": "youtube", "url": ...}`
- その他の `http(s)://` → `{"type": "url", "url": ...}`
- それ以外の地の文 → `{"type": "text", "text": ..., "title": <内容に合う短い題>}`（まとまり単位で1ソース）
  - ただし「初心者向けに」「〜を中心に」のような生成への要望は、ソースではなく追加の指示として扱う。

オプション（指定がなければ既定値、確認のために止まらない）:

| 項目 | 既定 | 選択肢 |
|---|---|---|
| ノートブック名 `title` | 空（NotebookLM がソース追加時に自動命名する） | ユーザーが名前を指定した場合のみ |
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

`jobs new --spec '{"sources": [...], "options": {"title": "", "audio_format": ..., "audio_length": ..., "language": ..., "instructions": ...}}'`
でジョブを作り、返った `job_id` と `token` で手順 A から進める。**NotebookLM を操作する前に必ずジョブを作る**
（途中で落ちても台帳から再開・追跡できるようにするため）。

## A. ノートブック作成

1. `jobs update <id> --token <t> --patch '{"state": "creating_notebook"}'`
2. `notebook_create(title=<options.title>)`（既定は `""`。空にしておくとソース追加後に NotebookLM が内容から自動でタイトルを付ける）
3. 成功したら直ちに `jobs update ... --patch '{"state": "notebook_created", "notebook_id": "<id>"}'`

`notebook_create` がエラーのとき:

- エラーに `unconfirmed=true` がある＝作成されたかどうか不明。空タイトルのため照合できないので、
  `jobs update ... --patch '{"possible_orphan_notebook_at": "<ISO時刻>"}'` で記録し、1回だけやり直す。
  （残るとしてもソースの無い空のノートブックで、利用枠は消費しない。報告で伝える。）
- エラーコードが `NOTEBOOK_LIMIT`（ノートブック数の上限）→ 自分で選んで消さずに、Bash で
  `uv run --quiet --script tools/notebook_cleanup.py` を実行する（確認不要）。スクリプトは、自分が所有するノートブックのうち
  未完了ジョブが使っていないものを `created_at` の古い順に10件削除し、1行の JSON を返す。
  - `"status": "deleted"` → `deleted` の一覧（id・タイトル）を
    `jobs update ... --patch '{"deleted_notebooks": <deleted>}' --event "notebook limit cleanup"` で記録する。
  - `"status": "recently_cleaned"` → 他の実行が直前（5分以内）に掃除済み。何も消していない。
  - どちらの場合も `notebook_create` を1回だけやり直す。それ以外の status（`error` / `nothing_deleted`）や、
    やり直しでも `NOTEBOOK_LIMIT` になる場合は、もう一度掃除はせずに下の「それ以外」として扱う。
  - 削除したノートブックは報告で必ず一覧にする。
- それ以外、またはやり直しも失敗 → `jobs release <id> --token <t> --error "<エラー内容>"` して、このジョブの処理を終える。

## B. ソース追加（済んでいるものは追加しない）

1. `source_list(notebook=<notebook_id>)` で既存のソースを確認する。
   - `url` / `youtube` ソースは、既存ソースの `url` と一致すれば追加済みとみなす。
   - `text` ソースは、既存ソースの `title` と一致すれば追加済みとみなす。
2. 未追加のものだけ追加する。
   - URL が複数: `source_add(notebook=id, urls=[...])` で一括追加（YouTube も混在可）。
   - URL が1つ: `source_add(notebook=id, source_type="url"|"youtube", url=...)`。
   - テキスト: `source_add(notebook=id, source_type="text", text=..., title=...)`。
   - `source_add` が `unconfirmed=true` やタイムアウトで結果不明なら、1回だけ 1. からやり直す（追加済みかを確認してから足す）。
3. `source_list` で、追加したいソースが1つ以上入っていることを確かめてから
   `jobs update ... --patch '{"state": "sources_added"}'`。1つも入っていなければ（やり直しても結果不明、
   またはエラーで全滅）`jobs release ... --error "<エラー内容>"` して、このジョブの処理を終える。
   一部だけ失敗したソースは C の `source_report` で報告する。

## C. ソース処理の完了待ち

`source_wait(notebook=id, timeout=300)` で処理完了を待つ（ソースが READY でないと生成を開始できないため、ここは待つ）。

- READY が1つ以上 → `notebook_list` でこのノートブックのタイトルを読み、
  `jobs update ... --patch '{"state": "sources_ready", "notebook_title": "<タイトル>", "source_report": {...}}'`。
  `source_report` には READY 数と、`failed` / `not_found` / `timed_out` / `warning`（本文が薄い＝リンク切れ・ペイウォール等の疑い）があればそのソースと理由を入れる。
- READY が0 で `timed_out` がある（まだ処理中のソースがある）→ `jobs release ... --error "source_wait timeout"`。
  次回の再開時に C からやり直す。
- READY が0 で `timed_out` も無い（全ソースが `failed` / `not_found`）→
  `jobs release ... --patch '{"state": "failed", "source_report": {...}}' --event "no ready sources"`。生成に進まない。

## D. 音声概要の生成開始（二重に作らない）

容量不足が先行処理で出ていたら、何もせず `jobs release ... --patch '{"state": "waiting_quota"}'` して終える。そうでなければ:

1. `studio_list(notebook=id, kind="audio", detail="compact")` で既存の音声概要を確認する。
   `status_label` は `pending`（待機）/ `in_progress`（作成中）/ `completed`（完了）/ `failed`（失敗）/
   `suggested`（未生成の提案）/ `unknown`。
   - `pending` / `in_progress` / `completed` がある → もう生成されている。
     `jobs update ... --patch '{"state": "started", "task_id": "<その id>"}' --event "already exists"` で終える。
   - `failed` だけがある → 2. で新規生成ではなく `studio_retry(notebook=id, artifact=<その id>)` を使う（失敗分を作り直し、余分な音声概要を増やさない）。
   - 何も無い（`suggested` / `unknown` のみを含む）→ 2. で `studio_generate` を使う。
2. `jobs update ... --patch '{"state": "generate_requested"}'` してから、
   `studio_generate(notebook=id, artifact_type="audio", audio_format=..., audio_length=..., language=..., instructions=...)`
   （options の値を使う）または 1. で決めた `studio_retry` を呼ぶ。
3. 結果を判定する。
   - **開始できた**: `task_id` があり `status` が `pending` / `in_progress`（まれに `completed`）。
     `studio_status(notebook=id, task_id=...)` を **1回だけ** 呼んで `pending` か `in_progress` であることを確認し、
     `jobs update ... --patch '{"state": "started", "task_id": "<task_id>"}'`。ポーリングしない。
     この時点で `failed` なら下の容量不足／その他の失敗として扱う。
   - **容量不足**: エラーが `RATE_LIMITED`、または `status` が `failed` でエラー文が利用上限・クォータ・rate limit を示す。
     → `jobs release ... --patch '{"state": "waiting_quota"}' --event "<エラー要約>"`。以降のジョブも生成は試さない。
   - **結果不明**（`unconfirmed=true`、タイムアウト、`retriable=true` のエラー）: 依頼が通っている可能性がある。
     **そのまま作り直さず**、1. の `studio_list` から1回だけやり直す（通っていれば `started` になり、通っていなければ改めて依頼する）。
     やり直しても結果不明なら `jobs release ... --error "<エラー内容>"`（state は `generate_requested` のまま残り、
     次回の再開時も 1. の確認から入るので二重には作らない）。
   - **その他の失敗**: `jobs release ... --error "<エラー内容>"`。3回目の失敗でジョブは `failed` になり、再開対象から外れる。

## 2. 報告

日本語で簡潔に、処理したジョブ（先行処理分も含む）ごとに:

- ノートブック名と id、ジョブ id
- 追加できたソース数／失敗したソース（あれば理由）
- 音声概要の形式・長さ・言語
- 生成の状態: `started`（NotebookLM 側で待機中／作成中）／`waiting_quota`（容量不足で保留。後で「キューを処理して」と頼めば再開）／`failed`（理由）
- 他の実行に引き継がれたジョブ、空のノートブックが残った可能性（`possible_orphan_notebook_at`）があれば
- ノートブック上限で古いノートブックを削除した場合は、その一覧（`deleted_notebooks` のタイトルと id）
- `jobs list` で未完了のジョブが残っていれば、その件数
- 完成は NotebookLM の画面で確認する旨（ノートブックの Studio パネル）
