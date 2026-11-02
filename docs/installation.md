# インストールと環境設定

## 新規インストール

1. ZIP を解凍します。`devflow/.claude-plugin/marketplace.json` が存在することを確認します。
2. **Claude Code 2.1.295 以降**と、同じ環境で起動できる `uv`、互換 Python 3.11 以降、Codex CLI、OpenCode CLI を用意します。必要な認証は各 CLI の通常手順で行います。Windows の `uv` は実行可能な `uv.exe` が必要です。
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

対象プロジェクトで `/devflow:doctor` を実行します。doctor は環境診断であり、アカウントで指定モデルが使えることまで保証するものではありません。doctor はプロジェクトのファイルを書きませんが、設定したレビュアー CLI の `--version` と `--help` を実行します。

### Claude Code の Bash の上限を上げる

レビュアーの既定の制限時間は 900 秒です。Claude Code の Bash は、既定では 1 回 10 分で打ち切ります。打ち切られると、run は処理中のまま残り、人が `recover` する必要があり、その回は使用済みになります。Bash の実際の上限は `BASH_MAX_TIMEOUT_MS` と `BASH_DEFAULT_TIMEOUT_MS` の大きい方です。通常は `BASH_MAX_TIMEOUT_MS` を、必要な値（レビュアーの `timeout_seconds` に 60 秒を足したミリ秒。既定では 960000）以上にします。`BASH_DEFAULT_TIMEOUT_MS` を上げると、すべての Bash の既定の待ち時間も長くなります。

```json
{"env": {"BASH_MAX_TIMEOUT_MS": "960000"}}
```

これを `~/.claude/settings.json`（またはプロジェクトの `.claude/settings.json`）に書き、Claude Code を起動し直します。Implement のチェックは 1 回の `check` で全チェックを順に実行するので、各チェックの `timeout_seconds` の合計に 60 秒を足した時間が必要です。足りない場合、`doctor` と run の開始時に警告します。run の中では、ランナーの結果と状態の通知の `bash_timeout_ms` に、次の `review` と `check` に要る値が出ます。

### Windows

Windows では Git for Windows（Git Bash）を入れてください。Claude Code は Git Bash があれば Bash ツールにそれを使い、`grep` と `find` も Linux・macOS と同じものになります。PowerShell では、DevFlow はランナーだけを通します。Git Bash が無い場合も、ランナー・`Read`・`Grep`・`Glob`・Web は使えますが、Bash のコマンドは使えません。

claude.ai や Console のアカウントでは、Git Bash があっても PowerShell ツールが既定で有効で、Claude はそれを主に使います。拒否されると Bash に切り替えますが、`CLAUDE_CODE_USE_POWERSHELL_TOOL=0` を設定すると、最初から Bash を主にできます。uv が MSYS2 や Cygwin の Python を選ぶと、パスの扱いが変わるため doctor がエラーにします。Windows 版の Python を使ってください。

面接スキルの診断も静的確認です。`static_ready` や `model_invocable` が真でも、Claude Code での実際の呼び出し確認を意味しません。`runtime_verified` は偽であることを区別してください。診断の探索範囲外なら `unverified`、探索できる場所で見つからない場合は `missing` として扱い、`reason` と `checked_locations` を確認します。

`/devflow:doctor` は現在のプロジェクト設定を診断します。Plan 実行中にランナーから呼ぶ `plan doctor` は、対象の session / run がある場合、その実行に保存された固定設定を診断します。結果の `configuration_source` と `run_id` で対象を確認できます。新しい設定が進行中の実行に適用済みだとは解釈しないでください。

スキル本文が要求するすべてのツールとの互換性までは診断しません。DevFlow は呼び出し時に Plan の制限を伝え、`grilling` 内の調査も同じ会話の、書き込まないツール・Web・許可された Bash のコマンドへ限定します。サブエージェントや任意の Bash を許可するためにガードを緩めず、必要な情報をその範囲で取得できない場合は人間にエスカレーションします。

## GitHub から導入する

ZIP 内の `devflow` ディレクトリの **中身** を GitHub リポジトリのルートへ置きます。直下に `.claude-plugin/marketplace.json` が必要です。

```text
claude plugin marketplace add OWNER/REPOSITORY
claude plugin install devflow@devflow-marketplace --scope user
```

`OWNER/REPOSITORY` はプレースホルダーです。本配布物の作成によって GitHub リポジトリを作成・公開・push することはありません。

public は必須ではありません。private も、実行環境の通常の GitHub/Git 認証で読み取れる場合に利用できます。認証情報を plugin、設定、ZIP に埋め込まないでください。組織の marketplace 許可ポリシーも適用されます。

## 更新する

更新の前に、README の「0.2.0 への更新」などの節で、その版の注意点を確認してください。版によっては、それまでの run を続けられないことがあります（例：0.1.x で始めた run は 0.2.0 以降では続けられません）。

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
- プラグインを更新した後にランナーの場所が拒否された場合は、DevFlow の状態の通知にある `runner` の場所を使います。

## よくある問題

### Plugin が marketplace にない

登録したルートが `.claude-plugin/marketplace.json` を含み、その `plugins` に `devflow` があることを確認します。登録元とキャッシュを確認して `claude plugin marketplace update devflow-marketplace` を実行し、`devflow@devflow-marketplace` を指定してください。

### 互換 Python が一つもない

plugin は offline で動き自動ダウンロードしません。人間が Python 3.11 以降を導入してください。既に3.12や3.13などがあるなら、3.11を追加する必要はありません。ランナーだけを書き換えて hooks とスキルの検証を不一致にしないでください。

### 面接スキルが見つからない・呼び出せない

