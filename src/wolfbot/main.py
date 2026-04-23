"""Entrypoint. Load env → migrate DB → connect Discord → recover → run."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import discord
from discord.ext import commands
from dotenv import load_dotenv

from wolfbot.config import Settings
from wolfbot.persistence.schema import migrate
from wolfbot.persistence.sqlite_repo import SqliteRepo
from wolfbot.services.discord_service import DiscordBotAdapter, WolfCog
from wolfbot.services.game_service import GameService
from wolfbot.services.llm_service import LLMAdapter, make_xai_decider
from wolfbot.services.recovery_service import RecoveryService
from wolfbot.services.timer_service import EngineRegistry

log = logging.getLogger("wolfbot")


async def _run() -> None:
    load_dotenv()
    settings = Settings()  # type: ignore[call-arg]
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    await migrate(settings.WOLFBOT_DB_PATH)
    repo = SqliteRepo(settings.WOLFBOT_DB_PATH)
    await repo.connect()

    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    registry = EngineRegistry()
    discord_adapter = DiscordBotAdapter(bot=bot, repo=repo, settings=settings)
    decider = make_xai_decider(
        api_key=settings.XAI_API_KEY.get_secret_value(),
        model=settings.XAI_MODEL,
    )
    llm_adapter = LLMAdapter(repo=repo, decider=decider, message_poster=discord_adapter)
    game_service = GameService(repo=repo, discord=discord_adapter, llm=llm_adapter, wake=registry)
    discord_adapter.set_game_service(game_service)
    llm_adapter.set_game_service(game_service)

    cog = WolfCog(
        bot=bot,
        repo=repo,
        game_service=game_service,
        discord_adapter=discord_adapter,
        llm_adapter=llm_adapter,
        registry=registry,
        settings=settings,
    )
    await bot.add_cog(cog)
    bot.tree.add_command(cog.wolf, guild=discord.Object(id=settings.DISCORD_GUILD_ID))

    recovery = RecoveryService(
        repo=repo,
        game_service=game_service,
        registry=registry,
        discord=discord_adapter,
    )
    recovery_done = asyncio.Event()

    @bot.event
    async def on_ready() -> None:
        guild = discord.Object(id=settings.DISCORD_GUILD_ID)
        await bot.tree.sync(guild=guild)
        log.info("synced slash commands to guild %s", settings.DISCORD_GUILD_ID)
        # on_ready re-fires on reconnect. Engines started on the first ready keep
        # ticking locally across reconnects, so re-running recovery would only
        # duplicate them. Run it once per process.
        if recovery_done.is_set():
            return
        recovery_done.set()
        recovered = await recovery.recover_all()
        log.info("recovered %d game(s)", len(recovered))

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_sigterm() -> None:
        log.info("shutdown requested")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _handle_sigterm)

    bot_task = asyncio.create_task(
        bot.start(settings.DISCORD_TOKEN.get_secret_value()),
        name="wolfbot-main",
    )
    await stop_event.wait()

    log.info("stopping engines...")
    await registry.stop_all()
    log.info("closing bot...")
    await bot.close()
    bot_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await bot_task
    await repo.close()
    log.info("bye")


def cli() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    cli()
