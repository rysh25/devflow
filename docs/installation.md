# インストールと環境設定

## 新規インストール

1. 使うホスト（**Claude Code 2.1.316 以降**、Codex CLI、Devin CLI のどれか）と、同じ環境で起動できる `uv`、互換 Python 3.11 以降、使うレビュアーの CLI（既定は Codex CLI と OpenCode CLI）を用意します。必要な認証は各 CLI の通常手順で行います。Windows の `uv` は実行可能な `uv.exe` が必要です。
2. Plan を使うなら、ホストから呼び出せる `grilling` スキルを別途導入します。実際の呼び出し名を `plan.grill_skill` に設定できます。Claude Code では `disable-model-invocation: true` のスキルは利用できません。
3. 使うホストに DevFlow のプラグインを入れます（[プラグインの導入と更新](#プラグインの導入と更新)）。GitHub からでも、ローカルの配布物からでも入れられます。
4. 対象プロジェクトで `/devflow:doctor`（Codex では `$devflow:doctor`）を実行します。

doctor は環境診断であり、アカウントで指定モデルが使えることまで保証するものではありません。DevFlow のランナー自身は、doctor でプロジェクトのファイルを書きません。ただし、設定したレビュアー CLI の `--version` と、使うサブコマンドの `--help` を実行します。Devin をレビュアーに設定した場合は、レビュアーを起動するディレクトリ（結果の `reviewer_cwd`）を Devin が信頼しているかも確かめます（存在しないモデル名で Devin を起動し、断りの文を見ます。通常モデルを呼びません。例外の範囲は、その場合に Devin がファイルを書きうることも含めて [信頼の考え方と注意](security.md)）。確かめた版より新しい CLI や Claude Code では警告を出します（止めません）。

### 長い処理の待ち時間（任意）

`review` と `check` は、外部の処理が終わるまで待つランナーの操作です。Claude は、ランナーが返す `bash_timeout_ms`（既定では `review` が 1915000 ms）を Bash ツールの `timeout` に渡し、Claude Code の Bash の上限より大きいときはバックグラウンドで実行して、終わりの知らせを待ちます。そのため、何も設定しなくてもレビューは最後まで動きます。

バックグラウンドで動いたあとの次のターンでは、ランナーの呼び出しに承認を求められることがあります（スキルの事前の許可は、そのスキルを呼んだターンの間だけ効くため）。権限のモードが `auto` のときなど、Claude Code が自動で許可する場合は求められません。避けたい場合は、Bash の上限（`BASH_MAX_TIMEOUT_MS` と `BASH_DEFAULT_TIMEOUT_MS` の大きい方）を `bash_timeout_ms` 以上にします。

```json
{"env": {"BASH_MAX_TIMEOUT_MS": "1915000"}}
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

Codex の `elevated` のサンドボックスを使っていると、レビュアーの Codex が uv で入れた Python を起動できないことがあります。対処は[Windows の Codex レビュアーが Python を起動できない](#windows-の-codex-レビュアーが-python-を起動できない)にあります。

### 面接スキルの診断

面接スキルの診断は静的な確認です。`static_ready` や `model_invocable` が真でも、Claude Code での実際の呼び出し確認を意味しません。`runtime_verified` は偽であることを区別してください。診断の探索範囲外なら `unverified`、探索できる場所で見つからない場合は `missing` として扱い、`reason` と `checked_locations` を確認します。

`/devflow:doctor` は現在のプロジェクト設定を診断します。Plan 実行中にランナーから呼ぶ `plan doctor` は、対象の session / run がある場合、その実行に保存された固定設定を診断します。結果の `configuration_source` と `run_id` で対象を確認できます。新しい設定が進行中の実行に適用済みだとは解釈しないでください。

## プラグインの導入と更新

DevFlow は1つのプラグイン `devflow` で、Claude Code、Codex CLI、Devin CLI に入れられます。入れる元は次の2つです。

- **GitHub**：リポジトリ `rysh25/devflow`。既定のブランチ（`main`）にある、公開した最新の版が入ります。
- **ローカルの配布物**：配布 ZIP を解凍した `devflow` ディレクトリか、リポジトリのクローン。直下に `.claude-plugin/marketplace.json`（Claude Code 用）と `.agents/plugins/marketplace.json`（Codex 用）があり、プラグイン本体は `plugins/devflow` です。ローカルの配布物は更新元にもなるので、プロジェクトの外の安定した場所に置き、入れた後に削除・移動しないでください。対象プロジェクトの `.devflow/` 作業ディレクトリとは別のものです。

| ホスト | GitHub から入れる | 更新する |
|---|---|---|
| Claude Code | `claude plugin marketplace add rysh25/devflow`<br>`claude plugin install devflow@devflow-marketplace --scope user` | `claude plugin marketplace update devflow-marketplace`<br>`claude plugin update devflow@devflow-marketplace` |
| Codex CLI | `codex plugin marketplace add rysh25/devflow`<br>`codex plugin add devflow@devflow-marketplace` | `codex plugin marketplace upgrade devflow-marketplace` |
| Devin CLI | `devin plugins install rysh25/devflow#plugins/devflow` | `devin plugins update devflow` |
| OpenCode | DevFlow のプラグインはありません（レビュアーとして CLI を呼ぶだけです） | CLI の更新は `opencode upgrade` |

marketplace の名前は `devflow-marketplace`、プラグインの名前は `devflow` です。インストールするプラグインはこの一つだけです。どのホストでも、新しい版は、会話がそれを読み込んだ時点（ホストの起動し直し、またはホストのプラグインの再読み込み）から使われます。0.13.0 を読み込んだ会話からは、0.12.x で始めた run は続けられません。更新の前に「[更新の前後](#更新の前後)」を確認してください。

### Claude Code に入れる

GitHub から入れる：

```text
claude plugin marketplace add rysh25/devflow
claude plugin install devflow@devflow-marketplace --scope user
```

ローカルの配布物から入れる（配布物の `devflow` ディレクトリでターミナルを開きます）：

```text
claude plugin marketplace add .
claude plugin install devflow@devflow-marketplace --scope user
```

Claude Code のセッション内からは、次のように入れます。ローカルの配布物なら、`rysh25/devflow` の代わりに実際の配布物の絶対パス（例：`/absolute/path/to/devflow`）を使います。

```text
/plugin marketplace add rysh25/devflow
/plugin install devflow@devflow-marketplace
```

画面でスコープと導入内容を確認し、反映時期は install summary に従ってください。必要なら `/reload-plugins` または新しいセッションを使用します。

`--scope user` はその環境の各プロジェクトで使う場合の指定です。チーム共通の `project` や、このプロジェクトだけの `local` は利用方針に合わせて選択してください。user scope でも別 PC・WSL へ自動インストールされるわけではありません。

更新する（GitHub からでも、ローカルの配布物からでも同じです）：

```text
claude plugin marketplace update devflow-marketplace
claude plugin update devflow@devflow-marketplace
```

- 1つ目で、登録した元（GitHub のリポジトリかローカルの配布物）から最新の内容を読み込みます。2つ目で、プラグインを新しい版に入れ替えます。`claude plugin update` にはプラグインの名前 `devflow@devflow-marketplace` が必要です。
- ローカルの配布物から入れた場合は、先に登録したディレクトリの中身を新しい配布物の中身に置き換えます。Claude Code はローカルのプラグインも版ごとの場所へ写して使うので、中身を置き換えただけでは入れ替わりません（Claude Code 2.1.325 で確認）。
- `user` 以外のスコープに入れた場合は、`--scope project` のようにスコープを付けます。
- 入れ替えた版は、Claude Code を起動し直すか、セッション内で `/reload-plugins` を実行すると使われます。それまでは、起動中のセッションは前の版のまま動きます。
- `already at the latest version` と表示された場合は、版が変わっていません。DevFlow は `plugin.json` に版を書いているので、版が上がるまで新しい内容は入りません。

### Codex CLI に入れる

対象はローカル Codex CLI 0.166.0 以降です。GitHub から入れる：

```text
codex plugin marketplace add rysh25/devflow
codex plugin add devflow@devflow-marketplace
```

ローカルの配布物から入れる：

```text
codex plugin marketplace add /absolute/path/to/devflow
codex plugin add devflow@devflow-marketplace
```

入れた後に、フックの信頼と質問機能の有効化が要ります（「[Codex CLI の導入（0.8.0）](#codex-cli-の導入080)」）。

更新する：

- **GitHub から入れた場合**：`codex plugin marketplace upgrade devflow-marketplace` を実行します。登録したリポジトリの最新を読み込み、入れたプラグインも新しい版に入れ替わります（codex-cli 0.169.0 で、0.9.0 から 0.9.1 への入れ替わりを確認）。Codex CLI には `plugin update` の操作はありません。
- **ローカルの配布物から入れた場合**：`marketplace upgrade` は Git の marketplace だけが対象で、ローカルでは `is not configured as a Git marketplace` で終わります。登録したディレクトリの中身を新しい配布物の中身に置き換えてから、`codex plugin add devflow@devflow-marketplace` をもう一度実行すると、新しい版に入れ替わります。
- 入れ替えた版は新しいセッションで使われます。`/hooks` で、フックの再確認を求められることがあります。

### Devin CLI に入れる

プラグインの管理には Devin へのログイン（`devin auth login`）が要ります。GitHub から入れる：

```text
devin plugins install rysh25/devflow#plugins/devflow
```

ローカルの配布物から入れる（プラグイン本体の `plugins/devflow` を指定します）：

```text
devin plugins install /absolute/path/to/devflow/plugins/devflow
```

この機械だけに入れるときは `--local` を付けます。入れたら `devin plugins list` で `devflow` が表示されることを確かめます。プロジェクトの信頼とランナーの承認は「[Devin CLI](#devin-cli)」を見てください。

更新する（GitHub からでも、ローカルの配布物からでも同じです）：

```text
devin plugins update devflow
```

GitHub から入れた場合は最新の内容を取り直して入れ直し、ローカルの配布物から入れた場合は登録したパスから入れ直します（Devin CLI 3000.12.57 で、ローカルのパスから入れたプラグインの版が上がることを確認）。入れ直しても更新できます。以前に Devin へ 0.9.x を入れていた場合も、0.10.0 以降に更新するか入れ直してください（0.9.x は Devin に対応しておらず、すべてのプロンプトが止まります）。

### OpenCode

DevFlow は OpenCode をレビュアーとして呼ぶだけで、OpenCode に入れる DevFlow のプラグインはありません。OpenCode CLI を OpenCode の手順で入れて認証し、`opencode upgrade` で更新します。Claude Code、Codex CLI、Devin CLI をレビュアーとしてだけ使う場合も、プラグインは要らず、その CLI と認証だけが要ります。

### フォークや private のリポジトリから入れる

自分のリポジトリに置く場合は、ZIP 内の `devflow` ディレクトリの **中身** を GitHub リポジトリのルートへ置き、上のコマンドの `rysh25/devflow` を `OWNER/REPOSITORY` に読み替えます。直下に `.claude-plugin/marketplace.json` と `.agents/plugins/marketplace.json` が必要です。本配布物の作成によって GitHub リポジトリを作成・公開・push することはありません。

public は必須ではありません。private も、実行環境の通常の GitHub/Git 認証で読み取れる場合に利用できます。認証情報を plugin、設定、ZIP に埋め込まないでください。組織の marketplace 許可ポリシーも適用されます。

### 更新の前後

更新の前に、README の「0.13.0 への更新とバージョンを戻す場合」（それより前の版から上げるときは「0.12.15 への更新」「0.12.14 への更新」「0.12.13 への更新」「0.12.12 への更新」「0.12.11 への更新」「0.12.10 への更新」「0.12.9 への更新」「0.12.8 への更新」「0.12.7 への更新」「0.12.6 への更新」「0.12.5 への更新」「0.12.4 への更新」「0.12.3 への更新」「0.12.2 への更新」「0.12.1 への更新」「0.12.0 への更新とバージョンを戻す場合」「0.11.0 への更新とバージョンを戻す場合」「0.10.1 への更新」「0.10.0 への更新」「0.9.1 への更新」「0.9.0 への更新」「0.8.1 への更新」「0.8.0 への更新とバージョンを戻す場合」「0.7.3 への更新」「0.7.2 への更新」「0.7.1 への更新」「0.7.0 への更新」「0.6.0 への更新」「0.5.1 への更新」「0.5.0 への更新」「0.4.0 への更新」も）で、その版の注意点を確認してください。版によっては、それまでの run を続けられないことがあります（例：0.6.x で始めた run は 0.7.0 では続けられません）。

更新の後：

- 対象プロジェクトで `/devflow:doctor` を実行し、`version` が新しい版になっていることを確認します。
- 実行中の run がある場合は、レビューやチェックの外部処理が終わってから更新してください。新しい会話で続けるときは `/devflow:resume RUN_ID` を使います。ただし 0.13.0 では run の形式が 11 になり、0.12.x で始めた run（形式 10）は `resume` が断られるので、0.13.0 を読み込む前に 0.12.15 で終えておいてください（README の「0.13.0 への更新とバージョンを戻す場合」）。面接の途中の Plan は元の会話でしか続けられないので、面接を終えてから更新してください。
- プラグインを更新した後は、DevFlow の状態の通知にある `runner` の場所のランナーを使います。

## よくある問題

### Plugin が marketplace にない

登録したルートが `.claude-plugin/marketplace.json` を含み、その `plugins` に `devflow` があることを確認します。登録元とキャッシュを確認して `claude plugin marketplace update devflow-marketplace` を実行し、`devflow@devflow-marketplace` を指定してください。

### 互換 Python が一つもない

plugin は offline で動き自動ダウンロードしません。人間が Python 3.11 以降を導入してください。既に3.12や3.13などがあるなら、3.11を追加する必要はありません。

### 面接スキルが見つからない・呼び出せない

`/devflow:doctor` の結果と、Claude Code から利用できるスキルの一覧を確認してください。既定の依存は `grilling` です。別の名前や plugin の名前空間が必要なら、[設定の選択と優先順位](../README.md#プロジェクト設定) を確認し、選ばれた設定の `plan.grill_skill` に、実際に `Skill` ツールから呼べる名前を指定します。

外部スキルに `disable-model-invocation: true` がある場合、DevFlow から自動呼び出しすることはできません。別の読み込み方法や手順の模倣で回避せず、人間が呼び出し可能なスキルを選びます。DevFlow は外部スキルの導入・書き換え・権限変更をしません。Implement だけを使う場合、この依存は不要です。

設定は run 開始時に固定されます。進行中 run の設定ファイルを直接編集して呼び出し先を差し替えないでください。

### レビュアーの CLI がフラグを持たないと言われる

`doctor` や `review` が「lacks flags DevFlow passes」と報告した場合、その CLI の版に、DevFlow が渡すフラグがありません。CLI を更新するか、[設定の選択と優先順位](../README.md#プロジェクト設定) に従って、有効なレビュアー一覧を指定する設定ファイルの `command` を直し、新しい run を開始してください。この確認はレビューの枠を確保する前に行うので、回数は減りません。

### 設定の知らない項目のエラー

`Invalid reviewer fields (unknown: …)` と表示された場合は、診断に表示された設定ファイルから該当項目を取り除いてください。0.4.0 でなくなった項目は、README の「0.4.0 への更新」にあります。

### テストが自動実行されない

`implement.checks` は既定で空です。その場合は人間の確認が必要です。[設定例](../examples/README.md) を参考に既存の検証コマンドを登録し、新しい run を始めてください。

### Plan 完了直後に「既存変更」の確認が出る

Git モードでは、新規作成された正式プランや設定ファイルも未コミットの変更として検出されます。意図した変更であることを確認し、`AskUserQuestion` に回答すると、その内容を開始時の基準として保持したまま進められます。事前に同じ運用を許可するなら `implement.allow_dirty: true` を使えますが、無関係な既存変更まで対象になることを理解して設定してください。

### 外部モデルを利用できない

モデル ID、provider ID、CLI 版、effort の対応、通常の認証を確認してください。代わりのモデルを自動で選びません。レビューが送信済みならその枠は消費済みです。失敗した回のあと、run は自動で次のレビュアーへ進みます（同じレビュアーが自分の回で 2 回続けて失敗すると止まります）。

### レビューが毎回失敗になる

失敗した回のあと、run は `ready` / `ready_review` に戻って次の `review` が設定の順で次のレビュアーを実行します。止まるのは、同じレビュアーが自分の回で 2 回続けて失敗したとき、レビューの最中にレビューの対象が変わったとき、レビュアーの子プロセスを完全には終わらせられなかった可能性があるとき（子プロセスが残っているかもしれないので、人間が止めてから再開します。上限の回でも止まります）です。同じレビュアーの連続した失敗で止まったときの `status` の `last_error` には、続けて失敗したレビュアーと 2 つの回の番号、原因を直してから resume コマンド（Codex では `$devflow:resume RUN_ID`、ほかのホストでは `/devflow:resume RUN_ID`）を使うことが入ります。その回の `error.json` には元のエラーのまま入ります。原因は `status` の `last_error` と、`review-processes/NN/` の `stderr.log`・`stdout.jsonl` を確認してください。プロジェクトの CLI の設定（Codex の `.codex/config.toml` など）が壊れていると、レビュアーが起動できません。レビュアーがスナップショットの範囲のファイルを変えた回（テストがキャッシュやカバレッジのファイルを作った場合など）も、合格に数えられません。

### Windows の Codex レビュアーが Python を起動できない

Windows ネイティブの Codex で `elevated` のサンドボックスを使っていると、レビュアーの Codex がサンドボックスの中から Python を起動できないことがあります。`elevated` は、`%USERPROFILE%\.codex\config.toml` の `[windows]` に `sandbox = "elevated"` と書いたときの方式です。uv が既定の場所に入れた Python で起き、次のように表示されます。

```text
error: uv trampoline failed to spawn Python child process
  Caused by: permission denied (os error 5)
```

`elevated` は、コマンドを Codex 専用の低い権限のユーザーで実行します。uv の既定では、起動用の `%USERPROFILE%\.local\bin\python.exe` が、本体（`%APPDATA%\uv\python\` の下）を起動します。起動用の exe は動きますが、本体を起動する権限がありません。対話で使う Codex では動くことがあります。DevFlow はレビュアーを `approval_policy="never"` で起動し、サンドボックスの外での実行を求めないので、この失敗がそのまま残ります。

影響はレビュアーが自分で Python を使って確かめるときだけです。`implement.checks` は DevFlow がサンドボックスの外で実行するので、影響を受けません。WSL2 の Codex は Linux のサンドボックスを使うので、この設定の影響を受けません。

**対処（確認済み）**: プロジェクト直下で選ばれるローカル設定で、DevFlow のレビュアーの Codex にだけ `unelevated` のサンドボックスを使います。対話で使う Codex の `sandbox = "elevated"` は、そのまま残せます。

```json
{
  "schema_version": 1,
  "reviewers": [
    {"id": "codex", "kind": "codex", "command": ["codex", "-c", "windows.sandbox=\"unelevated\""],
     "model": "gpt-6.2-sol", "effort": "high", "timeout_seconds": 1800},
    {"id": "opencode", "kind": "opencode", "command": ["opencode"], "model": "opencode-go/deepseek-v4.1-flash",
     "effort": "high", "timeout_seconds": 1800}
  ]
}
```

- `command` の後ろに DevFlow の引数（`exec …`）がつながります。Codex の `-c` は `exec` の前に置いても効きます。
- `reviewers` の配列は丸ごと置き換わります。使うレビュアーをすべて書いてください。
- ローカルの共通の `reviewers` は、共有設定 `devflow.config.json` の `implement.reviewers`（`hosts.<起動ホスト>` 内を含む）より優先されます。Implement で別の顔ぶれを使うなら、ローカルの `implement.reviewers` にも同じように書きます。ただしローカルの `hosts.<起動ホスト>.reviewers` がある場合はそちらが優先されるため、その起動ホスト専用の Implement 設定は `hosts.<起動ホスト>.implement.reviewers` に書いてください。
- 利用者自身で、共通名と全旧ホスト別ローカル名を `.gitignore` に追加してください。名前とスナップショット・配布の扱いは [ローカル設定の除外](../README.md#ローカル設定の除外) にあります。
- `unelevated` は、利用者のアカウントから作った権限の低いトークンで動きます。OpenAI の資料では、`elevated` よりネットワークの隔離が弱く、読み取りを禁止する場所の指定も使えないとされています。

**別の対処（未確認）**: Python を全ユーザーが読める場所に入れ直します。uv なら、管理者の PowerShell で次を実行し、新しいターミナルを開きます。これで `elevated` のまま動くかは、まだ確かめていません。

```powershell
$env:UV_PYTHON_INSTALL_DIR = 'C:\Program Files\uv-python'
$env:UV_PYTHON_BIN_DIR     = 'C:\Program Files\uv-python\bin'
uv python install 3.14 --default
$machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
[Environment]::SetEnvironmentVariable('Path', "$machine;C:\Program Files\uv-python\bin", 'Machine')
```

この Python を後で更新・削除するときも、同じ2つの環境変数を設定してから uv を実行します。

## 公式資料

- [Claude Code plugin 導入・スコープ・private marketplace](https://code.claude.com/docs/en/discover-plugins)
- [Claude Code plugin manifest](https://code.claude.com/docs/en/plugins-reference)
- [Claude Code plugin の読み込み・版・更新](https://code.claude.com/docs/en/plugins/loading)
- [Claude Code のツール（Bash の待ち時間とバックグラウンド実行）](https://code.claude.com/docs/en/tools-reference)
- [Codex CLI のコマンド（`codex plugin`）](https://developers.openai.com/codex/cli/reference)
- [Codex の Windows のサンドボックス](https://developers.openai.com/codex/windows)
- [Devin CLI のプラグイン](https://docs.devin.ai/cli/extensibility/plugins/overview)
- [uv script metadata](https://docs.astral.sh/uv/guides/scripts/)

## Codex CLI の導入（0.8.0）

対象はローカル Codex CLI 0.166.0 以降です。既存 CLI・Python・uv・各レビュアーの認証を用意し、
導入先とフックを人間が確認します。プラグインの入れ方と更新は「[Codex CLI に入れる](#codex-cli-に入れる)」にあります。
入れた後、質問機能を有効にして起動します。

```text
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
Codex の workspace-write のサンドボックスの中では、Codex 自身を起動するランナーの操作が失敗します。
会話 API の確認（`plan doctor --session`）と面接の確定（`plan seal`）は「Codex conversation API closed before replying」で止まり、
次のレビュアーが Codex の `review` は、そのレビュアーが `os error 30` で失敗します（codex-cli 0.169.0 で実測。0.9.1 でも同じです）。
DevFlow は Codex に、これらを実行する前にサンドボックスの外で実行する標準の承認を求めるよう指示しています。
承認するとその回だけサンドボックスの外で実行され、レビューの回をサンドボックスの失敗で使わずに済みます。
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

ローカルの切り替えにはプロジェクト直下の `devflow.config.local.json` を使います（ホスト専用の差分はその `hosts` に書きます）。利用者自身で、共通名と全旧ホスト別ローカル名を `.gitignore` に追加してください。[ローカル設定の除外](../README.md#ローカル設定の除外) を参照してください。POSIX は umask 077 / chmod 600、
Windows は所有者だけのアクセス権を推奨します。利用者のファイルを自動作成・編集しません。

0.8.0 は run 形式10・セッション形式3です。0.7.x の進行中 run は更新前に終えてください。
CLI の --host 常時必須化も0.8.0の破壊的変更です。0.7.x用の手動・外部スクリプトの呼び出しは、
更新と同時に --host claude / --host codex をグローバル引数へ加えます。status / doctor / recover も同じです。
0.7.xの手動コマンドはそのままでは動きません。省略時に Claude を選ぶ互換動作はありません。--help / --version の表示だけはホスト指定なしで使えます。
0.7.3へ戻すときはプロセスを止め、更新前の設定・.devflow のバックアップを戻すか、
.devflow 全体を保管して空から始めます。0.8.0 の記録を0.7.3で続けたり形式番号を直したりしません。

## Codex の会話APIを面接前に確認する

全体の doctor は、プロジェクト設定と静的な前提、各レビュアー CLI の手元での確認（`--version`、`--help`、Devin の信頼の確認）だけを行います。
実際のセッションIDを指定した plan doctor は、app-server の初期化、ルートの識別、
thread/items/list の先頭1ページまでを追加で確認します。この会話 API の確認は、モデル呼び出しやログインを行いません（レビュアー CLI の確認の例外は [信頼の考え方と注意](security.md)）。

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

## Devin CLI

0.10.0 から、ローカルの Devin CLI をホストとレビュアーに使えます。確かめたのは Linux / WSL の
Devin CLI 3000.12.57 です。Devin Desktop、Devin Cloud、ACP のエディター連携、macOS と
ネイティブの Windows は確かめていません。`uv`、互換 Python 3.11 以降、使うレビュアーの CLI を、
Devin と同じ環境に用意してください。

### 導入と更新

入れ方と更新は「[Devin CLI に入れる](#devin-cli-に入れる)」にあります。プラグインの `.devin-plugin/plugin.json` は
`"platforms": ["local"]` なので、ローカルの CLI と Desktop だけに読み込まれ、Devin Cloud には同期されません。

Devin は、プラグインのルートの `hooks.json` を読みます。DevFlow はそこで `UserPromptSubmit` と、
`ask_user_question` だけの `PreToolUse` / `PostToolUse` を登録します。フックはシェルのコマンド文字列で、
`--host devin hook` でランナーを起動します。

### 信頼と承認

1. **プロジェクトを信頼する。** 初めてのプロジェクトでは、`devin` をそのプロジェクトで起動し、信頼を選びます。
   Devin は、信頼したディレクトリとその下でだけ動きます。Devin レビュアーは、Plan ではプロジェクト、Implement では
   実装場所で起動します。本体の外にある worktree で Plan を始めた場合や、Implement で別の worktree を実装場所に
   する場合は、その worktree かその上のディレクトリを信頼してください。DevFlow は、`doctor`、`plan doctor`、
   `implement start`、各レビューの直前に、レビュアーを起動するディレクトリを Devin が信頼しているかを確かめ、
   信頼していなければレビュー回数を使わずに止めて、そのディレクトリと信頼の手順を示します。`implement start` が
   断ったときは、信頼したあと同じ `/devflow:implement` をもう一度入力します。確認は通常モデルを呼びません
   （例外の範囲は [信頼の考え方と注意](security.md)）。DevFlow は `--respect-workspace-trust false` や
   `--permission-mode` を渡しません。
2. **ランナーを承認する。** スキルはランナーを事前に許可しません。Devin は許可の規則を文字どおりに照合し、
   プラグインのパスを展開しないので、ランナーだけを許す規則を配布物に書けないためです。Devin がランナーの
   コマンドの承認を求めたら、Devin の標準の承認で許可してください。DevFlow は利用者の設定を変えません。

### 確かめる

Devin でプロジェクトを開き、次を送ります。

```text
/devflow:doctor
```

`host_diagnostics` に Devin CLI の版、`workflows.plan.interview_skill` に面接スキルの判定が出ます。
Devin の doctor に、会話の API の確認（Codex の `conversation_api`）はありません。Devin をレビュアーに設定した場合、doctor はレビュアーを起動するディレクトリ（`reviewer_cwd`）を Devin が信頼しているかを確かめます。

### 面接スキルの場所

`plan.grill_skill` のスキルは、Devin の skill ツールから呼べる必要があります。doctor は、Devin の公開資料と
`devin skills paths` に載っている次の場所の `<名前>/SKILL.md` を調べます。プロジェクトは根だけを調べ、
親のディレクトリへはたどりません。Windsurf のグローバルのスキル（`~/.codeium/<チャンネル>/skills`）は、Devin が自分のリリースのチャンネルに合う場所だけを読み、そのチャンネルをオフラインで確かめる方法が公開されていないので、調べません。

| 範囲 | 場所 |
|---|---|
| プロジェクト | `.devin/skills`、`.cognition/skills`、`.agents/skills`、`.windsurf/skills`、`.kiro/skills`、取り込みの `.claude/skills`、`.github/skills`、`.codex/skills`、`.codeium/skills` |
| 利用者 | `~/.config/devin/skills`（Windows は `%APPDATA%\devin\skills`）、`~/.config/cognition/skills`、`~/.agents/skills`、`~/.kiro/skills`、取り込みの `~/.copilot/skills`（`COPILOT_HOME` があればその下）、`~/.codex/skills` |

判定には Devin の frontmatter の項目だけを使います（Claude の `disable-model-invocation` などは使いません）。

- `triggers` に `model` が無い（例 `[user]`）と、モデルが呼べないので使えません。
- `subagent: true` か、`agent` にプロファイル名があると、サブエージェントで動き、サブエージェントには
  `ask_user_question` が無いので使えません。`subagent: false` は省略したときと同じです。
- Devin が読めない frontmatter（`: ` を含む引用符なしの値など）は、Devin が警告なしにスキルを読み込まないので使えません。
- `triggers` がリストでない、`subagent` が引用符なしの真偽値（`true`・`false`）でない場合は、公開資料の型に合わないので「未確認」と表示します（`"false"`、`0`、`yes` などを読み替えません）。
- 調べた場所に無いときは「調べた場所には無い」と表示します。プラグインのスキル、Devin Cloud のスキル、
  設定（取り込みの無効化など）による変更は確かめません。

### フックの終了コード

Devin は、フックのコマンドが終了コード 2 で終わるとプロンプトを止めます（`Prompt blocked`）。DevFlow のランナーは
hook の失敗を、引数の誤りも含めていつも終了コード 1 で返すので、止まりません。ランナーより前の失敗は防げません。
存在する `uv` が引数の誤りで 2 を返すと止まります（0.9.1 を Devin に入れたときに実測）。`uv` が見つからないときは
シェルが 127 を返し、公開資料どおりなら止まりませんが、Devin での動作は確かめていません。`/devflow:doctor` の
`executables.uv` で `uv` が見つかることを確かめてください。

### Devin をレビュアーにする

`reviewers` に `{"kind": "devin", "command": ["devin"], "model": "swe-2", "effort": "high"}` のように書くか、
起動時に `--reviewer devin swe-2 high` を指定します。DevFlow は `--model swe-2-high` のように `model` と `effort` を
`-` でつないで渡します。選べる組み合わせは `devin models list` で確かめてください。レビュアーは run の
プロジェクト（Implement では実装場所）で `devin --print` として動き、承認が要るツールは Devin 自身が断ります
（待ち続けません）。
