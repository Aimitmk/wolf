"""Markdown prompt template engine — load + render with placeholder substitution.

Templates live under :mod:`wolfbot.prompts.templates` (file-system path
``src/wolfbot/prompts/templates``) and are referenced by a
slash-separated id like ``"master/task_vote"`` (no ``.md`` suffix).

Two responsibilities split into two modules:

* :func:`load_template` (``loader``) — find the template on disk, return
  the raw string. LRU-cached so prompt files are read at most once per
  process.
* :func:`render` (``parser``) — substitute ``{{var}}`` placeholders and
  evaluate ``{{#if var}}...{{/if}}`` conditional blocks against a
  caller-supplied context dict.

The two-pieces split lets unit tests cover the parser in isolation
(no I/O) while the loader handles only the FS shape. Callers usually
chain them via :func:`render_template`.

Why a tiny in-house engine rather than Jinja2 / Mustache:
- The placeholder syntax we need is intentionally minimal — no loops,
  no filters, no auto-escaping. Anything more complex stays in Python.
- Zero new dependency for a 9-player Werewolf bot.
- Predictable error surface: a missing variable raises immediately
  with the placeholder name, instead of silently rendering as empty.
"""

from __future__ import annotations

from wolfbot.llm.template.loader import (
    TEMPLATES_ROOT,
    TemplateNotFoundError,
    load_template,
)
from wolfbot.llm.template.parser import (
    TemplateRenderError,
    render,
    render_template,
)

__all__ = [
    "TEMPLATES_ROOT",
    "TemplateNotFoundError",
    "TemplateRenderError",
    "load_template",
    "render",
    "render_template",
]
