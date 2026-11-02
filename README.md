# DevFlow 0.3.0 — Claude Code Plugin

人間との要件整理から計画作成、実装、独立レビューまでを支援する **単一の Claude Code Plugin** です。インストールするプラグインは `devflow` だけです。

| コマンド | 入力と役割 |
| --- | --- |
| `/devflow:plan <依頼>` | Claude Code が `grilling` を直接呼び、人間への面接後に計画を作成・修正する |
| `/devflow:implement <plan-file>` | 明示した Markdown プランを読み、Claude Code がコードを実装・修正する |
| `/devflow:status [run-id]` | 保存された進捗・レビュー回数・停止理由を確認する |
| `/devflow:resume <run-id>` | 中断した同じ実行を再開する。レビュー回数はリセットしない |
| `/devflow:doctor` | 導入状態・設定・外部 CLI を診断する。モデルは呼び出さない |

Plan と Implement はファイルで接続します。Implement は Plan の run-id、会話履歴、内部状態を必要とせず、人間や別のツールが書いた Markdown でも開始できます。Plan 完了後に実装を勝手に始めることはありません。

## インストール

ZIP を解凍し、`.claude-plugin/marketplace.json` がある `devflow` ディレクトリへ移動して、ターミナルで実行します。

```text
claude plugin marketplace add .
claude plugin install devflow@devflow-marketplace --scope user
```

この `devflow` は配布物のディレクトリです。対象プロジェクトの `.devflow/` 作業ディレクトリとは異なります。配布物はプロジェクト外の安定した場所に置いて構いません。ローカル marketplace の更新元になるため、インストール直後に削除・移動しないでください。

GitHub に置く場合も公開リポジトリは必須ではありません。認証済みの private リポジトリを marketplace として登録できます。詳細は [インストールと環境設定](docs/installation.md) を参照してください。

