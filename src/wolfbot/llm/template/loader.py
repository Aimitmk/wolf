"""Locate and read .md template files under ``prompts/templates``.

Public API:

* :data:`TEMPLATES_ROOT` — the on-disk directory used as the search
  base. Tests can monkeypatch this to point at a fixture dir.
* :func:`load_template` — given a slash-separated id (``"master/task_vote"``
  → ``master/task_vote.md``), return the raw template body as a string.
  Caches by template id so repeated calls during one process share the
  same string. Raises :class:`TemplateNotFoundError` when the file
  doesn't exist.

The loader is deliberately minimal — it does not parse / render. That
job lives in :mod:`wolfbot.llm.template.parser` so unit tests can cover
parsing without touching the filesystem.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

TEMPLATES_ROOT: Path = (
    Path(__file__).resolve().parent.parent.parent / "prompts" / "templates"
)
"""Filesystem directory containing the .md templates.

Resolved relative to this module so the layout works regardless of
where the project is installed (editable, wheel, frozen). All template
ids are interpreted relative to this root.
"""


class TemplateNotFoundError(FileNotFoundError):
    """Raised when :func:`load_template` is asked for a missing id.

    Inherits from ``FileNotFoundError`` so callers that just want
    "treat as missing file" can keep their existing handlers; the
    distinct subclass name surfaces in tracebacks where the difference
    matters (template-id typo vs unrelated I/O failure).
    """


def _resolve(template_id: str) -> Path:
    """Map ``"master/task_vote"`` → ``<TEMPLATES_ROOT>/master/task_vote.md``.

    Refuses absolute paths and ``..`` components defensively — template
    ids must stay inside the templates root.
    """
    if not template_id:
        msg = "template id must be non-empty"
        raise ValueError(msg)
    parts = template_id.split("/")
    for part in parts:
        if not part or part in (".", ".."):
            msg = f"illegal template id segment in {template_id!r}"
            raise ValueError(msg)
    return TEMPLATES_ROOT.joinpath(*parts).with_suffix(".md")


@cache
def load_template(template_id: str) -> str:
    """Read a template file by id.

    The id is the path under :data:`TEMPLATES_ROOT` without the ``.md``
    extension, using forward slashes (``"npc/decision_vote_user"``).

    LRU-cached unbounded — templates are tiny and bounded in count, so
    one cache miss per id per process is fine. Tests that need a fresh
    read should call :func:`load_template.cache_clear`.
    """
    path = _resolve(template_id)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        msg = (
            f"template not found: {template_id!r} (resolved to {path})"
        )
        raise TemplateNotFoundError(msg) from exc


__all__ = ["TEMPLATES_ROOT", "TemplateNotFoundError", "load_template"]
