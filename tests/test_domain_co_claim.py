"""Drift detection for the canonical CO-declaration definitions.

:data:`CO_CLAIM_VALUES` (runtime tuple), :data:`CoDeclaration` (Pydantic
``Literal`` alias), and :data:`CO_CLAIMABLE_ROLES` (the :class:`Role`
subset) MUST agree. They cannot be mechanically derived from one
another because :class:`Literal` requires statically-resolvable values
(``Literal[*tuple]`` is not legal Python), so when we add a new
CO-claimable role all three definitions need updating in lockstep.

Without this test, a stale ``CoDeclaration`` Literal would silently
narrow the wire schema while validators continued to accept the new
value, producing confusing "field rejected by Pydantic but accepted by
runtime" bugs at the boundary between Master and the WS messages.
"""

from __future__ import annotations

from typing import get_args

from wolfbot.domain.enums import (
    CO_CLAIM_VALUES,
    CO_CLAIMABLE_ROLES,
    CoDeclaration,
    Role,
    format_co_claim_options,
    role_to_co_claim,
)


def test_co_claim_values_match_co_claimable_roles() -> None:
    """The runtime tuple is mechanically derived from the Role subset —
    if this drifts, the derivation in :mod:`wolfbot.domain.enums` is
    broken."""
    derived = tuple(role_to_co_claim(r) for r in CO_CLAIMABLE_ROLES)
    assert derived == CO_CLAIM_VALUES


def test_co_declaration_literal_matches_runtime_tuple() -> None:
    """The Pydantic Literal MUST contain the same values, in the same
    order, as :data:`CO_CLAIM_VALUES`. ``Literal`` ordering matters for
    JSON schema dumps and for human-readable error messages."""
    literal_args = get_args(CoDeclaration)
    assert literal_args == CO_CLAIM_VALUES


def test_co_claimable_roles_excludes_wolf_madman_villager() -> None:
    """No real game logic openly claims wolf, madman, or villager — the
    first two are always faked, the last has no power. Locking this in
    here so a drive-by edit to :data:`CO_CLAIMABLE_ROLES` triggers a
    test failure rather than silent gameplay change."""
    assert Role.WEREWOLF not in CO_CLAIMABLE_ROLES
    assert Role.MADMAN not in CO_CLAIMABLE_ROLES
    assert Role.VILLAGER not in CO_CLAIMABLE_ROLES


def test_role_to_co_claim_lowercases_uppercase_role_value() -> None:
    """``Role`` values are uppercase StrEnum strings (matches the DB
    column); the wire/storage form is lowercase. Verify the mapping is
    a pure ``.lower()`` so renaming a role doesn't accidentally diverge
    the two surfaces."""
    for role in CO_CLAIMABLE_ROLES:
        assert role_to_co_claim(role) == role.value.lower()


def test_format_co_claim_options_default_form() -> None:
    """Default render matches the Japanese analyzer prompts'
    ``"seer"/"medium"/"knight"`` shape; preserves quotes and slash
    separator."""
    assert format_co_claim_options() == '"seer"/"medium"/"knight"'


def test_format_co_claim_options_custom_separator_and_quote() -> None:
    """Callers building prose-style prompts (e.g. prompt_builder) can
    override the separator. Removing the quote char yields a bare,
    unquoted list."""
    assert format_co_claim_options(separator=" / ") == '"seer" / "medium" / "knight"'
    assert format_co_claim_options(quote="") == "seer/medium/knight"
