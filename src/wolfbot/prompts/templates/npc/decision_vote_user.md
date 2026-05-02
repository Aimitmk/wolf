## フェイズ: {{round_label}} (day {{day_number}})

{{persona_block}}

{{role_block}}

## 自分の状況 (非公開を含む)
{{state_block}}

## 場の状況 (Master ダイジェスト)
{{digest}}

## 投票候補席
{{candidates_str}}

上記すべてを踏まえ、この投票で誰に票を入れるかを決めてください。**棄権は禁止**: 必ず候補席の中から1人を選んで `target_seat` に入れる。情報が薄くても、最も怪しい/役割上吊りたい/相方ライン以外の中から相対的に最も票を入れたい1人を選ぶこと。JSON は {"target_seat": <候補席番号>, "reason": "<短い理由>"} の形 (`target_seat` は必ず整数、null 不可)。