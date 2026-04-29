#!/usr/bin/env python3
"""Generate per-persona NPC env files from a single template + tokens list.

Source files (read, not written):
    tokens.txt                  - "<persona>: <discord-bot-token>" per line
                                  (typo-aliases like "setu" → "setsu" allowed)
    .env.master                 - DISCORD_GUILD_ID / MAIN_VOICE_CHANNEL_ID /
                                  MASTER_NPC_PSK / GAMEPLAY_LLM_API_KEY copied
                                  forward
    envs/npc/.env.npc.example   - {{...}} placeholders substituted per persona
    wolfbot.npc.personas        - NPC_PERSONA_KEY + tts_voice_id authoritative
                                  source

Outputs:
    envs/npc/.env.<persona>     - one file per persona present in tokens.txt

Usage:
    python3 scripts/generate_npc_envs.py
    python3 scripts/generate_npc_envs.py --tokens tokens.txt --master .env.master
    python3 scripts/generate_npc_envs.py --no-overwrite
    python3 scripts/generate_npc_envs.py --dry-run

Failure modes (non-zero exit):
    - tokens.txt or .env.master missing
    - .env.master missing a required shared key
    - tokens.txt names a persona that isn't in NPC_PERSONAS_BY_KEY (after alias)
    - persona has no tts_voice_id (template can't substitute it)
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TOKENS = REPO_ROOT / "tokens.txt"
DEFAULT_MASTER_ENV = REPO_ROOT / ".env.master"
DEFAULT_TEMPLATE = REPO_ROOT / "envs" / "npc" / ".env.npc.example"
DEFAULT_OUT_DIR = REPO_ROOT / "envs" / "npc"

# Common typos in tokens.txt → canonical persona key. Add aliases here as
# they show up; the alias map is intentionally explicit (not fuzzy match)
# so a wrong key fails loudly rather than silently mapping to neighbours.
PERSONA_ALIASES: dict[str, str] = {
    "setu": "setsu",
}

# Master tokens are persona-shaped lines but don't generate a per-persona
# env file. The Master's DISCORD_TOKEN already lives in .env.master.
NON_NPC_KEYS: frozenset[str] = frozenset({"master"})

REQUIRED_MASTER_KEYS: tuple[str, ...] = (
    "DISCORD_GUILD_ID",
    "MAIN_VOICE_CHANNEL_ID",
    "MASTER_NPC_PSK",
    "GAMEPLAY_LLM_API_KEY",
)


@dataclass(frozen=True)
class TokenEntry:
    raw_key: str           # exactly what was in tokens.txt before alias resolution
    persona_key: str       # canonical key after alias resolution
    token: str


def parse_tokens(path: Path) -> list[TokenEntry]:
    """Parse `<persona>: <token>` lines, ignore comments / blanks."""
    if not path.exists():
        sys.exit(f"ERROR: tokens file not found: {path}")
    entries: list[TokenEntry] = []
    seen_raw_keys: set[str] = set()
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            sys.exit(f"ERROR: {path}:{lineno}: expected '<key>: <token>', got {line!r}")
        key_part, token_part = line.split(":", 1)
        key = key_part.strip().lower()
        token = token_part.strip()
        if not key or not token:
            sys.exit(f"ERROR: {path}:{lineno}: empty key or token")
        if key in seen_raw_keys:
            sys.exit(f"ERROR: {path}:{lineno}: duplicate entry for key {key!r}")
        seen_raw_keys.add(key)
        canonical = PERSONA_ALIASES.get(key, key)
        entries.append(TokenEntry(raw_key=key, persona_key=canonical, token=token))
    return entries


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal `.env` reader: KEY=VALUE per line, ignore comments / blanks.

    Strips matching surrounding single or double quotes. Does not handle
    multi-line values or shell expansion (we don't need those for our envs).
    """
    if not path.exists():
        sys.exit(f"ERROR: env file not found: {path}")
    out: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            sys.exit(f"ERROR: {path}:{lineno}: expected 'KEY=VALUE', got {line!r}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        out[key] = value
    return out


def load_npc_personas() -> dict[str, object]:
    """Late import so this script remains stdlib-only at import time."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from wolfbot.npc.personas import NPC_PERSONAS_BY_KEY
    except ImportError as exc:
        sys.exit(f"ERROR: failed to import NPC_PERSONAS_BY_KEY: {exc}")
    return NPC_PERSONAS_BY_KEY  # type: ignore[no-any-return]


_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_][A-Z0-9_]*)\}\}")


def render_template(template: str, values: dict[str, str]) -> str:
    """Substitute `{{KEY}}` placeholders. Fail fast on missing keys."""
    missing: list[str] = []

    def _repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            missing.append(key)
            return match.group(0)
        return values[key]

    out = _PLACEHOLDER_RE.sub(_repl, template)
    if missing:
        unique = sorted(set(missing))
        sys.exit(f"ERROR: template referenced unknown placeholder(s): {unique}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate envs/npc/.env.<persona> files from a single template.",
    )
    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(REPO_ROOT))
        except ValueError:
            return str(p)

    parser.add_argument("--tokens", type=Path, default=DEFAULT_TOKENS,
                        help=f"path to tokens file (default: {_rel(DEFAULT_TOKENS)})")
    parser.add_argument("--master", type=Path, default=DEFAULT_MASTER_ENV,
                        help=f"path to .env.master (default: {_rel(DEFAULT_MASTER_ENV)})")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE,
                        help=f"path to template (default: {_rel(DEFAULT_TEMPLATE)})")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                        help=f"output dir (default: {_rel(DEFAULT_OUT_DIR)})")
    parser.add_argument("--no-overwrite", action="store_true",
                        help="refuse to overwrite existing per-persona env files")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be written but don't touch the filesystem")
    args = parser.parse_args()

    tokens = parse_tokens(args.tokens)
    master_env = parse_env_file(args.master)
    template = args.template.read_text() if args.template.exists() else None
    if template is None:
        sys.exit(f"ERROR: template not found: {args.template}")

    missing_master_keys = [k for k in REQUIRED_MASTER_KEYS if not master_env.get(k)]
    if missing_master_keys:
        sys.exit(
            f"ERROR: {args.master} is missing or has empty value for: "
            f"{', '.join(missing_master_keys)}"
        )

    npc_personas = load_npc_personas()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    skipped: list[str] = []
    aliased: list[tuple[str, str]] = []
    skipped_master: list[str] = []

    for entry in tokens:
        if entry.raw_key in NON_NPC_KEYS:
            skipped_master.append(entry.raw_key)
            continue
        if entry.raw_key != entry.persona_key:
            aliased.append((entry.raw_key, entry.persona_key))
        persona = npc_personas.get(entry.persona_key)
        if persona is None:
            valid = ", ".join(sorted(npc_personas.keys()))
            sys.exit(
                f"ERROR: tokens.txt references persona {entry.raw_key!r} "
                f"(canonical {entry.persona_key!r}) which is not in NPC_PERSONAS_BY_KEY.\n"
                f"       Valid keys: {valid}"
            )
        tts_voice_id = getattr(persona, "tts_voice_id", None)
        if tts_voice_id is None:
            sys.exit(
                f"ERROR: persona {entry.persona_key!r} has no tts_voice_id set; "
                f"add one to wolfbot.npc.personas.NPC_PERSONAS."
            )

        out_path = args.out_dir / f".env.{entry.persona_key}"
        if out_path.exists() and args.no_overwrite:
            skipped.append(entry.persona_key)
            continue

        substitutions = {
            "NPC_ID": f"npc_{entry.persona_key}",
            "NPC_DISCORD_TOKEN": entry.token,
            "NPC_PERSONA_KEY": entry.persona_key,
            "DISCORD_GUILD_ID": master_env["DISCORD_GUILD_ID"],
            "MAIN_VOICE_CHANNEL_ID": master_env["MAIN_VOICE_CHANNEL_ID"],
            "MASTER_NPC_PSK": master_env["MASTER_NPC_PSK"],
            "NPC_LLM_API_KEY": master_env["GAMEPLAY_LLM_API_KEY"],
            "TTS_VOICE_ID": str(tts_voice_id),
        }
        rendered = render_template(template, substitutions)

        if args.dry_run:
            print(f"[dry-run] would write {_rel(out_path)}")
        else:
            out_path.write_text(rendered)
            written.append(entry.persona_key)

    # Summary
    print()
    if aliased:
        for raw, canonical in aliased:
            print(f"note: tokens.txt key {raw!r} aliased to canonical {canonical!r}")
    if skipped_master:
        print(f"note: skipped non-NPC entries from tokens.txt: {', '.join(skipped_master)} "
              f"(Master token belongs in .env.master, not envs/npc/)")
    if args.dry_run:
        print(f"\ndry-run complete. {len(tokens) - len(skipped_master)} files would be written.")
    else:
        if skipped:
            print(f"skipped (already exist, --no-overwrite): {', '.join(skipped)}")
        print(f"\n✅ wrote {len(written)} per-persona env file(s) to {_rel(args.out_dir)}/")
        for persona in written:
            print(f"   - .env.{persona}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
