# discord-integration Specification

## Purpose

Defines the Discord-side surface of the bot: the `/wolf` slash-command set hosted by `WolfCog`, the per-phase channel-permission reconciliation performed by `PermissionManager` across the main / wolves / heaven channel triplet, the DM-based `VoteView` and `NightActionView` interactions used for private submissions, and the lifecycle of the per-game wolves and heaven channels (created on game start, deleted on game end).

## Requirements

### Requirement: /wolf slash commands are registered to a single guild

The bot SHALL register the `/wolf` command set (`join`, `leave`, `start`, `abort`, `force-skip`, `extend`) inside a single Discord guild whose id is read from the `DISCORD_GUILD_ID` setting. Host-only commands (`start`, `abort`, `force-skip`, `extend`) MUST verify that the invoking user is the recorded game host before performing any state mutation.

#### Scenario: Non-host abort attempt is rejected
- **WHEN** a user who is not the host invokes `/wolf abort` on an active game
- **THEN** the bot replies ephemerally with a permission error and does not call `GameService.host_abort`

#### Scenario: Host abort tears down only on first call
- **WHEN** the host invokes `/wolf abort` while the game is still active
- **THEN** `GameService.host_abort` returns `True`, the cog detaches and stops the `GameEngine`, and posts the public `強制終了` message

#### Scenario: Host abort on already-ended game is a no-op
- **WHEN** the host invokes `/wolf abort` on a game whose `ended_at` is non-null
- **THEN** `GameService.host_abort` returns `False`, the cog replies ephemerally, and no public message is posted and no engine teardown runs

### Requirement: Lobby seat mutations use phase-guarded repo methods

`/wolf join` and `/wolf leave` SHALL invoke `SqliteRepo.join_lobby` / `SqliteRepo.leave_lobby` respectively, never raw `insert_seat` / `delete_seat`. These repo methods MUST be phase-guarded in a single transaction so that a stale `/wolf join` arriving after `LOBBY → SETUP` does not corrupt the seat list.

#### Scenario: Join after start is rejected atomically
- **WHEN** a user issues `/wolf join` between the moment `/wolf start` has flipped the phase to `SETUP` and the moment the bot has finished any post-start work
- **THEN** `SqliteRepo.join_lobby` observes `phase != LOBBY`, the transaction rolls back, and the user is shown an ephemeral error

### Requirement: Channel permissions are reconciled per phase and idempotent

`PermissionManager` SHALL maintain three classes of channel state — main text, wolves (private), and heaven (private) — and reconcile them whenever the game phase changes or `GameEngine` reattaches on restart. Living players see+send in the main channel; dead players see only. Living werewolves see the wolves channel always but send only during `NIGHT`. Dead players see+send in the heaven channel; living players cannot see it. The reconciliation MUST be idempotent: only diffs trigger Discord API calls.

#### Scenario: Wolves can send only during NIGHT
- **WHEN** the game enters `NIGHT` with two living werewolves
- **THEN** the wolves-channel overwrites for both wolves grant send permission, and outside of `NIGHT` (e.g. after entering `DAY_DISCUSSION`) those overwrites grant view-only

#### Scenario: Heaven hides from the living
- **WHEN** the game has at least one dead player
- **THEN** the heaven-channel overwrite for `@everyone` denies view, the bot's own overwrite allows view+send, and each dead player's overwrite allows view+send while no living player has any heaven overwrite

#### Scenario: Repeat reconciliation is a no-op
- **WHEN** `PermissionManager.apply` is invoked twice in succession with no underlying state change
- **THEN** the second invocation issues no Discord API calls because all overwrites already match the desired state

### Requirement: Wolves and heaven channels are deleted at game end

When a game enters `GAME_OVER` (via execution victory, attack victory, or `/wolf abort`), the bot SHALL delete the per-game wolves and heaven channels (`Game.wolves_channel_id` and `Game.heaven_channel_id`) rather than merely clearing their permission overwrites. This deletion is required to prevent cross-game channel leak.

