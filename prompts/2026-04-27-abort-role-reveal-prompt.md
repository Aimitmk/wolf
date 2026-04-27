# `wolfbot` 2026-04-27 `/wolf abort` 役職公開プロンプト

この文書は、このリポジトリの `wolfbot` を今回の確認事項に合わせて更新するための固定プロンプトです。下の `## Prompt` セクションを、そのまま別の LLM / coding agent に渡すことを想定しています。

今回の主眼は、bot の実行テストを効率化するために、ホストまたは管理者が `/wolf abort` でゲームを強制終了したとき、各プレイヤーの役職を main channel に公開することです。通常の勝利終了時には既に `ROLE_REVEAL` が投稿されているため、その表示形式と投稿経路を再利用します。

## Prompt

```md
あなたは、このリポジトリで直接コードを修正する Python エンジニアです。

目的:
- Discord 人狼 bot `wolfbot` の `/wolf abort` 成功時に、全プレイヤーの役職一覧を main channel に表示する。
- 用途は bot 実行テストの効率化。通常のゲーム中に死亡者の役職を途中公開しない既存ルールは維持する。
- 役職一覧の形式は、通常 GAME_OVER 時の `ROLE_REVEAL` と同じ `最終配役:` 形式に揃える。

今回必ず対応すること:
1. `/wolf abort` が成功したとき、main channel に `ROLE_REVEAL` として全席の役職一覧を投稿する。
2. 表示内容は全プレイヤーについて `席番号 / 表示名 / 役職名 / 生存または死亡` を含める。
3. 通常勝利終了時の `ROLE_REVEAL` 表示と同じ文面・並び順を使う。
4. 2 回目以降の abort や、既に終了済みのゲームでは役職一覧を再投稿しない。
5. `GameService.host_abort()` の既存契約を維持する。戻り値 `True` は実際に終了処理した場合、`False` は既に終了済みまたは game がない場合。
6. `/wolf abort` handler の既存挙動を維持する。成功時だけ engine を detach/stop し、公開の「ゲームを強制終了しました。」を返す。

最重要ルール:
- まず既存実装を読み、現在の abort 経路と通常終了時の role reveal 経路を把握してから修正すること。
- `domain/` は side-effect free のまま保つこと。
- Discord I/O は `GameService` の injected `DiscordAdapter` 経由に留めること。
- `DiscordService.abort()` が直接 DB から players を読んだり、役職文面を組み立てたりしないこと。
- `host_abort()` の二重実行防止を壊さないこと。
- 通常の昼死亡・夜死亡・朝アナウンスでは役職名を公開しない既存仕様を変更しないこと。
- DB schema、role distribution、勝利条件、phase transition、LLM prompt、LLM provider 実装は変更しないこと。
- 無関係な refactor をしないこと。
- 作業ツリーに既存の未コミット変更や未追跡ファイルがあっても、今回の目的に無関係なら戻さないこと。
- 実装後は必ず対象テストを走らせ、可能なら lint / 型チェックも走らせて結果を報告すること。

最初に必ず確認するファイル:
- `CLAUDE.md`
- `prompts/IMPLEMENTATION_PROMPT.md`
- `src/wolfbot/services/discord_service.py`
- `src/wolfbot/services/game_service.py`
- `src/wolfbot/domain/state_machine.py`
- `src/wolfbot/domain/enums.py`
- `src/wolfbot/persistence/sqlite_repo.py`
- `tests/fakes.py`
- `tests/test_game_service_advance.py`
- `tests/test_discord_service.py`

このリポジトリで確認済みの事実:
- `/wolf abort` は `src/wolfbot/services/discord_service.py` の `WolfCog.abort()` にある。
- `WolfCog.abort()` は guild / active game / host or admin を確認し、`self.gs.host_abort(game.id)` を呼ぶ。
- `host_abort()` が `True` を返したときだけ、`WolfCog.abort()` は `registry.detach(game.id)` と `engine.stop()` を実行し、`🛑 ゲームを強制終了しました。` を公開返信する。
- `host_abort()` は `src/wolfbot/services/game_service.py` にあり、現在は `load_game()`、`load_seats()`、`discord.on_game_end()`、`repo.end_game()`、`wake.wake()` を実行して `True` を返す。
- `CLAUDE.md` にも、`host_abort()` の戻り値 `False` は既に終了済みを意味し、呼び出し側は double-teardown を避ける必要があると書かれている。
- 通常勝利終了時の役職公開は `src/wolfbot/domain/state_machine.py` の `_role_reveal_log()` が作る。
- `_role_reveal_log()` は `ROLE_REVEAL` kind の public log を生成し、text は `最終配役:` から始まる。
- `_role_reveal_log()` は seat_no 昇順で、`- 席{p.seat_no} {name}: {role_ja} ({生存/死亡})` の行を出す。
- role 表示名は `src/wolfbot/domain/enums.py::ROLE_JA` にある。
- 通常 transition の public logs は `GameService.advance()` が `_safe_post_public(new_game, entry.text, entry.kind)` で投稿している。
- `tests/test_game_service_advance.py::test_game_over_posts_role_reveal_to_main_channel` は通常 GAME_OVER 時の `ROLE_REVEAL` 投稿を検証している。
- `tests/test_game_service_advance.py::test_host_abort_ends_game` と `test_host_abort_returns_false_when_already_ended` は abort の既存挙動を検証している。
- `tests/fakes.py::FakeDiscordAdapter.post_public()` は `post_public` call の `text` と `kind` を記録できる。

実装要求

## 1. 役職公開 text の重複を避ける

必要な仕様:
- abort 時の役職一覧は、通常勝利時の `ROLE_REVEAL` と同じ text を使う。
- 同じ組み立てロジックを `state_machine.py` 内で共有し、文面の二重管理を避ける。

実装方針:
- `src/wolfbot/domain/state_machine.py` に、副作用のない helper を追加する。
- 推奨名は `build_role_reveal_text()`。
- signature は以下を基本にする。

```py
def build_role_reveal_text(
    players_after: Sequence[Player],
    seats_by_no: Mapping[int, Seat],
) -> str:
    ...
