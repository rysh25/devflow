# インストールと環境設定

## 新規インストール

1. ZIP を解凍します。`devflow/.claude-plugin/marketplace.json` が存在することを確認します。
2. **Claude Code 2.1.316 以降**と、同じ環境で起動できる `uv`、互換 Python 3.11 以降、Codex CLI、OpenCode CLI を用意します。必要な認証は各 CLI の通常手順で行います。Windows の `uv` は実行可能な `uv.exe` が必要です。
3. Plan を使うなら、Claude Code の `Skill` ツールから呼び出せる `grilling` スキルを別途導入します。実際の呼び出し名を `plan.grill_skill` に設定できます。`disable-model-invocation: true` のスキルは利用できません。
4. 解凍先の `devflow` でターミナルを開きます。

```text
claude plugin marketplace add .
claude plugin install devflow@devflow-marketplace --scope user
```

marketplace の名前は `devflow-marketplace`、plugin の名前は `devflow` です。インストールするプラグインはこの一つだけです。

`--scope user` はその環境の各プロジェクトで使う場合の指定です。チーム共通の `project` や、このプロジェクトだけの `local` は利用方針に合わせて選択してください。user scope でも別 PC・WSL へ自動インストールされるわけではありません。

Claude Code のセッション内からは、実際の解凍先パスを使います。

```text
/plugin marketplace add /absolute/path/to/devflow
/plugin install devflow@devflow-marketplace
```

パスは例です。実在するパスへ置き換えてください。画面でスコープと導入内容を確認し、反映時期は install summary に従ってください。必要なら `/reload-plugins` または新しいセッションを使用します。

対象プロジェクトで `/devflow:doctor` を実行します。doctor は環境診断であり、アカウントで指定モデルが使えることまで保証するものではありません。doctor はプロジェクトのファイルを書きませんが、設定したレビュアー CLI の `--version` と、使うサブコマンドの `--help` を実行します。確かめた版より新しい CLI や Claude Code では警告を出します（止めません）。

### 長い処理の待ち時間（任意）

`review` と `check` は、外部の処理が終わるまで待つランナーの操作です。Claude は、ランナーが返す `bash_timeout_ms`（既定では `review` が 1900000 ms）を Bash ツールの `timeout` に渡し、Claude Code の Bash の上限より大きいときはバックグラウンドで実行して、終わりの知らせを待ちます。そのため、何も設定しなくてもレビューは最後まで動きます。

バックグラウンドで動いたあとの次のターンでは、ランナーの呼び出しに承認を求められることがあります（スキルの事前の許可は、そのスキルを呼んだターンの間だけ効くため）。権限のモードが `auto` のときなど、Claude Code が自動で許可する場合は求められません。避けたい場合は、Bash の上限（`BASH_MAX_TIMEOUT_MS` と `BASH_DEFAULT_TIMEOUT_MS` の大きい方）を `bash_timeout_ms` 以上にします。

```json
{"env": {"BASH_MAX_TIMEOUT_MS": "1900000"}}
```

これを `~/.claude/settings.json`（またはプロジェクトの `.claude/settings.json`）に書き、Claude Code を起動し直します。`BASH_DEFAULT_TIMEOUT_MS` を上げると、すべての Bash の既定の待ち時間も長くなります。Implement のチェックは 1 回の `check` で全チェックを順に実行するので、各チェックの `timeout_seconds` の合計に 60 秒を足した時間が要ります。承認の確認で「今後は確認しない」を選ぶか、Claude Code の許可の設定にランナーを足しても、承認は求められなくなります。

次の場合は、上限を超えた呼び出しがバックグラウンドへ移されず、止まります。run は処理中のまま残り、人間が `recover` する必要があり、その回は使用済みになります。この場合は、上限を `bash_timeout_ms` 以上にしてください。

- `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` を設定している。
- bare モードで動かしている。

