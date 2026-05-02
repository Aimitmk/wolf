"""Render a markdown template by substituting placeholders.

Syntax:

* ``{{name}}`` — simple substitution. ``name`` is a snake-case
  identifier that must exist in the supplied context. Missing keys
  raise :class:`TemplateRenderError` immediately (fail-fast); rendering
  to an empty string would silently corrupt the prompt.
* ``{{#if name}}...{{/if}}`` — conditional block. The body is included
  iff ``context[name]`` is truthy (``bool(value)``). Nesting is
  supported. Unknown ``name`` is treated as falsy (block omitted) —
  this matches how callers typically phrase optional sections without
  needing a separate "is the variable defined" check.

Deliberately not supported:
* loops (``{{#each}}``) — rendered lists belong in Python where the
  separator / formatting can be tested per item.
* filters / auto-escape — prompt text is plain markdown bound for an
  LLM, not HTML.
* nested attribute access (``{{persona.name}}``) — flatten the context
  dict at the call site instead.

If the syntax above is ever insufficient, escalate to Jinja2 — don't
extend this module ad-hoc.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from wolfbot.llm.template.loader import load_template

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
# Tempered-greedy body: match anything that does NOT start a nested
# ``{{#if`` opener, so a single regex pass picks up the *innermost* if
# blocks first. The caller loop expands repeatedly until no openers
# remain — by that point each outer block has had its inner blocks
# replaced, so the now-flat outer becomes innermost on the next pass.
# Without this, ``S{{#if A}}-{{#if B}}I{{/if}}-{{/if}}E`` would match
# ``{{#if A}}-{{#if B}}I{{/if}}`` greedily on the first pass and drop
# the trailing ``-{{/if}}E`` outside the regex.
_IF_BLOCK_RE = re.compile(
    r"\{\{#if\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}"
    r"(?P<body>(?:(?!\{\{#if\b).)*?)"
    r"\{\{/if\}\}",
    re.DOTALL,
)


class TemplateRenderError(KeyError):
    """A required ``{{var}}`` had no entry in the render context.

    Inherits from ``KeyError`` so callers that intentionally want to
    catch "missing variable" can use the broader builtin; the distinct
    subclass name carries the template id for log triage.
    """

    def __init__(self, variable: str, *, template_id: str | None = None) -> None:
        suffix = f" (template {template_id!r})" if template_id else ""
        super().__init__(
            f"missing required template variable {variable!r}{suffix}"
        )
        self.variable = variable
        self.template_id = template_id


def _expand_if_blocks(text: str, context: Mapping[str, Any]) -> str:
    """Resolve ``{{#if var}}...{{/if}}`` repeatedly until none remain.

    Inner-first expansion supports nesting because :data:`_IF_BLOCK_RE`
    is non-greedy on ``body`` and the loop walks until the regex finds
    no more matches. Without the loop, an outer block whose body
    contains an inner ``{{/if}}`` would be matched as one greedy span.
    """
    while True:
        match = _IF_BLOCK_RE.search(text)
        if match is None:
            return text
        var = match.group(1)
        body = match.group("body")
        keep = bool(context.get(var))
        replacement = body if keep else ""
        text = text[: match.start()] + replacement + text[match.end() :]


def _expand_placeholders(
    text: str,
    context: Mapping[str, Any],
    *,
    template_id: str | None,
) -> str:
    """Replace every ``{{var}}`` with ``str(context[var])``.

    Missing keys raise :class:`TemplateRenderError` immediately so a
    typo never silently produces a half-rendered prompt.
    """

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in context:
            raise TemplateRenderError(name, template_id=template_id)
        return str(context[name])

    return _PLACEHOLDER_RE.sub(_sub, text)


def render(
    template_text: str,
    /,
    *,
    template_id: str | None = None,
    **context: Any,
) -> str:
    """Render a raw template string against ``**context``.

    Order: ``{{#if}}`` blocks first, then ``{{var}}`` substitution.
    Reverse order would force callers to escape placeholders inside an
    ``{{#if}}`` body that should also reference variables.

    ``template_id`` is purely for error messages — pass it when the
    text was loaded from disk so :class:`TemplateRenderError` includes
    the id in its message.
    """
    expanded = _expand_if_blocks(template_text, context)
    return _expand_placeholders(expanded, context, template_id=template_id)


def render_template(template_id: str, /, **context: Any) -> str:
    """Convenience: load a template by id and render it in one call."""
    return render(
        load_template(template_id), template_id=template_id, **context
    )


__all__ = [
    "TemplateRenderError",
    "render",
    "render_template",
]
