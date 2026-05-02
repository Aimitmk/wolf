
---
出力形式 (json):
必ず次のキーを持つ JSON オブジェクトのみを返してください。前後にテキストや markdown コードフェンスを付けないでください。
- "text": string (発話本体、最大 300 文字)
- "intent": "speak" | "agree" | "disagree" | "question" | "accuse" | "defend" | "skip"
- "used_logic_ids": string の配列 (空配列でもよい)
- "co_declaration": "seer" | "medium" | "knight" | null
- "addressed_seat_nos": integer の配列 (向ける席番号たち。1人なら [3]、複数なら [2, 3]、誰宛でもない一般発言は [])
- "claimed_seer_result": object | null  (今回の発話で新しく占い結果を発表する場合のみ非 null。形式は {"target_seat": integer(1-9), "is_wolf": boolean}。本物でも偽でも同じ形式で出す。発表しないなら null)
- "claimed_medium_result": object | null  (今回の発話で新しく霊媒結果を発表する場合のみ非 null。形式は {"target_seat": integer(1-9), "is_wolf": boolean | null}。is_wolf=null は「昨日処刑なし」)

例:
{"text": "私もそこは引っかかってた。", "intent": "agree", "used_logic_ids": [], "co_declaration": null, "addressed_seat_nos": [], "claimed_seer_result": null, "claimed_medium_result": null}
{"text": "実は私、占い師なんだ。昨夜セツを占ったら人狼じゃなかったよ。", "intent": "speak", "used_logic_ids": [], "co_declaration": "seer", "addressed_seat_nos": [], "claimed_seer_result": {"target_seat": 6, "is_wolf": false}, "claimed_medium_result": null}
{"text": "霊媒結果を伝える。昨日処刑されたジョナスは人狼だった。", "intent": "speak", "used_logic_ids": [], "co_declaration": "medium", "addressed_seat_nos": [], "claimed_seer_result": null, "claimed_medium_result": {"target_seat": 2, "is_wolf": true}}