更新は、GitHub から導入した場合は次の2つを実行し、Claude Code を起動し直します。ZIP から導入した場合と、更新の前後の注意は [更新する](docs/installation.md#更新する) を参照してください。

```text
claude plugin marketplace update devflow-marketplace
claude plugin update devflow@devflow-marketplace
```

## 必要なもの

| 依存 | 用途・注意点 |
| --- | --- |
| Claude Code **2.1.295 以降** | `onFailure: "block"` を含む hooks 仕様が必要。計画・実装・修正を担当 |
| `uv` | Python ランナー。Claude Code と同じ環境の PATH に必要 |
| インストール済み Python 3.11 以降 | Python **3.11 固定ではない**。標準ライブラリだけを使用 |
| Codex CLI | 認証・指定モデルへのアクセスと、`--strict-config` / `--ignore-user-config` / `--ignore-rules` / JSONL 等の対応が必要。doctor で確認 |
| OpenCode CLI | 認証・指定 provider/model/effort へのアクセスが必要 |
| モデルから呼び出せる `grilling` スキル | **Plan のみ必要**。同梱・自動インストールはしない。`plan.grill_skill` で実際のスキル名を指定可能 |
| 対象プロジェクトの検証環境 | Implement のビルド・テストなど。事前に人間が設定する |
| Git | Implement の既定 `snapshot.mode: "git"` で使用。非 Git プロジェクトは明示的に `"files"` に設定 |

実行エントリーポイントには次のインラインメタデータがあります。

```python
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
```

hooks は `uv run --offline --no-project --no-config --no-python-downloads` で起動します。互換 Python を選び、対象プロジェクトの依存をインストールせず、Python 自体も自動ダウンロードしません。互換 Python が一つもなければ停止するので、必要な環境は人間が用意してください。

Windows ネイティブと WSL は別環境です。Claude Code を WSL で使うなら、`uv`、Python、Codex、OpenCode もその WSL から起動できる必要があります。Windows 側に導入済みであるだけでは十分でない場合があります。Windows ネイティブでは Git for Windows（Git Bash）を入れてください。PowerShell では DevFlow はランナーだけを通します（[Windows](docs/installation.md#windows)）。

レビュアーの既定の制限時間は 900 秒です。Claude Code の Bash は既定で 1 回 10 分で打ち切るので、`BASH_MAX_TIMEOUT_MS` を 960000 以上にしてください（[設定方法](docs/installation.md#claude-code-の-bash-の上限を上げる)）。足りない場合は `doctor` と開始時に警告します。

`plan.grill_skill` は Claude Code の `Skill` ツールから直接呼べるスキルを指定してください。`disable-model-invocation: true` のスキルは利用できません。DevFlow が外部スキルのファイルやその権限設定を変更することはありません。見つからない・呼び出せない場合は停止し、人間が依存と設定を確認します。設定は run 開始時に固定されます。

DevFlow 内では、面接スキルにも Plan のツール制限を適用します。`grilling` が調査用サブエージェントを推奨する場面でも、事実確認は同じ会話内の `Read`（環境にあれば `Grep` / `Glob`）、Web、書き込まない Bash のコマンド、ランナーの診断に限定し、その制約を Skill 呼び出し時に渡します。質問・回答の進め方は利用しますが、外部スキルのすべてのツール動作を許可するものではありません。必要な情報を安全に取得できなければ人間に確認します。

## 最初の使い方

対象プロジェクトで Claude Code を開き、導入状況を確認します。

```text
/devflow:doctor
```

計画作成を開始します。

```text
/devflow:plan REVIEW-10 のプランを作成して
```

1. Claude Code が設定された `grilling` を `Skill` ツールで直接呼び、人間が回答します。最後の条件確認は `AskUserQuestion` の画面で回答します。
2. 最初の依頼、質問・回答の原文、確定した要件を保存します。
3. Claude Code が計画を作成し、外部レビューと修正を繰り返します。
4. 同じプラン内容を両レビュアーが承認したら、正式な Markdown を `docs/plans/` へ保存します。
5. 出力された **実際のパス** を確認します。既存ファイルは上書きしません。

実装開始は別の明示的な操作です。

```text
/devflow:implement docs/plans/REVIEW-10.md
```

パスに空白がある場合は引用符で囲みます。相対パスは対象プロジェクトを基準に扱い、プランを自動検索・推測して選びません。

```text
/devflow:implement "docs/plans/player movement.md"
```

Implement は開始時のプランと作業ツリーを固定し、Claude Code が実装し、設定された検証とコードレビューを実施します。既定では未コミットの既存変更がある場合に一旦停止し、`AskUserQuestion` でその変更を保持したまま続行してよいか確認します。Plan で保存した新しいプランや追加した設定ファイルも、この確認の対象になり得ます。必ず先にコミットしなければならないわけではなく、プラグインが勝手に破棄・コミットすることもありません。

既存変更を含む作業ツリーからの開始を事前に許可する場合のみ、`implement.allow_dirty: true` を設定してください。実装差分の基準は、どちらの場合も開始時の実ファイルです。

## レビューの契約

| 回 | 既定レビュアー | 既定設定 |
| --- | --- | --- |
| 1 / 3 / 5 | Codex | `gpt-6.1-sol` / `xhigh` |
| 2 / 4 / 6 | OpenCode | `opencode-go/deepseek-v4.1-flash` / `high` |

これらはユーザー指定の設定値です。本配布物のテストで、各アカウントのモデル提供状況・認証・実際のモデル選択まで確認しているわけではありません。未対応時に別モデルへ黙って切り替えません。OpenCode は 2.x（プロトコル 2）だけに対応します。

- 上限は **Plan で合計6回、Implement でも別に合計6回**。各レビュアー6回ずつではありません。
- レビューを送信する前に枠を確保します。送信後のタイムアウト、不正な出力、通信失敗も使用済みです。
- 再開時にも回数・順序を保持します。失敗を理由に無料枠として再送信しません。
- 両者が **同じ内容ハッシュ** を承認したときだけ早期完了します。内容が変われば以前の承認はその版にしか適用されません。
- 6回目後の修正版を「レビュー済み」とは表示しません。上限到達と残課題を報告します。
- 指摘の採用・不採用・理由を保存します。外部レビューは人間の決定を上書きする権限ではありません。
- 外部レビュアーは渡されたパケットを審査し、計画やプロジェクトを直接編集しません。既定では、事実の確認に Web 検索と Web の取得だけを使えます（レビュアーごとに `"web": false` で切れます）。ほかのツールを使った回は失敗です。

要件変更、重要な設計判断、権限・機密性・費用の問題は停止してエスカレーションします。解除には、停止後に `AskUserQuestion` で得た実際の回答を記録します。普通の会話の返信や `/devflow:resume` だけを承認に読み替えません。通常の権限確認も迂回しません。

## プロジェクト設定

プロジェクト直下の **`devflow.config.json`** に設定します。共通の `reviewers` と `max_reviews`、および `plan` / `implement` ごとの設定を分けます。配列は全体置換です。設定は実行開始時に固定します。

```json
{
  "schema_version": 1,
  "max_reviews": 6,
  "plan": {
    "grill_skill": "grilling",
    "publish": {"enabled": true, "directory": "docs/plans"}
  },
  "implement": {
    "allow_dirty": false,
    "max_check_attempts": 3,
    "snapshot": {"mode": "git"},
    "checks": []
  }
}
```

レビュアーを設定する場合は、最上位の `reviewers` に全員を書きます（配列は全体置換）。各要素の `"web": false` で、そのレビュアーの Web を切れます。例は「0.3.0 への更新」にあります。

`checks: []` は「検証不要」という意味ではありません。変更後のコード版ごとに人間の実確認が必要です。可能な限り自動化するには、[設定例](examples/README.md) を参考に既存テスト・ビルドを登録してください。コマンドは文字列ではなく引数配列で指定します。設定例は依存の導入や実行可能性を保証しません。

## 保存場所と再開

| 保存先 | 内容 | Git 管理 |
| --- | --- | --- |
| `.devflow/runs/plan/<run-id>/` | 依頼、インタビュー、途中の計画、レビュー、採否、公開記録 | しない |
| `.devflow/runs/implement/<run-id>/` | 入力プラン、基準コード、差分、検証、レビュー、結果 | しない |
| `.devflow/sessions/` | Claude セッションと実行の対応 | しない |
| `docs/plans/*.md` | 双方のレビューを通過した正式なプラン | 必要に応じて推奨 |
| `devflow.config.json` | 共有可能なプロジェクト設定。秘密は書かない | 推奨 |

run-id は一回の作業を識別する自動生成 ID です。レビューごとに変わらず、Plan と Implement は別の ID を持ちます。次のコマンドでは実際に報告された ID を使ってください。

```text
/devflow:status
/devflow:resume 20261101T140000Z-0123456789ab
```

`.devflow/.gitignore` は非公開の実行データを無視するために作成します。プロジェクト直下の `.gitignore` を勝手に変更しません。Git で既に追跡済みのファイルは `.gitignore` では追跡解除されません。正式プランを保存しても `git add` / `commit` / `push` は実行しません。

## 制約と安全性

- hooks / skills は OS サンドボックスではありません。他セッションや他の hooks による変更まで完全に封じるものではありません。
- 一つのセッションで Plan と Implement を同時に動かしません。同じ作業ツリーの並行編集も避けてください。
- 常駐サービスではありません。Claude Code が終了・停止したら進行も止まり、明示的に再開します。
- 人間の対話セッションを対象とします。スケジュール実行やエージェント間メッセージを、人間の承認入力として利用する運用はサポートしません。
- 最初の依頼・表示された面接の質問と回答の原文・決定サマリー・プラン等が外部レビュアーへ送られます。内部思考や無関係な過去の会話を収集する機能ではありません。Web を許したレビュアーは、その内容をもとに検索語や URL を作って外へ送ることがあります。
- ソース・プラン・レビュー出力・Web の文章は信頼できない入力として扱います。秘密を送らず、必要な資料だけをパケット化してください。DevFlow のガードは秘密のフィルタではありません。
- テストやビルドはプロジェクトコードを実行します。コマンド自体の安全性は人間が確認してください。
- 依存のインストール、認証、コミット、プッシュ、公開、デプロイ、破壊的移行は自動実行の範囲外です。

詳しくは [設計](docs/architecture.md)、[安全性](docs/security.md)、[検証範囲](docs/validation.md) を参照してください。

## 0.2.0 への更新

0.2.0 は内部の構成と保存形式を整理した版です。利用者が使うコマンドと設定ファイルの名前は変わりません。

- 0.1.x で始めた run は 0.2.0 では続けられません。続ける場合は、更新前に 0.1.3 のまま終えてください。更新後は `status` に `unsupported` として表示されるだけで、新しい run の開始は止めません。
- 別プラグインとして配布していた時代の記録ディレクトリと設定ファイルは読みません。
- `reviewers` の規則が両フロー共通になりました。2人以上かつ `max_reviews` 以下で、異なる種類（codex / opencode）を2つ以上含む必要があります。同じ種類だけの構成は拒否され、Implement でも3人以上を設定できます。
- OpenCode の `protocol_version` を省略した場合の既定は 2 です（0.3.0 で v1 への対応を終えました。移行の手順は「0.3.0 への更新」を参照）。
- レビュアーの `command` は、引数のどこかにシェル（bash、sh、cmd、powershell など）や `--yolo`、`--dangerously-*` を含むと拒否されます。設定は人間が書く信頼済みのもので、この検査は事故を防ぐための多層の防御です。
- 終了レポートの `both_reviewers_approved` は `all_reviewers_approved` になりました。
- `status` の `human_events` は、保留中の質問に使える回答（質問の後に、所有するセッションで記録された AskUserQuestion の回答）だけを返します。
- ランナーの `resume` 動作はありません。再開は `/devflow:resume RUN_ID` だけで、入力したセッションへの引き継ぎまでフックが行います。引き継がれた元のセッションは、もう一度 resume するか run が終わるまで止まります（0.3.0 からは、書き込まない作業はできます）。
- Implement の開始時には、レビュアー CLI を確認しません。確認は `doctor` と各レビューの直前に行い、CLI が無くてもレビュー回数は減りません。
- `/devflow:implement` の後、`implement start` までの間は、書き込まないツール（Web を含む）・許可された Bash コマンド・`status`・`doctor`・その開始だけを使えます。AskUserQuestion も使えず、この間の発言は記録しません。
- `status`（`--run` なし）は、呼び出し元の状態を `idle` / `pending_start` / `bound` / `stale` のどれかで返し、`run`・`pending_start`・`owner_matches` を添えます（run の詳細は `--run RUN_ID status` で読みます）。あわせて、全 run の一覧（壊れた run・中断した書き込み `recovery_required` を含む）、他のセッションの開始待ちや壊れたセッション記録（`sessions`）、作成途中のディレクトリ（`incomplete_creations`）を表示します。
- 読み取りだけの `status` と `doctor` は、どのセッションからも使えます。run を変える操作は、所有するセッションだけが使えます。人間専用の `recover` は、両フローとも `--confirm-process-stopped` が必須です。

### 復旧の手順

- `recovery_required` の run は、`/devflow:resume RUN_ID` か cancel で書き込みを完了できます。
- 外部処理（チェック・レビュー）の中断は、関係するプロセスが止まったことを確認してから、人間が自分の端末で `recover` を実行します。
- `.devflow/runs/<flow>/.creating-*` や、失ったセッションの開始待ち・壊れたセッション記録（`.devflow/sessions/<ハッシュ>.json`）が新しい開始を止める場合は、`status` と `doctor` が示すファイルを、人間が `.devflow` の外へ移してください。run の記録そのものは消さないでください。

## 0.2.1 の変更

- Implement でも、レビュアーの失敗（不正な応答、タイムアウト、中断からの `recover` など）は、同じスナップショットに対する他のレビュアーの承認を取り消しません。Plan と同じ扱いです。
- レビューの応答の項目に過不足があるときのエラーが、足りない項目と余分な項目の名前を示すようになりました（例: `findings[0] has missing or unknown fields (missing: none; unknown: "consequence")`）。レビュアーへの指示文でも、スキーマに無い項目を足さないよう明記しました。

## 0.3.0 への更新

更新の前に、次を確認してください。

- **0.2.x の run は続けられません。** 続ける場合は、更新の前に 0.2.1 のまま終えるか、取り消してください。更新後は `status` に `unsupported` と表示され、新しい run の開始は止めません。
  - 0.2.x の run に紐づいた会話は、次の有効な入力で紐づけが外れます。正しい run-id で `/devflow:resume` を送って拒否された場合も外れます。外れるまでの間も、書き込みとランナーの操作は拒否されます。
  - 終えた run の報告は `status` に表示されなくなります。ファイルは残るので、必要なら更新の前に控えてください。Plan は `.devflow/runs/plan/RUN_ID/report.json`（正式なプランは公開先にも残ります）、Implement は `.devflow/runs/implement/RUN_ID/reports/final-*.json` です。
- **OpenCode 1.x には対応しません。** 更新の前に OpenCode を 2.x に上げ、`devflow.config.json` の `protocol_version` は削るか `2` にしてください。`1` のままだと設定エラーになります。
- **Claude Code の Bash の上限を上げてください。** レビュアーの既定の制限時間は、Plan も Implement も 900 秒になりました。Claude Code の Bash は既定で 1 回 10 分で打ち切るので、そのままだとレビューが途中で切れ、run が処理中のまま残ります（人が `recover` し、その回は使用済みになります）。Bash の実際の上限は `BASH_MAX_TIMEOUT_MS` と `BASH_DEFAULT_TIMEOUT_MS` の大きい方です。通常は `BASH_MAX_TIMEOUT_MS` を 960000 以上にします。`BASH_DEFAULT_TIMEOUT_MS` を上げると、すべての Bash の既定の待ち時間も長くなります。足りない場合は `doctor` と開始時に警告します。

```json
{"env": {"BASH_MAX_TIMEOUT_MS": "960000"}}
```

  これを `~/.claude/settings.json`（またはプロジェクトの `.claude/settings.json`）に書き、Claude Code を起動し直します。ランナーの結果と状態の通知の `bash_timeout_ms` に、次の `review` と `check` に要る値が出ます。

変わる動き：

- **外部レビュアーが Web を使います（既定）。** Codex は Web 検索（`web_search="live"`）、OpenCode は `websearch`（検索先は Exa、鍵なし）と `webfetch` を使えます。ほかのツールは今までどおり使えず、使えばその回は失敗です。検索語や URL にパケットの内容が入ることがあります。切るには、各レビュアーの要素の中に `"web": false` を書きます。`reviewers` は全体置換なので、全員を書きます。

```json
{
  "schema_version": 1,
  "reviewers": [
    {"id": "codex", "kind": "codex", "command": ["codex"], "model": "gpt-6.1-sol", "effort": "xhigh",
     "timeout_seconds": 900, "web": false},
    {"id": "opencode", "kind": "opencode", "command": ["opencode"], "model": "opencode-go/deepseek-v4.1-flash",
     "effort": "high", "timeout_seconds": 900, "web": false}
  ]
}
```

- **Claude の `WebSearch` と `WebFetch` を、run の中でも使えます。** URL や検索語に秘密を載せることもできます。DevFlow のガードは秘密のフィルタではありません。
- **Bash で使えるコマンドが増えました。** ファイルに書かず、ほかのプログラムを動かさないコマンドを、`|`、`&&`、`||`、`;`、改行でつないで使えます。入力の `< ファイル` と、出力を `/dev/null` へ捨てるリダイレクトも使えます。例：`grep -rn TODO src | sort | uniq -c`、`sed -n '10,40p' file`、`git --no-optional-locks --no-pager log --oneline`、プロジェクトの中への `cd`。git は `--no-optional-locks --no-pager` が必須です（付けないと `.git/index` を書き換えるため）。`--help` はマニュアルを開くので使えません。一覧と規則は [ガードに拒否される Bash コマンド](docs/installation.md#ガードに拒否される-bash-コマンド) にあります。
- **抜け道を塞ぎました。** Claude Code が `find` を置き換えた bfs の `-rm`（削除）と、引用符の外の `*` などが展開されてオプションとして働くことを、ガードが拒否します。`grep`・`rg`・`find`・`git` などでは、グロブを引用符で囲んでください（例：`--include='*.py'`）。
- **run の中で使えるツールが増えました。** `ReadNotifications`、`ListAgents`、`CronList`、`PushNotification`、`SendUserFile` です。`Monitor`、`TaskStop`、`Agent` などは引き続き使えません。
- **引き継がれた元の会話でも、書き込まない作業ができます。** 通常の入力、読み取り、Web、許可された Bash、`status`、`doctor` は使えます。書き込み、ランナーの操作、`AskUserQuestion` は使えず、その会話の入力は run の記録に加えません。
- **`status` と `doctor` は、どの会話からでも、どの run でも指定できます**（`--run RUN_ID`）。`status` は表示だけです。`doctor` はプロジェクトのファイルを書きませんが、レビュアー CLI の `--version` と `--help` を実行します。`status` は、0.2.x の run に紐づいた会話を `unsupported` と表示します。
- **Windows では Git for Windows（Git Bash）を入れてください。** PowerShell では、DevFlow はランナーだけを通します。Claude が PowerShell を主に使う環境では、`CLAUDE_CODE_USE_POWERSHELL_TOOL=0` で Bash を主にできます。スキルは PowerShell でのランナーの呼び出しも事前に許可します。
- レビュアーの出力の上限の既定は 16 MiB になりました。
- run の中の書き込みでは、`.git`、`.devflow`、`devflow.config.json`、`.ugrep` などを、draft の中も含めて作れません。Windows で同じファイルを指す別の書き方（`::$DATA` などのストリーム、末尾の点や空白、短い名前）でも作れません。`:` を含む名前のファイルは、どの OS でも書けません。

## 開発・配布

Python の第三者パッケージは不要です。互換 Python を使って以下を実行できます。

```text
python tools/test_all.py --quiet
python tools/package.py --validate-only
python tools/package.py
```

`python` は環境に応じて `python3` などに読み替えてください。ZIP は既存の同名ファイルを上書きせず、固定された順序・タイムスタンプと `SHA256SUMS` を含みます。

変更を配るときは、版を上げます。Claude Code は版が同じだと、GitHub から導入した利用者のプラグインを更新しません。版は `plugins/devflow/.claude-plugin/plugin.json` と `plugins/devflow/scripts/devflow_core/__init__.py` の2か所に書き、版を固定しているテスト（`tests/test_distribution.py`、`tests/router/test_entrypoint.py`）の期待値も合わせて直します。

`tools/package.py` は静的検証です。Claude Code 本体によるプラグイン検証・hooks 実行・認証済みモデル呼び出しとは異なります。GitHub Actions には Linux / macOS / Windows の試験設定を同梱しますが、設定があること自体は各 OS の成功実績ではありません。

## 一次資料

- [Claude Code：プラグインの導入・管理](https://code.claude.com/docs/en/discover-plugins)
- [Claude Code：Skills](https://code.claude.com/docs/en/skills)
- [Claude Code：Hooks](https://code.claude.com/docs/en/hooks)
- [uv：Python スクリプトの実行](https://docs.astral.sh/uv/guides/scripts/)
- [uv：Python バージョンの選択](https://docs.astral.sh/uv/concepts/python-versions/)
