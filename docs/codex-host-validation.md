# Codex ホストの成立確認

確認日: 2026-11-07。製品の基準は DevFlow 0.7.3
（`b9f5768`）、対象は Codex CLI 0.166.0。

これは実装前の測定記録であり、Codex 対応の完了報告ではありません。
この成立試験の時点では製品のコード、run 形式9、セッション形式2は変更していませんでした。
その後の0.8.0実装・回帰検証は docs/validation.md に区別して記録します。

## 確認方法

検証用プラグイン、marketplace、プロジェクト、`CODEX_HOME`、生ログを、
リポジトリ外の専用ディレクトリに置きました。通常の設定やプラグイン登録は
変更していません。Codex の認証ファイルは専用領域の一時コピーを使いました。
OpenCode のレビュアー試験は既存の CLI 設定・認証で実行しました。
どちらの認証情報も公開する記録には含めません。

検証プラグインは製品とは別物です。5つの短いスキルと、入力を保存して
追加コンテキストを返すだけのスクリプトを使いました。観察用に Stop と
SubagentStart / SubagentStop も登録しましたが、製品へ追加するフックは未決定です。

フックの信頼は CLI の標準画面で確認しました。信頼やサンドボックスを
無条件に迂回するフラグは使っていません。

## 配布・フック・5操作

- `plugin_hooks` は `removed / false` でしたが、同梱フックは実際に読み込まれました。
- `.agents/plugins/marketplace.json` と `.claude-plugin/marketplace.json` の両方を
  置いた場合、有効な DevFlow は1件でした。
- Claude 用カタログだけを置く別の検証領域からも導入できました。その場合も、
  `.codex-plugin/plugin.json` が指定する `hooks/codex.json` のコマンドが表示されました。
- 信頼前は各イベントの Installed が1、Active が0でした。信頼後は Active が1に
  なり、UserPromptSubmit と Stop の実行を確認しました。
- 信頼済みの同梱スクリプトを変更して再起動すると、再レビューが要求されました。
- スキルセレクターには5操作がそれぞれ1件表示されました。

確認できた明示構文は次のとおりです。

```text
$devflow:plan 本文
$devflow:implement 本文
$devflow:status 本文
$devflow:resume 本文
$devflow:doctor 本文
```

Plan はセレクターで選択し、残りは明示構文を入力して確認しました。
各操作の UserPromptSubmit.prompt は、この文字列と後続本文を保持しました。
5操作とも該当スキルが読み込まれ、フックが返す追加コンテキストをモデルが
受け取ったことを確認しました。

UserPromptSubmit で観察した項目は `cwd`、`hook_event_name`、`model`、
`permission_mode`、`prompt`、`session_id`、`transcript_path`、`turn_id` です。
検証セッションの ID は、起動元から継承したセッション ID と異なりました。

この測定は入口とコンテキスト配送の確認です。製品 router の読み取り専用経路や
所有者判定は、まだ実装・検証していません。

## 標準質問

既定では `default_mode_request_user_input` は `under development / false` でした。
通常モードの試行では質問ツールは呼ばれず、モデルは利用不可と報告しました。

検証 CLI の起動時だけ `--enable default_mode_request_user_input` を指定すると、
`request_user_input` の PreToolUse を取得できました。通常の設定ファイルは
変更していません。

PreToolUse は実際の `tool_use_id` と `tool_input.questions` を持っていました。
回答待ちが終了した試行では PostToolUse も取得できましたが、その応答は
**オブジェクトではなくJSON文字列**でした。

```json
{"tool_response": "{\"answers\":{}}"}
```

この応答には `is_error` がありませんでした。したがって、エラーフラグがないことを
成功した人間の回答の根拠にはできません。空の answers は回答数へ加えません。
これは未回答の試行であり、人間の実回答を取得した証拠には使いません。

子エージェントでも質問を試しました。子の PreToolUse は親と同じ session_id を
持ち、別の turn_id と非空の agent_id を持っていました。ネイティブの結果は
次のエラーでした。

```text
request_user_input can only be used by the root thread
```

子の質問で正常な PostToolUse は得られませんでした。子の会話記録にある
function_call_output からエラーを確認しました。成功した子の応答を推測して
フィクスチャへ追加していません。

親子の判定に session_id だけを使えないこと、質問の agent_id と、開始入力に
対応する turn_id を併せて確認する必要があることが分かりました。
実回答の試行では、利用者がこの会話で選んだ Alpha を、質問の PreToolUse が
届いた後で検証 CLI の標準画面から送信しました。質問前の入力には回答がなく、
同じ session / turn / tool ID の PostToolUse に次が届きました。

```json
{"tool_response": "{\"answers\":{\"probe_option\":{\"answers\":[\"Alpha\"]}}}"}
```

別の質問で Escape、その後 Ctrl-C による終了を試したところ、対応する
PostToolUse は届きませんでした。質問の開始だけから回答を作りません。
この記録だけでは取消と中断の理由を区別できないため、結果は unknown とし、
回答数にも承認にも使いません。取消成功を示す架空のイベントは作りません。

## 面接スキルと会話原文

