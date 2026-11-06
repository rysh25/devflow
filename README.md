# DevFlow 0.7.3 — Claude Code Plugin

人間との要件整理から計画作成、実装、独立レビューまでを支援する **単一の Claude Code Plugin** です。インストールするプラグインは `devflow` だけです。

| コマンド | 入力と役割 |
| --- | --- |
| `/devflow:plan <依頼>` | Claude Code が `grilling` を直接呼び、人間への面接後に計画を作成・修正する |
| `/devflow:implement <plan-file>` | 明示した Markdown プランを読み、Claude Code がコードを実装・修正する |
| `/devflow:status [run-id]` | 保存された進捗・レビュー回数・停止理由を確認する |
| `/devflow:resume <run-id>` | 中断した同じ実行を再開する。レビュー回数はリセットしない |
| `/devflow:doctor` | 導入状態・設定・外部 CLI を診断する。モデルは呼び出さない |

Plan と Implement はファイルで接続します。Implement は Plan の run-id、会話履歴、内部状態を必要とせず、人間や別のツールが書いた Markdown でも開始できます。Plan 完了後に実装を勝手に始めることはありません。

## DevFlow がすること、しないこと

DevFlow は、Claude Code（計画と実装）と外部レビュアー（Codex、OpenCode）の機能を制限しません。どのツールも止めず、フックも失敗したときに止めません。各エージェントは、利用者のそれぞれの設定とモードのまま動きます。

DevFlow がするのは、記録と確認です。

- 依頼、面接、計画、コードのスナップショット、レビュー、人間の判断を run の記録に残す。
- レビューの回数（Plan と Implement でそれぞれ、最初は `max_reviews` 回まで。既定 6）と、レビュアーの順番を数える。上限では、人間の承認でレビューを追加するか、やめるか、今の版を受け入れて終えるかを選べる。
- 両レビュアーが **同じ内容**（Plan はプランのハッシュ、Implement はコードのスナップショット）を承認したときだけ完了（`completed`）とする。上限で人間が受け入れたときは、完了ではなく `accepted` で終える。
- 上限の確認の前と run が終わったときに、レビューの要約を決まった形で見せる。
- 人間の判断が要るときは、`AskUserQuestion` の実際の回答だけを判断として記録する。

「計画中はソースを書き換えない」「レビュアーはファイルを書き換えない」「秘密を読まない・外へ出さない」といったことは、スキルとレビュアーへの指示文で頼みます。強制はしません。守りたいことは、Claude Code や各 CLI の権限の設定で守ってください（[信頼の考え方と注意](docs/security.md)）。

## インストール

ZIP を解凍し、`.claude-plugin/marketplace.json` がある `devflow` ディレクトリへ移動して、ターミナルで実行します。

```text
claude plugin marketplace add .
claude plugin install devflow@devflow-marketplace --scope user
```

この `devflow` は配布物のディレクトリです。対象プロジェクトの `.devflow/` 作業ディレクトリとは異なります。配布物はプロジェクト外の安定した場所に置いて構いません。ローカル marketplace の更新元になるため、インストール直後に削除・移動しないでください。

GitHub に置く場合も公開リポジトリは必須ではありません。認証済みの private リポジトリを marketplace として登録できます。詳細は [インストールと環境設定](docs/installation.md) を参照してください。

