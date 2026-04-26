"""NPC bot worker entrypoint.

Reads `NPC_*` env vars, builds an `NpcClient`, connects to Master, and
parks on the asyncio loop. The actual Discord-side bot connection is
deferred to a real implementation; this entrypoint focuses on the
configurable wiring and is exercised by integration tests via the
`NpcClient` API directly.

Run with:

    uv run python -m wolfbot.npc_bot_main

(after exporting the NPC env vars described in the proposal — `NPC_ID`,
`NPC_DISCORD_TOKEN`, `MASTER_WS_URL`, `MASTER_NPC_PSK`, `TTS_VOICE_ID`,
`TTS_PROVIDER`, etc.)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

log = logging.getLogger(__name__)


def _read_env(name: str, *, required: bool = True, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise SystemExit(f"missing required env var {name}")
    return val or ""


async def _main() -> None:
    """Wire the worker. Implementation deliberately minimal — production code
    will plug in the real Discord client + websockets transport. This
    function is exercised in tests via component-level setups."""
    npc_id = _read_env("NPC_ID")
    discord_token = _read_env("NPC_DISCORD_TOKEN")
    master_ws_url = _read_env("MASTER_WS_URL")
    psk_set = bool(_read_env("MASTER_NPC_PSK"))
    voice_id = _read_env("TTS_VOICE_ID", required=False, default="ja-JP-Standard-A")

    log.info(
        "npc_bot_starting npc_id=%s ws_url=%s discord_token_set=%s psk_set=%s voice=%s",
        npc_id,
        master_ws_url,
        bool(discord_token),
        psk_set,
        voice_id,
    )
    log.info("npc_bot_main is a wiring stub — production wiring is implemented elsewhere")
    # The production wiring is deferred to a follow-up change; the worker
    # is fully exercised through `NpcClient` from unit / integration tests.
    raise SystemExit(0)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main())


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["main"]