人がついていない実行（`claude -p` など）では、確認の画面が出ません。`--allowedTools` などでランナーを許可しておいてください。バックグラウンドのコマンドには、Claude が渡した `timeout` まで（最大 2 時間）の制限がかかります。

### Windows

Windows では Git for Windows を入れてください（DevFlow の git と、Implement の既定の `git` モードで使います）。Claude Code の Bash ツールと PowerShell ツールのどちらでも、DevFlow のランナーを使えます。スキルは、どちらのツールでのランナーの呼び出しも事前に許可します。

uv が MSYS2 や Cygwin の Python を選ぶと、パスの扱いが変わるため doctor がエラーにします。Windows 版の Python を使ってください。

DevFlow の git は、利用者の git の設定（`core.autocrlf` など）をそのまま使います。改行の扱いは、利用者の git と同じになります。

### 面接スキルの診断

面接スキルの診断は静的な確認です。`static_ready` や `model_invocable` が真でも、Claude Code での実際の呼び出し確認を意味しません。`runtime_verified` は偽であることを区別してください。診断の探索範囲外なら `unverified`、探索できる場所で見つからない場合は `missing` として扱い、`reason` と `checked_locations` を確認します。

`/devflow:doctor` は現在のプロジェクト設定を診断します。Plan 実行中にランナーから呼ぶ `plan doctor` は、対象の session / run がある場合、その実行に保存された固定設定を診断します。結果の `configuration_source` と `run_id` で対象を確認できます。新しい設定が進行中の実行に適用済みだとは解釈しないでください。

## GitHub から導入する

ZIP 内の `devflow` ディレクトリの **中身** を GitHub リポジトリのルートへ置きます。直下に `.claude-plugin/marketplace.json` が必要です。

```text
claude plugin marketplace add OWNER/REPOSITORY
claude plugin install devflow@devflow-marketplace --scope user
```

`OWNER/REPOSITORY` はプレースホルダーです。本配布物の作成によって GitHub リポジトリを作成・公開・push することはありません。

public は必須ではありません。private も、実行環境の通常の GitHub/Git 認証で読み取れる場合に利用できます。認証情報を plugin、設定、ZIP に埋め込まないでください。組織の marketplace 許可ポリシーも適用されます。

## 更新する

更新の前に、README の「0.5.1 への更新」（それより前の版から上げるときは「0.5.0 への更新」「0.4.0 への更新」も）で、その版の注意点を確認してください。版によっては、それまでの run を続けられないことがあります（例：0.4.x で始めた run は 0.5.0 では続けられません）。

### GitHub から導入した場合

```text
claude plugin marketplace update devflow-marketplace
claude plugin update devflow@devflow-marketplace
```

- 1つ目で、登録したリポジトリから最新の内容を読み込みます。2つ目で、プラグインを新しい版に入れ替えます。`claude plugin update` にはプラグインの名前 `devflow@devflow-marketplace` が必要です。
- `user` 以外のスコープに入れた場合は、`--scope project` のようにスコープを付けます。
- 入れ替えた版は、Claude Code を起動し直すか、セッション内で `/reload-plugins` を実行すると使われます。それまでは、起動中のセッションは前の版のまま動きます。
- `already at the latest version` と表示された場合は、版が変わっていません。DevFlow は `plugin.json` に版を書いているので、版が上がるまで新しい内容は入りません。

### ZIP から導入した場合

`claude plugin marketplace add .` で登録したディレクトリの中身を、新しい配布物の中身に置き換えます。このプラグインはそのディレクトリから直接読み込まれるので、`claude plugin update` は不要です。Claude Code を起動し直すか、`/reload-plugins` を実行すると反映されます。

### 更新の後

- 対象プロジェクトで `/devflow:doctor` を実行し、`version` が新しい版になっていることを確認します。
- 実行中の run がある場合は、レビューやチェックの外部処理が終わってから更新してください。新しい会話で続けるときは `/devflow:resume RUN_ID` を使います。面接の途中の Plan は元の会話でしか続けられないので、面接を終えてから更新してください。
- プラグインを更新した後は、DevFlow の状態の通知にある `runner` の場所のランナーを使います。