更新は、GitHub から導入した場合は次の2つを実行し、Claude Code を起動し直します。ZIP から導入した場合と、更新の前後の注意は [更新する](docs/installation.md#更新する) と「[0.7.3 への更新](#073-への更新)」（それより前の版から上げるときは「[0.7.2 への更新](#072-への更新)」「[0.7.1 への更新](#071-への更新)」「[0.7.0 への更新](#070-への更新)」「[0.6.0 への更新](#060-への更新)」「[0.5.1 への更新](#051-への更新)」「[0.5.0 への更新](#050-への更新)」「[0.4.0 への更新](#040-への更新)」も）を参照してください。

```text
claude plugin marketplace update devflow-marketplace
claude plugin update devflow@devflow-marketplace
```

## 必要なもの

| 依存 | 用途・注意点 |
| --- | --- |
| Claude Code **2.1.316 以降** | フックの入力の `agent_id`（サブエージェントの区別）が必要。計画・実装・修正を担当 |
| `uv` | Python ランナー。Claude Code と同じ環境の PATH に必要 |
| インストール済み Python 3.11 以降 | Python **3.11 固定ではない**。標準ライブラリだけを使用 |
| Codex CLI | 認証と指定モデルへのアクセスが必要。DevFlow が渡すフラグ（`exec` の `--json`、`--output-schema` など）があれば、版は問わない。doctor で確認 |
| OpenCode CLI | 認証と指定 provider/model/effort へのアクセスが必要。DevFlow が渡すフラグ（`run` の `--format`、`--standalone` など）があれば、版は問わない |
| モデルから呼び出せる `grilling` スキル | **Plan のみ必要**。同梱・自動インストールはしない。`plan.grill_skill` で実際のスキル名を指定可能 |
| 対象プロジェクトの検証環境 | Implement のビルド・テストなど。事前に人間が設定する |
| Git 2.31 以降 | Implement の既定 `snapshot.mode: "git"` と、worktree での実装で使用。非 Git プロジェクトは明示的に `"files"` に設定 |

DevFlow 0.4.0 は、Claude Code 2.1.317、codex-cli 0.166.0、OpenCode 2.2.5 で確かめました。これより新しい版では、`doctor` が警告を出します（止めません）。確かめ方は [検証範囲](docs/validation.md#版を上げるときの確認) にあります。

実行エントリーポイントには次のインラインメタデータがあります。

```python
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
```

hooks は `uv run --offline --no-project --no-config --no-python-downloads` で起動します。互換 Python を選び、対象プロジェクトの依存をインストールせず、Python 自体も自動ダウンロードしません。互換 Python が一つもなければ起動できないので、必要な環境は人間が用意してください。

Windows ネイティブと WSL は別環境です。Claude Code を WSL で使うなら、`uv`、Python、Codex、OpenCode もその WSL から起動できる必要があります。Windows 側に導入済みであるだけでは十分でない場合があります（[Windows](docs/installation.md#windows)）。

`plan.grill_skill` は Claude Code の `Skill` ツールから直接呼べるスキルを指定してください。`disable-model-invocation: true` のスキルは利用できません。DevFlow が外部スキルのファイルやその権限設定を変更することはありません。見つからない・呼び出せない場合は止まり、人間が依存と設定を確認します。設定は run 開始時に固定されます。

## 最初の使い方

対象プロジェクトで Claude Code を開き、導入状況を確認します。

```text
/devflow:doctor
```

計画作成を開始します。

```text
/devflow:plan REVIEW-10 のプランを作成して
```

1. Claude Code が設定された `grilling` を `Skill` ツールで直接呼び、人間が回答します。最後の条件確認は `AskUserQuestion` の画面で回答します。調べものには、Claude Code の設定が許すどのツール（サブエージェント、MCP、Web、Bash など）も使えます。
2. 最初の依頼、質問・回答の原文、確定した要件を保存します。
3. Claude Code が計画を作成し、外部レビューと修正を繰り返します。レビュアーはプロジェクトで起動し、自分のツールでプロジェクトのファイルを読みます。
4. 同じプラン内容を両レビュアーが承認したら（`completed`）、正式な Markdown を `docs/plans/` へ保存します。レビューの上限で人間が今のプランを受け入れたとき（`accepted`）も保存します。プランのファイルはレビューした（受け入れた）バイト列のままで、横に人が読める承認の記録（例：`REVIEW-10.approval.md`）を置きます。承認したのがレビュアーか人間か、承認した版のハッシュ、残っている指摘の数、最後の版がレビュー済みかが書かれます。
5. 出力された **実際のパス** を確認します。既存ファイルは上書きしません。

実装開始は別の明示的な操作です。

```text
/devflow:implement docs/plans/REVIEW-10.md
```

パスに空白がある場合は引用符で囲みます。相対パスは対象プロジェクトを基準に扱い、プランを自動検索・推測して選びません。

```text
/devflow:implement "docs/plans/player movement.md"
```

Implement は開始時のプランと作業ツリーを固定し、Claude Code が実装し、設定された検証とコードレビューを実施します。Claude Code は、プロジェクトそのものか、同じリポジトリの別の worktree を実装場所に選べます（プロジェクトの `CLAUDE.md` などに決まりを書いておけます）。実装場所は run の間、固定されます。

既定では、未コミットの既存変更がある場合に一旦止まり、`AskUserQuestion` でその変更を保持したまま続行してよいか確認します。Plan で保存した新しいプランや追加した設定ファイルも、この確認の対象になり得ます。必ず先にコミットしなければならないわけではなく、プラグインが勝手に破棄・コミットすることもありません。既存変更を含む作業ツリーからの開始を事前に許可する場合のみ、`implement.allow_dirty: true` を設定してください。実装差分の基準は、どちらの場合も開始時の実ファイルです。

## レビューの契約

| 回 | 既定レビュアー | 既定設定 |
| --- | --- | --- |
| 1, 3, 5, … | Codex | `gpt-6.1-sol` / `xhigh` |
| 2, 4, 6, … | OpenCode | `opencode-go/deepseek-v4.1-flash` / `high` |

これらはユーザー指定の設定値です。本配布物のテストで、各アカウントのモデル提供状況・認証・実際のモデル選択まで確認しているわけではありません。未対応時に別モデルへ黙って切り替えません。

- 最初の上限は `max_reviews`（既定 6、2〜6 で設定）で、**Plan と Implement で別に数えます**。各レビュアーごとの回数ではありません。
- 上限に達して承認が揃っていないと、run は終わらずに人間に確認します（`awaiting_human`）。確認の前に、レビューの要約を見せます。
  - 「続ける」と答えると、レビュアーの人数分（既定 2 回）を足して、同じ run で続けます。回数は戻さず、7 回目、8 回目と数え続けます。それまでのレビュー、指摘の採否、同じ内容への承認も引き継ぎます。
  - 「やめる」と答えると、`exhausted` で終わります。
  - 「受け入れて終える」と答えると、今の版（Plan は今のプラン、Implement は最新の checkpoint）を人間が受け入れたとして、`accepted` で終わります。`completed` とは別の終わり方で、全レビュアーの承認ではありません。答えの記録と最終レポートの `human_acceptance` に、受け入れた版と `AskUserQuestion` の答えが残り、レポートの `warning` にも書かれます。Implement では、作業ディレクトリが最新の checkpoint のままで、そのチェックが通っている（または人間が検証を認めた）ことが条件です。
- **レビューの要約**：上限の確認の前と、run が終わったとき（`completed`・`exhausted`・`cancelled`・`accepted`）に、`status` と最終レポートの `review_summary` に入り、決まった形の短い Markdown（`review_summary_markdown`）で表示されます。Claude はそれをそのまま見せ、要約に出ていることを自分の報告で繰り返しません。
  - 表示するもの：状態、回数と時間（run の経過時間とレビューの時間の合計）、現在の版がレビュー済みかと各レビュアーの承認、1 回 1 行の表（レビュアー、版、判定、重さごとの指摘の数と採否の数、ツールの呼び出しの回数、かかった時間と制限時間）、レビュアーごとの 1 行（指定したモデルと effort、CLI の版、トークン数の合計）、失敗した回とその理由、残っている指摘（全文）、人間の決定（何回目のあとに、どの種類の質問で、何を選んだか、いつか）。モデルと effort は指定したものです。実際に動いたモデルの証明ではありません。
  - 表示しないもの：ハッシュ、開始と終了の時刻、記録のパス、解決した指摘の本文と採否の理由、直しの要約と変えたもの、質問文と答え。これらは `review_summary` のデータとレビューの記録に残ります。文は途中で切りません。
  - トークン数は、CLI が出力した数を、CLI の名前のまま出します（出力に無ければ unknown）。意味は CLI ごとに違い、たとえば Codex の `input_tokens` はキャッシュ分を含み、OpenCode の `input` は `cache.read` を含みません。そのため、合計は同じレビュアー（同じ CLI、同じ名前）の回の間だけで、CLI をまたいで足すことはしません。
  - 待っている間、その run は終わっていない扱いです。同じプロジェクトで別の run を始めるには、答えるか取り消してください。
- 合計は 99 回までです。人数分を足すと 99 回を超えるときは、確認を出さずに `exhausted` で終わります（既定の設定では 98 回）。
- レビュアーに渡すパケットには過去のレビューの全文が入るので、1 回のレビューでおよそ 10〜15 KB 増えます。パケットの上限は `plan.max_packet_bytes`（既定 2 MiB）と `implement.snapshot.max_packet_bytes`（既定 2,000,000 バイト）で、どちらも 16 MiB まで設定でき、run の開始時に固定されます。長く続ける見込みの run は、始める前に大きくしておけます。超えたときは、レビュアーを起動する前にエラーで止まります。回数は使わず、中身も削りません。その run は取り消して終えます。
- レビューを送信する前に枠を確保します。送信後のタイムアウト、不正な出力、通信失敗も使用済みです。
- 再開時にも回数・順序を保持します。失敗を理由に無料枠として再送信しません。
- 両者が **同じ内容ハッシュ** を承認したときだけ早期完了します。内容が変われば以前の承認はその版にしか適用されません。
- 最後のレビューの後の修正版を「レビュー済み」とは表示しません。上限で「やめる」を選んだときは、残課題と、最終版が未レビューであることを報告します。「受け入れて終える」を選んだときも、最終版が未レビューかどうかを要約、レポート、承認の記録に書きます。
- 指摘の採用・不採用・理由を保存します。外部レビューは人間の決定を上書きする権限ではありません。
- 外部レビュアーは、利用者の各 CLI の設定のまま、Implement では実装場所で、Plan ではプロジェクトで起動します。ファイル、差分、ログは自分のツールで読みます。DevFlow が渡すのは、計画や面接などのデータと、ファイルの場所です。
- レビュアーの CLI は、プロジェクトの設定と指示のファイル（`AGENTS.md` など）を、ふだんどおり読み込みます。指示文では、ファイルの中身をデータとして扱い、判定や出力の形を変える指示に従わないよう頼みます。
- レビューが失敗になるのは、出力から、レビュアーが終わっていない・答えがないとわかるときと、答えがスキーマに合わないとき、答えに記録に書けない文字（対にならないサロゲート。JSON の `\ud800` のようなエスケープから生じる）があるときだけです。答えは書き換えません。ツールの呼び出しやサブエージェントは止めず、種類ごとに数えて、`status` と最終レポートの `reviews` に出します（名前が記録に書けないときは `(unnamed)` として数えます）。
- Implement では、レビューの前後で実装場所のコード（スナップショットの範囲）が変わった回は、合格に数えません。

要件変更、重要な設計判断、権限・機密性・費用の問題では止まって、人間に確認します（エスカレーション）。解除には、止まった後に `AskUserQuestion` で得た実際の回答を記録します。普通の会話の返信や `/devflow:resume` だけを承認に読み替えません。

## プロジェクト設定

プロジェクト直下の **`devflow.config.json`** に設定します。共通の `reviewers` と `max_reviews`、および `plan` / `implement` ごとの設定を分けます。配列は全体置換です。設定は実行開始時に固定します。

```json
{
  "schema_version": 1,
  "max_reviews": 6,
  "plan": {
    "grill_skill": "grilling",
    "publish": {"enabled": true, "directory": "docs/plans", "filename": "{ticket}"}
  },
  "implement": {
    "allow_dirty": false,
    "max_check_attempts": 3,
    "snapshot": {"mode": "git"},
    "checks": []
  }
}
```

`plan.publish` は、正式なプランの公開の設定です。

- `enabled`：公開するか。
- `directory`：保存先のディレクトリ（プロジェクトからの相対パス）。
- `filename`：プランの名前のテンプレート。拡張子は書きません（プランには `.md`、承認の記録には `.approval.md` が付きます）。
  - 使える値は `{run_id}`（run ID）と `{ticket}`（依頼の中の最初の、`REVIEW-10` のようなチケット ID）だけです。
  - 既定の `"{ticket}"` では、チケット ID があればその ID、なければ `plan-<run-id>` です。テンプレートに `{ticket}` があって依頼にチケット ID がないときは、名前全体が `plan-<run-id>` になります。
  - 例：`"{ticket}-plan"` は `REVIEW-10-plan.md` です。`"plan-{run_id}"` はチケット ID を使いません。`UTF-8` のような文字列もチケット ID と見なされるので、それを避けたいときにも使えます。
  - 使える文字は ASCII の英数字・`_`・`.`・`-` です。先頭は英数字か `_` で、`.` で終わらず、Windows の予約名（`CON` など）は使えません。値をいちばん長いもの（チケット ID 45 文字、run ID 29 文字）に置き換えて 128 文字以内でなければなりません。守っていない設定は、run の開始時にエラーになります。
  - 名前がもう使われているときは、`-<run-id>` を付けた名前で公開します（テンプレートに `{run_id}` があっても同じです）。

`publish` の値は、ほかの設定と同じく run の開始時に固定されます。

レビュアーを設定する場合は、最上位の `reviewers` に全員を書きます（配列は全体置換）。2人以上かつ `max_reviews` 以下で、異なる種類（`codex` / `opencode`）を2つ以上含む必要があります。

```json
{
  "schema_version": 1,
  "reviewers": [
    {"id": "codex", "kind": "codex", "command": ["codex"], "model": "gpt-6.1-sol", "effort": "xhigh",
     "timeout_seconds": 1800},
    {"id": "opencode", "kind": "opencode", "command": ["opencode"], "model": "opencode-go/deepseek-v4.1-flash",
     "effort": "high", "timeout_seconds": 1800}
  ]
}
```

| 項目 | 意味 |
| --- | --- |
| `id` | 小文字の名前。レビュアーごとに異なる |
| `kind` | `codex` か `opencode` |
| `command` | CLI を起動する引数の配列（例：`["codex"]`、`["node", "/path/to/cli.js"]`）。DevFlow はシェルを通さずに起動する |
| `model`、`effort` | モデルと推論の強さ。DevFlow が引数に組み込むので、`-` で始まらず、空白・`"`・`\`・`#` を含まないもの |
| `timeout_seconds` | 1回のレビューの制限時間（5〜3600 秒、既定 1800 秒）。DevFlow はレビュアーへの指示に、持ち時間、止める時刻、答えを書き始める目安の時刻（持ち時間の 80% の時点）を書く。時間を過ぎると DevFlow がレビュアーを止め、その回は失敗 |

知らない項目があると、その項目の名前を示すエラーになります。

`checks: []` は「検証不要」という意味ではありません。変更後のコード版ごとに人間の実確認が必要です。可能な限り自動化するには、[設定例](examples/README.md) を参考に既存テスト・ビルドを登録してください。コマンドは文字列ではなく引数配列で指定します。設定例は依存の導入や実行可能性を保証しません。

## 長い処理と Claude Code の Bash

`review` と `check` は、外部の処理（レビュアー、チェック）が終わるまで待つランナーの操作です。ランナーの結果と状態の通知には、次の `review` と `check` に要る Bash の待ち時間 `bash_timeout_ms` が出ます（レビュアーの `timeout_seconds` に、事前の処理の 40 秒と余裕の 60 秒を足したもの。既定では `review` が 1900000 ms）。

- Claude は、この値を小さくせずに Bash ツールの `timeout` に渡します。
- この値が Claude Code の Bash の上限（`BASH_MAX_TIMEOUT_MS` と `BASH_DEFAULT_TIMEOUT_MS` の大きい方。既定 10 分）より大きいときは、同じ `timeout` でバックグラウンドで実行し、終わりの知らせを待ちます。
- そのため `BASH_MAX_TIMEOUT_MS` を設定しなくても、レビューは最後まで動きます。

ただし、バックグラウンドで動いたあとの次のターンでは、ランナーの呼び出しに Claude Code の承認を求められることがあります（スキルの事前の許可は、そのスキルを呼んだターンの間だけ効くため）。これを避けたい場合は、次のどちらかをします。

- `BASH_MAX_TIMEOUT_MS` を `bash_timeout_ms` 以上（既定なら 1900000 以上）にする。いつも普通の実行になり、1つのターンで進みます。

  ```json
  {"env": {"BASH_MAX_TIMEOUT_MS": "1900000"}}
  ```

  `~/.claude/settings.json`（またはプロジェクトの `.claude/settings.json`）に書き、Claude Code を起動し直します。

- 承認の確認で「今後は確認しない」を選ぶか、Claude Code の許可の設定にランナーを足す。

人がついていない実行（`claude -p` など）では、確認の画面が出ないので、`--allowedTools` などでランナーを許可しておいてください。

## 保存場所と再開

| 保存先 | 内容 | Git 管理 |
| --- | --- | --- |
| `.devflow/runs/plan/<run-id>/` | 依頼、インタビュー、途中の計画、レビュー、採否、公開記録 | しない |
| `.devflow/runs/implement/<run-id>/` | 入力プラン、基準コード、差分、検証、レビュー、結果 | しない |
| `.devflow/sessions/` | Claude セッションと実行の対応 | しない |
| `docs/plans/*.md` | 双方のレビューを通過した、またはレビューの上限で人間が受け入れた正式なプラン（保存先は `plan.publish.directory`、名前は `plan.publish.filename` で決まる） | 必要に応じて推奨 |
| `docs/plans/*.approval.md` | 横のプランの承認の記録（承認したのがレビュアーか人間か、版のハッシュ、run ID、日時など） | プランと同じ |
| `devflow.config.json` | 共有可能なプロジェクト設定。秘密は書かない | 推奨 |

run-id は一回の作業を識別する自動生成 ID です。レビューごとに変わらず、Plan と Implement は別の ID を持ちます。次のコマンドでは実際に報告された ID を使ってください。

```text
/devflow:status
/devflow:resume 20261101T140000Z-0123456789ab
```

`.devflow/.gitignore` は非公開の実行データを無視するために作成します。プロジェクト直下の `.gitignore` を勝手に変更しません。Git で既に追跡済みのファイルは `.gitignore` では追跡解除されません。正式プランを保存しても `git add` / `commit` / `push` は実行しません。

run を変えるランナーの操作（`cancel` など）は、run を所有する会話のセッションからだけ使えます。人間が自分の端末からランナーで操作するときは `--session` にそのセッションの ID を渡すか、先に `/devflow:resume RUN_ID` で今の会話へ引き継いでください。`status` と `doctor` は、どこからでも使えます。

外部処理（チェック・レビュー）の途中で Claude Code を閉じた場合などは、run が処理中のまま残ります。関係するプロセスが止まったことを確かめてから、人間が自分の端末でランナーの `recover` を実行します。その回は使用済みのままです。`PLUGIN_DIR` はインストールしたプラグインのディレクトリ、`PROJECT_DIR` は対象プロジェクトです。Plan の `--expected-review` には、処理中だったレビューの回（`/devflow:status` の `review_count`）を渡します。

```text
uv run --offline --no-project --no-config --no-python-downloads "PLUGIN_DIR/scripts/devflow.py" --project "PROJECT_DIR" --run RUN_ID implement recover --confirm-process-stopped
uv run --offline --no-project --no-config --no-python-downloads "PLUGIN_DIR/scripts/devflow.py" --project "PROJECT_DIR" --run RUN_ID plan recover --expected-review N --confirm-process-stopped
```

## 制約と安全性

- DevFlow は OS サンドボックスではなく、どのツールも止めません。守りたいことは、Claude Code と各 CLI の権限・サンドボックスの設定で守ってください。
- 一つのセッションで Plan と Implement を同時に動かしません。同じ作業ツリーの並行編集も避けてください。
- 常駐サービスではありません。Claude Code が終了・停止したら進行も止まり、明示的に再開します。
- 人間の対話セッションを対象とします。スケジュール実行やエージェント間メッセージを、人間の承認入力として利用する運用はサポートしません。
- 外部レビュアーには、最初の依頼・表示された面接の質問と回答の原文・決定サマリー・プラン・過去のレビュー（Implement では加えてコードの場所と差分のファイル）が渡ります。レビュアーはプロジェクトのファイルを自分で読み、それをレビュアーのサービスへ送ります。
- テストやビルドはプロジェクトコードを実行します。コマンド自体の安全性は人間が確認してください。
- push、公開、デプロイ、依存の追加、破壊的移行の前には、人間に確認するようスキルで頼んでいます。

詳しくは [設計](docs/architecture.md)、[信頼の考え方と注意](docs/security.md)、[検証範囲](docs/validation.md) を参照してください。

## 0.7.3 への更新

0.7.2、0.7.1、0.7.0 の run は、そのまま続けられます（run の記録の形式は 9 のままです）。0.7.3 より前に始めた Plan の run は、今までの規則（依頼の最初のチケット ID、なければ `plan-<run-id>`）で公開します。

変わったこと：

- **公開するプランの名前を、設定 `plan.publish.filename` で変えられます。** `{run_id}` と `{ticket}` を使うテンプレートです（[プロジェクト設定](#プロジェクト設定)）。既定の `"{ticket}"` では、今までと同じ名前になります。承認の記録は、今までどおり `<名前>.approval.md` です。
- 0.7.3 で始めた Plan の run は、固定した設定に `filename` を持ちます。0.7.2 以前はこの項目を知らないので、その run の `doctor` はエラーになり、公開では今までの規則で名前を付けます。0.7.2 以前へ戻すときは、先に 0.7.3 で始めた Plan の run を終えてください。

## 0.7.2 への更新

0.7.1 と 0.7.0 の run は、そのまま続けられます（run の記録の形式は 9 のままです）。

変わったこと：

- **レビューの要約の表示が短くなりました。** 0.7.1 までは、レビューごとに約 9 行の詳細、すべての指摘の全文（残っている指摘は 2 回）、人間の答えの JSON を出していました。0.7.2 は、判断に要るものだけを出します（[レビューの契約](#レビューの契約)）。レビューが 1 回増えても、表が 1 行増えるだけです。表示しなくなったものは、`review_summary` のデータとレビューの記録に残ります。
- **要約の表に、レビュアーのツールの呼び出しの回数が入ります。** Claude は、要約に出ていること（レビューごとの判定、ツールの呼び出し、残っている指摘など）を自分の報告で繰り返しません。
- **質問の記録が増えました。** Implement の質問の記録には、その時点のレビューの回数が入ります。Plan では、すべての質問の記録に種類が入ります（Claude が止めて聞く確認は `plan_decision`、レビュアーが人間の判断を求めたときは `review_decision`）。0.7.1 までの記録では、要約に「After an unknown number of reviews」や「question」と出ます。
- 要約のデータ（`review_summary`）には、項目を足しただけです（レビューごとの `tool_call_count`、人間の決定の `question_kind`、レビュアーごとの `reviewer_totals`）。今ある項目は変わりません。

## 0.7.1 への更新

0.7.0 の run は、そのまま続けられます（run の記録の形式は 9 のままです）。

直ったこと：

- **レビュアーの出力に記録に書けない文字があっても、保存が止まりません。** 対にならないサロゲート（JSON の `\ud800` のようなエスケープから生じる）は、記録（UTF-8）に書けません。0.7.0 までは、レビューの答え、ツールや出来事の名前、CLI の出力から作るエラーの文にこの文字があると、レビューのあとの保存が止まり、run が `reviewing` のまま残りました。
  - 答えにあれば、ほかのスキーマ違反と同じく不正な答えとして、その回を失敗にします（回は使用済み）。答えは書き換えません。
  - ツールや出来事の名前にあれば、`(unnamed)` として数えます。
  - エラーの文にあれば、`\ud800` のようにエスケープして残します。

## 0.7.0 への更新

更新の前に、次を確認してください。

- **0.6.x の run は続けられません。** run の記録の形式が 9 になりました。続ける場合は、更新の前に 0.6.x のまま終えるか、取り消してください。更新後は `status` に `unsupported` と表示され、新しい run の開始は止めません。

変わる動き：

- **レビューの上限の確認に「受け入れて終える」が加わりました。** 選ぶと、run は新しい終わり方 `accepted` で終わります。`completed` の意味（全レビュアーが同じ版を承認した）は変わりません。
- **レビューの要約を見せます。** 上限の確認の前と run が終わったときに、`status` と最終レポートに `review_summary` が入り、Claude が決まった形の Markdown をそのまま見せます。Plan では、どの終わり方でも `plan report` を呼びます。
- **Plan はレビューの時刻を記録します。** 回の記録に `started_at`・`finished_at`、トークン数、レビューした版の番号が入ります。Implement の回の記録にも、トークン数と checkpoint の記録のパスが入ります。
- **accepted のプランも `docs/plans/` に公開します。** 公開するプランの横に、承認の記録 `<name>.approval.md` を置きます（completed でも置きます）。プランのファイルそのものは、レビューした（受け入れた）バイト列のままです。`publication.json` は `schema_version` 2 になり、`approval`（`reviewers` か `human`）と `approval_path` を持ちます。プランと承認の記録のどちらかの名前が使われているときは、両方に run ID を付けます。
- **終わった Implement の run は、取り消しで変わりません。** Plan と同じく、`cancel` は終わった run をそのまま返します。

## 0.6.0 への更新

更新の前に、次を確認してください。

- **0.5.x の run は続けられません。** 続ける場合は、更新の前に 0.5.x のまま終えるか、取り消してください。更新後は `status` に `unsupported` と表示され、新しい run の開始は止めません。
- **`devflow.config.json` から次の項目を消してください。** 0.6.0 にはなく、残っていると、その名前を示すエラーになります。
  - `implement.snapshot.max_files`、`implement.snapshot.max_file_bytes`、`implement.snapshot.max_total_bytes`
  - `reviewers[].max_output_bytes`
  - `implement.checks[].max_output_bytes`

変わる動き：

- **プロジェクトの中身や出力の大きさでは止めません。** ファイルの大きさと数、レビュアーとチェックの出力、Plan の面接の transcript に、上限はありません。外部の処理を止めるのは時間切れだけです。
- **Implement の記録は、ファイルの中身を写しません。**
  - 開始時は、ファイルごとのパス・ハッシュ・サイズ・実行属性の一覧を記録します。中身を保存するのは、Git が同じバイト列を持っていない本文ファイル（開始前の変更、未追跡のファイル、`files` モードの本文ファイル）だけで、開始時に1回だけです。
  - checkpoint では、変更の一覧と、差分のファイル `snapshots/NNN.diff` を1回だけ書きます。レビュアーに渡す差分も、このファイルです。
- **チェックの出力は、ログのファイルにだけあります。** チェックの記録には、ログの場所を書きます。
- **Stop の hook はなくなりました。** フックは `UserPromptSubmit`、`PreToolUse`、`PostToolUse` の3つです。
- **途中で止まった保存は、なかったことになります。** run の記録は、新しい記録を書いてから `state.json` を置き換えて確定します。「途中の保存が残っている」という状態はなくなりました。止まった操作は、もう一度実行します（チェックやレビュアーが終わった後の保存なら、これまでどおり `recover` で終えます）。
- **対象範囲がおよそ 25 万ファイル（パスの長さによる）を超えると、開始時にエラーになります。** 記録の JSON が読み込みの上限（64 MiB）を超えるためです。`snapshot.include` / `snapshot.exclude` で範囲を絞ってください。

## 0.5.1 への更新

- **0.5.0 で始めた run は、そのまま続けられます。** 始めたときの設定（既定なら 900 秒）のまま動きます。

変わる動き：

- **既定の `timeout_seconds` が、両レビュアーとも 1800 秒になります。** `devflow.config.json` に `timeout_seconds` を書いている場合は、その値のままです。
- **`review` の待ち時間（`bash_timeout_ms.review`）が、既定で 1900000 ms になります。** `BASH_MAX_TIMEOUT_MS` を 1000000 にしている場合、`review` はバックグラウンドで動きます（レビューは最後まで動きます）。1つのターンで進めたい場合は 1900000 以上にしてください（「[長い処理と Claude Code の Bash](#長い処理と-claude-code-の-bash)」）。
- **レビュアーへの指示に、持ち時間と時刻が入ります。** 持ち時間の秒数、止める時刻、答えを書き始める目安の時刻（持ち時間の 80% の時点）を UTC で伝え、時間に収まるよう調べ方を組み立てること、確かめられなかった大事な点を `summary` に書くことを頼みます。判定とスキーマは変わりません。

## 0.5.0 への更新

更新の前に、次を確認してください。

- **0.4.x の run は続けられません。** 続ける場合は、更新の前に 0.4.x のまま終えるか、取り消してください。更新後は `status` に `unsupported` と表示され、新しい run の開始は止めません。

変わる動き：

- **レビューの上限で終わらず、人間に確認します。** 「続ける」と答えると、レビュアーの人数分を足して同じ run で続けます。「やめる」と答えると `exhausted` で終わります（「[レビューの契約](#レビューの契約)」）。
- **人間の回答の記録に、この確認だけ判断を足します。** Plan では回答の JSON に `"decision": "continue"` か `"stop"`、Implement では `implement resolve --decision continue` か `--decision stop` です。他の確認の受け取り方は変わりません。
- **`status` と最終レポートに `review_limit`（今の上限）を出します。** `remaining_reviews` はそこから計算します。設定の `max_reviews` は最初の上限です。

## 0.4.0 への更新

更新の前に、次を確認してください。

- **0.3.x の run は続けられません。** 続ける場合は、更新の前に 0.3.0 のまま終えるか、取り消してください。更新後は `status` に `unsupported` と表示され、新しい run の開始は止めません。紐づいた会話は、次の入力で紐づけが外れます。
- **`devflow.config.json` から `web` と `protocol_version` を消してください。** 0.4.0 にはこの2つの項目がなく、残っていると、その名前を示すエラーになります。Web を使うかどうかは、各 CLI の設定に従います。
- **Claude Code 2.1.316 以降** が必要です。

変わる動き：

- **DevFlow は、どのツールも制限しません。** Plan と Implement の中で、Claude Code の設定とモードが許すツール（Bash、PowerShell、サブエージェント、MCP、Web など）をそのまま使えます。フックは失敗しても止めません。Plan で「ソースとドキュメントを書き換えない」「人間への質問はこの会話でする」は、スキルの指示で頼みます。
- **レビュアーは、自分のツールでファイルを読みます。** 利用者の各 CLI の設定のまま、実装場所（Plan ではプロジェクト）で起動します。パケットにはファイルの中身を入れず、場所と差分のファイルを渡します。プロジェクトの設定と `AGENTS.md` などは、各 CLI がふだんどおり読み込みます。ツールの呼び出しは止めず、件数を `status` と最終レポートに出します。
- **レビュアーの `command` の制限をなくしました。** シェルを通す起動や、各 CLI の確認を省くフラグも、利用者の選択として受け付けます。
- **CLI の版の制限をなくしました。** DevFlow が渡すフラグがあれば、どの版でも動きます。確かめた版より新しいときは、`doctor` が警告します。
- **`BASH_MAX_TIMEOUT_MS` の設定は任意になりました。** 足りないときは、Claude がレビューをバックグラウンドで実行します（「[長い処理と Claude Code の Bash](#長い処理と-claude-code-の-bash)」）。
- **DevFlow の git は、利用者の git の設定を使います。** 全体の gitignore（`core.excludesFile`）、改行の扱い（`core.autocrlf`）、`safe.directory` などが、利用者の git と同じに効きます。
- **ランナーで run を変える操作には `--session` が要ります。** 人間が端末から `cancel` などをするときは、`--session` を付けるか、先に `/devflow:resume` で引き継いでください（「[保存場所と再開](#保存場所と再開)」）。
- **Implement は worktree でも実装できます。** Claude Code が `start --workspace` で、同じリポジトリの別の worktree を実装場所に選べます。

## 開発・配布

Python の第三者パッケージは不要です。互換 Python を使って以下を実行できます。

```text
python tools/test_all.py --quiet
python tools/package.py --validate-only
python tools/package.py
```

`python` は環境に応じて `python3` などに読み替えてください。ZIP は既存の同名ファイルを上書きせず、固定された順序・タイムスタンプと `SHA256SUMS` を含みます。

変更を配るときは、版を上げます。Claude Code は版が同じだと、GitHub から導入した利用者のプラグインを更新しません。版は `plugins/devflow/.claude-plugin/plugin.json` と `plugins/devflow/scripts/devflow_core/__init__.py` の2か所に書き、版を固定しているテスト（`tests/test_distribution.py`、`tests/router/test_entrypoint.py`）の期待値も合わせて直します。

`tools/package.py` は静的検証です。Claude Code 本体によるプラグイン検証・hooks 実行・認証済みモデル呼び出しとは異なります。GitHub Actions で Linux / macOS / Windows × Python 3.11 / 3.12 / 3.13 のテストを実行します。

## 一次資料

- [Claude Code：プラグインの導入・管理](https://code.claude.com/docs/en/discover-plugins)
- [Claude Code：Skills](https://code.claude.com/docs/en/skills)
- [Claude Code：Hooks](https://code.claude.com/docs/en/hooks)
- [Claude Code：ツールの説明（Bash の待ち時間とバックグラウンド実行）](https://code.claude.com/docs/en/tools-reference)
- [uv：Python スクリプトの実行](https://docs.astral.sh/uv/guides/scripts/)
- [uv：Python バージョンの選択](https://docs.astral.sh/uv/concepts/python-versions/)
