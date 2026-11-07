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

許可を設定する場合は、スキルと同じ `uv --no-cache` を含む起動コマンドを指定します。
たとえば、確認したプラグインの実パスに対する Bash の許可は次の形です。
`/absolute/path/to/devflow` はインストール済みプラグインのパスへ置き換えてください。
スキルが使う引用符付きと、手動で使う引用符なしの両方を登録する例です。
PowerShell を使う場合は、両方の形の同じコマンドに対する `PowerShell(...)` の許可を指定します。

```json
{
  "permissions": {
    "allow": [
      "Bash(uv --no-cache run --offline --no-project --no-config --no-python-downloads /absolute/path/to/devflow/scripts/devflow.py *)",
      "Bash(uv --no-cache run --offline --no-project --no-config --no-python-downloads \"/absolute/path/to/devflow/scripts/devflow.py\" *)"
    ]
  }
}
```

引用符なしの規則だけでは引用符付きの呼び出しが許可されなかった0.7.1の実測を踏まえた例です。
今回の版で、実際のClaude Codeの許可照合を再検証したものではありません。

ランナーの実操作には、ホストを問わず常に `--host claude` または `--host codex` を渡します。
セッションなしの status / doctor も同じです。`--help` と `--version` は指定なしで表示できます。

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

更新の前に、README の「0.7.3 への更新」（それより前の版から上げるときは「0.7.2 への更新」「0.7.1 への更新」「0.7.0 への更新」「0.6.0 への更新」「0.5.1 への更新」「0.5.0 への更新」「0.4.0 への更新」も）で、その版の注意点を確認してください。版によっては、それまでの run を続けられないことがあります（例：0.6.x で始めた run は 0.7.0 では続けられません）。

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

## Codex CLI の導入（0.8.0）

対象はローカル Codex CLI 0.166.0 以降です。既存 CLI・Python・uv・各レビュアーの認証を用意し、
導入先とフックを人間が確認します。同じ配布ルートを登録します。

```text
codex plugin marketplace add /absolute/path/to/devflow
codex plugin add devflow@devflow-marketplace
codex --enable default_mode_request_user_input
```

CLI の `/hooks` で同梱コマンドを確認して信頼してください。更新で再確認が求められる場合があります。
`plugin_hooks` が removed と表示されても、0.166.0 では同梱フックの読み込みを実測しました。
質問機能は既定で無効の実験的機能です。上の起動指定は利用者が明示的に選びます。
DevFlow が設定ファイルを自動変更することはありません。

`$devflow:plan` / `$devflow:implement PATH` / `$devflow:status` /
`$devflow:resume RUN_ID` / `$devflow:doctor` の5操作を使います。
ホストのフックから実セッション ID・プロジェクト・ランナーを渡します。
`--host codex` が必要で、Claude の変数展開や、継承したセッション ID を推測して使いません。
フックが使えない場合や質問機能がない場合、面接を開始せず理由を報告します。

uv のキャッシュだけに追加権限を必要としないよう、全入口で `uv --no-cache run --offline ...` を使います。
この起動方法は Claude と Codex で共通です。
review / check でネットワークやホームへの書き込みが必要になり、既定権限で実行できない場合があります。
診断の静的 ready は実行権限・認証を保証しません。必要な処理ごとに Codex 標準の承認を求め、
利用者が同意したコマンドだけを実行します。昇格は必須条件にせず、設定も自動変更しません。
長い処理は exec_command の session_id を write_stdin へ渡して完了まで待ち、再実行しません。
この環境では、親 Codex 内に別の Codex を起動した際の bubblewrap の失敗も観測しています。
詳細・権限ごとの結果は [成立確認](codex-host-validation.md) を参照してください。

Claude の plan.grill_skill は Skill ツールで呼び出します。Codex はネイティブのスキル一覧から
指定した同じスキルを選び、その本文を読む標準の方法を使います。別スキルへの代替はしません。
Codex の探索先はプロジェクトと祖先の .agents/skills、ホームの .agents/skills、CODEX_HOME/skills です。
プラグインの名前付きスキルは実行時の一覧で確認し、doctor のファイル発見だけで実行済みとはしません。

## Claude レビュアーとローカル設定

Claude Code ホストの最低版は2.1.316、Claude レビュアーの最低版は2.1.319です。
Claude レビュアーを使う場合は command: ["claude"], model: "opus", effort: "high" を設定できます。
既定の Codex / OpenCode の一覧は変えません。
Windows では .cmd / .bat のレビュアーを起動しません。npm の claude.cmd ではなく
ネイティブ版 Claude を導入してください。設定ファイルの例と優先順位は README を参照してください。

ローカルの切り替えにはプロジェクト直下の devflow.config.local.json を使い、利用者自身で
.gitignore に /devflow.config.local.json を加えます。POSIX は umask 077 / chmod 600、
Windows は所有者だけのアクセス権を推奨します。利用者のファイルを自動作成・編集しません。

0.8.0 は run 形式10・セッション形式3です。0.7.x の進行中 run は更新前に終えてください。
CLI の --host 常時必須化も0.8.0の破壊的変更です。0.7.x用の手動・外部スクリプトの呼び出しは、
更新と同時に --host claude / --host codex をグローバル引数へ加えます。status / doctor / recover も同じです。
0.7.xの手動コマンドはそのままでは動きません。省略時に Claude を選ぶ互換動作はありません。--help / --version の表示だけはホスト指定なしで使えます。
0.7.3へ戻すときはプロセスを止め、更新前の設定・.devflow のバックアップを戻すか、
.devflow 全体を保管して空から始めます。0.8.0 の記録を0.7.3で続けたり形式番号を直したりしません。

## Codex の会話APIを面接前に確認する

全体の doctor はプロジェクト設定と静的な前提だけを確認します。
実際のセッションIDを指定した plan doctor は、app-server の初期化、ルートの識別、
thread/items/list の先頭1ページまでを追加で確認します。モデル呼び出しやログインは行いません。

```text
uv --no-cache run --offline --no-project --no-config --no-python-downloads "PLUGIN_DIR/scripts/devflow.py" --host codex --project "PROJECT_DIR" --session "ACTUAL_SESSION_ID" plan doctor
```

実IDはフックが渡した値を使い、推測しません。--runを指定する場合は、そのrunの所有者と
同じホスト・セッションのときだけAPIに接続します。別の所有者やセッション指定なしの場合は、
固定済み設定を診断し、conversation_apiはchecked=false、ready=nullと理由を返します。Codex の Plan スキルも、面接の質問より前に
この診断を実行し、conversation_api.checked / ready が true であることを確認します。
失敗した場合は理由を表示し、必要ならそのコマンドについて標準承認を求めます。
先頭ページの成功は、その後の全ページ取得や seal の成功を保証しません。
セッション指定がなければ checked=false で、APIの制限値を表示するだけです。

会話APIの待ち時間は1呼び出しごとに30秒です。全ページを合計30秒で打ち切る動作はありません。
可視ユーザー発言のcontentは実測したブロック配列を必要とします。文字列などの未確認の形では、
取得はCaptureErrorで止まり、推測した変換で続けません。
seal は1ページ100項目、最大10,000ページを取得します。doctor の conversation_api に制限値と検査範囲を表示します。
会話・質問の原文を取得できない場合は、要約への置き換えや、欠落した回答の推測で続行しません。
