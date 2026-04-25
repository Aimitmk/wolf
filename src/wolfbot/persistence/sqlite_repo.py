"""aiosqlite-backed repository. All DB access goes through here.

One long-lived connection per repo instance. Writes serialize naturally through aiosqlite.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any

import aiosqlite

from wolfbot.domain.enums import VILLAGE_SIZE, DeathCause, Phase, Role, SubmissionType
from wolfbot.domain.errors import ActiveGameExistsError
from wolfbot.domain.models import (
    Game,
    LogEntry,
    NightAction,
    PendingDecision,
    PendingSubmission,
    Player,
    PlayerUpdate,
    Seat,
    Transition,
    Vote,
)

log = logging.getLogger(__name__)


class JoinLobbyResult(StrEnum):
    """Outcome of `SqliteRepo.join_lobby`. Surfaced to the slash-command user."""

    ACCEPTED = "accepted"
    STALE_PHASE = "stale_phase"
    ALREADY_JOINED = "already_joined"
    LOBBY_FULL = "lobby_full"
    NO_FREE_SEAT = "no_free_seat"


class LeaveLobbyResult(StrEnum):
    """Outcome of `SqliteRepo.leave_lobby`. Surfaced to the slash-command user."""

    ACCEPTED = "accepted"
    STALE_PHASE = "stale_phase"
    NOT_JOINED = "not_joined"


def _row_to_game(row: aiosqlite.Row) -> Game:
    return Game(
        id=row["id"],
        guild_id=row["guild_id"],
        host_user_id=row["host_user_id"],
        phase=Phase(row["phase"]),
        day_number=row["day_number"],
        deadline_epoch=row["deadline_epoch"],
        main_text_channel_id=row["main_text_channel_id"],
        main_vc_channel_id=row["main_vc_channel_id"],
        heaven_channel_id=row["heaven_channel_id"],
        wolves_channel_id=row["wolves_channel_id"],
        created_at=row["created_at"],
        ended_at=row["ended_at"],
        force_skip_pending=bool(row["force_skip_pending"]),
    )


def _row_to_seat(row: aiosqlite.Row) -> Seat:
    return Seat(
        seat_no=row["seat_no"],
        display_name=row["display_name"],
        discord_user_id=row["discord_user_id"],
        is_llm=bool(row["is_llm"]),
        persona_key=row["persona_key"],
    )


def _row_to_player(row: aiosqlite.Row) -> Player:
    return Player(
        seat_no=row["seat_no"],
        role=Role(row["role"]) if row["role"] else None,
        alive=bool(row["alive"]),
        death_cause=DeathCause(row["death_cause"]) if row["death_cause"] else None,
        death_day=row["death_day"],
        dm_channel_id=row["dm_channel_id"],
    )


def _row_to_vote(row: aiosqlite.Row) -> Vote:
    return Vote(
        game_id=row["game_id"],
        day=row["day"],
        round=row["round"],
        voter_seat=row["voter_seat"],
        target_seat=row["target_seat"],
        submitted_at=row["submitted_at"],
    )


def _row_to_night_action(row: aiosqlite.Row) -> NightAction:
    return NightAction(
        game_id=row["game_id"],
        day=row["day"],
        actor_seat=row["actor_seat"],
        kind=SubmissionType(row["kind"]),
        target_seat=row["target_seat"],
        submitted_at=row["submitted_at"],
    )


def _row_to_pending(row: aiosqlite.Row) -> PendingDecision:
    missing = json.loads(row["missing_seats_json"])
    submissions_raw = row["submissions_json"]
    submissions: tuple[PendingSubmission, ...] = ()
    if submissions_raw:
        parsed = json.loads(submissions_raw)
        submissions = tuple(
            PendingSubmission(
                submission_type=SubmissionType(item["submission_type"]),
                missing_seats=tuple(item["missing_seats"]),
                # unresolved_seats was added post-M5; old rows lack the key.
                unresolved_seats=tuple(item.get("unresolved_seats", ())),
            )
            for item in parsed
        )
    return PendingDecision(
        game_id=row["game_id"],
        phase=Phase(row["phase"]),
        day=row["day"],
        required_submission=SubmissionType(row["required_submission"]),
        missing_seats=tuple(missing),
        submissions=submissions,
        created_at=row["created_at"],
    )


def _submissions_json(pending: PendingDecision) -> str:
    """Serialize submissions to JSON, synthesizing from primary when empty."""
    items = pending.effective_submissions()
    return json.dumps(
        [
            {
                "submission_type": s.submission_type.value,
                "missing_seats": list(s.missing_seats),
                "unresolved_seats": list(s.unresolved_seats),
            }
            for s in items
        ]
    )


class SqliteRepo:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._conn is not None:
            return
        # isolation_level=None puts sqlite3 in autocommit mode so we can drive
        # BEGIN/COMMIT/ROLLBACK explicitly from `_tx()`. Without this, sqlite3 opens
        # an implicit transaction on the first DML and our explicit BEGIN fails with
        # "cannot start a transaction within a transaction".
        self._conn = await aiosqlite.connect(self._db_path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteRepo.connect() must be called first")
        return self._conn

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize concurrent mutations with a single transaction boundary."""
        async with self._lock:
            await self._db.execute("BEGIN")
            try:
                yield self._db
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise

    # ------------------------------------------------------------------ games
    async def create_game(self, game: Game) -> None:
        try:
            async with self._tx() as db:
                await db.execute(
                    """
                    INSERT INTO games (id, guild_id, host_user_id, phase, day_number, deadline_epoch,
                                       main_text_channel_id, main_vc_channel_id,
                                       heaven_channel_id, wolves_channel_id,
                                       created_at, ended_at, force_skip_pending)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        game.id,
                        game.guild_id,
                        game.host_user_id,
                        game.phase.value,
                        game.day_number,
                        game.deadline_epoch,
                        game.main_text_channel_id,
                        game.main_vc_channel_id,
                        game.heaven_channel_id,
                        game.wolves_channel_id,
                        game.created_at,
                        game.ended_at,
                        int(game.force_skip_pending),
                    ),
                )
        except aiosqlite.IntegrityError as e:
            # SQLite surfaces the partial unique index violation as "UNIQUE constraint
            # failed: games.guild_id" (not the index name). Games.id is a UUID that
            # effectively can't collide, so any uniqueness failure here is the
            # active-game constraint.
            if "games.guild_id" in str(e):
                raise ActiveGameExistsError(game.guild_id) from e
            raise

    async def update_game_channels(
        self, game_id: str, heaven_channel_id: str, wolves_channel_id: str
    ) -> None:
        async with self._tx() as db:
            await db.execute(
                "UPDATE games SET heaven_channel_id=?, wolves_channel_id=? WHERE id=?",
                (heaven_channel_id, wolves_channel_id, game_id),
            )

    async def load_game(self, game_id: str) -> Game | None:
        async with self._db.execute("SELECT * FROM games WHERE id=?", (game_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_game(row) if row else None

    async def load_active_games(self) -> list[Game]:
        async with self._db.execute("SELECT * FROM games WHERE ended_at IS NULL") as cur:
            rows = await cur.fetchall()
        return [_row_to_game(r) for r in rows]

    async def load_active_game_for_guild(self, guild_id: str) -> Game | None:
        async with self._db.execute(
            "SELECT * FROM games WHERE guild_id=? AND ended_at IS NULL LIMIT 1",
            (guild_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_game(row) if row else None

    async def load_private_channel_ids_for_guild(self, guild_id: str) -> set[str]:
        """Return every heaven/wolves channel id ever recorded for this guild.

        Used by /wolf create to decide whether a same-named existing channel is
        a stale bot channel (safe to purge) or an unrelated channel (must not
        touch). Does NOT filter by ended_at — if a prior on_game_end failed to
        delete, the id is still only discoverable via the games table.
        """
        async with self._db.execute(
            """
            SELECT heaven_channel_id AS ch FROM games
             WHERE guild_id=? AND heaven_channel_id IS NOT NULL
            UNION
            SELECT wolves_channel_id AS ch FROM games
             WHERE guild_id=? AND wolves_channel_id IS NOT NULL
            """,
            (guild_id, guild_id),
        ) as cur:
            rows = await cur.fetchall()
        return {str(row["ch"]) for row in rows}

    async def end_game(self, game_id: str, ended_at_epoch: int) -> None:
        async with self._tx() as db:
            await db.execute(
                "UPDATE games SET ended_at=?, phase=?, deadline_epoch=NULL WHERE id=?",
                (ended_at_epoch, Phase.GAME_OVER.value, game_id),
            )

    async def set_deadline(self, game_id: str, deadline_epoch: int | None) -> None:
        async with self._tx() as db:
            await db.execute(
                "UPDATE games SET deadline_epoch=? WHERE id=?",
                (deadline_epoch, game_id),
            )

    # ------------------------------------------------------------------ seats
    async def insert_seat(self, game_id: str, seat: Seat) -> None:
        async with self._tx() as db:
            await db.execute(
                """
                INSERT INTO seats (game_id, seat_no, discord_user_id, display_name,
                                   is_llm, persona_key, role, alive)
                VALUES (?, ?, ?, ?, ?, ?, NULL, 1)
                """,
                (
                    game_id,
                    seat.seat_no,
                    seat.discord_user_id,
                    seat.display_name,
                    int(seat.is_llm),
                    seat.persona_key,
                ),
            )

    async def delete_seat(self, game_id: str, seat_no: int) -> None:
        async with self._tx() as db:
            await db.execute("DELETE FROM seats WHERE game_id=? AND seat_no=?", (game_id, seat_no))

    async def join_lobby(
        self,
        game_id: str,
        *,
        discord_user_id: str,
        display_name: str,
        expected_phase: Phase = Phase.LOBBY,
    ) -> tuple[JoinLobbyResult, int | None]:
        """Atomically seat a human in the lobby.

        One transaction: (a) phase guard, (b) duplicate-join check,
        (c) capacity check, (d) next-free seat_no selection, (e) INSERT.
        Returns (result, seat_no); seat_no is None unless result is ACCEPTED.
        Prevents stale `/wolf join` from corrupting a game that has already
        transitioned out of LOBBY.
        """
        async with self._tx() as db:
            async with db.execute("SELECT phase FROM games WHERE id=?", (game_id,)) as cur:
                game_row = await cur.fetchone()
            if game_row is None or game_row["phase"] != expected_phase.value:
                return (JoinLobbyResult.STALE_PHASE, None)

            async with db.execute(
                "SELECT seat_no FROM seats WHERE game_id=? AND discord_user_id=?",
                (game_id, discord_user_id),
            ) as cur:
                existing = await cur.fetchone()
            if existing is not None:
                return (JoinLobbyResult.ALREADY_JOINED, None)

            async with db.execute(
                "SELECT seat_no, is_llm FROM seats WHERE game_id=?", (game_id,)
            ) as cur:
                seat_rows = await cur.fetchall()
            used = {int(r["seat_no"]) for r in seat_rows}
            human_count = sum(1 for r in seat_rows if not bool(r["is_llm"]))
            if human_count >= 9:
                return (JoinLobbyResult.LOBBY_FULL, None)
            free_slots = [i for i in range(1, 10) if i not in used]
            if not free_slots:
                return (JoinLobbyResult.NO_FREE_SEAT, None)
            seat_no = free_slots[0]

            await db.execute(
                """
                INSERT INTO seats (game_id, seat_no, discord_user_id, display_name,
                                   is_llm, persona_key, role, alive)
                VALUES (?, ?, ?, ?, 0, NULL, NULL, 1)
                """,
                (game_id, seat_no, discord_user_id, display_name),
            )
            return (JoinLobbyResult.ACCEPTED, seat_no)

    async def leave_lobby(
        self,
        game_id: str,
        *,
        discord_user_id: str,
        expected_phase: Phase = Phase.LOBBY,
    ) -> LeaveLobbyResult:
        """Atomically remove a human seat in the lobby.

        One transaction: (a) phase guard, (b) locate the user's seat, (c) DELETE.
        Prevents stale `/wolf leave` from removing a seat after LOBBY → SETUP.
        """
        async with self._tx() as db:
            async with db.execute("SELECT phase FROM games WHERE id=?", (game_id,)) as cur:
                game_row = await cur.fetchone()
            if game_row is None or game_row["phase"] != expected_phase.value:
                return LeaveLobbyResult.STALE_PHASE

            async with db.execute(
                "SELECT seat_no FROM seats WHERE game_id=? AND discord_user_id=?",
                (game_id, discord_user_id),
            ) as cur:
                existing = await cur.fetchone()
            if existing is None:
                return LeaveLobbyResult.NOT_JOINED
            seat_no = int(existing["seat_no"])

            await db.execute(
                "DELETE FROM seats WHERE game_id=? AND seat_no=?",
                (game_id, seat_no),
            )
            return LeaveLobbyResult.ACCEPTED

    async def load_seats(self, game_id: str) -> list[Seat]:
        async with self._db.execute(
            "SELECT * FROM seats WHERE game_id=? ORDER BY seat_no", (game_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_seat(r) for r in rows]

    async def load_players(self, game_id: str) -> list[Player]:
        async with self._db.execute(
            "SELECT * FROM seats WHERE game_id=? ORDER BY seat_no", (game_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_player(r) for r in rows]

    async def set_player_dm_channel(self, game_id: str, seat_no: int, dm_channel_id: str) -> None:
        async with self._tx() as db:
            await db.execute(
                "UPDATE seats SET dm_channel_id=? WHERE game_id=? AND seat_no=?",
                (dm_channel_id, game_id, seat_no),
            )

    async def set_player_role(self, game_id: str, seat_no: int, role: Role) -> None:
        async with self._tx() as db:
            await db.execute(
                "UPDATE seats SET role=? WHERE game_id=? AND seat_no=?",
                (role.value, game_id, seat_no),
            )

    async def seat_of_user(self, game_id: str, discord_user_id: str) -> int | None:
        async with self._db.execute(
            "SELECT seat_no FROM seats WHERE game_id=? AND discord_user_id=?",
            (game_id, discord_user_id),
        ) as cur:
            row = await cur.fetchone()
        return int(row["seat_no"]) if row else None

    # ------------------------------------------------------------------ votes
    async def insert_vote(self, vote: Vote) -> None:
        async with self._tx() as db:
            await db.execute(
                """
                INSERT INTO votes (game_id, day, round, voter_seat, target_seat, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id, day, round, voter_seat) DO UPDATE SET
                    target_seat=excluded.target_seat,
                    submitted_at=excluded.submitted_at
                """,
                (
                    vote.game_id,
                    vote.day,
                    vote.round,
                    vote.voter_seat,
                    vote.target_seat,
                    vote.submitted_at,
                ),
            )

    async def load_votes(self, game_id: str, day: int, round_: int) -> list[Vote]:
        async with self._db.execute(
            "SELECT * FROM votes WHERE game_id=? AND day=? AND round=?",
            (game_id, day, round_),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_vote(r) for r in rows]

    # ---------------------------------------------------------- night_actions
    async def insert_night_action(self, action: NightAction) -> None:
        async with self._tx() as db:
            await db.execute(
                """
                INSERT INTO night_actions (game_id, day, actor_seat, kind, target_seat, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id, day, actor_seat, kind) DO UPDATE SET
                    target_seat=excluded.target_seat,
                    submitted_at=excluded.submitted_at
                """,
                (
                    action.game_id,
                    action.day,
                    action.actor_seat,
                    action.kind.value,
                    action.target_seat,
                    action.submitted_at,
                ),
            )

    async def load_night_actions(self, game_id: str, day: int) -> list[NightAction]:
        async with self._db.execute(
            "SELECT * FROM night_actions WHERE game_id=? AND day=?",
            (game_id, day),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_night_action(r) for r in rows]

    # ---------------------------------------------------------- previous_guard
    async def upsert_previous_guard(
        self,
        game_id: str,
        knight_seat: int,
        last_guard_seat: int | None,
        last_guard_day: int,
    ) -> None:
        async with self._tx() as db:
            await db.execute(
                """
                INSERT INTO previous_guard (game_id, knight_seat, last_guard_seat, last_guard_day)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(game_id) DO UPDATE SET
                    knight_seat=excluded.knight_seat,
                    last_guard_seat=excluded.last_guard_seat,
                    last_guard_day=excluded.last_guard_day
                """,
                (game_id, knight_seat, last_guard_seat, last_guard_day),
            )

    async def load_previous_guard(self, game_id: str) -> tuple[int, int | None, int | None] | None:
        async with self._db.execute(
            "SELECT knight_seat, last_guard_seat, last_guard_day FROM previous_guard WHERE game_id=?",
            (game_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return (row["knight_seat"], row["last_guard_seat"], row["last_guard_day"])

    # ------------------------------------------------------------------- logs
    async def _insert_log_public(self, db: aiosqlite.Connection, entry: LogEntry) -> None:
        await db.execute(
            """
            INSERT INTO logs_public (game_id, day, phase, kind, actor_seat, text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.game_id,
                entry.day,
                entry.phase.value,
                entry.kind,
                entry.actor_seat,
                entry.text,
                entry.created_at,
            ),
        )

    async def _insert_log_private(self, db: aiosqlite.Connection, entry: LogEntry) -> None:
        await db.execute(
            """
            INSERT INTO logs_private (game_id, day, phase, kind, actor_seat, audience_seat,
                                      text, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.game_id,
                entry.day,
                entry.phase.value,
                entry.kind,
                entry.actor_seat,
                entry.audience_seat,
                entry.text,
                entry.payload_json,
                entry.created_at,
            ),
        )

    async def insert_log_public(self, entry: LogEntry) -> None:
        async with self._tx() as db:
            await self._insert_log_public(db, entry)

    async def insert_log_private(self, entry: LogEntry) -> None:
        async with self._tx() as db:
            await self._insert_log_private(db, entry)

    async def load_public_logs(self, game_id: str, limit: int = 40) -> list[dict[str, Any]]:
        async with self._db.execute(
            """
            SELECT day, phase, kind, actor_seat, text, created_at
              FROM logs_public
             WHERE game_id=?
             ORDER BY id DESC
             LIMIT ?
            """,
            (game_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in reversed(list(rows))]

    async def load_private_logs_for_audience(
        self, game_id: str, audience_seat: int, limit: int = 40
    ) -> list[dict[str, Any]]:
        async with self._db.execute(
            """
            SELECT day, phase, kind, actor_seat, audience_seat, text, payload_json, created_at
              FROM logs_private
             WHERE game_id=? AND (audience_seat=? OR audience_seat IS NULL)
             ORDER BY id DESC
             LIMIT ?
            """,
            (game_id, audience_seat, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in reversed(list(rows))]

    # ----------------------------------------------------------- pending_decisions
    async def upsert_pending_decision(self, decision: PendingDecision) -> None:
        async with self._tx() as db:
            await db.execute(
                """
                INSERT INTO pending_decisions
                    (game_id, phase, day, required_submission, missing_seats_json,
                     submissions_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id) DO UPDATE SET
                    phase=excluded.phase,
                    day=excluded.day,
                    required_submission=excluded.required_submission,
                    missing_seats_json=excluded.missing_seats_json,
                    submissions_json=excluded.submissions_json,
                    created_at=excluded.created_at
                """,
                (
                    decision.game_id,
                    decision.phase.value,
                    decision.day,
                    decision.required_submission.value,
                    json.dumps(list(decision.missing_seats)),
                    _submissions_json(decision),
                    decision.created_at,
                ),
            )

    async def clear_pending_decision(self, game_id: str) -> None:
        async with self._tx() as db:
            await db.execute("DELETE FROM pending_decisions WHERE game_id=?", (game_id,))

    async def load_pending_decision(self, game_id: str) -> PendingDecision | None:
        async with self._db.execute(
            "SELECT * FROM pending_decisions WHERE game_id=?", (game_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_pending(row) if row else None

    # ------------------------------------------------------- persona_assignments
    async def insert_persona_assignment(self, game_id: str, seat_no: int, persona_key: str) -> None:
        async with self._tx() as db:
            await db.execute(
                """
                INSERT INTO persona_assignments (game_id, seat_no, persona_key)
                VALUES (?, ?, ?)
                """,
                (game_id, seat_no, persona_key),
            )

    async def load_persona_keys(self, game_id: str) -> dict[int, str]:
        async with self._db.execute(
            "SELECT seat_no, persona_key FROM persona_assignments WHERE game_id=?",
            (game_id,),
        ) as cur:
            rows = await cur.fetchall()
        return {r["seat_no"]: r["persona_key"] for r in rows}

    # --------------------------------------------------------- llm_speech_counts
    async def increment_llm_normal_speech(
        self, game_id: str, day: int, seat_no: int, now_epoch: int
    ) -> None:
        async with self._tx() as db:
            await db.execute(
                """
                INSERT INTO llm_speech_counts (game_id, day, seat_no, normal_count, last_spoke_epoch)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(game_id, day, seat_no) DO UPDATE SET
                    normal_count = normal_count + 1,
                    last_spoke_epoch = excluded.last_spoke_epoch
                """,
                (game_id, day, seat_no, now_epoch),
            )

    async def mark_llm_vote_intent(self, game_id: str, day: int, seat_no: int) -> None:
        async with self._tx() as db:
            await db.execute(
                """
                INSERT INTO llm_speech_counts (game_id, day, seat_no, vote_intent_done)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(game_id, day, seat_no) DO UPDATE SET vote_intent_done = 1
                """,
                (game_id, day, seat_no),
            )

    async def load_llm_speech(
        self, game_id: str, day: int, seat_no: int
    ) -> tuple[int, bool, int | None]:
        """Return (normal_count, vote_intent_done, last_spoke_epoch)."""
        async with self._db.execute(
            """
            SELECT normal_count, vote_intent_done, last_spoke_epoch
              FROM llm_speech_counts
             WHERE game_id=? AND day=? AND seat_no=?
            """,
            (game_id, day, seat_no),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return (0, False, None)
        return (
            int(row["normal_count"]),
            bool(row["vote_intent_done"]),
            row["last_spoke_epoch"],
        )

    async def increment_llm_discussion_round(self, game_id: str, day: int, seat_no: int) -> None:
        """Bump `discussion_rounds_done`, capped at 2 in SQL.

        Called by the DAY_DISCUSSION LLM round runner in `finally`, so progress
        advances regardless of decider success / skip / exception. The CAP via
        `CASE WHEN` is defence-in-depth — the caller already skips seats whose
        rounds_done is at the target round, so this should never push past 2.
        """
        async with self._tx() as db:
            await db.execute(
                """
                INSERT INTO llm_speech_counts
                    (game_id, day, seat_no, discussion_rounds_done)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(game_id, day, seat_no) DO UPDATE SET
                    discussion_rounds_done =
                        CASE WHEN discussion_rounds_done < 2
                             THEN discussion_rounds_done + 1
                             ELSE discussion_rounds_done END
                """,
                (game_id, day, seat_no),
            )

    async def mark_llm_runoff_speech_done(self, game_id: str, day: int, seat_no: int) -> None:
        """Set `runoff_speech_done = 1` (idempotent UPSERT)."""
        async with self._tx() as db:
            await db.execute(
                """
                INSERT INTO llm_speech_counts (game_id, day, seat_no, runoff_speech_done)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(game_id, day, seat_no) DO UPDATE SET
                    runoff_speech_done = 1
                """,
                (game_id, day, seat_no),
            )

    async def load_llm_speech_progress(
        self, game_id: str, day: int, seat_no: int
    ) -> tuple[int, bool, int | None, int, bool]:
        """Return all 5 progress fields for the given seat/day.

        Tuple: `(normal_count, vote_intent_done, last_spoke_epoch,
        discussion_rounds_done, runoff_speech_done)`. Default for missing row:
        `(0, False, None, 0, False)`. Used by `_plan_next` to decide whether
        DAY_DISCUSSION / DAY_RUNOFF_SPEECH can advance.
        """
        async with self._db.execute(
            """
            SELECT normal_count, vote_intent_done, last_spoke_epoch,
                   discussion_rounds_done, runoff_speech_done
              FROM llm_speech_counts
             WHERE game_id=? AND day=? AND seat_no=?
            """,
            (game_id, day, seat_no),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return (0, False, None, 0, False)
        return (
            int(row["normal_count"]),
            bool(row["vote_intent_done"]),
            row["last_spoke_epoch"],
            int(row["discussion_rounds_done"]),
            bool(row["runoff_speech_done"]),
        )

    # ------------------------------------------------------------- apply transition
    async def apply_transition(
        self,
        game_id: str,
        transition: Transition,
        *,
        expected_phase: Phase,
    ) -> bool:
        """Apply a Transition in a single SQLite transaction with optimistic lock.

        Returns True on commit, False if expected_phase did not match (lost race — a
        concurrent advance already moved us on; caller should reload and retry).
        """
        try:
            async with self._tx() as db:
                cur = await db.execute(
                    "UPDATE games SET phase=?, day_number=?, deadline_epoch=? "
                    "WHERE id=? AND phase=?",
                    (
                        transition.next_phase.value,
                        transition.next_day,
                        transition.new_deadline_epoch,
                        game_id,
                        expected_phase.value,
                    ),
                )
                if cur.rowcount != 1:
                    raise _OptimisticLockMiss()

                if transition.clear_force_skip:
                    await db.execute("UPDATE games SET force_skip_pending=0 WHERE id=?", (game_id,))
                if transition.set_force_skip:
                    await db.execute("UPDATE games SET force_skip_pending=1 WHERE id=?", (game_id,))

                for upd in transition.player_updates:
                    await _apply_player_update(db, game_id, upd)

                for entry in transition.public_logs:
                    await self._insert_log_public(db, entry)
                for entry in transition.private_logs:
                    await self._insert_log_private(db, entry)

                if transition.pending is not None:
                    await db.execute(
                        """
                        INSERT INTO pending_decisions
                            (game_id, phase, day, required_submission, missing_seats_json,
                             submissions_json, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(game_id) DO UPDATE SET
                            phase=excluded.phase,
                            day=excluded.day,
                            required_submission=excluded.required_submission,
                            missing_seats_json=excluded.missing_seats_json,
                            submissions_json=excluded.submissions_json,
                            created_at=excluded.created_at
                        """,
                        (
                            transition.pending.game_id,
                            transition.pending.phase.value,
                            transition.pending.day,
                            transition.pending.required_submission.value,
                            json.dumps(list(transition.pending.missing_seats)),
                            _submissions_json(transition.pending),
                            transition.pending.created_at,
                        ),
                    )
                elif not transition.requires_host_decision:
                    await db.execute("DELETE FROM pending_decisions WHERE game_id=?", (game_id,))

                if transition.record_guard is not None:
                    knight_seat, target_seat = transition.record_guard
                    await db.execute(
                        """
                        INSERT INTO previous_guard (game_id, knight_seat, last_guard_seat, last_guard_day)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(game_id) DO UPDATE SET
                            knight_seat=excluded.knight_seat,
                            last_guard_seat=excluded.last_guard_seat,
                            last_guard_day=excluded.last_guard_day
                        """,
                        (game_id, knight_seat, target_seat, transition.next_day),
                    )
        except _OptimisticLockMiss:
            return False
        return True

    # -------------------------------------------------- claim lobby start
    async def claim_start_and_backfill(
        self,
        game_id: str,
        *,
        expected_phase: Phase,
        llm_seats: Sequence[tuple[str, str]],
    ) -> bool:
        """Atomically claim LOBBY→SETUP and insert LLM seats in one transaction.

        `llm_seats` is a sequence of (display_name, persona_key) to insert into
        the next free seat numbers 1..9. Returns False (rollback, no writes) if
        expected_phase no longer matches. Any insert failure also rolls back.
        """
        try:
            async with self._tx() as db:
                cur = await db.execute(
                    "UPDATE games SET phase=?, day_number=?, deadline_epoch=? "
                    "WHERE id=? AND phase=?",
                    (Phase.SETUP.value, 0, None, game_id, expected_phase.value),
                )
                if cur.rowcount != 1:
                    raise _OptimisticLockMiss()

                async with db.execute(
                    "SELECT seat_no FROM seats WHERE game_id=?", (game_id,)
                ) as scur:
                    rows = await scur.fetchall()
                used = {int(r["seat_no"]) for r in rows}
                # The caller precomputes llm_seats against a seat snapshot taken
                # outside this tx. If join_lobby/leave_lobby won the race in
                # between, the snapshot is stale and the final roster would end
                # up != VILLAGE_SIZE. Validate in-tx so the phase UPDATE rolls
                # back with the seat layout untouched.
                if len(used) + len(llm_seats) != VILLAGE_SIZE:
                    log.warning(
                        "backfill seat count mismatch game=%s used=%d llm=%d — rolling back",
                        game_id,
                        len(used),
                        len(llm_seats),
                    )
                    raise _BackfillSeatCountMismatch()
                free_slots = [i for i in range(1, 10) if i not in used]
                if len(llm_seats) > len(free_slots):
                    raise ValueError(
                        f"not enough free seats: need {len(llm_seats)}, have {len(free_slots)}"
                    )

                for (display_name, persona_key), seat_no in zip(
                    llm_seats, free_slots, strict=False
                ):
                    await db.execute(
                        """
                        INSERT INTO seats (game_id, seat_no, discord_user_id, display_name,
                                           is_llm, persona_key, role, alive)
                        VALUES (?, ?, NULL, ?, 1, ?, NULL, 1)
                        """,
                        (game_id, seat_no, display_name, persona_key),
                    )
                    await db.execute(
                        """
                        INSERT INTO persona_assignments (game_id, seat_no, persona_key)
                        VALUES (?, ?, ?)
                        """,
                        (game_id, seat_no, persona_key),
                    )
        except _LobbyClaimAborted:
            return False
        return True


class _LobbyClaimAborted(Exception):
    """Base for conditions that roll back a LOBBY-claim tx to False.

    `async with self._tx()` catches any exception, rolls back, and re-raises;
    the outer boundary narrows to this base to convert "expected failures" into
    a False return while still letting genuine errors propagate.
    """


class _OptimisticLockMiss(_LobbyClaimAborted):
    """Raised inside _tx when the expected_phase no longer matches.

    SqliteRepo.apply_transition catches at the outer boundary and returns False.
    """


class _BackfillSeatCountMismatch(_LobbyClaimAborted):
    """Raised when the seat roster shifted between /wolf start preflight and
    the atomic LOBBY→SETUP claim. Triggers a full tx rollback so the lobby
    remains untouched and the host can retry /wolf start with a fresh count.
    """


async def _apply_player_update(db: aiosqlite.Connection, game_id: str, upd: PlayerUpdate) -> None:
    sets: list[str] = []
    params: list[Any] = []
    if upd.role is not None:
        sets.append("role=?")
        params.append(upd.role.value)
    if upd.alive is not None:
        sets.append("alive=?")
        params.append(int(upd.alive))
    if upd.death_cause is not None:
        sets.append("death_cause=?")
        params.append(upd.death_cause.value)
    if upd.death_day is not None:
        sets.append("death_day=?")
        params.append(upd.death_day)
    if not sets:
        return
    params.extend([game_id, upd.seat_no])
    await db.execute(
        f"UPDATE seats SET {', '.join(sets)} WHERE game_id=? AND seat_no=?",
        tuple(params),
    )
