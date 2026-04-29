"""CLI: export one finished/aborted game to viewer-compatible JSON.

Usage::

    uv run python scripts/export-game.py --game-id g_abc123def
    uv run python scripts/export-game.py --game-id g_abc123def \\
        --db ./wolfbot.db --output viewer/games/

The same export is also kicked off automatically by GameService at the
end of every game (victory or host_abort) — this CLI is for re-exporting
historical games without replaying them.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--game-id",
        required=True,
        help="Game id to export (the 12-char hex id printed by /wolf start).",
    )
    p.add_argument(
        "--db",
        default=os.environ.get("WOLFBOT_DB_PATH", "./wolfbot.db"),
        help="SQLite path. Defaults to $WOLFBOT_DB_PATH or ./wolfbot.db.",
    )
    p.add_argument(
        "--trace-dir",
        default=os.environ.get("WOLFBOT_LLM_TRACE_DIR", "logs/llm_calls"),
        help="LLM trace dir. Defaults to $WOLFBOT_LLM_TRACE_DIR or logs/llm_calls.",
    )
    p.add_argument(
        "--output",
        default="viewer/games",
        help="Output directory. The file is written as {output}/{game_id}.json.",
    )
    return p.parse_args()


async def _amain() -> int:
    # Make `from wolfbot.services.game_export import export_game` work when
    # this script is invoked directly without the package being installed.
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root / "src"))

    from wolfbot.services.game_export import export_game

    args = _parse_args()
    out_path = await export_game(
        game_id=args.game_id,
        db_path=args.db,
        trace_dir=args.trace_dir,
        output_dir=args.output,
    )
    print(f"exported {args.game_id} -> {out_path}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
