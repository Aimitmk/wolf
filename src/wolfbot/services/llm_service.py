"""xAI (Grok) LLM service + LLMAdapter that drives LLM players.

- `LLMAction`: the Pydantic shape returned by the model (same schema sent via
  response_format).
- `LLMActionDecider` Protocol: low-level "given a persona+context, return an LLMAction".
- `XAILLMActionDecider`: calls xAI's OpenAI-compat endpoint with structured output.
- `FakeLLMActionDecider`: deterministic stub for tests/offline dry runs.
- `LLMAdapter`: implements the LLMAdapter Protocol consumed by game_service; iterates
  LLM seats and submits their actions via GameService.submit_*.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from wolfbot.domain.enums import Phase, Role, SubmissionType
from wolfbot.domain.models import Game, LogEntry, Player, Seat
from wolfbot.domain.rules import (
    legal_attack_targets,
    legal_divine_targets,
    legal_guard_targets,
)
from wolfbot.llm.personas import PERSONAS_BY_KEY
from wolfbot.llm.prompt_builder import (
    build_system_prompt,
    build_user_context,
    task_daytime_speech,
    task_night_action,
    task_vote,
    task_wolf_chat,
)

if TYPE_CHECKING:  # avoid importing heavy modules unless needed
    from openai import AsyncOpenAI

    from wolfbot.persistence.sqlite_repo import SqliteRepo
    from wolfbot.services.game_service import GameService


class MessagePoster(Protocol):
    """Subset of DiscordBotAdapter's public-post API; decoupled for testing."""

    async def post_public(self, game: Game, text: str, kind: str) -> None: ...
    async def post_wolves_chat(self, game: Game, text: str, kind: str) -> None: ...


log = logging.getLogger(__name__)


def seat_token(seat: Seat) -> str:
    """Stable LLM/UI identifier: `席{N} {display_name}`.

    Disambiguates candidates when display_name collides (duplicate humans, or a
    human named the same as a persona). Resolver parses the leading `席\\d+`.
    """
    return f"席{seat.seat_no} {seat.display_name}"


_SEAT_TOKEN_RE = re.compile(r"^\s*席(\d+)\b")


# ---------------------------------------------------------------- LLMAction
class LLMAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    intent: Literal["speak", "vote", "night_action", "skip"]
    public_message: str = ""
    target_name: str | None = None
    reason_summary: str = ""
    confidence: float = 0.5


RESPONSE_SCHEMA: dict[str, object] = {
    "name": "wolfbot_action",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "intent",
            "public_message",
            "target_name",
            "reason_summary",
            "confidence",
        ],
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["speak", "vote", "night_action", "skip"],
            },
            "public_message": {"type": "string", "maxLength": 400},
            "target_name": {"type": ["string", "null"]},
            "reason_summary": {"type": "string", "maxLength": 200},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
    },
}


# ---------------------------------------------------------- low-level deciders
class LLMActionDecider(Protocol):
    async def decide(self, system_prompt: str, user_context: str) -> LLMAction: ...


class XAILLMActionDecider:
    """Calls xAI's OpenAI-compatible chat completions endpoint."""

    def __init__(self, client: AsyncOpenAI, model: str, timeout: float = 30.0) -> None:
        self.client = client
        self.model = model
        self.timeout = timeout

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def decide(self, system_prompt: str, user_context: str) -> LLMAction:
        # xAI model IDs aren't in the openai SDK's Literal, hence the ignore.
        resp = await self.client.chat.completions.create(  # type: ignore[call-overload]
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_context},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": RESPONSE_SCHEMA,
            },
            timeout=self.timeout,
        )
        message = resp.choices[0].message
        content = message.content or "{}"
        return LLMAction.model_validate_json(content)


class FakeLLMActionDecider:
    """Deterministic stub. Returns ACTIONS[n] round-robin per call."""

    def __init__(
        self,
        scripted: Sequence[LLMAction] | None = None,
        default: LLMAction | None = None,
    ) -> None:
        self._scripted: list[LLMAction] = list(scripted or [])
        self._default = default or LLMAction(intent="skip", reason_summary="fake-default")
        self.call_count = 0

    async def decide(self, system_prompt: str, user_context: str) -> LLMAction:
        self.call_count += 1
        if self._scripted:
            return self._scripted.pop(0)
        return self._default


