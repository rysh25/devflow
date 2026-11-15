# Devin をホストとレビュアーとして使うための調査

調査日: 2026-11-15。対象は DevFlow 0.9.1（`096bd6c`）、Devin CLI 3000.12.57、
Claude Code 2.1.325、Codex CLI 0.169.0。実装は変更していない。公開資料で確認できた契約、
この環境での実測、まだ確かめていない点を分けて記録する。

これは実装前の調査であり、Devin 対応の完了報告やインストール手順ではない。

## 確認方法

公開資料は [Devin CLI のドキュメント](https://docs.devin.ai/cli/reference/commands) の
コマンド、フック、プラグイン、スキル、サブエージェント、変更履歴を読んだ。

実測では、検証用のプラグインとプロジェクトをリポジトリ外の一時領域に置いた。
Devin のデータディレクトリは `XDG_DATA_HOME` で一時領域へ移し、認証ファイルだけを
参照させた。通常の設定、プラグイン登録、セッションの記録は変更していない。

モデルは無料の `swe-2-flash-medium` を使い、`devin -p`（非対話）で実行した。
権限は既定（`auto`）のままとした。全ツールを自動承認する `--permission-mode dangerous`
は、作業していた Claude Code の安全確認で拒否されたため使っていない。

## 既存の DevFlow を Devin に入れたときの問題

Devin は Claude Code 形式のプラグインを読み込める。このため、DevFlow 0.9.1 を
Devin に入れると5つのスキルとフックが登録される。しかし、Devin のフックは
`command` の文字列だけを使い、`args` の配列を無視する。DevFlow のフックは `uv` を
引数なしで起動し、`uv` は終了コード 2 で終わる。Devin は終了コード 2 を
「止める」として扱うため、すべてのプロンプトが `Prompt blocked` になった。

公式資料は「プラグインのフックは失敗しても止めない（fail open）」と説明しているが、
終了コード 2 は止める扱いだった。このとき `devin -p` は標準出力が空のまま
終了コード 0 を返した。

## レビュアーとしての成立

| DevFlow が必要とする点 | 実測した結果 |
|---|---|
| 非対話で1回実行して終わる | `-p` で実行し、約6秒で終わった |
| プロンプトの渡し方 | 標準入力はエラーで止まる。`--prompt-file` で渡せる |
| 回答 | 標準出力は最終回答の文字だけだった。JSON を指示すると JSON 1行が出た |
| JSON スキーマ | 指定する仕組みはない。OpenCode と同じく、指示文と DevFlow 側の厳密な検証で扱う |
| モデルと effort | `--model` だけを持つ。effort はモデル名の末尾に付く（例: `gpt-6-sol-high`）。選べる effort はモデルごとに違う |
| 作業ディレクトリ | 起動したディレクトリで動く。DevFlow は既に `cwd=` で起動している |
| 承認待ち | 承認が要る操作は自動で拒否され、標準エラーに警告が出た。止まらない。`git status` と `git diff` は承認なしで動いた |
| 活動とトークン数 | `--export <path>` が `ATIF-v1.7` の JSON を書く。ツールの呼び出しと結果、最終回答、`final_metrics` のトークン数を含む。形式は公式に説明されていない |
| 事前確認 | 使う予定の引数は `devin --help` に載っている |

起動の形は次のとおりになる。

```text
devin -p --prompt-file <work>/review-input.md --model <model>-<effort> --export <work>/export.json
```

注意点:

- 信頼していないディレクトリでは `-p` が失敗する。`--respect-workspace-trust false` で
  迂回できるが、DevFlow は渡さない。人間が先にプロジェクトを信頼し、そうでなければ
  `doctor` かエラーで案内する。
- 止められても終了コードが 0 になる場合がある。回答の厳密な検証で失敗として扱う。
- 無料プランでは `swe-2-flash-medium` 以外のモデルが `Upgrade to Pro` で拒否された。
- `-p` の出力形式、終了コード、export の形式は公式に説明されていない。Devin は既定で
  自動更新するので、版が変わると動きが変わるおそれがある。
- Devin は既定で `~/.claude` とプロジェクトの `.claude/` のフック・スキル・ルールも読む。

## ホストとしての成立

### 配布とフックの場所

Devin のマニフェストは `.devin-plugin/plugin.json` で、`.claude-plugin/plugin.json` より
優先される。`skills` で専用のスキルの場所を指定できた。

フックについて、3つのホストの読み方を確かめた。

| ホスト | 読むフックのファイル | 根拠 |
|---|---|---|
| Devin 3000.12.57 | ルートの `hooks.json` と `hooks/hooks.json` の両方。マニフェストの `hooks` は読まない | 実測。`hooks` をパス・配列・インラインのどれで書いても警告なしで無視された。本体のマニフェスト項目にも `hooks` がない |
| Claude Code 2.1.325 | `hooks/hooks.json` を常に読み、マニフェストの `hooks` と合わせて読む。ルートの `hooks.json` は読まない | [Manifest reference](https://code.claude.com/docs/en/plugins/manifest-reference.md) の「`hooks/hooks.json` と一緒に読む」と、実測 |
| Codex 0.169.0 | `.codex-plugin/plugin.json` で指定した `hooks/codex.json`。ルートの `hooks.json` は読まない | 公式資料では、指定があれば `hooks/hooks.json` を探さない。実測でも、信頼後に動いたのは `hooks/codex.json` だけだった |

`hooks/hooks.json` は Claude と Devin が必ず読むので、どちらかの専用にできない。
成立する形は次の一つだけだった。

```text
plugins/devflow/
  .claude-plugin/plugin.json   "hooks": "./hooks/claude.json" を追加
  .codex-plugin/plugin.json    変更なし
  .devin-plugin/plugin.json    新規。"skills": "./devin-skills/"
  hooks/claude.json            現在の hooks/hooks.json から名前を変える
  hooks/codex.json             変更なし
  hooks.json                   新規。Devin 用
  skills/  codex-skills/  devin-skills/
```

Devin がこの形で Devin 用のフックとスキルだけを読み、Claude Code と Codex がルートの
`hooks.json` を読まないことを確かめた。

スキルの frontmatter が YAML として正しくないと（たとえば引用符なしの説明文に `: ` が
あると）、Devin は警告なしにそのスキルを読み込まなかった。

### フックの入力

| 項目 | 実測した結果 |
|---|---|
| 書き方 | シェルのコマンド文字列。`${PLUGIN_ROOT}` は展開された。`DEVIN_PLUGIN_ROOT`、`PLUGIN_ROOT`、`CLAUDE_PLUGIN_ROOT`、`DEVIN_PROJECT_DIR` が渡る |
| 共通の項目 | `hook_event_name`、`session_id`（`respected-iceberg` のような形）、`prompt_id`（ターンごとに変わる）。`transcript_path` と `cwd` はない |
| 明示的な開始 | `/probe:plan alpha beta` と入力すると、`UserPromptSubmit.prompt` にその文字列がそのまま届き、`source: "user"` が付いた |
| 追加の文脈 | `hookSpecificOutput.additionalContext` がモデルに届いた。Codex と同じく、セッション ID などをこの経路で渡せる |
| ツール | `PreToolUse` は `tool_name`、`tool_input`、`tool_use_id` を持つ。`PostToolUse` の `tool_response` は `{success, output, error}` |
| 承認の拒否 | `PreToolUse` と `PermissionRequest` が届き、`PostToolUse` は届かなかった |
| Stop | `last_assistant_message` でそのターンの最後の発言が取れる |
| 環境変数 | 起動元の `AI_AGENT` などがそのまま引き継がれた。ホストを環境変数から推測しない |

スキルを明示的に呼ぶと、ツールを通さずに本文が読み込まれた。モデルは `skill` ツールで
スキルを呼べた（`tool_input` は `{"skill": "helper"}`）。

### サブエージェントと質問

サブエージェントのツール呼び出しは、親と同じ `session_id` と `prompt_id` で届き、
フックの入力では区別できなかった。

公式資料によれば `ask_user_question` はサブエージェントに渡されない
（[Subagents](https://docs.devin.ai/cli/subagents.md)）。したがって、届いた質問は
すべて親の会話のものとして扱える。DevFlow は質問以外のツールを記録しないので、
区別が要る場面はない。親の会話の回答だけを数える既存の規則は変えない。

`ask_user_question` の `PreToolUse` の入力は Claude とほぼ同じ形だった。

```json
{"questions": [{"header": "Pick one", "question": "Pick one",
  "options": [{"label": "Alpha", "description": "Select Alpha."},
              {"label": "Beta", "description": "Select Beta."}]}]}
```

非対話の実行では質問がすぐ取り消され、`PostToolUse` は届かなかった。

人間が Devin の画面で回答すると、同じ `tool_use_id` の `PostToolUse` が届いた。
`tool_response.output` はオブジェクトではなく、前置きの1行と JSON を合わせた文字列だった。
回答は質問文をキーにし、選んだ選択肢と `skipped` を持つ。

```json
{"success": true, "error": null,
 "output": "User answered your questions:\n{\n  \"Pick a fruit\": {\n    \"selected\": [\n      \"Apple\"\n    ],\n    \"skipped\": false\n  }\n}"}
```

1回の呼び出しで2問を聞いた場合も、2問の回答が同じ形で1つの `output` に入った。

人間が質問を取り消すと `PostToolUse` は届かなかった（Codex と同じ）。モデルの入力が
ツールの検証で失敗した呼び出しも、`PreToolUse` だけが届き、`PostToolUse` は届かなかった。
回答を確認できない質問は回答として数えない。`skipped: true` の質問も回答として数えない。
この形式は公式に説明されていない。

### 会話の記録

| 方法 | 結果 |
|---|---|
| ACP（`devin acp`）の `session/load` | 終わったセッションは、ユーザー入力、発言、ツール呼び出し、質問と結果を順に再生した。サブエージェントの発言は `cognition.ai/subagent_context` で区別できた。実行中のセッションは、再生のあとに `session_locked` エラーになった |
| `--export` | 起動時に指定したセッションだけが対象になる |
| セッションのデータベース | 公開されていない SQLite。フックに `CHISEL_SESSION_DB` でパスが渡る |

DevFlow は会話の記録が無いとき、フックの記録だけで面接を作る形を既に持っている。
Devin ではこの形を使う。ACP の再生に頼る方法はエラーの前の動作に頼り、データベースを
読む方法は公開されていない形式に頼るため、採らない。

この形では、元の依頼より後の人間の発言を回答に数えず、質問ツールの回答だけを数える。
Codex の最初の対応と同じ制限である。Stop の発言の記録と、`source: "user"` による
人間の発言の判定は Devin だけの規則になるため、最初は加えない。必要になれば実測して
から判断する。

バックグラウンドのサブエージェントが終わると、Devin は自動で次のターンを始めた。
このとき `UserPromptSubmit` は届かず、`prompt_id` も変わらず、Stop だけが届いた。
実測した範囲では、`UserPromptSubmit` は人間の入力でだけ届き、すべて `source: "user"` だった。
ほかの自動の入力（Stop フックによる継続、途中入力など）は確かめていない。

### ランナーの起動

exec の `tool_input` は `command`、`timeout`（既定 30000 ms）、`workdir` を持っていた。
非対話の実行では承認が要るため、ランナーの起動は拒否された。

人間が Devin の画面で1回承認すると、90秒かかるコマンドが動いた。exec は出力が
10秒止まった時点でバックグラウンドへ移り、シェル ID を返した。モデルはその ID を
`get_output`（`timeout` 280000 ms）に渡して完了まで待ち、同じコマンドは再実行しなかった。
Codex の `exec_command` と `write_stdin` と同じ待ち方であり、専用の待機機構は要らない。

設定済みの面接スキルは、モデルが `skill` ツールで `grilling` を呼び、
`~/.agents/skills/grilling/SKILL.md` が読み込まれた。その指示に従い、2問を
`ask_user_question` で聞き、人間の回答を `PostToolUse` で取得できた。

## 設計方針

利用者の「汎用性・拡張性と一貫性を重視し、一般的な実装を基本とする。複雑・特殊な
実装は可能な限り避け、必要なら確認する」という条件に従う。

| 観点 | 方針 |
|---|---|
| 配布 | 一つのプラグインに、ホストごとのマニフェスト・フック・スキルを一組ずつ置く。Devin のフックだけはルートに置くしかない |
| ホスト連携 | 既存の `common/hosts/` に Devin を加え、フックの入力を共通の形に直す。共通の判断条件は変えない |
| 面接の記録 | フックの記録だけで作る既存の形を使う |
| 質問の出所 | Devin の質問はすべて親の会話として扱う |
| レビュアー | `CLIS` に Devin を一つ加える。effort は `<model>-<effort>` で渡す |
| 信頼と権限 | 信頼の確認、サンドボックス、権限の設定を迂回するフラグを渡さない |
| ホストのモデル | 利用者が選ぶ。DevFlow は制限も指定もしない |

ホストをまたぐ利用について:

- Implement は指定した Markdown のプランを読むだけで、作ったホストを確かめない。
  あるホストで作ったプランを別のホストで実装できる作りだが、実機では通していない。
- 1つの run を別のホストで再開・復旧することはできない。
- レビュアーの条件は「2人以上で、種類が2つ以上」だけである。Devin で実装し、
  Claude と Codex でレビューすることは、Devin のホスト対応後に設定だけでできる。
  ただし、レビュアーは Devin の exec から起動したランナーの子プロセスとして動くため、
  Devin の権限とサンドボックスの設定が効く。

## 人間の操作による確認

2026-11-15、検証用のプラグインを隔離した Devin に入れ、人間が Devin の画面で操作した。
Codex は隔離した `CODEX_HOME` で、標準の `/hooks` の画面からフックを信頼した。
信頼やサンドボックスを迂回するフラグは使っていない。

| 確認したこと | 結果 |
|---|---|
| 質問への回答 | 成立。`PostToolUse` の `output` の文字列に、質問文ごとの回答が入る |
| 質問の取り消し | `PostToolUse` は届かない。回答として数えない |
| 自動の入力 | サブエージェントの完了では `UserPromptSubmit` が届かない |
| ランナーの起動と長い処理 | 1回の承認で動き、`get_output` で完了まで待てた |
| 面接スキル | `grilling` を呼び、2問の実回答を取得できた |
| Codex のルートの `hooks.json` | 読まない |

これで、ホストとして必要な点はすべて標準の機能で成立した。Devin の質問の回答は
前置きの1行と JSON を合わせた文字列で届くため、ホスト連携でこの形を読み取る。
形式が公式に説明されていないので、読めない形のときは既存の規則どおり回答なしとして扱う。

この結果をもとに、`/devflow:plan` で計画を作る。