```

- 内容は既存 `_role_reveal_log()` の text 組み立て部分をそのまま移す。
- `_role_reveal_log()` は `text=build_role_reveal_text(players_after, seats_by_no)` を使うようにする。
- `_role_reveal_log()` の kind、phase、now_epoch、public log 生成挙動は変えない。
- helper は純粋関数にし、Discord / DB / clock / logging に触れない。

期待する表示形式:

```text
最終配役:
- 席1 Alice: 人狼 (生存)
- 席2 Bob: 占い師 (死亡)
...
```

## 2. `host_abort()` で role reveal を投稿する

必要な仕様:
- `GameService.host_abort()` が成功する abort の中で、`ROLE_REVEAL` を main channel に投稿する。
- 投稿は `self._safe_post_public(game, text, "ROLE_REVEAL")` を使う。
- `discord.on_game_end()` と `repo.end_game()` の既存処理は維持する。
- `game is None` または `game.ended_at is not None` の場合は、既存どおり何も投稿せず `False` を返す。

実装方針:
- `src/wolfbot/services/game_service.py` で `build_role_reveal_text` を import する。
- `host_abort()` 内で seats に加えて players も読む。
- `seats_by_no = {s.seat_no: s for s in seats}` を作る。
- `discord.on_game_end()` を呼ぶ前に `ROLE_REVEAL` を投稿することを推奨する。
  - 理由: `on_game_end()` は権限や一時チャンネル削除を扱うため、main channel への投稿は teardown 前に済ませるほうが安全。
  - `_safe_post_public()` は投稿失敗を握りつぶして logging するため、role reveal 投稿失敗で abort 自体を止めない。
- その後、既存どおり `discord.on_game_end()`、`repo.end_game()`、`wake.wake()`、`return True` を実行する。

推奨コード形:

```py
players = await self.repo.load_players(game_id)
seats_by_no = {s.seat_no: s for s in seats}
await self._safe_post_public(
    game,
    build_role_reveal_text(players, seats_by_no),
    "ROLE_REVEAL",
)
try:
    await self.discord.on_game_end(game, seats)
except Exception:
    log.exception("on_game_end failed during abort %s", game_id)
await self.repo.end_game(game_id, ended_at_epoch=self.clock())
self.wake.wake(game_id)
return True
```

注意:
- `host_abort()` で `Transition` を作る必要はない。
- abort は勝利終了ではないため、`VICTORY` log は投稿しない。
- role reveal のために `game.phase` を `GAME_OVER` に先に変更する必要はない。DB 終了は既存の `repo.end_game()` に任せる。
- `players` に未割当 role がある場合は、既存 `_role_reveal_log()` と同じく `?` 表示でよい。

## 3. Discord command handler は最小変更に留める

必要な仕様:
- `WolfCog.abort()` の host/admin check、active game check、engine detach/stop、成功/失敗 message を維持する。
- role reveal の投稿責務は `GameService.host_abort()` に置く。

実装方針:
- `src/wolfbot/services/discord_service.py` は原則変更しない。
- 変更が必要になった場合も、public reply 文面や `ok` branch の意味を変えない。

## 4. テストを追加・更新する

必要なテスト:
- `tests/test_game_service_advance.py::test_host_abort_ends_game` を拡張するか、新規 test を追加する。
- abort 成功時に `FakeDiscordAdapter` が `post_public` を `kind="ROLE_REVEAL"` で受け取ることを確認する。
- reveal text が `最終配役:\n` で始まることを確認する。
- 席 1 から 9 までが各 1 回ずつ含まれることを確認する。
- text に既存 role label のいずれか、または全 role label が含まれることを確認する。
- text に `(生存)` または `(死亡)` が含まれることを確認する。
- `test_host_abort_returns_false_when_already_ended` を拡張し、2 回目の abort で `ROLE_REVEAL` が増えないことを確認する。

推奨 assertion 例:

```py
public_posts = [c for c in disc.calls if c.name == "post_public"]
role_reveals = [c for c in public_posts if c.kwargs["kind"] == "ROLE_REVEAL"]
assert len(role_reveals) == 1
reveal_text = role_reveals[0].kwargs["text"]
assert reveal_text.startswith("最終配役:\n")
for sn in range(1, 10):
    assert f"- 席{sn} " in reveal_text
assert "(生存)" in reveal_text or "(死亡)" in reveal_text
```

2 回目 abort の推奨 assertion:

```py
role_reveals_after_first = sum(
    1
    for c in disc.calls
    if c.name == "post_public" and c.kwargs["kind"] == "ROLE_REVEAL"
)
second = await service.host_abort(game.id)
assert not second
role_reveals_after_second = sum(
    1
    for c in disc.calls
    if c.name == "post_public" and c.kwargs["kind"] == "ROLE_REVEAL"
)
assert role_reveals_after_second == role_reveals_after_first
```

## 5. 実行する確認コマンド

最低限:

```sh
uv run pytest tests/test_game_service_advance.py
```

可能なら追加:

```sh
uv run pytest tests
uv run ruff check src tests
uv run mypy
```

完了報告に含めること:
- 変更したファイル。
- `/wolf abort` 成功時に `ROLE_REVEAL` が投稿されるようになったこと。
- 2 回目 abort では再投稿されないこと。
- 実行したテスト / lint / 型チェックの結果。
```
