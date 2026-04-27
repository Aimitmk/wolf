"""Discord I/O layer.

Two classes live here:

  - `DiscordBotAdapter` implements the `DiscordAdapter` + `RecoveryDiscordAdapter`
    protocols consumed by `game_service` and `recovery_service`. It owns channel
    posting, DM sending, permission delegation, and status announcements.

  - `WolfCog` is the slash-command surface. It handles `/wolf create / join / leave
    / start / status / extend / force-skip / abort` and dispatches to game_service.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from random import Random
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from wolfbot.config import MasterSettings
from wolfbot.domain.enums import (
    Phase,
    Role,
    SubmissionType,
)
from wolfbot.domain.errors import ActiveGameExistsError
from wolfbot.domain.models import Game, LogEntry, PendingDecision, Player, Seat
from wolfbot.domain.rules import (
    legal_attack_targets,
    legal_divine_targets,
    legal_guard_targets,
    previous_guard_seat_for_night,
)
from wolfbot.llm.persona_base import pick_personas
from wolfbot.npc.personas import NPC_PERSONAS, NPC_PERSONAS_BY_KEY
from wolfbot.persistence.sqlite_repo import (
    JoinLobbyResult,
    LeaveLobbyResult,
    SqliteRepo,
)
from wolfbot.services.game_service import GameService, new_game_id
from wolfbot.services.llm_service import LLMAdapter
from wolfbot.services.permission_manager import PermissionManager
from wolfbot.services.timer_service import EngineRegistry, GameEngine
from wolfbot.ui.views import NightActionView, VoteView

if TYPE_CHECKING:
    from wolfbot.master.npc_registry import NpcRegistry
    from wolfbot.services.discussion_service import DiscussionService

log = logging.getLogger(__name__)


# Role-identifying submission kinds — naming seats, counts, or split state for
# any of these in a public surface would reveal role assignments (a seat owing
# WOLF_ATTACK is a wolf; a count of 2 confirms two wolves are alive). The
# generic line below replaces all per-kind detail in the main channel,
# /wolf status, and recovery announce.
ROLE_IDENTIFYING_KINDS: frozenset[SubmissionType] = frozenset(
    {SubmissionType.WOLF_ATTACK, SubmissionType.SEER_DIVINE, SubmissionType.KNIGHT_GUARD}
)

GENERIC_SECRET_PENDING_LINE = "秘密行動の未確定があります。該当者へ DM を送信しました。"


def render_pending_host_lines(
    pending: PendingDecision,
    seat_name: dict[int, str],
) -> list[str]:
    """Per-submission lines for the /wolf status ホスト待ち field.

    For role-identifying kinds (WOLF_ATTACK/SEER_DIVINE/KNIGHT_GUARD) we emit
    a single generic line regardless of which kinds are pending — kind name,
    seat names, count, and split language are all withheld because any of
    them would let villagers infer wolf count or pinpoint the seer/knight.
    For VOTE/RUNOFF_VOTE the names stay (who's voting is public info).
    """
    lines: list[str] = []
    has_role_id = False
    for sub in pending.effective_submissions():
        is_role_id = sub.submission_type in ROLE_IDENTIFYING_KINDS
        if is_role_id:
            if sub.missing_seats or sub.unresolved_seats:
                has_role_id = True
            continue
        if sub.missing_seats:
            names = "、".join(seat_name.get(sn, str(sn)) for sn in sub.missing_seats)
            lines.append(f"`{sub.submission_type.value}` 未提出: {names}")
        if sub.unresolved_seats:
            names = "、".join(seat_name.get(sn, str(sn)) for sn in sub.unresolved_seats)
            lines.append(f"`{sub.submission_type.value}` 再提出待ち: {names}")
    if has_role_id:
        lines.append(GENERIC_SECRET_PENDING_LINE)
    return lines


def _main_channel_should_llm_react(author_seat: int | None, players: Sequence[Player]) -> bool:
    """Alive-participant gate for DAY_DISCUSSION main-channel messages.

    Returns True only when the author is a living seated player. Used as the
    common precondition for both (a) persisting the message as PLAYER_SPEECH
    and (b) triggering an LLM reaction. A non-participant (spectator, admin)
    or a dead player must not steer the LLMs — nor pollute the public log
    that is fed back into every later LLM prompt via build_user_context.
    """
    if author_seat is None:
        return False
    author = next((p for p in players if p.seat_no == author_seat), None)
    return author is not None and author.alive


# --------------------------------------------------------------- DiscordBotAdapter
class DiscordBotAdapter:
    """Implements the DiscordAdapter protocol by operating on a live discord.Client."""

    def __init__(
        self,
        bot: discord.Client,
        repo: SqliteRepo,
        settings: MasterSettings,
        game_service_ref: dict[str, GameService] | None = None,
    ) -> None:
        self.bot = bot
        self.repo = repo
        self.settings = settings
        self.perms = PermissionManager(bot=bot)
        # Circular: DiscordBotAdapter needs GameService (for submit callbacks passed into
        # Views) and GameService needs DiscordBotAdapter. We stash a dict to break the cycle.
        self._gs_slot: dict[str, GameService] = game_service_ref or {}

    def set_game_service(self, gs: GameService) -> None:
        self._gs_slot["gs"] = gs

    @property
    def gs(self) -> GameService:
        gs = self._gs_slot.get("gs")
        if gs is None:
            raise RuntimeError("DiscordBotAdapter.set_game_service(...) was not called")
        return gs

    # ------------------------------------------------------ permissions
    async def apply_permissions(
        self, game: Game, seats: Sequence[Seat], players: Sequence[Player]
    ) -> None:
        await self.perms.apply(game, seats, players)

    async def kill_permissions(
        self, game: Game, seats: Sequence[Seat], seat_no: int, was_wolf: bool
    ) -> None:
        await self.perms.kill(game, seats, seat_no, was_wolf=was_wolf)

    async def reconcile(self, game: Game, seats: Sequence[Seat], players: Sequence[Player]) -> None:
        await self.perms.apply(game, seats, players)

    async def on_game_end(self, game: Game, seats: Sequence[Seat]) -> None:
        await self.perms.on_game_end(game, seats)

    # ------------------------------------------------------ channel posts
    async def post_public(self, game: Game, text: str, kind: str) -> None:
        channel = self._main_text(game)
        if channel is None:
            return
        try:
            await channel.send(text)
        except discord.DiscordException:
            log.exception("post_public failed %s", game.id)

    async def post_morning(self, game: Game, text: str) -> None:
        channel = self._main_text(game)
        if channel is None:
            return
        try:
            await channel.send(f"☀️ {text}")
        except discord.DiscordException:
            log.exception("post_morning failed %s", game.id)

    async def post_wolves_chat(self, game: Game, text: str, kind: str) -> None:
        channel = self._wolves_channel(game)
        if channel is None:
            return
        try:
            await channel.send(text)
        except discord.DiscordException:
            log.exception("post_wolves_chat failed %s", game.id)

    async def send_private(self, game: Game, audience_seat: int, text: str, kind: str) -> None:
        seat = await self._seat(game.id, audience_seat)
        if seat is None or seat.discord_user_id is None:
            return
        user = await self._fetch_user(int(seat.discord_user_id))
        if user is None:
            return
        try:
            await user.send(text)
        except discord.Forbidden:
            log.warning(
                "DM forbidden for user %s (seat %s, game %s)",
                seat.discord_user_id,
                audience_seat,
                game.id,
            )
        except discord.DiscordException:
            log.exception("send_private failed %s seat %s", game.id, audience_seat)

    async def send_vote_dms(
        self,
        game: Game,
        voters: Sequence[Player],
        candidates: Sequence[Seat],
        round_: int,
    ) -> None:
        seats_by_no = {s.seat_no: s for s in await self.repo.load_seats(game.id)}
        for voter in voters:
            seat = seats_by_no.get(voter.seat_no)
            if seat is None or seat.is_llm or seat.discord_user_id is None:
                continue
            user = await self._fetch_user(int(seat.discord_user_id))
            if user is None:
                continue
            view = VoteView(
                game_id=game.id,
                voter_seat=voter.seat_no,
                candidates=candidates,
                round_=round_,
                day=game.day_number,
                on_submit=self.gs.submit_vote,
            )
            title = "投票" if round_ == 0 else "決選投票"
            try:
                await user.send(f"【{title}】対象を選んでください。", view=view)
            except discord.Forbidden:
                log.warning("vote DM forbidden for seat %s", voter.seat_no)
            except discord.DiscordException:
                log.exception("vote DM failed seat %s", voter.seat_no)

    async def send_night_action_dms(
        self,
        game: Game,
        actors: Sequence[Player],
        alive_players: Sequence[Player],
        seats: Sequence[Seat],
    ) -> None:
        """Send a role-specific night-action DM to each actor.

        `actors` is the subset to DM (typically "still pending"); `alive_players`
        is the full alive pool used for legal-target computation — they must be
        kept separate so a resend to a single split wolf still offers the full
        legal attack list.
        """
        seats_by_no = {s.seat_no: s for s in seats}
        prev = await self.repo.load_previous_guard(game.id)
        prev_guard_seat = previous_guard_seat_for_night(prev, game.day_number)

        for p in actors:
            if p.role is None:
                continue
            seat = seats_by_no.get(p.seat_no)
            if seat is None or seat.is_llm or seat.discord_user_id is None:
                continue
            kind = None
            candidates: list[Seat] = []
            if p.role is Role.WEREWOLF:
                kind = SubmissionType.WOLF_ATTACK
                legal = legal_attack_targets(alive_players, p.seat_no)
                candidates = [seats_by_no[sn] for sn in legal if sn in seats_by_no]
            elif p.role is Role.SEER:
                kind = SubmissionType.SEER_DIVINE
                legal = legal_divine_targets(alive_players, p.seat_no)
                candidates = [seats_by_no[sn] for sn in legal if sn in seats_by_no]
            elif p.role is Role.KNIGHT:
                kind = SubmissionType.KNIGHT_GUARD
                legal = legal_guard_targets(alive_players, p.seat_no, prev_guard_seat)
                candidates = [seats_by_no[sn] for sn in legal if sn in seats_by_no]
            if kind is None or not candidates:
                continue
            user = await self._fetch_user(int(seat.discord_user_id))
            if user is None:
                continue
            view = NightActionView(
                game_id=game.id,
                actor_seat=p.seat_no,
                kind=kind,
                candidates=candidates,
                day=game.day_number,
                on_submit=self.gs.submit_night_action,
            )
            prompts = {
                SubmissionType.WOLF_ATTACK: "襲撃対象を選択してください。",
                SubmissionType.SEER_DIVINE: "占い対象を選択してください。",
                SubmissionType.KNIGHT_GUARD: "護衛対象を選択してください。",
            }
            try:
                await user.send(prompts[kind], view=view)
            except discord.Forbidden:
                log.warning("night DM forbidden for seat %s", p.seat_no)
            except discord.DiscordException:
                log.exception("night DM failed seat %s", p.seat_no)

    async def announce_waiting(
        self, game: Game, pending: PendingDecision, seats: Sequence[Seat]
    ) -> None:
        seats_by_no = {s.seat_no: s for s in seats}
        public_lines = [
            "⏸ **ホスト待ち**",
            f"フェイズ: `{pending.phase.value}` (day {pending.day})",
        ]
        has_role_id = False
        for sub in pending.effective_submissions():
            is_role_id = sub.submission_type in ROLE_IDENTIFYING_KINDS
            if is_role_id:
                if sub.missing_seats or sub.unresolved_seats:
                    has_role_id = True
                continue
            if sub.missing_seats:
                names = [
                    seats_by_no[sn].display_name for sn in sub.missing_seats if sn in seats_by_no
                ]
                public_lines.append(
                    f"`{sub.submission_type.value}` 未提出: {'、'.join(names) or '(なし)'}"
                )
            if sub.unresolved_seats:
                names = [
                    seats_by_no[sn].display_name for sn in sub.unresolved_seats if sn in seats_by_no
                ]
                public_lines.append(
                    f"`{sub.submission_type.value}` 再提出待ち: {'、'.join(names) or '(なし)'}"
                )
        if has_role_id:
            public_lines.append(GENERIC_SECRET_PENDING_LINE)
        public_lines.append(
            "`/wolf extend <秒>` で延長、または `/wolf force-skip` で未提出を確定処理します。"
        )
        text = "\n".join(public_lines)
        channel = self._main_text(game)
        if channel is not None:
            try:
                await channel.send(text)
            except discord.DiscordException:
                log.exception("announce_waiting failed %s", game.id)
        # Wolves channel auto-relay deliberately omitted: posting split details
        # there would itself be evidence (visible in private logs / DB) that the
        # split happened. The existing `resend_pending_dms` path re-DMs the
        # affected wolves directly with their re-pick options.

    async def announce_recovery(self, game: Game, pending: PendingDecision | None) -> None:
        channel = self._main_text(game)
        if channel is None:
            return
        lines = [f"♻️ 復帰しました。現在フェイズ: `{game.phase.value}` / day {game.day_number}"]
        if pending:
            has_role_id = False
            for sub in pending.effective_submissions():
                if sub.submission_type in ROLE_IDENTIFYING_KINDS:
                    if sub.missing_seats or sub.unresolved_seats:
                        has_role_id = True
                    continue
                count = len(set(sub.missing_seats) | set(sub.unresolved_seats))
                if count:
                    lines.append(f"未提出あり: `{sub.submission_type.value}` → {count} 件未確定")
            if has_role_id:
                lines.append(GENERIC_SECRET_PENDING_LINE)
        try:
            await channel.send("\n".join(lines))
        except discord.DiscordException:
            log.exception("announce_recovery failed %s", game.id)

    # ------------------------------------------------------ helpers
    def _main_text(self, game: Game) -> discord.TextChannel | None:
        channel = self.bot.get_channel(int(game.main_text_channel_id))
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    def _wolves_channel(self, game: Game) -> discord.TextChannel | None:
        if game.wolves_channel_id is None:
            return None
        channel = self.bot.get_channel(int(game.wolves_channel_id))
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    async def _fetch_user(self, user_id: int) -> discord.User | None:
        user = self.bot.get_user(user_id)
        if user is not None:
            return user
        try:
            return await self.bot.fetch_user(user_id)
        except discord.DiscordException:
            log.warning("fetch_user failed for %s", user_id)
            return None

    async def _seat(self, game_id: str, seat_no: int) -> Seat | None:
        for s in await self.repo.load_seats(game_id):
            if s.seat_no == seat_no:
                return s
        return None


# --------------------------------------------------------------- slash cog
class WolfCog(commands.Cog):
    wolf = app_commands.Group(name="wolf", description="9 人村人狼")

    def __init__(
        self,
        bot: commands.Bot,
        repo: SqliteRepo,
        game_service: GameService,
        discord_adapter: DiscordBotAdapter,
        llm_adapter: LLMAdapter,
        registry: EngineRegistry,
        settings: MasterSettings,
        rng: Random | None = None,
        discussion_service: DiscussionService | None = None,
        on_speech_recorded: Callable[[str], Awaitable[None]] | None = None,
        npc_registry: NpcRegistry | None = None,
        text_analyzer: Any = None,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.repo = repo
        self.gs = game_service
        self.adapter = discord_adapter
        self.llm_adapter = llm_adapter
        self.registry = registry
        self.settings = settings
        self.rng = rng or Random()
        self._create_locks: dict[str, asyncio.Lock] = {}
        self._discussion_service = discussion_service
        self._on_speech_recorded = on_speech_recorded
        # Set when reactive_voice pipeline is active.  In that mode `/wolf
        # start` fills LLM seats from online NPC bots (each carrying a fixed
        # persona) instead of randomly drawing from NPC_PERSONAS.
        self._npc_registry = npc_registry
        # Optional structured analyzer for text-channel utterances. Mirrors
        # the voice path's `GeminiAudioAnalyzer` — extracts `addressed_name`
        # and `co_declaration` so SpeakArbiter can route replies to the
        # named NPC seat just like for voice. Wired only when reactive_voice
        # is active and a Voice LLM key is set; falls back to plain raw
        # capture when None.
        self._text_analyzer = text_analyzer

    def _select_llm_seat_personas(
        self,
        *,
        shortfall: int,
        discussion_mode: str,
    ) -> tuple[list[tuple[str, str]], str | None]:
        """Choose ``(display_name, persona_key)`` pairs to back-fill LLM seats.

        Two backfill strategies, switched on the game's discussion mode:

        * ``rounds``: random draw from :data:`NPC_PERSONAS` (Master drives
          these seats internally via xAI; no NPC bot process required).
        * ``reactive_voice``: pull online NPC bots from the registry and use
          *their* personas (each NPC bot is bound to a fixed persona at
          startup).  Fails with a friendly message if not enough bots are
          online.

        Returns ``(seats, None)`` on success or ``([], error_message)`` on
        failure. The caller surfaces the error message via interaction
        followup.
        """
        if discussion_mode == "reactive_voice" and self._npc_registry is not None:
            online = [e for e in self._npc_registry.all_online() if e.persona_key]
            # Skip bots already bound to another active game.
            available = [e for e in online if e.assigned_seat is None]
            if len(available) < shortfall:
                return ([], (
                    f"reactive_voice モードで LLM 席を {shortfall} 席埋める必要がありますが、"
                    f"利用可能な NPC bot は {len(available)} 体だけです。"
                    f"NPC bot プロセスを追加で起動するか、人間プレイヤーを集めてください。"
                ))
            # Deterministic ordering keeps tests/replay-friendly; pick the
            # first `shortfall` bots in registration order.
            chosen = available[:shortfall]
            seen_keys: set[str] = set()
            seats: list[tuple[str, str]] = []
            for entry in chosen:
                if entry.persona_key in seen_keys:
                    return ([], (
                        f"NPC bot {entry.npc_id} の persona_key={entry.persona_key} が"
                        " 重複しています。bot ごとに別の persona を割り当ててください。"
                    ))
                seen_keys.add(entry.persona_key)
                persona = NPC_PERSONAS_BY_KEY.get(entry.persona_key)
                if persona is None:
                    return ([], (
                        f"NPC bot {entry.npc_id} の persona_key={entry.persona_key} は"
                        f" 未知の persona です。"
                    ))
                seats.append((persona.display_name, persona.key))
            return (seats, None)

        # rounds mode (or no registry wired): random draw.
        picks = pick_personas(NPC_PERSONAS, shortfall, self.rng)
        return ([(p.display_name, p.key) for p in picks], None)


    def _create_lock_for(self, guild_id: str) -> asyncio.Lock:
        lock = self._create_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._create_locks[guild_id] = lock
        return lock

    # ----------------------------------------------------------- on_message
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:
            return
        if not message.content.strip():
            return
        game = await self.repo.load_active_game_for_guild(str(message.guild.id))
        if game is None:
            return
        channel_id = str(message.channel.id)
        is_main = channel_id == game.main_text_channel_id
        is_wolves = game.wolves_channel_id is not None and channel_id == game.wolves_channel_id
        if not (is_main or is_wolves):
            return
        author_seat = await self.repo.seat_of_user(game.id, str(message.author.id))
        players = await self.repo.load_players(game.id)

        if is_main and game.phase in (Phase.DAY_DISCUSSION, Phase.DAY_RUNOFF_SPEECH):
            if not _main_channel_should_llm_react(author_seat, players):
                return
            if self._discussion_service is not None and author_seat is not None:
                # Full SpeechEvent path: record() persists the event row AND
                # emits the PLAYER_SPEECH LogEntry (for source=text it skips
                # the channel post since the original message is already visible).
                try:
                    from wolfbot.domain.discussion import make_phase_id
                    from wolfbot.services.discussion_service import make_human_text_event

                    alive_seat_nos = sorted(p.seat_no for p in players if p.alive)
                    await self._discussion_service.begin_phase_if_absent(
                        game_id=game.id,
                        day=game.day_number,
                        phase=game.phase,
                        alive_seat_nos=alive_seat_nos,
                    )
                    phase_id = make_phase_id(game.id, game.day_number, game.phase)

                    # Mirror the voice path: when a TextAnalyzer is wired,
                    # extract `addressed_name` + `co_declaration` so the
                    # downstream SpeakArbiter / state fold see the same
                    # structured signal regardless of whether the human
                    # spoke or typed. Failures are best-effort — a slow or
                    # broken analyzer must not block the SpeechEvent write.
                    addressed_seat_no: int | None = None
                    co_declaration: str | None = None
                    if self._text_analyzer is not None:
                        try:
                            analysis = await self._text_analyzer.analyze(
                                text=message.content, timeout_s=8.0
                            )
                        except Exception:
                            log.exception(
                                "text_analyzer_failed game=%s seat=%s",
                                game.id,
                                author_seat,
                            )
                        else:
                            co_declaration = analysis.co_declaration
                            if analysis.addressed_name:
                                from wolfbot.master.ingest_service import (
                                    resolve_seat_by_name,
                                )

                                seats = await self.repo.load_seats(game.id)
                                alive_set = {
                                    p.seat_no for p in players if p.alive
                                }
                                seat_no = resolve_seat_by_name(
                                    analysis.addressed_name,
                                    seats,
                                    alive=frozenset(alive_set),
                                )
                                if seat_no is not None and seat_no != author_seat:
                                    addressed_seat_no = seat_no

                    event = make_human_text_event(
                        game_id=game.id,
                        phase_id=phase_id,
                        day=game.day_number,
                        phase=game.phase,
                        speaker_seat=author_seat,
                        text=message.content,
                        co_declaration=co_declaration,
                        addressed_seat_no=addressed_seat_no,
                    )
                    await self._discussion_service.record(event)
                    # Trigger arbiter dispatch so NPCs can respond to new text.
                    if self._on_speech_recorded is not None:
                        try:
                            await self._on_speech_recorded(game.id)
                        except Exception:
                            log.exception(
                                "on_speech_recorded callback failed game=%s", game.id
                            )
                except Exception:
                    log.exception(
                        "SpeechEvent(text) write failed for game=%s seat=%s",
                        game.id,
                        author_seat,
                    )
            else:
                # Legacy path: no DiscussionService wired (or spectator with
                # no seat). Fall back to direct PLAYER_SPEECH log insert.
                try:
                    await self.repo.insert_log_public(
                        LogEntry(
                            game_id=game.id,
                            day=game.day_number,
                            phase=game.phase,
                            kind="PLAYER_SPEECH",
                            actor_seat=author_seat,
                            visibility="PUBLIC",
                            text=message.content,
                            created_at=int(time.time()),
                        )
                    )
                except Exception:
                    log.exception("PLAYER_SPEECH log insert failed for %s", game.id)
            return

        if is_wolves and game.phase is Phase.NIGHT and author_seat is not None:
            # Author must be a living wolf — otherwise discard (defence-in-depth;
            # Discord permissions already enforce this).
            author_player = next((p for p in players if p.seat_no == author_seat), None)
            if author_player is None or not author_player.alive:
                return
            if author_player.role is not Role.WEREWOLF:
                return
            alive_wolves = [p for p in players if p.alive and p.role is Role.WEREWOLF]
            now_ts = int(time.time())
            for wolf in alive_wolves:
                try:
                    await self.repo.insert_log_private(
                        LogEntry(
                            game_id=game.id,
                            day=game.day_number,
                            phase=game.phase,
                            kind="WOLF_CHAT",
                            actor_seat=author_seat,
                            audience_seat=wolf.seat_no,
                            visibility="PRIVATE",
                            text=message.content,
                            created_at=now_ts,
                        )
                    )
                except Exception:
                    log.exception(
                        "WOLF_CHAT log insert failed for %s audience %s",
                        game.id,
                        wolf.seat_no,
                    )

    # -------------------------------------------------------------- /wolf create
    @wolf.command(name="create", description="新しい 9 人村を作成")
    async def create(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("ギルド内で実行してください。", ephemeral=True)
            return
        guild_id = str(interaction.guild_id)
        existing = await self.repo.load_active_game_for_guild(guild_id)
        if existing is not None:
            await interaction.response.send_message(
                f"既に進行中のゲームがあります (id: `{existing.id}`)。",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)

        async with self._create_lock_for(guild_id):
            # Re-check under the lock: a concurrent /wolf create may have
            # claimed this guild while we were deferring, and serialize
            # channel creation so the stale-channel purge in
            # _create_private_channel can't eat a sibling's fresh channel.
            existing = await self.repo.load_active_game_for_guild(guild_id)
            if existing is not None:
                await interaction.followup.send(
                    f"既に進行中のゲームがあります (id: `{existing.id}`)。"
                )
                return

            # Snapshot the set of channel IDs the bot has previously owned in
            # this guild. `_create_private_channel` will only purge same-named
            # existing channels whose ID is in this set — a manually-made
            # `wolf-heaven` / `wolf-wolves` lacking a matching history row
            # gets refused rather than silently deleted.
            safe_ids = await self.repo.load_private_channel_ids_for_guild(guild_id)
            create_failed_msg = (
                "チャンネル作成に失敗しました。"
                "Bot の権限、または同名の `wolf-heaven` / `wolf-wolves` が手動作成されていないかを確認してください。"
            )
            heaven = await self._create_private_channel(
                interaction.guild, name="wolf-heaven", safe_to_delete_ids=safe_ids
            )
            if heaven is None:
                await interaction.followup.send(create_failed_msg)
                return
            wolves = await self._create_private_channel(
                interaction.guild, name="wolf-wolves", safe_to_delete_ids=safe_ids
            )
            if wolves is None:
                try:
                    await heaven.delete(reason="wolfbot: partial /wolf create rollback")
                except discord.DiscordException:
                    log.exception("cleanup of heaven %s failed", heaven.id)
                await interaction.followup.send(create_failed_msg)
                return

            discussion_mode = self.settings.LLM_DISCUSSION_MODE
            if discussion_mode not in ("rounds", "reactive_voice"):
                log.warning(
                    "LLM_DISCUSSION_MODE=%r invalid, falling back to 'rounds'",
                    discussion_mode,
                )
                discussion_mode = "rounds"
            game = Game(
                id=new_game_id(),
                guild_id=guild_id,
                host_user_id=str(interaction.user.id),
                phase=Phase.LOBBY,
                day_number=0,
                main_text_channel_id=str(self.settings.MAIN_TEXT_CHANNEL_ID),
                main_vc_channel_id=str(self.settings.MAIN_VOICE_CHANNEL_ID),
                heaven_channel_id=str(heaven.id),
                wolves_channel_id=str(wolves.id),
                created_at=int(time.time()),
                discussion_mode=discussion_mode,
            )
            try:
                await self.repo.create_game(game)
            except ActiveGameExistsError:
                # Belt-and-suspenders for bot-restart-mid-create: the in-process
                # lock normally prevents this, but a restart between create and
                # claim leaves the DB uniqueness check as the only guard.
                for ch in (heaven, wolves):
                    try:
                        await ch.delete(reason="wolfbot: duplicate /wolf create race")
                    except discord.DiscordException:
                        log.exception("cleanup of %s failed", ch.id)
                winner = await self.repo.load_active_game_for_guild(guild_id)
                winner_id = winner.id if winner else "?"
                await interaction.followup.send(
                    f"既に進行中のゲームがあります (id: `{winner_id}`)。"
                )
                return
            except Exception:
                # The channels exist on Discord but the games row never landed,
                # so on the next /wolf create the same-name safety check would
                # refuse to delete them as orphans (their IDs aren't in the
                # DB-tracked safe set). Clean them up here while we still know
                # their IDs, then re-raise so the failure surfaces.
                for ch in (heaven, wolves):
                    try:
                        await ch.delete(reason="wolfbot: /wolf create failed before DB commit")
                    except discord.DiscordException:
                        log.exception("cleanup of %s failed", ch.id)
                raise
            await interaction.followup.send(
                f"🎲 ゲーム作成 (id: `{game.id}`)。`/wolf join` で参加してください。"
            )

    # ---------------------------------------------------------------- /wolf join
    @wolf.command(name="join", description="ロビーに参加")
    async def join(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("ギルド内で実行してください。", ephemeral=True)
            return
        game = await self.repo.load_active_game_for_guild(str(interaction.guild_id))
        if game is None:
            await interaction.response.send_message(
                "ロビー中のゲームがありません。", ephemeral=True
            )
            return
        # join_lobby atomically re-checks phase inside the transaction so a
        # /wolf start that wins the race can't be silently corrupted by a
        # stale insert here.
        result, seat_no = await self.repo.join_lobby(
            game.id,
            discord_user_id=str(interaction.user.id),
            display_name=interaction.user.display_name,
        )
        if result is JoinLobbyResult.ACCEPTED:
            await interaction.response.send_message(
                f"✅ {interaction.user.display_name} が座席 {seat_no} に着席しました。"
            )
            return
        messages: dict[JoinLobbyResult, str] = {
            JoinLobbyResult.STALE_PHASE: "ロビーは既に閉じています。",
            JoinLobbyResult.ALREADY_JOINED: "既に参加しています。",
            JoinLobbyResult.LOBBY_FULL: "人数が 9 に達しているので参加できません。",
            JoinLobbyResult.NO_FREE_SEAT: "空き席がありません。",
        }
        await interaction.response.send_message(
            messages.get(result, "参加できませんでした。"), ephemeral=True
        )

    # --------------------------------------------------------------- /wolf leave
    @wolf.command(name="leave", description="ロビー中のみ退出")
    async def leave(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("ギルド内で実行してください。", ephemeral=True)
            return
        game = await self.repo.load_active_game_for_guild(str(interaction.guild_id))
        if game is None:
            await interaction.response.send_message(
                "ロビー中のゲームがありません。", ephemeral=True
            )
            return
        result = await self.repo.leave_lobby(
            game.id,
            discord_user_id=str(interaction.user.id),
        )
        if result is LeaveLobbyResult.ACCEPTED:
            await interaction.response.send_message(
                f"👋 {interaction.user.display_name} が退出しました。"
            )
            return
        messages: dict[LeaveLobbyResult, str] = {
            LeaveLobbyResult.STALE_PHASE: "ロビーは既に閉じています。",
            LeaveLobbyResult.NOT_JOINED: "参加していません。",
        }
        await interaction.response.send_message(
            messages.get(result, "退出できませんでした。"), ephemeral=True
        )

    # ---------------------------------------------------------------- /wolf start
    @wolf.command(name="start", description="ゲーム開始 (人数不足は LLM で補完)")
    async def start(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("ギルド内で実行してください。", ephemeral=True)
            return
        game = await self.repo.load_active_game_for_guild(str(interaction.guild_id))
        if game is None or game.phase is not Phase.LOBBY:
            await interaction.response.send_message(
                "ロビー中のゲームがありません。", ephemeral=True
            )
            return
        if str(interaction.user.id) != game.host_user_id:
            await interaction.response.send_message("ホストのみ開始できます。", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)

        seats = await self.repo.load_seats(game.id)
        humans = [s for s in seats if not s.is_llm]
        # Preflight DM check for humans
        bad = await self._preflight_dms(humans)
        if bad:
            names = "、".join(b for b in bad)
            await interaction.followup.send(
                f"以下のメンバーに DM が送れません。DM を開放してから再実行してください: {names}"
            )
            return

        shortfall = 9 - len(humans)
        if shortfall > 0:
            llm_seats, err = self._select_llm_seat_personas(
                shortfall=shortfall,
                discussion_mode=game.discussion_mode,
            )
            if err is not None:
                await interaction.followup.send(err)
                return
        else:
            llm_seats = []

        ok = await self.repo.claim_start_and_backfill(
            game.id,
            expected_phase=Phase.LOBBY,
            llm_seats=llm_seats,
        )
        if not ok:
            await interaction.followup.send(
                "開始処理でラップ競合が発生しました。もう一度お試しください。"
            )
            return

        final_seats = await self.repo.load_seats(game.id)
        roster_lines = [f"席{seat.seat_no} {seat.display_name}" for seat in final_seats]

        engine = GameEngine(game_id=game.id, repo=self.repo, advance=self.gs.advance)
        await self.registry.attach(engine)
        engine.start()

        await interaction.followup.send(
            "\n".join(
                [
                    f"🎮 ゲーム開始。参加者: {len(humans)} 人 + LLM {shortfall} 人。",
                    "参加者一覧:",
                    *roster_lines,
                ]
            )
        )

    # --------------------------------------------------------------- /wolf status
    @wolf.command(name="status", description="現在のフェイズと参加者を表示")
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("ギルド内で実行してください。", ephemeral=True)
            return
        game = await self.repo.load_active_game_for_guild(str(interaction.guild_id))
        if game is None:
            await interaction.response.send_message("進行中のゲームはありません。", ephemeral=True)
            return
        seats = await self.repo.load_seats(game.id)
        players = await self.repo.load_players(game.id)
        pending = await self.repo.load_pending_decision(game.id)
        now = int(time.time())
        remaining = max(0, game.deadline_epoch - now) if game.deadline_epoch else None
        alive = [p for p in players if p.alive]
        dead = [p for p in players if not p.alive]
        seat_name = {s.seat_no: s.display_name for s in seats}

        embed = discord.Embed(title="人狼ゲーム状況", color=0x5865F2)
        embed.add_field(name="フェイズ", value=f"`{game.phase.value}` (day {game.day_number})")
        if remaining is not None:
            embed.add_field(name="残り時間", value=f"{remaining} 秒")
        embed.add_field(
            name="生存者",
            value=", ".join(seat_name[p.seat_no] for p in alive) or "(なし)",
            inline=False,
        )
        embed.add_field(
            name="死亡者",
            value=", ".join(seat_name[p.seat_no] for p in dead) or "(なし)",
            inline=False,
        )
        if pending is not None:
            lines = render_pending_host_lines(pending, seat_name)
            if lines:
                embed.add_field(name="ホスト待ち", value="\n".join(lines), inline=False)
        await interaction.response.send_message(embed=embed)

    # --------------------------------------------------------------- /wolf extend
    @wolf.command(name="extend", description="WAITING_HOST_DECISION 中に締切を延長")
    @app_commands.describe(seconds="延長する秒数 (例: 60)")
    async def extend(self, interaction: discord.Interaction, seconds: int) -> None:
        if seconds <= 0 or seconds > 1800:
            await interaction.response.send_message(
                "seconds は 1〜1800 の範囲で指定してください。", ephemeral=True
            )
            return
        game = await self._host_check(interaction)
        if game is None:
            return
        ok = await self.gs.host_extend(game.id, extra_seconds=seconds)
        if ok:
            await interaction.response.send_message(f"⏱ 締切を {seconds} 秒延長しました。")
        else:
            await interaction.response.send_message(
                "延長できませんでした (WAITING 中ではない可能性)。",
                ephemeral=True,
            )

    # ---------------------------------------------------------- /wolf force-skip
    @wolf.command(name="force-skip", description="未提出を確定処理して進行")
    async def force_skip(self, interaction: discord.Interaction) -> None:
        game = await self._host_check(interaction)
        if game is None:
            return
        ok = await self.gs.host_force_skip(game.id)
        if ok:
            await interaction.response.send_message("⏭ 未提出を確定扱いで進行します。")
        else:
            await interaction.response.send_message(
                "実行できませんでした (WAITING 中ではない可能性)。",
                ephemeral=True,
            )

    # --------------------------------------------------------------- /wolf abort
    @wolf.command(name="abort", description="進行中のゲームを強制終了")
    async def abort(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("ギルド内で実行してください。", ephemeral=True)
            return
        game = await self.repo.load_active_game_for_guild(str(interaction.guild_id))
        if game is None:
            await interaction.response.send_message("進行中のゲームはありません。", ephemeral=True)
            return
        caller = str(interaction.user.id)
        is_host = caller == game.host_user_id
        is_admin = False
        if isinstance(interaction.user, discord.Member):
            is_admin = interaction.user.guild_permissions.administrator
        if not (is_host or is_admin):
            await interaction.response.send_message(
                "ホストまたは管理者のみ abort できます。", ephemeral=True
            )
            return
        ok = await self.gs.host_abort(game.id)
        if ok:
            engine = self.registry.detach(game.id)
            if engine is not None:
                try:
                    await engine.stop()
                except Exception:
                    log.exception("engine.stop failed during abort %s", game.id)
            await interaction.response.send_message("🛑 ゲームを強制終了しました。")
        else:
            await interaction.response.send_message(
                "終了できませんでした (既に終了している可能性があります)。",
                ephemeral=True,
            )

    @wolf.command(
        name="settings",
        description="フェイズ時間などの設定をホスト用 UI で調整",
    )
    async def settings_command(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "ギルド内で実行してください。", ephemeral=True
            )
            return
        game = await self.repo.load_active_game_for_guild(
            str(interaction.guild_id)
        )
        if game is None:
            await interaction.response.send_message(
                "進行中のゲームがありません。`/wolf create` で作成してから設定してください。",
                ephemeral=True,
            )
            return
        if str(interaction.user.id) != game.host_user_id:
            await interaction.response.send_message(
                "設定はホストのみが変更できます。", ephemeral=True
            )
            return
        from wolfbot.ui.settings_view import render_initial_message

        embed, view = render_initial_message(host_user_id=game.host_user_id)
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True
        )

    # ----------------------------------------------------------- internals
    async def _host_check(self, interaction: discord.Interaction) -> Game | None:
        if interaction.guild is None:
            await interaction.response.send_message("ギルド内で実行してください。", ephemeral=True)
            return None
        game = await self.repo.load_active_game_for_guild(str(interaction.guild_id))
        if game is None:
            await interaction.response.send_message("進行中のゲームはありません。", ephemeral=True)
            return None
        if str(interaction.user.id) != game.host_user_id:
            await interaction.response.send_message("ホストのみ実行できます。", ephemeral=True)
            return None
        return game

    async def _preflight_dms(self, human_seats: Sequence[Seat]) -> list[str]:
        """Try to open a DM AND send a probe to each human; report failed display names.

        `create_dm()` alone only opens the channel; Discord privacy rejections
        (Forbidden) surface only on `send()`. Sending a short confirmation here
        guarantees that post-start role/vote/night DMs will actually reach the
        player — otherwise preflight passes but the game locks up waiting on a
        submission that can never arrive.
        """
        failures: list[str] = []
        for seat in human_seats:
            if seat.discord_user_id is None:
                continue
            user = self.bot.get_user(int(seat.discord_user_id))
            if user is None:
                try:
                    user = await self.bot.fetch_user(int(seat.discord_user_id))
                except discord.DiscordException:
                    failures.append(seat.display_name)
                    continue
            try:
                await user.create_dm()
                await user.send("人狼bot DM疎通確認です。まもなく役職をお伝えします。")
            except discord.DiscordException:
                failures.append(seat.display_name)
        return failures

    async def _create_private_channel(
        self,
        guild: discord.Guild,
        name: str,
        *,
        safe_to_delete_ids: set[str],
    ) -> discord.TextChannel | None:
        # Paranoia layer for secrecy: if a same-named channel lingers (previous
        # game's on_game_end failed, or it was created manually), delete it so
        # we never reuse history across games. `safe_to_delete_ids` must match
        # a channel the bot itself created (tracked via heaven/wolves
        # channel_ids in the games table). A same-name match with a foreign ID
        # — e.g. an admin-made channel that happens to collide — is refused:
        # returning None aborts /wolf create so the admin can resolve the
        # collision manually.
        existing = discord.utils.get(guild.text_channels, name=name)
        if existing is not None:
            if str(existing.id) not in safe_to_delete_ids:
                log.error(
                    "refusing to delete %s (id=%s) — not in bot-managed history for guild=%s",
                    name,
                    existing.id,
                    guild.id,
                )
                return None
            try:
                await existing.delete(reason="wolfbot: purge stale private channel from prior game")
            except discord.DiscordException:
                log.exception(
                    "cleanup of stale private channel %s failed — refusing to reuse",
                    getattr(existing, "id", "?"),
                )
                return None
        overwrites: dict[
            discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite
        ] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_messages=True
            ),
        }
        try:
            return await guild.create_text_channel(
                name=name, overwrites=overwrites, reason="wolfbot"
            )
        except discord.DiscordException:
            log.exception("create_private_channel failed name=%s", name)
            return None
