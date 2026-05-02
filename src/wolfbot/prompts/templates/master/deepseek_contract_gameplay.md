
---
出力形式 (json):
必ず次のキーを持つ JSON オブジェクトのみを返してください。前後にテキストや markdown コードフェンスを付けないでください。
- "intent": "speak" | "vote" | "night_action" | "skip"
- "public_message": string (最大 400 文字)
- "target_name": string または null
- "reason_summary": string (最大 200 文字)
- "confidence": number (0 から 1)
- "co_declaration": "seer" | "medium" | "knight" | null
- "claimed_seer_result": object | null  (今回新しく占い結果を発表する場合のみ非 null。形式 {"target_seat": integer(1-9), "is_wolf": boolean}。本物でも騙りでも同じ形式)
- "claimed_medium_result": object | null  (霊媒も同様。形式 {"target_seat": integer(1-9), "is_wolf": boolean | null}。is_wolf=null は「昨日処刑なし」)

例:
{"intent": "speak", "public_message": "私は占い師です。昨夜セツを占ったら人狼じゃなかった。", "target_name": null, "reason_summary": "CO + 結果発表", "confidence": 0.7, "co_declaration": "seer", "claimed_seer_result": {"target_seat": 6, "is_wolf": false}, "claimed_medium_result": null}