## よくある問題

### Plugin が marketplace にない

登録したルートが `.claude-plugin/marketplace.json` を含み、その `plugins` に `devflow` があることを確認します。登録元とキャッシュを確認して `claude plugin marketplace update devflow-marketplace` を実行し、`devflow@devflow-marketplace` を指定してください。

### 互換 Python が一つもない

plugin は offline で動き自動ダウンロードしません。人間が Python 3.11 以降を導入してください。既に3.12や3.13などがあるなら、3.11を追加する必要はありません。

### 面接スキルが見つからない・呼び出せない

`/devflow:doctor` の結果と、Claude Code から利用できるスキルの一覧を確認してください。既定の依存は `grilling` です。別の名前や plugin の名前空間が必要なら、実際に `Skill` ツールから呼べる名前を `devflow.config.json` の `plan.grill_skill` へ指定します。

外部スキルに `disable-model-invocation: true` がある場合、DevFlow から自動呼び出しすることはできません。別の読み込み方法や手順の模倣で回避せず、人間が呼び出し可能なスキルを選びます。DevFlow は外部スキルの導入・書き換え・権限変更をしません。Implement だけを使う場合、この依存は不要です。

設定は run 開始時に固定されます。進行中 run の設定ファイルを直接編集して呼び出し先を差し替えないでください。

### レビュアーの CLI がフラグを持たないと言われる

`doctor` や `review` が「lacks flags DevFlow passes」と報告した場合、その CLI の版に、DevFlow が渡すフラグがありません。CLI を更新するか、`devflow.config.json` の `command` で別の版を指定してください。この確認はレビューの枠を確保する前に行うので、回数は減りません。

### 設定の知らない項目のエラー

`Invalid reviewer fields (unknown: …)` と表示された場合は、示された項目を `devflow.config.json` から消してください。0.4.0 でなくなった項目は、README の「0.4.0 への更新」にあります。

### テストが自動実行されない

`implement.checks` は既定で空です。その場合は人間の確認が必要です。[設定例](../examples/README.md) を参考に既存の検証コマンドを登録し、新しい run を始めてください。

### Plan 完了直後に「既存変更」の確認が出る

Git モードでは、新規作成された正式プランや設定ファイルも未コミットの変更として検出されます。意図した変更であることを確認し、`AskUserQuestion` に回答すると、その内容を開始時の基準として保持したまま進められます。事前に同じ運用を許可するなら `implement.allow_dirty: true` を使えますが、無関係な既存変更まで対象になることを理解して設定してください。

### 外部モデルを利用できない

モデル ID、provider ID、CLI 版、effort の対応、通常の認証を確認してください。代わりのモデルを自動で選びません。レビューが送信済みならその枠は消費済みです。

### レビューが毎回失敗になる

`status` の `last_error` と、`review-processes/NN/` の `stderr.log`・`stdout.jsonl` を確認してください。プロジェクトの CLI の設定（Codex の `.codex/config.toml` など）が壊れていると、レビュアーが起動できません。レビュアーがスナップショットの範囲のファイルを変えた回（テストがキャッシュやカバレッジのファイルを作った場合など）も、合格に数えられません。

## 公式資料

- [Claude Code plugin 導入・スコープ・private marketplace](https://code.claude.com/docs/en/discover-plugins)
- [Claude Code plugin manifest](https://code.claude.com/docs/en/plugins-reference)
- [Claude Code plugin の読み込み・版・更新](https://code.claude.com/docs/en/plugins/loading)
- [Claude Code のツール（Bash の待ち時間とバックグラウンド実行）](https://code.claude.com/docs/en/tools-reference)
- [uv script metadata](https://docs.astral.sh/uv/guides/scripts/)