# ---------------------------------------------------------------- LLMAdapter
class LLMAdapter:
    """Iterates LLM players and submits decisions via GameService.

    The returned `target_name` must match one of the legal candidate display names; on
    mismatch we try one re-prompt (via the decider's own retry path), then fall back to
    a uniform-random legal target (logged as a warning). For nullable intent, we call
    `submit_*` with target_seat=None (abstain / no-action).
    """

    NORMAL_SPEECH_CAP = 3
    COOLDOWN_SECONDS = 20
    REACTION_KEYWORDS = ("CO", "占い", "霊媒", "白", "黒", "疑", "カミングアウト")

    def __init__(
        self,
        repo: SqliteRepo,
        decider: LLMActionDecider,
        message_poster: MessagePoster | None = None,
        game_service_ref: dict[str, GameService] | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        import time as _time

        self.repo = repo
        self.decider = decider
        self.message_poster = message_poster
        self._gs_slot: dict[str, GameService] = game_service_ref or {}
        self.rng = rng or random.Random()
        self._clock: Callable[[], int] = clock or (lambda: int(_time.time()))
        self._background_tasks: set[asyncio.Task[None]] = set()
        # Per-seat speech lock. Serializes read→check→write for daily cap and
        # cooldown so concurrent triggers (multiple reactive calls, or a
        # daystart racing with a reactive) can't both post past the cap.
        self._speech_locks: dict[tuple[str, int], asyncio.Lock] = {}

    def set_game_service(self, gs: GameService) -> None:
        self._gs_slot["gs"] = gs

    @property
    def gs(self) -> GameService:
        gs = self._gs_slot.get("gs")
        if gs is None:
            raise RuntimeError("LLMAdapter.set_game_service(...) was not called")
        return gs

    # ------------------------------------------------------ night actions
    async def submit_llm_night_actions(
        self,
        game: Game,
        players: Sequence[Player],
        seats: Sequence[Seat],
        restrict_to_seats: frozenset[int] | None = None,
        unresolved_seats: frozenset[int] = frozenset(),
    ) -> None:
        """Schedule each LLM's night action in a background task.

        Fire-and-forget — the caller (game_service.advance) doesn't wait, so a
        slow xAI call can't block the GameEngine's deadline watcher.

        When called from `resend_pending_dms` (on `/wolf extend`), pass
        `restrict_to_seats` = union of missing + unresolved LLM seats so we only
        re-dispatch the ones that still owe a submission. `unresolved_seats`
        are those in a wolf-attack split — the in-loop "already submitted?"
        guard otherwise skips them, which would keep the split locked.
        """
        seats_by_no = {s.seat_no: s for s in seats}
        llm_players = [
            p
            for p in players
            if p.alive and seats_by_no.get(p.seat_no) is not None and seats_by_no[p.seat_no].is_llm
        ]
        if restrict_to_seats is not None:
            llm_players = [p for p in llm_players if p.seat_no in restrict_to_seats]
        if not llm_players:
            return
        task = asyncio.create_task(
            self._run_night_actions(game, llm_players, players, seats, unresolved_seats),
            name=f"llm-night-{game.id}-d{game.day_number}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_night_actions(
        self,
        game: Game,
        llm_players: Sequence[Player],
        all_players: Sequence[Player],
        seats: Sequence[Seat],
        unresolved_seats: frozenset[int] = frozenset(),
    ) -> None:
        seats_by_no = {s.seat_no: s for s in seats}
        prev = await self.repo.load_previous_guard(game.id)
        prev_guard_seat = prev[1] if prev else None

        # Wolf-chat prelude: before submitting attacks, LLM wolves post a
        # coordination message to the wolves-only channel so both wolves
        # converge on one target. Runs serially — wolf B sees wolf A's just-
        # logged message.
        await self._run_wolf_chat(game, llm_players, all_players, seats)

        async def _one_night_action(player: Player) -> None:
            # Per-seat stale check, idempotency, and submission. Wrapped in
            # try/except so one seat's failure does not cancel peers launched
            # alongside us in the gather below.
            try:
                fresh = await self.repo.load_game(game.id)
                if (
                    fresh is None
                    or fresh.ended_at is not None
                    or fresh.phase is not Phase.NIGHT
                    or fresh.day_number != game.day_number
                ):
                    return
                seat = seats_by_no[player.seat_no]
                if player.role is None or seat.persona_key is None:
                    return
                kind, legal = self._role_to_kind(player, all_players, prev_guard_seat)
                if kind is None or not legal:
                    return
                # Idempotency: skip if this seat already submitted for this
                # kind — unless it is a split-wolf (unresolved_seats), which
                # must re-ask to break the lockout.
                existing = await self.repo.load_night_actions(game.id, day=game.day_number)
                already = any(a.actor_seat == player.seat_no and a.kind is kind for a in existing)
                if already and player.seat_no not in unresolved_seats:
                    return
                candidates = [seats_by_no[sn] for sn in legal if sn in seats_by_no]
                action = await self._ask(
                    game,
                    player,
                    seat,
                    all_players,
                    seats,
                    task_text=task_night_action(kind, [seat_token(c) for c in candidates]),
                )
                target_seat = self._resolve_target(action.target_name, candidates, allow_none=False)
                await self.gs.submit_night_action(
                    game.id, player.seat_no, kind, target_seat, game.day_number
                )
            except Exception:
                log.exception(
                    "llm night action failed for game %s seat %s", game.id, player.seat_no
                )

        # Dispatch per-seat in parallel. xAI round-trips no longer stack
        # serially across LLM seats.
        await asyncio.gather(*(_one_night_action(p) for p in llm_players))

    async def _run_wolf_chat(
        self,
        game: Game,
        llm_players: Sequence[Player],
        all_players: Sequence[Player],
        seats: Sequence[Seat],
    ) -> None:
        """LLM wolves post a coordination message to the wolves-only channel.

        Runs at the start of NIGHT, before attack submissions, so each wolf's
        `task_night_action` sees the partner's proposed target in its WOLF_CHAT
        private log and can converge instead of split. Serial (wolf B reads
        wolf A's just-logged message). No-ops when the village has <2 alive
        wolves total, no LLM wolves alive, or no wolves channel configured.
        """
        if game.wolves_channel_id is None or self.message_poster is None:
            return
        seats_by_no = {s.seat_no: s for s in seats}
        alive_wolves_all = [p for p in all_players if p.alive and p.role is Role.WEREWOLF]
        if len(alive_wolves_all) < 2:
            return
        llm_wolves = [p for p in llm_players if p.role is Role.WEREWOLF]
        if not llm_wolves:
            return

        for wolf in llm_wolves:
            fresh = await self.repo.load_game(game.id)
            if (
                fresh is None
                or fresh.ended_at is not None
                or fresh.phase is not Phase.NIGHT
                or fresh.day_number != game.day_number
            ):
                return
            seat = seats_by_no.get(wolf.seat_no)
            if seat is None or seat.persona_key is None:
                continue
            legal = legal_attack_targets(all_players, wolf.seat_no)
            candidates = [seats_by_no[sn] for sn in legal if sn in seats_by_no]
            if not candidates:
                continue
            partner_tokens = [
                seat_token(seats_by_no[p.seat_no])
                for p in alive_wolves_all
                if p.seat_no != wolf.seat_no and p.seat_no in seats_by_no
            ]
            try:
                action = await self._ask(
                    game,
                    wolf,
                    seat,
                    all_players,
                    seats,
                    task_text=task_wolf_chat(partner_tokens, [seat_token(c) for c in candidates]),
                )
            except Exception:
                log.exception("llm wolf-chat ask failed for game %s seat %s", game.id, wolf.seat_no)
                continue
            if action.intent != "speak":
                continue
            message = action.public_message.strip()
            if not message:
                continue
            try:
                await self.message_poster.post_wolves_chat(
                    fresh, f"**{seat.display_name}**: {message}", kind="WOLF_CHAT"
                )
            except Exception:
                log.exception("post_wolves_chat for LLM wolf failed seat %s", wolf.seat_no)
                continue
            now_ts = self._clock()
            for audience in alive_wolves_all:
                try:
                    await self.repo.insert_log_private(
                        LogEntry(
                            game_id=fresh.id,
                            day=fresh.day_number,
                            phase=fresh.phase,
                            kind="WOLF_CHAT",
                            actor_seat=wolf.seat_no,
                            audience_seat=audience.seat_no,
                            visibility="PRIVATE",
                            text=message,
                            created_at=now_ts,
                        )
                    )
                except Exception:
                    log.exception(
                        "WOLF_CHAT log insert failed game=%s actor=%s audience=%s",
                        fresh.id,
                        wolf.seat_no,
                        audience.seat_no,
                    )

    # ------------------------------------------------------ votes
    async def submit_llm_votes(
        self,
        game: Game,
        players: Sequence[Player],
        seats: Sequence[Seat],
        candidates: Sequence[int] | None,
        round_: int,
        restrict_to_seats: frozenset[int] | None = None,
    ) -> None:
        """Schedule each LLM's vote in a background task. Fire-and-forget.

        `restrict_to_seats` lets `resend_pending_dms` re-dispatch for only the
        LLM seats that still owe a vote after `/wolf extend`.
        """
        seats_by_no = {s.seat_no: s for s in seats}
        llm_voters = [
            p
            for p in players
            if p.alive and p.seat_no in seats_by_no and seats_by_no[p.seat_no].is_llm
        ]
        if restrict_to_seats is not None:
            llm_voters = [p for p in llm_voters if p.seat_no in restrict_to_seats]
        if not llm_voters:
            return
        task = asyncio.create_task(
            self._run_votes(game, llm_voters, players, seats, candidates, round_),
            name=f"llm-votes-{game.id}-d{game.day_number}-r{round_}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_votes(
        self,
        game: Game,
        llm_voters: Sequence[Player],
        all_players: Sequence[Player],
        seats: Sequence[Seat],
        candidates: Sequence[int] | None,
        round_: int,
    ) -> None:
        seats_by_no = {s.seat_no: s for s in seats}
        expected_phase = Phase.DAY_VOTE if round_ == 0 else Phase.DAY_RUNOFF

        async def _one_vote(voter: Player) -> None:
            # Per-seat stale check, idempotency, and submission. try/except
            # contains per-seat failures so peers scheduled alongside via
            # asyncio.gather are not cancelled.
            try:
                fresh = await self.repo.load_game(game.id)
                if (
                    fresh is None
                    or fresh.ended_at is not None
                    or fresh.phase is not expected_phase
                    or fresh.day_number != game.day_number
                ):
                    return
                seat = seats_by_no[voter.seat_no]
                if seat.persona_key is None:
                    return
                # Idempotency: skip if this seat already has a vote row for
                # this round. Also guards the resend path (restrict_to_seats)
                # from double-submitting if the original task lands first.
                existing_votes = await self.repo.load_votes(
                    game.id, day=game.day_number, round_=round_
                )
                if any(v.voter_seat == voter.seat_no for v in existing_votes):
                    return
                if candidates is None:
                    cand_seats = [
                        s
                        for s in seats
                        if s.seat_no != voter.seat_no
                        and any(p.seat_no == s.seat_no and p.alive for p in all_players)
                    ]
                else:
                    cand_seats = [
                        s
                        for s in seats
                        if s.seat_no in set(candidates) and s.seat_no != voter.seat_no
                    ]
                if not cand_seats:
                    await self.gs.submit_vote(
                        game.id,
                        voter.seat_no,
                        target_seat=None,
                        round_=round_,
                        day=game.day_number,
                    )
                    return
                action = await self._ask(
                    game,
                    voter,
                    seat,
                    all_players,
                    seats,
                    task_text=task_vote([seat_token(c) for c in cand_seats], runoff=round_ == 1),
                )
                target = self._resolve_target(
                    action.target_name, cand_seats, allow_none=action.intent == "skip"
                )
                await self.gs.submit_vote(
                    game.id,
                    voter.seat_no,
                    target_seat=target,
                    round_=round_,
                    day=game.day_number,
                )
            except Exception:
                log.exception(
                    "llm vote failed for game %s seat %s round %s",
                    game.id,
                    voter.seat_no,
                    round_,
                )

        # Dispatch per-seat in parallel.
        await asyncio.gather(*(_one_vote(v) for v in llm_voters))

    # --------------------------------------------------- daytime speeches
    async def submit_llm_daystart_speeches(
        self, game: Game, players: Sequence[Player], seats: Sequence[Seat]
    ) -> None:
        """Schedule each alive LLM's first speech of the day, sequentially with jitter.

        Fire-and-forget — the caller (game_service.advance) doesn't wait.
        """
        seats_by_no = {s.seat_no: s for s in seats}
        llm_players = [
            p
            for p in players
            if p.alive and p.seat_no in seats_by_no and seats_by_no[p.seat_no].is_llm
        ]
        if not llm_players:
            return
        task = asyncio.create_task(
            self._run_daystart(game, llm_players, seats),
            name=f"llm-daystart-{game.id}-{game.day_number}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_daystart(
        self,
        game: Game,
        llm_players: Sequence[Player],
        seats: Sequence[Seat],
    ) -> None:
        seats_by_no = {s.seat_no: s for s in seats}
        for llm in llm_players:
            # Re-read the live phase before each speech. The loop sleeps up to
            # 10s between LLM turns; by the time we get here the game may have
            # moved on (discussion ended, recovery swapped phases, etc.) and
            # posting a "daystart" line would be noise.
            fresh = await self.repo.load_game(game.id)
            if (
                fresh is None
                or fresh.phase is not Phase.DAY_DISCUSSION
                or fresh.day_number != game.day_number
            ):
                return
            seat = seats_by_no.get(llm.seat_no)
            if seat is None:
                continue
            await self._maybe_speak(fresh, llm, seat, seats)
            try:
                await asyncio.sleep(self.rng.uniform(3, 10))
            except asyncio.CancelledError:
                return

    async def maybe_react_to_message(
        self,
        game: Game,
        players: Sequence[Player],
        seats: Sequence[Seat],
        author_seat: int | None,
        text: str,
    ) -> None:
        """Called on every main-text human message. Alive LLMs may react."""
        if game.phase is not Phase.DAY_DISCUSSION:
            return
        seats_by_no = {s.seat_no: s for s in seats}
        for llm in players:
            if not llm.alive:
                continue
            seat = seats_by_no.get(llm.seat_no)
            if seat is None or not seat.is_llm:
                continue
            if author_seat == llm.seat_no:
                continue
            if not self._is_triggered(seat, text):
                continue
            # Check cap + cooldown before even calling the LLM
            count, _, last = await self.repo.load_llm_speech(game.id, game.day_number, llm.seat_no)
            now = self._clock()
            if count >= self.NORMAL_SPEECH_CAP:
                continue
            if last is not None and now - last < self.COOLDOWN_SECONDS:
                continue
            await self._maybe_speak(game, llm, seat, seats)

    def _is_triggered(self, seat: Seat, text: str) -> bool:
        if seat.display_name and seat.display_name in text:
            return True
        if seat.persona_key and seat.persona_key.lower() in text.lower():
            return True
        return any(kw in text for kw in self.REACTION_KEYWORDS)

    def _speech_lock(self, game_id: str, seat_no: int) -> asyncio.Lock:
        key = (game_id, seat_no)
        lock = self._speech_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._speech_locks[key] = lock
        return lock

    async def _maybe_speak(
        self,
        game: Game,
        player: Player,
        seat: Seat,
        seats: Sequence[Seat],
    ) -> None:
        """Ask the LLM; if intent=speak, post to main and increment the count.

        The entire read(cap/cooldown) → LLM call → post → increment pipeline is
        serialized per (game, seat) so two concurrent triggers can't both pass
        the cap check and double-post.
        """
        async with self._speech_lock(game.id, player.seat_no):
            count, _, last = await self.repo.load_llm_speech(
                game.id, game.day_number, player.seat_no
            )
            if count >= self.NORMAL_SPEECH_CAP:
                return
            now = self._clock()
            if last is not None and now - last < self.COOLDOWN_SECONDS:
                return
            players = await self.repo.load_players(game.id)
            action = await self._ask(
                game,
                player,
                seat,
                players,
                seats,
                task_text=task_daytime_speech(game.day_number),
            )
            if action.intent != "speak":
                return
            message = action.public_message.strip()
            if not message:
                return
            # Belt-and-suspenders: the LLM call itself can take seconds. Re-check
            # phase right before posting so we don't dump a stale speech into a
            # channel that has already moved on to voting or night.
            fresh = await self.repo.load_game(game.id)
            if (
                fresh is None
                or fresh.phase is not Phase.DAY_DISCUSSION
                or fresh.day_number != game.day_number
            ):
                return
            posted = False
            if self.message_poster is not None:
                try:
                    await self.message_poster.post_public(
                        fresh, f"**{seat.display_name}**: {message}", kind="LLM_SPEAK"
                    )
                    posted = True
                except Exception:
                    log.exception("post_public for LLM speech failed")
            if posted:
                # Use the post-completion time as both the log timestamp and
                # the cooldown anchor. A slow xAI round-trip would otherwise
                # leave last_spoke_epoch pointing at the pre-inference now,
                # letting the cooldown window expire earlier than the actual
                # post time and enabling unintended back-to-back speeches.
                posted_at = self._clock()
                try:
                    await self.repo.insert_log_public(
                        LogEntry(
                            game_id=fresh.id,
                            day=fresh.day_number,
                            phase=fresh.phase,
                            kind="PLAYER_SPEECH",
                            actor_seat=player.seat_no,
                            visibility="PUBLIC",
                            text=message,
                            created_at=posted_at,
                        )
                    )
                except Exception:
                    log.exception("PLAYER_SPEECH log insert failed for seat %s", player.seat_no)
                await self.repo.increment_llm_normal_speech(
                    game.id, game.day_number, player.seat_no, posted_at
                )

    # ------------------------------------------------------ helpers
    async def _ask(
        self,
        game: Game,
        player: Player,
        seat: Seat,
        players: Sequence[Player],
        seats: Sequence[Seat],
        task_text: str,
    ) -> LLMAction:
        persona = PERSONAS_BY_KEY.get(seat.persona_key or "")
        if persona is None:
            return LLMAction(intent="skip", reason_summary="persona missing")
        assert player.role is not None
        public_logs = await self.repo.load_public_logs(game.id, limit=40)
        private_logs = await self.repo.load_private_logs_for_audience(
            game.id, audience_seat=player.seat_no, limit=40
        )
        system = build_system_prompt(
            persona=persona,
            role=player.role,
            phase=game.phase,
            day_number=game.day_number,
            task_text=task_text,
        )
        user = build_user_context(
            game=game,
            me=player,
            my_seat=seat,
            seats=seats,
            players=players,
            public_logs=public_logs,
            private_logs=private_logs,
        )
        try:
            return await self.decider.decide(system, user)
        except Exception:
            log.exception("LLM decide failed for seat %s game %s", player.seat_no, game.id)
            return LLMAction(intent="skip", reason_summary="decider error")

    def _resolve_target(
        self,
        target_name: str | None,
        candidates: Sequence[Seat],
        *,
        allow_none: bool,
    ) -> int | None:
        """Map an LLM `target_name` back to a seat_no.

        Candidates are presented to the LLM as `席{N} {display_name}` tokens. The
        primary path parses the leading `席N` prefix, which disambiguates duplicate
        display_names. Bare-name matches are a legacy fallback and only accepted
        when exactly one candidate has that display_name.
        """
        if target_name is None:
            if allow_none:
                return None
            pick = self.rng.choice(list(candidates))
            log.warning("LLM returned null target; fallback to seat %d", pick.seat_no)
            return pick.seat_no
        m = _SEAT_TOKEN_RE.match(target_name)
        if m is not None:
            seat_no = int(m.group(1))
            for c in candidates:
                if c.seat_no == seat_no:
                    return c.seat_no
        matches = [c for c in candidates if c.display_name == target_name]
        if len(matches) == 1:
            return matches[0].seat_no
        log.warning(
            "LLM target_name %r not resolvable (ambiguous or unknown); random fallback",
            target_name,
        )
        return self.rng.choice(list(candidates)).seat_no

    @staticmethod
    def _role_to_kind(
        player: Player,
        all_players: Sequence[Player],
        prev_guard_seat: int | None,
    ) -> tuple[SubmissionType | None, list[int]]:
        if player.role is Role.WEREWOLF:
            return (
                SubmissionType.WOLF_ATTACK,
                legal_attack_targets(all_players, player.seat_no),
            )
        if player.role is Role.SEER:
            return (
                SubmissionType.SEER_DIVINE,
                legal_divine_targets(all_players, player.seat_no),
            )
        if player.role is Role.KNIGHT:
            return (
                SubmissionType.KNIGHT_GUARD,
                legal_guard_targets(all_players, player.seat_no, prev_guard_seat),
            )
        return (None, [])


# ------------------------------------------------------------- factory
def make_xai_decider(api_key: str, model: str, timeout: float = 30.0) -> XAILLMActionDecider:
    """Build an xAI-backed decider. Imports openai lazily so tests can skip it."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
    return XAILLMActionDecider(client=client, model=model, timeout=timeout)


__all__ = [
    "RESPONSE_SCHEMA",
    "FakeLLMActionDecider",
    "LLMAction",
    "LLMActionDecider",
    "LLMAdapter",
    "XAILLMActionDecider",
    "make_xai_decider",
]

# keep Phase referenced for mypy
_ = Phase