検証プロジェクトで plan.grill_skill を grilling に設定し、登録済みスキルから
実際の SKILL.md を読み込み、その指示を使った1問の面接で実回答を取得しました。
指定した名前は0.7.3の既定値と同じです。Codex では登録済みスキルの本文を
読み込むことが標準の利用方法であり、別の面接方法を再現する fallback は使いません。

会話原文は、既存 CLI の app-server を stdio で一度起動し、公開 API の
initialize、thread/read、thread/items/list で取得できました。
experimentalApi をこの接続で明示しました。独自の常駐サービスは作っていません。

アクティブな検証セッションでは34項目を取得し、ユーザー入力6件が
UserPromptSubmit の原文と一致しました。最終発言6件も Stop の原文と一致しました。
途中のアシスタント発言7件も含まれており、Stop の最後の発言だけに依存せず
会話を取得できました。thread の id と sessionId が一致し、親 thread がないことも
確認しました。

終了済みの最初の検証セッションも取得できました。1ページ2項目の指定で7ページ、
合計14項目を末尾まで読み出しました。このセッションには status / doctor の
実際の操作入力も含まれています。

これにより、非公開 JSONL の新しいパーサーを作らず、標準 API から文字列と
出所の項目ID・turn IDを得る方法を確認できました。内部推論は面接原文に含めず、
公開フィクスチャでも内容を取り除いています。

## サンドボックスとランナー起動

親 Codex のサンドボックス内から検証 CLI を起動すると、その CLI のシェルは
次のエラーで開始できませんでした。

```text
error building bubblewrap command: app-server socket directory must be a user-owned directory with mode 0700
```

標準の承認経路で検証 CLI を親サンドボックス外から起動し直すと、
`--sandbox workspace-write` のシェルは実行できました。直接の
`codex sandbox --permission-profile :workspace` でも比較しました。

| 操作 | 観察した結果 |
|---|---|
| プロジェクト内の作成・削除 | 成功 |
| 一時ディレクトリ内の作成・削除 | 成功 |
| 一時ディレクトリ内の検証用 CODEX_HOME への書き込み | 成功 |
| プロジェクト外の uv キャッシュへの書き込み | 読み取り専用、errno 30 |
| 外部へのソケット接続 | 拒否、errno 1 |

一時領域の CODEX_HOME に書けたことは、通常のホーム配下にも書けるという
証拠にはしません。

既存のランナー起動コマンドを通常権限で実行すると、uv がホーム内のキャッシュに
一時ファイルを作れず、ランナー開始前に失敗しました。

uv の標準オプション `--no-cache` を追加すると、同じ workspace-write の
シェルから、既存ランナーのプロジェクト全体 status が成功しました。
対象は空の検証プロジェクトで、結果は idle、run と session は空でした。
これはキャッシュ書き込みのためだけに追加権限を要求せず起動できる方法です。
製品へ適用する場合は共通の uv 起動列と配布検査を合わせて更新します。

この確認で、レビュアー CLI のネットワークや通常ホームへの書き込みまで
許可されたことにはしません。それらは別に確認します。

12秒後に完了マーカーを返す処理では、最初の exec_command を yield_time_ms=1000
で起動し、返された session_id を write_stdin へ渡して完了を取得できました。
同じコマンドの再実行は行わず、最終出力のマーカーを確認してから終了しました。

## 既存レビュアーの接続

標準承認後の権限で、既存の common.reviewers.run_review を人工的なパケットと
既存の Plan 用スキーマに対して実行しました。製品の run は作っていません。

| CLI | 指定モデル・effort | スキーマ検証 | stdout.jsonl |
|---|---|---|---|
| Codex CLI 0.166.0 | gpt-6.1-sol / xhigh | 成功 | 577バイト |
| OpenCode v2.2.5 | opencode-go/deepseek-v4.1-flash / high | 成功 | 4,699バイト |

各試験の timeout は60秒です。Codex は隔離起動のため command に --no-daemon を
付けました。Codex のレビュー thread ID は、継承した起動元の ID と異なりました。
通常の既定コマンドにこのフラグを追加するかどうかを決めたものではありません。
両試験とも検証プロジェクトに DevFlow の run / session は作られていません。

これは CLI の起動・応答・スキーマ搬送の確認であり、製品の変更に対する承認や、
新しいホスト対応の統合試験の完了を意味しません。

## 成立判定

標準機能の明示設定と、必要な処理への標準承認を条件に、Codex の5入口、
フック、実質問、設定済み面接スキル、会話原文の取得は成立しました。
無回答・取消・中断の詳細理由は推測せず、回答を確認できない試行を除外します。
この制限で Plan の実回答要件を弱めず実装できます。
この成立試験と製品の統合試験は区別します。製品実装後の両ホストの Plan / Implement、
review / check の結果は [検証範囲](validation.md#製品の両ホストでの全フロー確認) に記録しています。
既定設定で失敗した項目と、明示設定・標準承認後に成立した項目を区別して記録します。

公開資料は [プラグインの配布](https://developers.openai.com/plugins/build/plugins)、
[フック](https://learn.chatgpt.com/docs/hooks)、
[権限プロファイル](https://learn.chatgpt.com/docs/permissions) を参照しました。
公開仕様から期待したことと、上記の実測結果は区別しています。
