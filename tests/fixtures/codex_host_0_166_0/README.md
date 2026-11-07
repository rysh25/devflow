# Codex CLI 0.166.0 の実測ペイロード

2026-11-07 に、リポジトリ外の検証用プラグインと専用 CODEX_HOME で取得しました。
製品の CanonicalEvent ではなく、ホストが返した native の項目です。

`prompt_plan.json` はスキルセレクターで選択した入力、ほかの4操作は
明示構文を入力したものです。いずれもスキルと追加コンテキストの配送を確認しました。

質問の7ファイルは、実回答の `question_answered_pre.json` / `question_answered_post.json`、
未回答の `question_unanswered_pre.json` / `question_unanswered_post.json`、
取消の `question_cancel_pre.json`、子の `question_child_pre.json` と `question_child_error.txt` です。
各ペアは同じ tool ID の PreToolUse / PostToolUse です。
`default_mode_request_user_input` を起動時に有効にしました。
実回答ペアには、人間が画面で選んだ Alpha が **JSON文字列**の answers に入っています。
未回答ペアの応答は空の answers です。
`is_error` がないことだけでは、回答があると判断できません。

子の質問は親と同じ session_id、別の turn_id、非空の agent_id を持ちました。
`question_child_error.txt` は子の会話記録の function_call_output にあった
ネイティブのエラーです。子の成功 PostToolUse は取得されておらず、作っていません。

パスとセッション・ターン・ツールIDを一貫した人工値へ置換しました。
正常なJSONとして加工・再シリアライズした例だけを収録しています。
重複キー、不正JSON、不正UTF-8を持つ生バイトの例は含めていません。
派生した異常系を追加する際は、実測そのものと区別してください。

test_codex_history.py の再生テストでは、別セッションの未回答ペアを会話API記録と組み合わせる際、
セッション・turnの識別子をメモリ内で揃えています。質問入力の不一致、複数質問ID、事前入力、
時計の逆行・同じ秒、未知の項目・非テキストブロック・async配信の例も、
実測データから派生させたケースです。新たな実機取得とは扱いません。
取消は question_cancel_pre.json のみ、子は question_child_pre.json のみを使い、
取得していない成功Postや取消Postは作っていません。

`history_api_pages.json` は終了済みの検証セッションを公開 API で読み出した
7ページ分の応答です。1ページ2項目で、末尾の nextCursor は null です。
thread のメタデータは必要な項目だけに絞り、ID・パス・opaque cursor を置換し、
時刻を一貫してずらしました。内部推論の配列の内容は取り除いています。
元ペイロードのJSONLを推測して作ったものではありません。

取得条件・加工内容は `metadata.json`、全体の進捗と限界は
[成立確認記録](../../../docs/codex-host-validation.md) に記載しています。
