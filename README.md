# DevFlow 0.12.3 — Claude Code / Codex CLI / Devin CLI Plugin

人間との要件整理から計画作成、実装、独立レビューまでを支援する **3つのホスト（Claude Code、Codex CLI、Devin CLI）で使える単一のプラグイン** です。インストールするプラグインは `devflow` だけです。

| コマンド | 入力と役割 |
| --- | --- |
| `/devflow:plan <依頼>` | Claude Code が `grilling` を直接呼び、人間への面接後に計画を作成・修正する |
| `/devflow:implement <plan-file>` | 明示した Markdown プランを読み、Claude Code がコードを実装・修正する |
| `/devflow:status [run-id]` | 保存された進捗・レビュー回数・停止理由を確認する |
| `/devflow:resume <run-id>` | 中断した同じ実行を再開する。レビュー回数はリセットしない |
| `/devflow:doctor` | 導入状態・設定・外部 CLI を診断する。モデルは呼び出さない |

Plan と Implement はファイルで接続します。Implement は Plan の run-id、会話履歴、内部状態を必要とせず、人間や別のツールが書いた Markdown でも開始できます。Plan 完了後に実装を勝手に始めることはありません。

## 0.8.0 の利用方法

Claude Code の5コマンドは従来の `/devflow:...` のままです。Codex CLI では
`$devflow:plan`、`$devflow:implement`、`$devflow:status`、`$devflow:resume`、
`$devflow:doctor` を明示的に呼びます。Codex の対象はローカル CLI 0.166.0 以降です。
導入・フックの信頼と、質問機能の明示的な有効化は [導入手順](docs/installation.md) を参照してください。
両ホストの run は、それぞれ開始したホストで再開します。
両ホストで Plan から Implement まで通した結果は [検証範囲](docs/validation.md#製品の両ホストでの全フロー確認) に記録しています。

Plan / Implement の起動時にレビュアーの種類・モデル・effort を指定できます。
実際の選択画面は追加しません。次はレビュアー一覧を Claude と Codex の2人で置き換える例です。

```text
/devflow:plan --reviewer claude opus high --reviewer codex gpt-6.2-sol xhigh -- REVIEW-42 を計画して
$devflow:implement --reviewer claude opus high --reviewer codex gpt-6.2-sol xhigh -- docs/plans/REVIEW-42.md
```

Devin をレビュアーにするときは、たとえば `--reviewer devin swe-2 high` と指定します（[プロジェクト設定](#プロジェクト設定)）。

`--max-reviews N` で、その run の最初のレビューの上限（`max_reviews`）を指定できます。`--reviewer` と一緒にも、単独でも使えます。

```text
/devflow:plan --max-reviews 4 --reviewer codex gpt-6.2-sol high -- 依頼
```

`N` は 1〜99 の整数（ASCII の数字だけ。`04` や `+4` はエラー）で、1回だけ指定できます。
レビュアーの人数より少ない値はエラーです（全員が一度は承認できるようにするため）。

指定しなければ、設定ファイルと既定値の従来の選び方で動きます。
`--` で始まる起動引数は常にオプションとして解析し、本文の前の単独の `--` が必須です。
綴り間違い・未知のオプション・省略形はエラーで止まります。`-- 依頼` も使えます。
同じ種類の既存レビュアーの command・timeout がすべて同じなら、その値を引き継ぎます。
候補は設定をすべて解決した最終一覧です。異なる値があればエラーです。
候補がなければ種類ごとの標準コマンドと1800秒を使います。
一覧にない種類には command・timeout を指定する起動オプションはありません。
1人以上、max_reviews 以下の人数が必要です（種類は問いません）。順番と設定は開始時に固定され、
resume では選び直しません。Plan の Implement 用案内に `--reviewer` と `--max-reviews` は引き継ぎません。

プロジェクト直下の `devflow.config.local.json` は、`devflow.config.json` と同じ形式1の
ローカル設定です。すべての公開設定項目を使えます。優先順位は
既定値 → 共有の共通項目 → 共有のフロー別 → ローカルの共通項目 → ローカルのフロー別 → 起動指定です。
`publish` / `snapshot` は項目単位でマージし、reviewers・checks などの配列は全体置換します。
どちらのファイルも個別に検査し、エラーにはファイル名を出します。

利用者自身で `.gitignore` に `/devflow.config.local.json` を加えてください。
DevFlow は利用者の設定・権限・`.gitignore` を自動編集しません。
ローカル設定もプロジェクト起点の目印になります。両設定ファイルは Implement のスナップショットに入れず、
ローカル設定を明示的な context_paths に入れるとエラーです。配布物の収集では、プロジェクト直下の
`devflow.config.local.json`（この名前と完全一致するもの）だけを読み飛ばすので、配布 ZIP には入りません。
それ以外の場所にあるものと、大文字小文字が違う名前は作成を止めます。
収集した結果にローカル設定が混じっていた場合も、最後の検査で作成を止めます。

`status` / `doctor` は、開始待ち・紐付け・イベントを変更しません。
両ホストで、この操作入力と注入文は面接・回答数から除外します。通常のアシスタントの状態説明は残ります。
Claude では通常の人間の返信も数えますが、Codex では出所を確認できた標準質問ツールの実回答だけを数えます。

### 0.8.0 への更新とバージョンを戻す場合

**ランナーCLIの破壊的変更：`--host` は全実操作で必須です。** 0.7.xまでの手動コマンド、
外部スクリプト、復旧手順も、プラグイン更新と同時に `--host claude` または `--host codex` を
グローバル引数へ追加してください。省略した `devflow.py status` / `doctor` は引数エラーになります。
0.7.xの手動コマンドは、そのままでは動きません。Claude を暗黙に選ぶ互換動作は設けません。`--help` / `--version` は指定なしで使えます。
更新後の復旧例は[保存場所と再開](#保存場所と再開)を参照してください。

run 形式は10、セッション形式は3です。0.7.x の run（形式9）は unsupported となり、
0.8.0 で続けられません。進行中の run は0.7.3で完了させてから更新してください。
形式1・2のセッション記録は Claude の記録として扱い、新しい開始を妨げません。
同じホストの次の通常入力で紐付け・開始待ちを外します。データ変換は行いません。

0.8.0 から0.7.3へ戻して、そのまま run を開くことはサポートしません。
0.7.3 は形式10・3を invalid とし、新しい開始も止まる場合があります。
形式9を維持する分割案でも kind: claude の run は0.7.3で使えません。
プロセスをすべて止め、更新前の `.devflow` と共有・ローカル設定のバックアップを戻すか、
`.devflow` 全体を保管して空の状態から始めてください。形式番号や固定済み設定を書き換えないでください。

## Devin CLI での利用（0.10.0）

Devin CLI（ローカルの端末で動く `devin`）でも、Claude Code と同じ `/devflow:plan`、`/devflow:implement`、
`/devflow:status`、`/devflow:resume`、`/devflow:doctor` を使えます。導入は次のとおりです（詳しくは
[導入手順](docs/installation.md#devin-cli)）。

```text
devin plugins install rysh25/devflow#plugins/devflow
```

この機械だけに入れるときは `devin plugins install --local ...` も使えます。プラグインは
`"platforms": ["local"]` で、ローカルの CLI と Desktop だけに読み込まれ、Devin Cloud のセッションには入りません。
確かめたのは Linux / WSL の Devin CLI 3000.12.57 だけです。Devin Desktop、Devin Cloud、ACP のエディター連携（Zed など）、
macOS とネイティブの Windows は確かめていません。

- **プロジェクトを信頼する。** 初めてのプロジェクトでは、`devin` をそのプロジェクトで一度起動して信頼してください。
  Devin は、信頼したディレクトリとその下でだけ動きます。Implement で別の worktree を実装場所にすると、Devin レビュアーは
  その worktree で起動します。すでに信頼したディレクトリの下にある worktree（例：プロジェクトの中の Git で無視した
  ディレクトリ）はその信頼を引き継ぎます。それ以外の場所の worktree は、`start` の前にその worktree で `devin` を
  一度起動して信頼してください。DevFlow は信頼や権限の確認を迂回するフラグを渡しません。信頼していないと、
  Devin をレビュアーにした回は失敗します。
- **ランナーの承認。** Devin のスキルは、ランナーのコマンドを事前に許可しません。Devin は許可の規則を文字どおりに照合し、
  プラグインのパスを展開しないため、ランナーだけを許す規則を配布物に書けないからです（[調査](docs/devin-host-research.md#実装時に追加で確かめたこと2026-11-15devin-cli-30001257)）。
  ランナーを起動するときに Devin が承認を求めたら、Devin の標準の承認で許可してください。DevFlow は利用者の設定を変えません。
- **面接の記録。** Devin の面接は、フックの記録だけで作ります。人間の回答として数えるのは `ask_user_question` の画面での回答だけで、
  普通の文章で答えたものは数えません（取り消した質問、飛ばした質問も数えません）。Devin のスキルは、面接の質問を
  `ask_user_question` で聞くよう頼みます。Devin 自身の説明の文章は記録に入りません。
- **長い処理。** `review` と `check` は exec で1回だけ実行し、バックグラウンドへ移ったら `get_output` で終わるまで待ちます。
- **ホストのモデル** は利用者が選びます（例：無料の `swe-2-high`）。DevFlow は決めません。
- **フックの終了コード。** Devin は、フックが終了コード 2 で終わるとプロンプトを止めます。DevFlow のランナーは、hook の失敗を
  いつも終了コード 1 で返します（記録だけされ、止まりません）。ランナーより前の失敗は DevFlow では防げません。たとえば `uv` が
  見つからないとシェルは 127 を返し、公開資料どおりなら止まりませんが、確かめていません。`/devflow:doctor` で `uv` を確かめてください。

あるホストで作ったプランを、別のホストの `/devflow:implement` で実装できます（Implement は Markdown のプランを読むだけです）。
1つの run を別のホストで再開・復旧することはできません。

## DevFlow がすること、しないこと

DevFlow は、ホスト（Claude Code、Codex、Devin：計画と実装）と外部レビュアー（Codex、OpenCode、Claude Code、Devin）の機能を制限しません。どのツールも止めず、フックも失敗したときに止めません。各エージェントは、利用者のそれぞれの設定とモードのまま動きます。

DevFlow がするのは、記録と確認です。

- 依頼、面接、計画、コードのスナップショット、レビュー、人間の判断を run の記録に残す。
- レビューの回数（Plan と Implement でそれぞれ、最初は `max_reviews` 回まで。既定 6）と、レビュアーの順番を数える。上限では、人間の承認でレビューを追加するか、やめるか、今の版を受け入れて終えるかを選べる。
- 設定したレビュアー全員が **同じ内容**（Plan はプランのハッシュ、Implement はコードのスナップショット）を承認したときだけ完了（`completed`）とする。上限で人間が受け入れたときは、完了ではなく `accepted` で終える。
- 上限の確認の前と run が終わったときに、レビューの要約を決まった形で見せる。
- 人間の判断が要るときは、ホストの質問ツール（Claude の `AskUserQuestion`、Codex の `request_user_input`、Devin の `ask_user_question`）の実際の回答だけを判断として記録する。

「計画中はソースを書き換えない」「レビュアーはファイルを書き換えない」「秘密を読まない・外へ出さない」といったことは、スキルとレビュアーへの指示文で頼みます。強制はしません。守りたいことは、Claude Code や各 CLI の権限の設定で守ってください（[信頼の考え方と注意](docs/security.md)）。

## インストール

DevFlow のプラグイン `devflow` を、使うホスト（Claude Code、Codex CLI、Devin CLI）に入れます。入れる元は GitHub のリポジトリ `rysh25/devflow` か、ローカルの配布物（配布 ZIP を解凍した `devflow` ディレクトリ、またはクローン）です。OpenCode はレビュアーとして CLI を呼ぶだけなので、プラグインはありません。

| ホスト | GitHub から入れる | 更新する |
|---|---|---|
| Claude Code | `claude plugin marketplace add rysh25/devflow`<br>`claude plugin install devflow@devflow-marketplace --scope user` | `claude plugin marketplace update devflow-marketplace`<br>`claude plugin update devflow@devflow-marketplace` |
| Codex CLI | `codex plugin marketplace add rysh25/devflow`<br>`codex plugin add devflow@devflow-marketplace` | `codex plugin marketplace upgrade devflow-marketplace` |
| Devin CLI | `devin plugins install rysh25/devflow#plugins/devflow` | `devin plugins update devflow` |
| OpenCode | DevFlow のプラグインはありません | CLI の更新は `opencode upgrade` |

ローカルの配布物から入れるときは、`rysh25/devflow` の代わりに配布物のパスを指定します（Devin ではその中の `plugins/devflow`）。たとえば Claude Code では、配布物の `devflow` ディレクトリで次を実行します。

```text
claude plugin marketplace add .
claude plugin install devflow@devflow-marketplace --scope user
```

この `devflow` は配布物のディレクトリです。対象プロジェクトの `.devflow/` 作業ディレクトリとは異なります。配布物はプロジェクト外の安定した場所に置いて構いません。ローカル marketplace の更新元になるため、インストール直後に削除・移動しないでください。

ローカルの配布物から入れた場合の更新（中身を置き換えてから更新の操作をします。Codex では `codex plugin add` をもう一度実行します）、フォークや private のリポジトリから入れる方法、更新の前後の注意は [プラグインの導入と更新](docs/installation.md#プラグインの導入と更新) を参照してください。更新の前には、「[0.12.3 への更新](#0123-への更新)」（それより前の版から上げるときは「[0.12.2 への更新](#0122-への更新)」「[0.12.1 への更新](#0121-への更新)」「[0.12.0 への更新とバージョンを戻す場合](#0120-への更新とバージョンを戻す場合)」「[0.11.0 への更新とバージョンを戻す場合](#0110-への更新とバージョンを戻す場合)」「[0.10.1 への更新](#0101-への更新)」「[0.10.0 への更新](#0100-への更新)」「[0.7.3 への更新](#073-への更新)」「[0.7.2 への更新](#072-への更新)」「[0.7.1 への更新](#071-への更新)」「[0.7.0 への更新](#070-への更新)」「[0.6.0 への更新](#060-への更新)」「[0.5.1 への更新](#051-への更新)」「[0.5.0 への更新](#050-への更新)」「[0.4.0 への更新](#040-への更新)」も）で、その版の注意点を確認してください。入れ替えた版は、ホストを起動し直した新しいセッションから使われます。

## 必要なもの

| 依存 | 用途・注意点 |
| --- | --- |
| Claude Code **2.1.316 以降** | フックの入力の `agent_id`（サブエージェントの区別）が必要。計画・実装・修正を担当 |
| `uv` | Python ランナー。Claude Code と同じ環境の PATH に必要 |
| インストール済み Python 3.11 以降 | Python **3.11 固定ではない**。標準ライブラリだけを使用 |
| Codex CLI | 認証と指定モデルへのアクセスが必要。DevFlow が渡すフラグ（`exec` の `--json`、`--output-schema` など）があれば、版は問わない。doctor で確認 |
| OpenCode CLI | 認証と指定 provider/model/effort へのアクセスが必要。DevFlow が渡すフラグ（`run` の `--format`、`--standalone` など）があれば、版は問わない |
| Devin CLI **3000.12.57 以降** | ホストとして使うとき。レビュアーとしては、DevFlow が渡すフラグ（`--print`、`--prompt-file`、`--resume`、`--model`、`--export`）があれば版は問わない。ログインと、指定モデルへのアクセスが必要 |
| モデルから呼び出せる `grilling` スキル | **Plan のみ必要**。同梱・自動インストールはしない。`plan.grill_skill` で実際のスキル名を指定可能 |
| 対象プロジェクトの検証環境 | Implement のビルド・テストなど。事前に人間が設定する |
| Git 2.31 以降 | Implement の既定 `snapshot.mode: "git"` と、worktree での実装で使用。非 Git プロジェクトは明示的に `"files"` に設定 |

DevFlow 0.4.0 は、Claude Code 2.1.317、codex-cli 0.166.0、OpenCode 2.2.5 で確かめました。Devin CLI は、0.10.0 で 3000.12.57 を確かめました。これより新しい版では、`doctor` が警告を出します（止めません）。確かめ方は [検証範囲](docs/validation.md#版を上げるときの確認) にあります。

実行エントリーポイントには次のインラインメタデータがあります。

```python
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
```

両ホストの hooks とスキルは `uv --no-cache run --offline --no-project --no-config --no-python-downloads` でランナーを起動します。uv のキャッシュを使わず、互換 Python を選び、対象プロジェクトの依存をインストールせず、Python 自体も自動ダウンロードしません。互換 Python が一つもなければ起動できないので、必要な環境は人間が用意してください。

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
4. 同じプラン内容を設定したレビュアー全員が承認したら（`completed`）、正式な Markdown を `docs/plans/` へ保存します。レビューの上限で人間が今のプランを受け入れたとき（`accepted`）も保存します。プランのファイルはレビューした（受け入れた）バイト列のままで、横に人が読める承認の記録（例：`REVIEW-10.approval.md`）を置きます。承認したのがレビュアーか人間か、承認した版のハッシュ、残っている指摘の数、最後の版がレビュー済みかが書かれます。
5. 出力された **実際のパス** を確認します。既存ファイルは上書きしません。

実装開始は別の明示的な操作です。

```text
/devflow:implement docs/plans/REVIEW-10.md
```

パスに空白がある場合は引用符で囲みます。相対パスは対象プロジェクトを基準に扱い、プランを自動検索・推測して選びません。

```text
/devflow:implement "docs/plans/player movement.md"
```

Implement は開始時のプランと作業ツリーを固定し、Claude Code が実装し、設定された検証とコードレビューを実施します。Claude Code は、プロジェクトそのものか、同じリポジトリの別の worktree を実装場所に選べます（プロジェクトの `CLAUDE.md` などに決まりを書いておけます）。実装場所は run の間、固定されます。1つの実装場所で同時に進められる未完了の Implement は1つです。別の worktree を選べば、複数の Implement を並行できます。

既定では、未コミットの既存変更がある場合に一旦止まり、`AskUserQuestion` でその変更を保持したまま続行してよいか確認します。Plan で保存した新しいプランや追加した設定ファイルも、この確認の対象になり得ます。必ず先にコミットしなければならないわけではなく、プラグインが勝手に破棄・コミットすることもありません。既存変更を含む作業ツリーからの開始を事前に許可する場合のみ、`implement.allow_dirty: true` を設定してください。実装差分の基準は、どちらの場合も開始時の実ファイルです。

## レビューの契約

| 回 | 既定レビュアー | 既定設定 |
| --- | --- | --- |
| 1, 3, 5, … | Codex | `gpt-6.2-sol` / `high` |
| 2, 4, 6, … | OpenCode | `opencode-go/deepseek-v4.1-flash` / `high` |

これらはユーザー指定の設定値です。本配布物のテストで、各アカウントのモデル提供状況・認証・実際のモデル選択まで確認しているわけではありません。未対応時に別モデルへ黙って切り替えません。

- 最初の上限は `max_reviews`（既定 6、1〜99 で設定）で、**Plan と Implement で別に数えます**。各レビュアーごとの回数ではありません。Plan と Implement で別の値にするには、`plan.max_reviews` と `implement.max_reviews` に書きます（例：`{"schema_version": 1, "plan": {"max_reviews": 3}, "implement": {"max_reviews": 8}}`）。起動時の `--max-reviews N` は、それより優先されます。
- 上限に達して承認が揃っていないと、run は終わらずに人間に確認します（`awaiting_human`）。確認の前に、レビューの要約を見せます。
  - 「続ける」と答えると、レビュアーの人数分（既定 2 回）を足して、同じ run で続けます。回数は戻さず、7 回目、8 回目と数え続けます。それまでのレビュー、指摘の採否、同じ内容への承認も引き継ぎます。
  - 「やめる」と答えると、`exhausted` で終わります。
  - 「受け入れて終える」と答えると、今の版（Plan は今のプラン、Implement は最新の checkpoint）を人間が受け入れたとして、`accepted` で終わります。`completed` とは別の終わり方で、全レビュアーの承認ではありません。答えの記録と最終レポートの `human_acceptance` に、受け入れた版と `AskUserQuestion` の答えが残り、レポートの `warning` にも書かれます。Implement では、作業ディレクトリが最新の checkpoint のままで、そのチェックが通っている（または人間が検証を認めた）ことが条件です。
- **レビューの要約**：上限の確認の前と、run が終わったとき（`completed`・`exhausted`・`cancelled`・`accepted`）に、`status` と最終レポートの `review_summary` に入り、決まった形の短い Markdown（`review_summary_markdown`）で表示されます。Claude はそれをそのまま見せ、要約に出ていることを自分の報告で繰り返しません。
  - 表示するもの：状態、回数と時間（run の経過時間とレビューの時間の合計）、現在の版がレビュー済みかと各レビュアーの承認、1 回 1 行の表（レビュアー、版、判定、重さごとの指摘の数と採否の数、ツールの呼び出しの回数、かかった時間と制限時間）、レビュアーごとの 1 行（指定したモデルと effort、CLI の版、トークン数の合計）、失敗した回とその理由、残っている指摘（全文）、人間の決定（何回目のあとに、どの種類の質問で、何を選んだか、いつか）。モデルと effort は指定したものです。実際に動いたモデルの証明ではありません。
  - 表示しないもの：ハッシュ、開始と終了の時刻、記録のパス、解決した指摘の本文と採否の理由、直しの要約と変えたもの、質問文と答え。これらは `review_summary` のデータとレビューの記録に残ります。文は途中で切りません。
  - トークン数は、CLI が出力した数を、CLI の名前のまま出します（出力に無ければ unknown）。意味は CLI ごとに違い、たとえば Codex の `input_tokens` はキャッシュ分を含み、OpenCode の `input` は `cache.read` を含みません。そのため、合計は同じレビュアー（同じ CLI、同じ名前）の回の間だけで、CLI をまたいで足すことはしません。
  - 待っている間、その run は終わっていない扱いです。同じプロジェクトで別の run を始めるには、答えるか取り消してください。
- 合計は 99 回までです。人数分を足すと 99 回を超えるときは、確認を出さずに `exhausted` で終わります（既定の設定では 98 回）。`max_reviews` を 99 にすると、上限に届いたところで確認を出さずに `exhausted` で終わります。
- レビュアーに渡すパケットには過去のレビューの全文が入るので、1 回のレビューでおよそ 10〜15 KB 増えます。パケットの上限は `plan.max_packet_bytes`（既定 2 MiB）と `implement.snapshot.max_packet_bytes`（既定 2,000,000 バイト）で、どちらも 16 MiB まで設定でき、run の開始時に固定されます。長く続ける見込みの run は、始める前に大きくしておけます。超えたときは、レビュアーを起動する前にエラーで止まります。回数は使わず、中身も削りません。その run は取り消して終えます。
- レビューを送信する前に枠を確保します。送信後のタイムアウト、不正な出力、通信失敗も使用済みです。
- 再開時にも回数・順序を保持します。失敗を理由に無料枠として再送信しません。
- 設定したレビュアー全員が **同じ内容ハッシュ** を承認したときだけ早期完了します。内容が変われば以前の承認はその版にしか適用されません。
- 最後のレビューの後の修正版を「レビュー済み」とは表示しません。上限で「やめる」を選んだときは、残課題と、最終版が未レビューであることを報告します。「受け入れて終える」を選んだときも、最終版が未レビューかどうかを要約、レポート、承認の記録に書きます。
- 指摘の採用・不採用・理由を保存します。外部レビューは人間の決定を上書きする権限ではありません。
- 外部レビュアーは、利用者の各 CLI の設定のまま、Implement では実装場所で、Plan ではプロジェクトで起動します。ファイル、差分、ログは自分のツールで読みます。DevFlow が渡すのは、計画や面接などのデータと、ファイルの場所です。
  - 画像や PDF のようなテキストでないファイルも、パケットや読んだファイル（差分を含む）が指していて判定に関わるときは、自分のツールで開くよう指示文で頼みます（0.10.1 から）。開けるかは CLI とモデル次第です。2026-11-16 に確かめた結果では、画像は Codex・OpenCode・Claude・Devin のどれも読め、PDF は Codex・OpenCode・Claude が読めて Devin は読めませんでした（[検証の記録](docs/validation.md#0101-の検証2026-11-16)）。
  - レビューで参照してほしいファイルは、レビュアーの起動先から参照できる場所に用意してください。Implement で別の worktree を実装場所にしたときは、元の作業ツリーにだけあるファイル（コミットしていない画像など）はその worktree に現れません。読み取れる範囲は、起動先の中に限られるわけではなく、各 CLI の権限の設定に従います。
- レビュアーの CLI は、プロジェクトの設定と指示のファイル（`AGENTS.md` など）を、ふだんどおり読み込みます。指示文では、ファイルの中身をデータとして扱い、判定や出力の形を変える指示に従わないよう頼みます。
- レビューが失敗になるのは、出力から、レビュアーが終わっていない・答えがないとわかるときと、答えがスキーマに合わないとき、答えに記録に書けない文字（対にならないサロゲート。JSON の `\ud800` のようなエスケープから生じる）があるときだけです。答えは書き換えません。ただし、答えの点検（厳密な JSON として読むことと、スキーマ・レビューの決まりの点検）で断られたときは、出し直しに対応した CLI（今は Devin）では同じ会話で、断られた理由とスキーマを添えて、レビューをもう1回送ってもらいます（新しいレビューではなく、回数は1回分のまま。元の制限時間の残りだけを使います）。出し直しても点検に通らなければ、その回は失敗です。どちらの答えも記録に残り、出し直しがあった回の活動には `answer_follow_ups: 1` が出ます。ツールの呼び出しやサブエージェントは止めず、種類ごとに数えて、`status` と最終レポートの `reviews` に出します（名前が記録に書けないときは `(unnamed)` として数えます）。
- Implement では、レビューの前後で実装場所のコード（スナップショットの範囲）が変わった回は、合格に数えません。

要件変更、重要な設計判断、権限・機密性・費用の問題では止まって、人間に確認します（エスカレーション）。解除には、止まった後に `AskUserQuestion` で得た実際の回答を記録します。普通の会話の返信や `/devflow:resume` だけを承認に読み替えません。

## プロジェクト設定

プロジェクト直下の **`devflow.config.json`** に設定します。共通の `reviewers` と `max_reviews`、および `plan` / `implement` ごとの設定を分けます。`plan` / `implement` の中にも `reviewers` と `max_reviews` を書け、そのフローでは共通の値より優先されます。配列は全体置換です。設定は実行開始時に固定します。

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

レビュアーを設定する場合は、最上位の `reviewers` に全員を書きます（配列は全体置換）。1人以上かつ `max_reviews` 以下で、種類（`codex` / `opencode` / `claude` / `devin`）は問いません。同じ種類だけの一覧（Codex 2人など）も使えます。

```json
{
  "schema_version": 1,
  "reviewers": [
    {"id": "codex", "kind": "codex", "command": ["codex"], "model": "gpt-6.2-sol", "effort": "high",
     "timeout_seconds": 1800},
    {"id": "opencode", "kind": "opencode", "command": ["opencode"], "model": "opencode-go/deepseek-v4.1-flash",
     "effort": "high", "timeout_seconds": 1800}
  ]
}
```

| 項目 | 意味 |
| --- | --- |
| `id` | 小文字の名前。レビュアーごとに異なる |
| `kind` | `codex`、`opencode`、`claude`、`devin` のどれか |
| `command` | CLI を起動する引数の配列（例：`["codex"]`、`["node", "/path/to/cli.js"]`）。DevFlow はシェルを通さずに起動する |
| `model`、`effort` | モデルと推論の強さ。DevFlow が引数に組み込むので、`-` で始まらず、空白・`"`・`\`・`#` を含まないもの |
| `timeout_seconds` | 1回のレビューの制限時間（5〜3600 秒、既定 1800 秒）。DevFlow はレビュアーへの指示に、持ち時間、止める時刻、答えを書き始める目安の時刻（持ち時間の 80% の時点）を書く。時間を過ぎると DevFlow がレビュアーを止め、その回は失敗 |

知らない項目があると、その項目の名前を示すエラーになります。

Devin をレビュアーにする例です。DevFlow は `devin --print --prompt-file <入力> --model <model>-<effort> --export <記録>` で起動し、`model` と `effort` を `-` でつないで渡します（`swe-2` と `high` なら `swe-2-high`）。選べる effort はモデルごとに違うので、`devin models list` で確かめてください。Devin は、信頼したディレクトリとその下でだけ動きます。Implement で別の worktree を実装場所にするとレビューはその worktree で起動するので、その worktree かその上のディレクトリを先に信頼しておいてください。答えは、Devin が `--export` に書く記録の最後の発言から読みます。Devin には答えの形を強制する機能が無いので、モデルが JSON の前後に文章を付けたり、スキーマに合わない JSON を返したりすることがあります。答えの点検で断られたときは、DevFlow が同じ会話の続き（`devin --print --prompt-file <出し直しの依頼> --resume <1回目の記録のセッション ID> --model <model>-<effort> --export <出し直しの記録>`）を1回だけ起動し、断られた理由とスキーマを添えてレビューを送り直してもらいます（DevFlow は文章から JSON を拾いません）。出し直しの答えも点検に通らなければ、その回は失敗です。出し直しの起動は新しいレビューではなく、元の制限時間の残りだけを使います。長い入力では Devin が会話を自動で要約し、要約でスキーマが消えることがあるので、出し直しの依頼にはスキーマを入れています。

```json
{"id": "devin", "kind": "devin", "command": ["devin"], "model": "swe-2", "effort": "high", "timeout_seconds": 1800}
```

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
| `.devflow/sessions/` | ホストの会話（セッション）と実行の対応 | しない |
| `docs/plans/*.md` | 設定したレビュアー全員のレビューを通過した、またはレビューの上限で人間が受け入れた正式なプラン（保存先は `plan.publish.directory`、名前は `plan.publish.filename` で決まる） | 必要に応じて推奨 |
| `docs/plans/*.approval.md` | 横のプランの承認の記録（承認したのがレビュアーか人間か、版のハッシュ、run ID、日時など） | プランと同じ |
| `devflow.config.json` | 共有可能なプロジェクト設定。秘密は書かない | 推奨 |

run-id は一回の作業を識別する自動生成 ID です。レビューごとに変わらず、Plan と Implement は別の ID を持ちます。次のコマンドでは実際に報告された ID を使ってください。

```text
/devflow:status
/devflow:resume 20261101T140000Z-0123456789ab
```

`.devflow/.gitignore` は非公開の実行データを無視するために作成します。プロジェクト直下の `.gitignore` を勝手に変更しません。Git で既に追跡済みのファイルは `.gitignore` では追跡解除されません。正式プランを保存しても `git add` / `commit` / `push` は実行しません。

ランナーでは、読み取りも含めて常に `--host claude`、`--host codex`、`--host devin` のどれかを明示します。`--help` と `--version` はホスト指定なしで表示できます。run を変える操作（`cancel` など）は、run を所有する会話のセッションからだけ使えます。人間が自分の端末からランナーで操作するときは `--session` にそのセッションの ID を渡すか、先に `/devflow:resume RUN_ID` で今の会話へ引き継いでください。`status` と `doctor` は、どこからでも使えます。

外部処理（チェック・レビュー）の途中で Claude Code を閉じた場合などは、run が処理中のまま残ります。関係するプロセスが止まったことを確かめてから、人間が自分の端末でランナーの `recover` を実行します。その回は使用済みのままです。`PLUGIN_DIR` はインストールしたプラグインのディレクトリ、`PROJECT_DIR` は対象プロジェクトです。Plan の `--expected-review` には、処理中だったレビューの回（`/devflow:status` の `review_count`）を渡します。

```text
uv --no-cache run --offline --no-project --no-config --no-python-downloads "PLUGIN_DIR/scripts/devflow.py" --host claude --project "PROJECT_DIR" --run RUN_ID implement recover --confirm-process-stopped
uv --no-cache run --offline --no-project --no-config --no-python-downloads "PLUGIN_DIR/scripts/devflow.py" --host claude --project "PROJECT_DIR" --run RUN_ID plan recover --expected-review N --confirm-process-stopped
```

上の例は Claude の run 用です。Codex の run を復旧するときは `--host codex`、Devin の run は `--host devin` を指定します。

## 制約と安全性

- DevFlow は OS サンドボックスではなく、どのツールも止めません。守りたいことは、Claude Code と各 CLI の権限・サンドボックスの設定で守ってください。
- 1つのセッション（Claude Code / Codex / Devin の1つの会話）で進める run は1つです。並行作業は会話を分けて行います。Plan は何本でも並行できます。Implement は実装場所（プロジェクトか worktree）ごとに、未完了の run を1つまで持てます。
- Implement を並行するときは、別の worktree を実装場所にします。worktree はプロジェクトの外か、`.gitignore` で除外した場所に作ってください。プロジェクトの中にある Git で無視されていない worktree は、プロジェクト本体を実装場所にする Implement のスナップショットを止めます（`Nested repositories require separate implementation runs`）。Devin をレビュアーにするときは、信頼したディレクトリの下にない worktree を `start` の前にその worktree かその上のディレクトリで信頼してください（Devin は信頼したディレクトリとその下でだけ動きます）。
- 並行する会話は、どれも `.devflow` を持つ同じプロジェクトルートで始め、worktree は `--workspace` で指定します。worktree の中で始めた会話は worktree 側の `.devflow` を使うので、ほかの会話の run が見えず、この制約も共有されません。
- 実装場所はパスで比べます。macOS の大文字小文字を区別しないファイルシステムで、同じ worktree を大文字小文字だけ違うパスで指定すると、別の実装場所とみなされます。いつも同じ書き方で指定してください。
- 同じ実装場所で Plan の公開と Implement を同時に進めると、公開した計画のファイル（Git で無視されていない場合）が Implement の差分に入ります。同時に進めるなら、Implement は worktree で行ってください。
- 常駐サービスではありません。Claude Code が終了・停止したら進行も止まり、明示的に再開します。
- 人間の対話セッションを対象とします。スケジュール実行やエージェント間メッセージを、人間の承認入力として利用する運用はサポートしません。
- 外部レビュアーには、最初の依頼・表示された面接の質問と回答の原文・決定サマリー・プラン・過去のレビュー（Implement では加えてコードの場所と差分のファイル）が渡ります。レビュアーはプロジェクトのファイルを自分で読み、それをレビュアーのサービスへ送ります。
- テストやビルドはプロジェクトコードを実行します。コマンド自体の安全性は人間が確認してください。
- push、公開、デプロイ、依存の追加、破壊的移行の前には、人間に確認するようスキルで頼んでいます。

詳しくは [設計](docs/architecture.md)、[信頼の考え方と注意](docs/security.md)、[検証範囲](docs/validation.md) を参照してください。

## 0.12.3 への更新

0.12.2 の run は、そのまま続けられます（run の形式は変わりません）。

変わったこと：

- **Devin が信頼を拒んだときの案内が、実際にレビュアーを起動したディレクトリを示すようになりました。** Implement で `--workspace` に別の worktree を指定すると、Devin レビュアーはその worktree で起動します。Devin は信頼したディレクトリとその下でだけ動くため、プロジェクトの外の worktree は別に信頼が要ります。エラーの文は、その worktree のパスと「その worktree かその上のディレクトリを信頼する」手順を示します。
- **3ホストの実装スキルと doctor の案内に、worktree の信頼の注意を足しました。** Devin がレビュアーのとき、`start` の前に信頼の要否を判断できるようにするためです。

0.12.2 に戻すときは、プラグインを戻します。run の形式は同じなので、0.12.3 で作った run も読めます。

## 0.12.2 への更新

0.12.1 の run は、そのまま続けられます（run の形式は変わりません）。

変わったこと：

- **Codex レビュアーの既定のモデルが `gpt-6.1-sol` から `gpt-6.2-sol` になりました**（Plan と Implement の両方）。effort（`high`）と制限時間（1800 秒）は変わりません。
- 新しい既定を使うのは、0.12.2 で始めた run のうち、設定ファイルで `reviewers` を書かず、起動時に `--reviewer` も指定しないものだけです。設定は run の開始時に固定されるので、続けられる既存の run は開始時のレビュアーのままで、旧既定の `gpt-6.1-sol` で始めた run は resume しても `gpt-6.1-sol` を使います。
- 今までどおり `gpt-6.1-sol` を使うときは、`devflow.config.json` か `devflow.config.local.json` の `reviewers` に `"model": "gpt-6.1-sol"` を書くか（[プロジェクト設定](#プロジェクト設定)）、起動時に `--reviewer codex gpt-6.1-sol high` を指定してください。

0.12.1 に戻すときは、プラグインを戻します。run の形式は同じなので、0.12.2 で作った run も読めます。

## 0.12.1 への更新

0.12.0 の run は、そのまま続けられます（run の形式は変わりません）。

変わったこと：

- **答えの点検で断られた答えは、どの点検で断られた場合も出し直してもらえます。** 0.12.0 では、答えが厳密な JSON として読めないときだけでした。0.12.1 では、JSON としては読めるがスキーマ・レビューの決まりに合わない答え（足りない・知らないフィールド、verdict と指摘の食い違い、別の版へのレビュー）も、同じ会話で1回だけ送り直してもらいます。出し直しの決まりは変わりません（新しいレビューではなく、元の制限時間の残りだけを使います。出し直しの答えも点検に通らなければ失敗です）。
- **出し直しの依頼は1つの雛形になり、断られた理由とスキーマの全文を添えます。** Devin が長い会話を自動で要約し、要約でスキーマとフィールド名が消えることがあるためです。
- **Implement の「別の版へのレビュー」の点検は、答えの点検の中に入りました。** 記録の形は変わりませんが、利用者に見える誤りの文は2つ変わりました。別の版へのレビューは `snapshot_id must be the snapshot under review: <版の ID>` となり、Devin ではほかの断られた答えと同じく1回だけ出し直します。重複した JSON キーは、キーの名前を ASCII にエスケープして長い名前を切った形で示します（例：`Duplicate JSON key: "\ud800"`）。

0.12.0 に戻すときは、プラグインを戻します。0.12.1 で作った run（点検で断られて出し直した `follow-up/` と、活動の `answer_follow_ups: 1` を含む）は、0.12.0 の `status`・`report`・`doctor` がそのまま読めます（確かめた結果は [検証範囲](docs/validation.md) にあります）。戻した版では、JSON として読めるが点検に合わない答えは、0.12.0 までどおり出し直されずに失敗になります。

## 0.12.0 への更新とバージョンを戻す場合

0.11.0 の run は、そのまま続けられます（run の形式は変わりません。試行の活動に `answer_follow_ups` が足されるだけです）。

変わったこと：

- **答えが JSON として読めないときだけ、同じ会話で1回出し直してもらえます。** 今までは、レビュアーが JSON の前後に文章を付けると、その回はそのまま失敗でした。0.12.0 では、出し直しに対応した CLI（今は Devin）に限り、DevFlow が同じ会話の続きを `--resume` で1回だけ起動し、同じレビューを JSON だけで送り直してもらいます。新しいレビューではなく、レビューの回数とレビュアーの制限時間は1回分のまま（出し直しは残りの時間だけを使います）。出し直しても読めなければ失敗です。ほかの失敗（終了コード・時間切れ・答えなし・スキーマ違反）では出し直しません（[レビューの契約](#レビューの契約)、[プロジェクト設定](#プロジェクト設定)）。
- **出し直しの記録は `review-processes/NN/follow-up/` に入ります。** 依頼文・起動の記録・出力・Devin の export が、1回目と同じ名前で残ります。1回目の記録は変わりません。
- **試行の活動に `answer_follow_ups`（0 か 1）が出ます。** `status` の reviews、最終レポートの reviews、`review.json`、試行の記録に同じ値が出ます。トークン数と活動の数は、出し直しがあったときはその記録から読みます（Devin の export は会話全体の累計なので、最後の回の数がそのまま合計です）。

0.11.0 に戻すときは、プラグインを戻します。ただし次の点に注意してください。

- 0.12.0 で出し直しがあった run の記録には、活動の `answer_follow_ups` と `follow-up/` の記録が入ります。0.11.0 の `status`・`report`・`doctor` は、そのままの形で読めます（確かめた結果は [検証範囲](docs/validation.md) にあります）。
- 出し直しの途中で止まった試行は、0.11.0 でも 0.12.0 でも、今までどおり `recover` で中断として終わり、回数は消費したままです（答えの形の失敗として記録されるわけではありません）。ただし 0.11.0 のトークン数の読み取りは1回目の `stdout.jsonl` だけを見るので、その試行では1回目の数が出ます（出し直しを含む累計は出ません）。

## 0.11.0 への更新とバージョンを戻す場合

0.10.1 の run は、そのまま続けられます（run の形式 10、セッションの形式 3、設定の `schema_version` 1 は変わりません）。

変わったこと：

- **レビュアーを1人から設定できます。** 今までは2人以上で、種類も2つ以上が必要でした。0.11.0 では1人以上、`max_reviews` 以下なら、種類は問いません。同じ種類だけの一覧（Codex 2人など）も使えます。完了の条件は今までどおり、設定したレビュアー全員が同じ版を承認したときです（[プロジェクト設定](#プロジェクト設定)）。
- **`max_reviews` を 1〜99 で設定できます。** 今までは 2〜6 でした。既定は 6 のままです。Plan と Implement で別の値にするには、`plan.max_reviews` と `implement.max_reviews` に書きます（この書き方は前からあります）。上限で「続ける」を選ぶと、今までどおりレビュアーの人数分を足します。足すと合計 99 回を超えるときは、確認を出さずに `exhausted` で終わります（[レビューの契約](#レビューの契約)）。
- **起動時に `--max-reviews N` を指定できます。** その run の `max_reviews` を、設定より優先して決めます。`--reviewer` と同じく開始時に固定し、resume では変えません。Plan の後の `/devflow:implement` の案内には引き継ぎません。
- **文言を人数に依存しない言い方にしました。** 「両レビュアー」「双方」などを「設定したレビュアー全員」に直しました。公開を拒むときのエラーも "approved by every reviewer" になります。

0.10.1 に戻すときは、プラグインを戻します。ただし次の点に注意してください。

- `max_reviews` が 1 または 7 以上の設定ファイル（共有・ローカル・フロー別のどれでも）は、0.10.1 では設定のエラーになり、新しい開始も止まります。戻す前に 2〜6 に直してください。
- `max_reviews` が 1 または 7 以上で始めた run は、0.10.1 では開けません（保存された設定のエラーになります）。0.11.0 で終えてから戻してください。
- レビュアー1人や同じ種類だけで始めた run は、`max_reviews` が 2〜6 なら 0.10.1 でも開けます。ただし、その設定で新しい run は始められません。

## 0.10.1 への更新

0.10.0 の run は、そのまま続けられます（記録の形式は変わりません）。

変わったこと：

- **レビュアーが、テキストでないファイルも自分で開きます。** 全レビュアー共通の指示（`common/reviewer-rules.md`）に、画像や PDF のようなテキストでないファイルでも、パケットや読んだファイル（差分を含む）が指していて判定に関わるなら、自分のツールで開いて中身を見る、という一文を足しました。DevFlow がファイルの中身を渡すわけではなく、開けるかは CLI とモデル次第です（[レビューの契約](#レビューの契約)）。2026-11-16 の確認では、画像は Codex・OpenCode・Claude・Devin のどれも読め、PDF は Codex・OpenCode・Claude が読めて Devin は読めませんでした（[検証の記録](docs/validation.md#0101-の検証2026-11-16)）。
- **Plan の面接での添付の扱いの案内を直しました。** チャットに貼った画像や PDF は、今までどおり中身がレビュアーに送られません。レビュアーに見てほしいものは、プロジェクトの中に保存してパスを書いてもらい、プランにもそのパスを書きます。`@` で指したファイルはパスが残るので、プロジェクトの中にあればレビュアーが開けます。どちらも、大事な点は言葉でも書いてもらいます。レビュアーが開いたファイルは、そのレビュアーの提供元へ送られます。

0.10.0 に戻すときは、プラグインを戻すだけで足ります（記録の形式が同じなので、0.10.1 で始めた run も 0.10.0 で続けられます）。

## 0.10.0 への更新

0.9.x の run は、そのまま続けられます（run の記録の形式は 10、セッションの記録の形式は 3 のままです）。

変わったこと：

- **Devin CLI をホストとレビュアーに加えました**（[Devin CLI での利用](#devin-cli-での利用0100)）。レビュアーの `kind` に `devin` が加わりました。既定のレビュアー（Codex と OpenCode）は変わりません。
- **Claude Code のフックのファイルの名前が `hooks/claude.json` になりました**（`.claude-plugin/plugin.json` の `hooks` で指定します）。Claude Code と Devin はどちらも `hooks/hooks.json` を必ず読むため、そこにはどのホストのフックも置きません。Claude Code はいつもどおりプラグインを更新し、起動し直せば足ります。Codex は `hooks/codex.json` のままです。
- ランナーの hook は、引数の誤り（たとえば古いランナーに知らないホストを渡したとき）でも終了コード 1 で終わります。

**0.9.1 に戻すとき。** 0.9.1 は、Devin がホストの run（`owner_host: "devin"`）を「壊れた記録」として扱います。壊れた Implement の run があると、終わっているかに関わらず、ほかの Implement の開始と再開が止まります。また 0.9.1 は `kind: "devin"` のレビュアーを設定の誤りとします。プラグインを戻すだけでは Claude と Codex 側も使えなくなることがあるので、次の順で戻してください。

1. 関係するプロセス（ホスト、ランナー、レビュアー、チェック）がすべて止まっていることを確かめる。
2. `.devflow/`、`devflow.config.json`、`devflow.config.local.json` を丸ごと控える。
3. 次の run は 0.9.1 で続けられないので、0.10.0 のうちに終えるか取り消す。
   - Devin がホストの run
   - どのホストの run でも、固定された設定（`config.reviewers`）に `kind: "devin"` のレビュアーを含む未完了の run。run は開始時に固定した設定からレビュアーを選ぶので、プロジェクトの設定から Devin を外しても、この run には効きません。
4. Devin がホストの run とセッションの記録（`owner_host` か `host` が `devin` のもの）を、`.devflow/` の外の保管場所へ移す。
5. 設定から `kind: "devin"` のレビュアーを外す（または 0.10.0 にする前に控えた設定へ戻す）。
6. 各ホストで 0.9.1 に戻す。Devin からは DevFlow を外す（0.9.1 を Devin に入れると、すべてのプロンプトが止まります）。

## 0.9.1 への更新

0.9.0 の run は、そのまま続けられます（記録の形式は変わりません）。

変わったこと：

- **レビュアーの答えがコードブロックで囲まれていても読めます。** OpenCode が答えの JSON を ` ```json … ``` ` で囲んで返すと、0.9.0 までは「Invalid reviewer JSON」で失敗扱いになり、レビュー1回を消費していました。答え全体がちょうど1つの ` ```json `（または言語名なし）のコードブロックなら、その中身を読みます。中身の JSON の検査は今までどおり厳密で、囲みの外に文章がある答えは失敗のままです。
- レビュアーへのプロンプトで、出力スキーマをコードブロックで囲まなくなりました。「コードブロックを付けない」という指示と手本の形をそろえ、囲まれた答えを起きにくくします。

## 0.9.0 への更新

0.8.x の run は、そのまま続けられます（run の記録の形式は 10、セッションの記録の形式は 3 のままです）。

変わったこと：

- **計画と実装を並行できます。** 0.8.x までは、プロジェクト全体で未完了の run を1つしか持てませんでした。0.9.0 では、Plan は何本でも並行でき、Implement は実装場所（プロジェクトか worktree）ごとに未完了の run を1つまで持てます。別の会話で、別の worktree を指定すれば、Implement も並行できます（[制約と安全性](#制約と安全性)）。
- ほかの会話の開始待ち（`/devflow:implement` を送ったが、まだ run ができていない状態）は、新しい開始を止めなくなりました。
- 読めない記録で止まるのは、読めない Implement の run があるときの Implement の開始と再開だけです（その run の実装場所が分からないため）。読めない Plan の run、読めないセッション記録、run ID の形でない名前（`.DS_Store` など）は開始を止めず、doctor の警告に出るだけです。
- `status` の出力から `sessions.pending_starts` がなくなりました。読む外部スクリプトがあれば直してください。doctor の警告は、止めるものにだけ `blocks implement starts and resumes` と書きます。
- 同じ公開名の Plan が同時に公開しても、`.devflow/publish.lock` で順番に公開し、後の run は run ID 付きの名前になります。公開の途中でプロセスが止まると `publish.lock` が残ります。ほかのロックと同じく、プロセスが止まったことを確かめてから人間が退避してください。
- Implement の開始は、スナップショットをロックの外で取ってから、ロックの中で確かめ直して run を作ります。大きなリポジトリでも、ほかの会話の入力がロック待ちで失敗しにくくなりました。

0.9.0 から 0.8.x に戻すと、0.9.0 で作った run は読めますが、0.8.x の制約に戻るので、未完了の run が1つでもあると新しい run を始められません。

## 0.8.1 への更新

0.8.0 の run は、そのまま続けられます（run の記録の形式は 10、セッションの記録の形式は 3 のままです）。

変わったこと：

- **Codex レビュアーの既定の effort が `xhigh` から `high` になりました**（Plan と Implement の両方）。モデル（`gpt-6.1-sol`）と制限時間（1800 秒）は変わりません。
- 新しい既定値を使うのは、0.8.1 で始めた run のうち、設定ファイルで `reviewers` を書かず、起動時に `--reviewer` も指定しないものだけです。設定は run の開始時に固定されるので、0.8.0 で始めた run は resume しても `xhigh` のままです。
- 今までどおり `xhigh` を使うときは、`devflow.config.json` か `devflow.config.local.json` の `reviewers` に `"effort": "xhigh"` を書くか（[プロジェクト設定](#プロジェクト設定)）、起動時に `--reviewer codex gpt-6.1-sol xhigh` を指定してください。

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

変更を配るときは、版を上げます。Claude Code は版が同じだと、GitHub から導入した利用者のプラグインを更新しません。版は3つのマニフェスト（`plugins/devflow/.claude-plugin/plugin.json`、`.codex-plugin/plugin.json`、`.devin-plugin/plugin.json`）と `plugins/devflow/scripts/devflow_core/__init__.py` に書き、版を固定しているテスト（`tests/test_distribution.py`、`tests/router/test_entrypoint.py`）の期待値も合わせて直します。

`tools/package.py` は静的検証です。Claude Code 本体によるプラグイン検証・hooks 実行・認証済みモデル呼び出しとは異なります。GitHub Actions で Linux / macOS / Windows × Python 3.11 / 3.12 / 3.13 のテストを実行します。

## 一次資料

- [Claude Code：プラグインの導入・管理](https://code.claude.com/docs/en/discover-plugins)
- [Claude Code：Skills](https://code.claude.com/docs/en/skills)
- [Claude Code：Hooks](https://code.claude.com/docs/en/hooks)
- [Claude Code：ツールの説明（Bash の待ち時間とバックグラウンド実行）](https://code.claude.com/docs/en/tools-reference)
- [Devin CLI：Plugins](https://docs.devin.ai/cli/extensibility/plugins/overview)
- [Devin CLI：Skills](https://docs.devin.ai/cli/extensibility/skills/overview)
- [Devin CLI：Hooks](https://docs.devin.ai/cli/extensibility/hooks/overview)
- [Devin CLI：Permissions](https://docs.devin.ai/cli/reference/permissions)
- [uv：Python スクリプトの実行](https://docs.astral.sh/uv/guides/scripts/)
- [uv：Python バージョンの選択](https://docs.astral.sh/uv/concepts/python-versions/)