#### Scenario: End-of-game cleanup deletes private channels
- **WHEN** a game reaches `GAME_OVER`
- **THEN** the bot deletes both the wolves channel referenced by `Game.wolves_channel_id` and the heaven channel referenced by `Game.heaven_channel_id`

### Requirement: Wolf-attack splits show asymmetric public information

While a wolf-attack split is unresolved during `NIGHT`, the main text channel SHALL announce only an aggregate `未確定: N件` count and MUST NOT reveal the per-target breakdown. The wolves private channel, by contrast, MAY display the real per-target tally so the wolves can coordinate. This asymmetry is intentional and MUST be preserved.

#### Scenario: Main channel hides the breakdown
- **WHEN** two wolves submit different attack targets and the split is still unresolved
- **THEN** the main-channel announcement reads `未確定: 2件` (or equivalent) and does not name the targets

#### Scenario: Wolves channel sees the split
- **WHEN** the same split is in progress
- **THEN** the wolves-channel message lists both targets and the count for each so the wolves can converge

### Requirement: DM views capture day at send time

`VoteView` and `NightActionView` sent to a player as a Discord DM SHALL embed the `Game.day_number` value as it was at the moment the DM was dispatched. When the player clicks a button on a DM after the game has advanced to a new day, `submit_vote` / `submit_night_action` MUST observe the captured `day` against the current `game.day_number` and reject the submission with `SubmitResult.STALE_PHASE` even if the current phase happens to coincidentally match.

#### Scenario: Yesterday's DM is rejected today
- **WHEN** a player clicks the vote button on a DM that was sent during day 1's `DAY_VOTE` after the game has advanced into day 2's `DAY_VOTE`
- **THEN** `submit_vote(day=1)` is rejected with `SubmitResult.STALE_PHASE` and no vote is recorded

### Requirement: DM resends cover both missing and unresolved seats

`resend_pending_dms` (used by `/wolf extend` and the recovery path) SHALL re-send DMs to the union of `PendingSubmission.missing_seats` (never submitted) and `PendingSubmission.unresolved_seats` (submitted but unresolved, e.g. wolf attack split). This guarantees that an extend after a split lockout reaches the not-yet-submitted seat too without requiring the host to use `/wolf force-skip`.

#### Scenario: Extend during attack split resends to both wolves
- **WHEN** during a wolf-attack split one wolf has submitted and one has not, and the host issues `/wolf extend`
- **THEN** the resend reaches both wolves, with the not-yet-submitted wolf seeing the full legal attack list

### Requirement: Night DMs separate "who to DM" from "the legal target pool"

`DiscordAdapter.send_night_action_dms(game, actors, alive_players, seats)` SHALL accept two separate player pools: `actors` (the recipients to DM, typically the still-pending subset) and `alive_players` (the full alive pool used to compute legal targets). Implementations MUST NOT collapse these into a single argument, because resends to a single not-yet-submitted wolf during a split still need the full legal attack list.

#### Scenario: Single-wolf resend still offers full target list
- **WHEN** `send_night_action_dms` is called with `actors = [seat_5]` and `alive_players` containing all living non-wolf seats
- **THEN** the DM sent to seat 5 lists every legal attack target derived from `alive_players`, not only seats present in `actors`

### Requirement: WAITING_HOST_DECISION requires explicit host intervention

When `RecoveryService` finds a game whose `deadline_epoch < now` on startup, it SHALL park the game in `WAITING_HOST_DECISION` instead of silently auto-resolving stale submissions. The host MUST then run either `/wolf force-skip` or `/wolf extend` to unblock the game.

#### Scenario: Past-deadline game on restart awaits host
- **WHEN** the bot restarts and finds an active game whose `deadline_epoch` already passed
- **THEN** the game is moved to `WAITING_HOST_DECISION` and remains there until the host runs `/wolf force-skip` or `/wolf extend`

#### Scenario: Per-game isolation in recovery
- **WHEN** one game's recovery raises an exception during reconciliation
- **THEN** the exception is logged for that game only and other active games still complete their recovery
