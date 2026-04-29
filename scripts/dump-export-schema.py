"""Dump the JSON Schema of the viewer export contract.

Writes the JSON Schema produced by
:class:`wolfbot.services.game_export_types.GameExport` to
``viewer/sample-data/export-schema.json``. The viewer contract test
loads this file and validates every committed export against it.

Run after touching ``game_export_types.py``::

    uv run python scripts/dump-export-schema.py

A drift check (``tests/test_game_export_integration.py``) asserts the
committed file equals the freshly emitted schema, so CI fails if you
forget this step.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root / "src"))

    from wolfbot.services.game_export_types import GameExport

    schema = GameExport.model_json_schema()
    target = repo_root / "viewer" / "sample-data" / "export-schema.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {target.relative_to(repo_root)}")


if __name__ == "__main__":
    main()