`/devflow:doctor` の結果と、Claude Code から利用できるスキルの一覧を確認してください。既定の依存は `grilling` です。別の名前や plugin の名前空間が必要なら、実際に `Skill` ツールから呼べる名前を `devflow.config.json` の `plan.grill_skill` へ指定します。

外部スキルに `disable-model-invocation: true` がある場合、DevFlow から自動呼び出しすることはできません。その拒否を別の読み込み方法や手順の模倣で回避せず、人間が呼び出し可能なスキルを選びます。DevFlow は外部スキルの導入・書き換え・権限変更をしません。Implement だけを使う場合、この依存は不要です。

設定は run 開始時に固定されます。進行中 run の設定ファイルを直接編集して呼び出し先を差し替えないでください。

### ガードに拒否される Bash コマンド

Plan・Implement の実行中、Bash で使えるのは次の2つです。

- スキルに記載された `uv` ランナー。単独で呼びます。ほかのコマンドとつなげません。
- ファイルに書かず、ほかのプログラムを動かさないコマンド。`|`、`&&`、`||`、`;`、改行でつなげ、`< ファイル` で読み、出力は `/dev/null` へだけ捨てられます（`2>/dev/null`、`2>&1` など）。

使えるコマンドは次のとおりです（状態の通知にも一覧が出ます）。

| コマンド | 使える形 |
| --- | --- |
| `basename`、`cat`、`cmp`、`comm`、`cut`、`df`、`dirname`、`du`、`echo`、`head`、`ls`、`md5sum`、`nl`、`od`、`pwd`、`readlink`、`realpath`、`sha256sum`、`shasum`、`stat`、`tail`、`tr`、`type`、`uname`、`wc`、`which` | そのまま |
| `grep`、`rg`、`find`、`diff`、`sort`、`file` | プログラムを動かす・ファイルに書くオプションは不可（`grep --filter`、`rg --pre`・`-z`、`find -exec`・`-delete`・`-rm`、`diff -l`、`sort -o`・`-T`、`file -C`・`-z`・`-p` など。略記も不可） |
| `sed` | `-n`・`-E`・`-r` と、`10,40p` や `/正規表現/,/正規表現/p` のような `p` の命令だけ |
| `git` | `git --no-optional-locks --no-pager` に続けて `status`・`log`・`diff`・`grep`・`show`・`rev-parse`・`ls-files`・`blame`・`merge-base`。`-C パス` はこのプロジェクトの中だけ。`--output`・`--ext-diff`・`--show-signature`・書式の `%G…`・`--help`（マニュアルを開くため）、`git grep` の `-O`（一致したファイルをページャーで開くため）と `--ext-grep`（外部の grep を呼べるため）は不可 |
| `uniq` | オプションだけ（ファイルは指定できません） |
| `date` | `+書式` と表示用のオプション（`-u`、`-R`、`-I`、`-r`、`-v`）だけ |
| `cd` | このプロジェクトの中の、`..` を含まない絶対パスへだけ |
| `command`、`uv` | `command -v 名前`、`uv --version` だけ |

拒否されるもの：

- `$` による展開とコマンド置換、`` ` ``、`( )`、`{ }`、コメント（`#`）、背後実行（`&`）、行の継続（`\` と改行）。
- 上の表に無いコマンド（`env`、`printenv`、`printf`、`awk`、`xargs`、`python` など）。
- ファイルへのリダイレクト。

シェルの記号は、引用符の中なら文字として使えます。ただし二重引用符の中の `$` と `` ` `` は不可です（例：`grep -rn 'foo|bar' docs`、`grep -E "a|b" .`）。`grep`・`rg`・`find`・`git` などオプションに制限のあるコマンドでは、`*` などのグロブや非 ASCII の文字は引用符で囲みます。シェルがファイル名に展開した結果が、オプションとして働くのを防ぐためです（例：`grep -rn --include='*.py' foo .`、`find . -name '*.md'`）。プロジェクトの調査には `Read`（環境にあれば `Grep` / `Glob`）、Web、これらのコマンドを使います。依存の確認には、Claude はランナーの `doctor` を、人は `/devflow:doctor` を使います。PowerShell ではランナーだけを通します。プラグインを更新した後にランナーの場所が拒否された場合は、DevFlow の状態の通知にある `runner` の場所を使います。

拒否された場合は、拒否の理由と実際のコマンドを照合します。エラー表示だけから特定の原因を断定したり、ガードの削除・権限の拡大で継続したりしないでください。

### テストが自動実行されない

`implement.checks` は既定で空です。その場合は人間の確認が必要です。[設定例](../examples/README.md) を参考に既存の検証コマンドを登録し、新しい run を始めてください。

### Plan 完了直後に「既存変更」の確認が出る

Git モードでは、新規作成された正式プランや設定ファイルも未コミットの変更として検出されます。意図した変更であることを確認し、`AskUserQuestion` に回答すると、その内容を開始時の基準として保持したまま進められます。事前に同じ運用を許可するなら `implement.allow_dirty: true` を使えますが、無関係な既存変更まで対象になることを理解して設定してください。

### 外部モデルを利用できない

モデル ID、provider ID、CLI 版、effort の対応、通常の認証を確認してください。代替モデルや権限緩和を自動選択しません。レビューが送信済みならその枠は消費済みです。

## 公式資料

- [Claude Code plugin 導入・スコープ・private marketplace](https://code.claude.com/docs/en/discover-plugins)
- [Claude Code plugin manifest](https://code.claude.com/docs/en/plugins-reference)
- [Claude Code plugin の読み込み・版・更新](https://code.claude.com/docs/en/plugins/loading)
- [uv script metadata](https://docs.astral.sh/uv/guides/scripts/)
