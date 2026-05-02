## フェイズ: {{action_label}} (day {{day_number}})

## 共通ルール
{{game_rules_block}}

{{persona_block}}

{{role_block}}

## 自分の状況 (非公開を含む)
{{state_block}}

## 場の状況 (Master ダイジェスト)
{{digest}}

## 行動候補席
{{candidates_str}}

上記すべてを踏まえ、夜の行動対象を決めてください。**スキップ禁止**: 必ず候補席の中から1人を選ぶ (`target_seat` に整数、null 不可)。情報が薄くても、相対的に最も対象として価値がある 1 人を選ぶこと (占い: 情報を取りたい灰、人狼: 噛み価値の高い位置、騎士: 守るべき情報役/重要位置)。「捨て護衛」のような戦術選択でも null ではなく合法候補から 1 人を選ぶ。

JSON は {"target_seat": <候補席番号>, "reason": "<短い理由>"} の形で返す。
