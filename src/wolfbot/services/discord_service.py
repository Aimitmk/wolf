"""Discord I/O layer.

Two classes live here:

  - `DiscordBotAdapter` implements the `DiscordAdapter` + `RecoveryDiscordAdapter`
    protocols consumed by `game_service` and `recovery_service`. It owns channel
    posting, DM sending, permission delegation, and status announcements.

  - `WolfCog` is the slash-command surface. It handles `/wolf create / join / leave
    / start / status / extend / force-skip / abort` and dispatches to game_service.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from random import Random

import discord
from discord import app_commands
from discord.ext import commands

from wolfbot.config import Settings
from wolfbot.domain.enums import (
    Phase,
    Role,
    SubmissionType,
)
from wolfbot.domain.errors import ActiveGameExistsError
from wolfbot.domain.models import Game, PendingDecision, Player, Seat
from wolfbot.domain.rules import legal_attack_targets, legal_divine_targets, legal_guard_targets
from wolfbot.llm.personas import pick_personas
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.game_service import GameService, new_game_id
from wolfbot.services.llm_service import LLMAdapter
from wolfbot.services.permission_manager import PermissionManager
from wolfbot.services.timer_service import EngineRegistry, GameEngine
from wolfbot.ui.views import NightActionView, VoteView

log = logging.getLogger(__name__)


# --------------------------------------------------------------- DiscordBotAdapter
class DiscordBotAdapter:
    """Implements the DiscordAdapter protocol by operating on a live discord.Client."""

    def __init__(
        self,
        bot: discord.Client,
        repo: SqliteRepo,
        settings: Settings,
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
        self, game: Game, players: Sequence[Player], seats: Sequence[Seat]
    ) -> None:
        seats_by_no = {s.seat_no: s for s in seats}
        prev = await self.repo.load_previous_guard(game.id)
        prev_guard_seat = prev[1] if prev else None

        for p in players:
            if p.role is None:
                continue
            seat = seats_by_no.get(p.seat_no)
            if seat is None or seat.is_llm or seat.discord_user_id is None:
                continue
            kind = None
            candidates: list[Seat] = []
            if p.role is Role.WEREWOLF:
                kind = SubmissionType.WOLF_ATTACK
                legal = legal_attack_targets(players, p.seat_no)
                candidates = [seats_by_no[sn] for sn in legal if sn in seats_by_no]
            elif p.role is Role.SEER:
                kind = SubmissionType.SEER_DIVINE
                legal = legal_divine_targets(players, p.seat_no)
                candidates = [seats_by_no[sn] for sn in legal if sn in seats_by_no]
            elif p.role is Role.KNIGHT:
                kind = SubmissionType.KNIGHT_GUARD
                legal = legal_guard_targets(players, p.seat_no, prev_guard_seat)
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
        lines = [
            "⏸ **ホスト待ち**",
            f"フェイズ: `{pending.phase.value}` (day {pending.day})",
        ]
        for sub in pending.effective_submissions():
            if sub.missing_seats:
                names = [
                    seats_by_no[sn].display_name for sn in sub.missing_seats if sn in seats_by_no
                ]
                lines.append(
                    f"`{sub.submission_type.value}` 未提出: {'、'.join(names) or '(なし)'}"
                )
            if sub.unresolved_seats:
                names = [
                    seats_by_no[sn].display_name for sn in sub.unresolved_seats if sn in seats_by_no
                ]
                lines.append(
                    f"`{sub.submission_type.value}` 再提出待ち(意見が割れました): "
                    f"{'、'.join(names) or '(なし)'}"
                )
        lines.append(
            "`/wolf extend <秒>` で延長、または `/wolf force-skip` で未提出を確定処理します。"
        )
        text = "\n".join(lines)
        channel = self._main_text(game)
        if channel is not None:
            try:
                await channel.send(text)
            except discord.DiscordException:
                log.exception("announce_waiting failed %s", game.id)

    async def announce_recovery(self, game: Game, pending: PendingDecision | None) -> None:
        channel = self._main_text(game)
        if channel is None:
            return
        lines = [f"♻️ 復帰しました。現在フェイズ: `{game.phase.value}` / day {game.day_number}"]
        if pending:
            for sub in pending.effective_submissions():
                count = len(set(sub.missing_seats) | set(sub.unresolved_seats))
                lines.append(f"未提出あり: `{sub.submission_type.value}` → {count} 件未確定")
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
        settings: Settings,
        rng: Random | None = None,
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

    # ----------------------------------------------------------- on_message
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:
            return
        game = await self.repo.load_active_game_for_guild(str(message.guild.id))
        if game is None:
            return
        if str(message.channel.id) != game.main_text_channel_id:
            return
        if game.phase is not Phase.DAY_DISCUSSION:
            return
        author_seat = await self.repo.seat_of_user(game.id, str(message.author.id))
        players = await self.repo.load_players(game.id)
        seats = await self.repo.load_seats(game.id)
        try:
            await self.llm_adapter.maybe_react_to_message(
                game,
                players,
                seats,
                author_seat=author_seat,
                text=message.content,
            )
        except Exception:
            log.exception("llm_adapter reaction failed for %s", game.id)

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

        heaven = await self._create_private_channel(interaction.guild, name="wolf-heaven")
        wolves = await self._create_private_channel(interaction.guild, name="wolf-wolves")
        if heaven is None or wolves is None:
            await interaction.followup.send(
                "チャンネル作成に失敗しました。Bot の権限を確認してください。"
            )
            return

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
        )
        try:
            await self.repo.create_game(game)
        except ActiveGameExistsError:
            # Concurrent /wolf create won the race; drop the channels we just made.
            for ch in (heaven, wolves):
                try:
                    await ch.delete(reason="wolfbot: duplicate /wolf create race")
                except discord.DiscordException:
                    log.exception("cleanup of %s failed", ch.id)
            winner = await self.repo.load_active_game_for_guild(guild_id)
            winner_id = winner.id if winner else "?"
            await interaction.followup.send(f"既に進行中のゲームがあります (id: `{winner_id}`)。")
            return
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
        if game is None or game.phase is not Phase.LOBBY:
            await interaction.response.send_message(
                "ロビー中のゲームがありません。", ephemeral=True
            )
            return
        user_id = str(interaction.user.id)
        existing_seat = await self.repo.seat_of_user(game.id, user_id)
        if existing_seat is not None:
            await interaction.response.send_message("既に参加しています。", ephemeral=True)
            return
        seats = await self.repo.load_seats(game.id)
        human_seats = [s for s in seats if not s.is_llm]
        if len(human_seats) >= 9:
            await interaction.response.send_message(
                "人数が 9 に達しているので参加できません。", ephemeral=True
            )
            return
        next_seat_no = _next_seat_no(seats)
        seat = Seat(
            seat_no=next_seat_no,
            display_name=interaction.user.display_name,
            discord_user_id=user_id,
            is_llm=False,
            persona_key=None,
        )
        await self.repo.insert_seat(game.id, seat)
        await interaction.response.send_message(
            f"✅ {interaction.user.display_name} が座席 {next_seat_no} に着席しました。"
        )

    # --------------------------------------------------------------- /wolf leave
    @wolf.command(name="leave", description="ロビー中のみ退出")
    async def leave(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("ギルド内で実行してください。", ephemeral=True)
            return
        game = await self.repo.load_active_game_for_guild(str(interaction.guild_id))
        if game is None or game.phase is not Phase.LOBBY:
            await interaction.response.send_message(
                "ロビー中のゲームがありません。", ephemeral=True
            )
            return
        user_id = str(interaction.user.id)
        seat_no = await self.repo.seat_of_user(game.id, user_id)
        if seat_no is None:
            await interaction.response.send_message("参加していません。", ephemeral=True)
            return
        await self.repo.delete_seat(game.id, seat_no)
        await interaction.response.send_message(
            f"👋 {interaction.user.display_name} が退出しました。"
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
            await _backfill_llm_seats(self.repo, game.id, shortfall, self.rng)

        # Move to SETUP; engine picks it up
        from wolfbot.domain.models import Transition

        ok = await self.repo.apply_transition(
            game.id,
            Transition(next_phase=Phase.SETUP, next_day=0, new_deadline_epoch=None),
            expected_phase=Phase.LOBBY,
        )
        if not ok:
            await interaction.followup.send(
                "開始処理でラップ競合が発生しました。もう一度お試しください。"
            )
            return

        engine = GameEngine(game_id=game.id, repo=self.repo, advance=self.gs.advance)
        await self.registry.attach(engine)
        engine.start()

        await interaction.followup.send(
            f"🎮 ゲーム開始。参加者: {len(humans)} 人 + LLM {shortfall} 人。"
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
            lines: list[str] = []
            for sub in pending.effective_submissions():
                missing = ", ".join(seat_name.get(sn, str(sn)) for sn in sub.missing_seats)
                lines.append(f"`{sub.submission_type.value}`: {missing if missing else '(なし)'}")
            embed.add_field(
                name="未提出",
                value="\n".join(lines),
                inline=False,
            )
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
        await self.gs.host_abort(game.id)
        self.registry.detach(game.id)
        await interaction.response.send_message("🛑 ゲームを強制終了しました。")

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
        """Try to open a DM to each human; report display names that failed."""
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
            except discord.DiscordException:
                failures.append(seat.display_name)
        return failures

    async def _create_private_channel(
        self, guild: discord.Guild, name: str
    ) -> discord.TextChannel | None:
        # Paranoia layer for secrecy: if a same-named channel lingers (previous
        # game's on_game_end failed, or it was created manually), delete it so we
        # never reuse history across games. Refuse to fall back to reuse on
        # failure — secrecy trumps availability.
        existing = discord.utils.get(guild.text_channels, name=name)
        if existing is not None:
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


def _next_seat_no(existing: Sequence[Seat]) -> int:
    used = {s.seat_no for s in existing}
    for i in range(1, 10):
        if i not in used:
            return i
    return 9


async def _backfill_llm_seats(
    repo: SqliteRepo,
    game_id: str,
    shortfall: int,
    rng: Random,
) -> None:
    """Insert `shortfall` LLM seats into the game using the next free seat numbers.

    Factored out of /wolf start so the seat-number assignment can be unit tested
    without a Discord interaction. Re-reads seats between inserts so the next
    free seat (1..9) is always correct even when starting from a partial lineup.
    """
    if shortfall <= 0:
        return
    picks = pick_personas(shortfall, rng)
    seats = await repo.load_seats(game_id)
    for persona in picks:
        seat_no = _next_seat_no(seats)
        llm_seat = Seat(
            seat_no=seat_no,
            display_name=persona.display_name,
            discord_user_id=None,
            is_llm=True,
            persona_key=persona.key,
        )
        await repo.insert_seat(game_id, llm_seat)
        await repo.insert_persona_assignment(game_id, seat_no, persona.key)
        seats = await repo.load_seats(game_id)
