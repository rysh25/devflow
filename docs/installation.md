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

対象プロジェクトで `/devflow:doctor` を実行します。doctor は環境診断であり、アカウントで指定モデルが使えることまで保証するものではありません。

面接スキルの診断も静的確認です。`static_ready` や `model_invocable` が真でも、Claude Code での実際の呼び出し確認を意味しません。`runtime_verified` は偽であることを区別してください。診断の探索範囲外なら `unverified`、探索できる場所で見つからない場合は `missing` として扱い、`reason` と `checked_locations` を確認します。

`/devflow:doctor` は現在のプロジェクト設定を診断します。Plan 実行中にランナーから呼ぶ `plan doctor` は、対象の session / run がある場合、その実行に保存された固定設定を診断します。結果の `configuration_source` と `run_id` で対象を確認できます。新しい設定が進行中の実行に適用済みだとは解釈しないでください。

スキル本文が要求するすべてのツールとの互換性までは診断しません。DevFlow は呼び出し時に Plan の制限を伝え、`grilling` 内の調査も同じ会話の読み取りツールへ限定します。サブエージェントや任意の Bash を許可するためにガードを緩めず、必要な情報をその範囲で取得できない場合は人間にエスカレーションします。

## GitHub から導入する

ZIP 内の `devflow` ディレクトリの **中身** を GitHub リポジトリのルートへ置きます。直下に `.claude-plugin/marketplace.json` が必要です。

```text
claude plugin marketplace add OWNER/REPOSITORY
claude plugin install devflow@devflow-marketplace --scope user
```

`OWNER/REPOSITORY` はプレースホルダーです。本配布物の作成によって GitHub リポジトリを作成・公開・push することはありません。

public は必須ではありません。private も、実行環境の通常の GitHub/Git 認証で読み取れる場合に利用できます。認証情報を plugin、設定、ZIP に埋め込まないでください。組織の marketplace 許可ポリシーも適用されます。

## よくある問題

### Plugin が marketplace にない

登録したルートが `.claude-plugin/marketplace.json` を含み、その `plugins` に `devflow` があることを確認します。登録元とキャッシュを確認して `claude plugin marketplace update devflow-marketplace` を実行し、`devflow@devflow-marketplace` を指定してください。

### 互換 Python が一つもない

plugin は offline で動き自動ダウンロードしません。人間が Python 3.11 以降を導入してください。既に3.12や3.13などがあるなら、3.11を追加する必要はありません。ランナーだけを書き換えて hooks とスキルの検証を不一致にしないでください。

### 面接スキルが見つからない・呼び出せない

`/devflow:doctor` の結果と、Claude Code から利用できるスキルの一覧を確認してください。既定の依存は `grilling` です。別の名前や plugin の名前空間が必要なら、実際に `Skill` ツールから呼べる名前を `devflow.config.json` の `plan.grill_skill` へ指定します。

外部スキルに `disable-model-invocation: true` がある場合、DevFlow から自動呼び出しすることはできません。その拒否を別の読み込み方法や手順の模倣で回避せず、人間が呼び出し可能なスキルを選びます。DevFlow は外部スキルの導入・書き換え・権限変更をしません。Implement だけを使う場合、この依存は不要です。

設定は run 開始時に固定されます。進行中 run の設定ファイルを直接編集して呼び出し先を差し替えないでください。

### Plan の Bash コマンドがガードに拒否される

Plan・Implement 実行中の Bash は、スキルに記載された `uv` ランナーの単独呼び出しと、読み取り専用の検索コマンド（`grep`、`rg`、`find`、`ls`、`cat`、`head`、`tail`、`wc`）の単独呼び出しだけを許可します。`pwd`、`cd`、`which`、`env`、`uv --version` などの補助コマンド、`&&` や `|` での連結、リダイレクト、`find -exec` や `rg --pre` のように別のプログラムを動かすオプションは許可しません。シェルの記号は単一引用符の中でだけ文字として使えます（例：`grep -rn 'foo|bar' docs`）。プロジェクトの調査には `Read`（環境にあれば `Grep` / `Glob`）か、これらの検索コマンド、依存の確認には `/devflow:doctor` を使います。プラグインを更新した後にランナーの場所が拒否された場合は、DevFlow の状態の通知にある `runner` の場所を使います。

拒否された場合は実際のコマンドとスキルの起動書式を照合し、正規の呼び出しであるか確認します。エラー表示だけから特定の原因を断定したり、ガードの削除・権限の拡大で継続したりしないでください。

### テストが自動実行されない

`implement.checks` は既定で空です。その場合は人間の確認が必要です。[設定例](../examples/README.md) を参考に既存の検証コマンドを登録し、新しい run を始めてください。

### Plan 完了直後に「既存変更」の確認が出る

Git モードでは、新規作成された正式プランや設定ファイルも未コミットの変更として検出されます。意図した変更であることを確認し、`AskUserQuestion` に回答すると、その内容を開始時の基準として保持したまま進められます。事前に同じ運用を許可するなら `implement.allow_dirty: true` を使えますが、無関係な既存変更まで対象になることを理解して設定してください。

### 外部モデルを利用できない

モデル ID、provider ID、CLI 版、effort の対応、通常の認証を確認してください。代替モデルや権限緩和を自動選択しません。レビューが送信済みならその枠は消費済みです。

## 公式資料

- [Claude Code plugin 導入・スコープ・private marketplace](https://code.claude.com/docs/en/discover-plugins)
- [Claude Code plugin manifest](https://code.claude.com/docs/en/plugins-reference)
- [uv script metadata](https://docs.astral.sh/uv/guides/scripts/)
