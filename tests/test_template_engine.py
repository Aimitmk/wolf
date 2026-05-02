"""Unit tests for the markdown prompt template engine.

Covers two-piece API:
* :func:`load_template` (loader): id → file path resolution, missing-id
  error, and that the cache returns the same string for repeated reads.
* :func:`render` (parser): placeholder substitution, nested ``{{#if}}``
  conditional blocks, fail-fast on missing variables.
* :func:`render_template`: the convenience load+render integration.

The parser tests deliberately operate on raw strings (no I/O) so a FS
mock isn't needed; only the loader-level tests touch tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wolfbot.llm import template as tmpl
from wolfbot.llm.template import (
    TemplateNotFoundError,
    TemplateRenderError,
    load_template,
    render,
    render_template,
)
from wolfbot.llm.template import loader as loader_module

# -------------------------------------------------------- parser tests


def test_render_simple_placeholder() -> None:
    out = render("Hello, {{name}}!", name="Sakura")
    assert out == "Hello, Sakura!"


def test_render_multiple_placeholders_and_repeats() -> None:
    out = render(
        "{{role}} は {{day}} 日目に発言。{{role}} は再度言及。",
        role="占い師",
        day=2,
    )
    assert out == "占い師 は 2 日目に発言。占い師 は再度言及。"


def test_render_missing_variable_raises_with_var_name() -> None:
    with pytest.raises(TemplateRenderError) as exc_info:
        render("Hi {{missing}}!", existing="ok")
    assert exc_info.value.variable == "missing"
    assert "missing" in str(exc_info.value)


def test_render_template_id_surfaces_in_error_message() -> None:
    with pytest.raises(TemplateRenderError) as exc_info:
        render(
            "Hi {{missing}}!",
            template_id="master/example",
            existing="ok",
        )
    assert "master/example" in str(exc_info.value)
    assert exc_info.value.template_id == "master/example"


def test_render_if_block_truthy_keeps_body() -> None:
    out = render(
        "頭 {{#if extra}}追加: {{detail}}{{/if}} 末尾",
        extra=True,
        detail="ABC",
    )
    assert out == "頭 追加: ABC 末尾"


def test_render_if_block_falsy_omits_body() -> None:
    out = render(
        "頭 {{#if extra}}非表示{{/if}} 末尾",
        extra=False,
    )
    assert out == "頭  末尾"


def test_render_if_block_unknown_var_treated_as_falsy() -> None:
    """A missing context key for an `{{#if}}` block is treated as
    falsy rather than raising — this matches the documented behaviour
    so callers can phrase optional sections without first checking
    that the variable is even defined."""
    out = render(
        "前 {{#if maybe}}HIDE{{/if}} 後",
    )
    assert out == "前  後"


def test_render_nested_if_blocks() -> None:
    text = (
        "S{{#if outer}}-O[{{#if inner}}I{{/if}}]-{{/if}}E"
    )
    assert render(text, outer=True, inner=True) == "S-O[I]-E"
    assert render(text, outer=True, inner=False) == "S-O[]-E"
    # Outer falsy → body (including the inner block) is dropped wholesale.
    assert render(text, outer=False, inner=True) == "SE"


def test_render_if_body_can_reference_placeholder() -> None:
    out = render(
        "{{#if active}}seat={{seat}}{{/if}}",
        active=True,
        seat=3,
    )
    assert out == "seat=3"


def test_render_substitutes_non_string_via_str_cast() -> None:
    out = render("count={{n}}", n=42)
    assert out == "count=42"


# -------------------------------------------------------- loader tests


@pytest.fixture
def temp_templates_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the loader at a clean tmp dir for the duration of the test."""
    root = tmp_path / "templates"
    (root / "master").mkdir(parents=True)
    (root / "master" / "hello.md").write_text(
        "Hi, {{name}}!", encoding="utf-8"
    )
    monkeypatch.setattr(loader_module, "TEMPLATES_ROOT", root)
    monkeypatch.setattr(tmpl, "TEMPLATES_ROOT", root, raising=False)
    # Cache is module-global; clear so the new root is honoured.
    load_template.cache_clear()
    yield root
    load_template.cache_clear()


def test_load_template_reads_file_under_root(temp_templates_root: Path) -> None:
    body = load_template("master/hello")
    assert body == "Hi, {{name}}!"


def test_load_template_caches_repeat_reads(temp_templates_root: Path) -> None:
    """Two reads of the same id should return identical strings without
    a second filesystem hit. Probe by mutating the file after the first
    read — the cached value must NOT pick up the change."""
    first = load_template("master/hello")
    (temp_templates_root / "master" / "hello.md").write_text(
        "Different!", encoding="utf-8"
    )
    second = load_template("master/hello")
    assert first == second == "Hi, {{name}}!"


def test_load_template_missing_id_raises(temp_templates_root: Path) -> None:
    with pytest.raises(TemplateNotFoundError):
        load_template("master/does_not_exist")


def test_load_template_rejects_traversal(temp_templates_root: Path) -> None:
    """`..` segments must be refused so a typo can't escape the
    templates root."""
    with pytest.raises(ValueError, match="illegal template id segment"):
        load_template("master/../escape")


def test_load_template_rejects_empty_id(temp_templates_root: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        load_template("")


def test_render_template_loads_and_substitutes(temp_templates_root: Path) -> None:
    out = render_template("master/hello", name="セツ")
    assert out == "Hi, セツ!"


def test_render_template_propagates_missing_variable(
    temp_templates_root: Path,
) -> None:
    with pytest.raises(TemplateRenderError) as exc_info:
        render_template("master/hello")  # no `name` supplied
    assert exc_info.value.variable == "name"
    assert exc_info.value.template_id == "master/hello"
