"""Overriding configuration by path: the mechanism a bundle is made of.

A bundle is a named set of overrides -- "headless" turns the web channel off and
shortens the scheduler lease.  What makes that declarative is expressing it as
data, and what makes it *safe* is the rule at the centre of this file: **a patch
may not invent a key**.  A typo like ``orchestration.max_parallel_agent`` would
otherwise sit in the configuration looking authoritative while the real setting
kept its default.

These tests are the specification for that rule and for the three others that are
easy to get wrong: merging must be deep, removal must be idempotent, and the
caller's mapping must come back untouched.
"""

from __future__ import annotations

import pytest

from agent.config_patch import (
    PatchEntry,
    UnknownConfigPath,
    apply_patch,
    deep_merge,
    entry,
)


BASE: dict = {
    "orchestration": {
        "max_parallel_agents": 4,
        "sub_agent_retries": 2,
        "sub_agent_timeout_seconds": 600,
    },
    "channels": {"feishu": {"enabled": False}, "web": {"enabled": True}},
    "context": {"storage": {"path": "/tmp/agent"}, "consolidation": {"enabled": True}},
    "shell_allowed_commands": ["ls", "cat"],
}


# ── Deep merge ───────────────────────────────────────────────────────────────


def test_merging_a_section_keeps_its_siblings() -> None:
    """Shallow replacement is how a patch silently drops every other setting."""
    merged = deep_merge(BASE, {"orchestration": {"max_parallel_agents": 2}})
    assert merged["orchestration"]["max_parallel_agents"] == 2
    assert merged["orchestration"]["sub_agent_retries"] == 2
    assert merged["orchestration"]["sub_agent_timeout_seconds"] == 600


def test_merging_nested_sections_recurses() -> None:
    merged = deep_merge(BASE, {"channels": {"web": {"enabled": False}}})
    assert merged["channels"]["web"] == {"enabled": False}
    assert merged["channels"]["feishu"] == {"enabled": False}, "sibling untouched"


def test_a_list_is_replaced_not_appended() -> None:
    """Every list here is a decision; order-dependence would be a bug."""
    merged = deep_merge(BASE, {"shell_allowed_commands": ["pwd"]})
    assert merged["shell_allowed_commands"] == ["pwd"]


def test_merging_does_not_mutate_either_input() -> None:
    before = deep_merge(BASE, {})
    deep_merge(BASE, {"orchestration": {"max_parallel_agents": 9}})
    assert BASE["orchestration"]["max_parallel_agents"] == 4
    assert before == BASE


def test_a_scalar_replaces_a_mapping() -> None:
    """A patch that changes a section to a scalar is doing it on purpose."""
    merged = deep_merge(BASE, {"channels": "none"})
    assert merged["channels"] == "none"


# ── Applying entries ─────────────────────────────────────────────────────────


def test_a_declared_path_is_overridden() -> None:
    result = apply_patch(BASE, [entry("orchestration.sub_agent_retries", 5)])
    assert result["orchestration"]["sub_agent_retries"] == 5
    assert result["orchestration"]["max_parallel_agents"] == 4


def test_no_entries_returns_an_equivalent_configuration() -> None:
    assert apply_patch(BASE, []) == BASE


def test_the_result_is_a_copy() -> None:
    result = apply_patch(BASE, [entry("orchestration.sub_agent_retries", 5)])
    assert result is not BASE
    assert BASE["orchestration"]["sub_agent_retries"] == 2


def test_nested_mappings_are_copied_too() -> None:
    """A shallow copy would let a later patch reach into the caller's config."""
    result = apply_patch(BASE, [entry("channels.web.enabled", False)])
    result["channels"]["feishu"]["enabled"] = True
    assert BASE["channels"]["feishu"]["enabled"] is False


def test_entries_apply_in_order() -> None:
    result = apply_patch(
        BASE,
        [entry("orchestration.sub_agent_retries", 1), entry("orchestration.sub_agent_retries", 9)],
    )
    assert result["orchestration"]["sub_agent_retries"] == 9


def test_a_whole_section_can_be_replaced() -> None:
    result = apply_patch(BASE, [entry("orchestration", {"max_parallel_agents": 1})])
    assert result["orchestration"] == {"max_parallel_agents": 1}


# ── The rule: no inventing keys ──────────────────────────────────────────────


def test_an_undeclared_path_is_refused() -> None:
    """A typo must not become a setting nobody reads."""
    with pytest.raises(UnknownConfigPath) as caught:
        apply_patch(BASE, [entry("orchestration.max_parallel_agent", 2)])
    assert "max_parallel_agent" in str(caught.value)


def test_the_refusal_names_nearby_sections() -> None:
    """The message has to be enough to find the typo."""
    with pytest.raises(UnknownConfigPath) as caught:
        apply_patch(BASE, [entry("orchestraton.x", 2)])
    assert "orchestration" in str(caught.value)


def test_an_undeclared_top_level_section_is_refused() -> None:
    with pytest.raises(UnknownConfigPath):
        apply_patch(BASE, [entry("nonexistent.key", 1)])


def test_a_deeper_typo_is_refused() -> None:
    with pytest.raises(UnknownConfigPath):
        apply_patch(BASE, [entry("channels.web.enabld", True)])


def test_allow_new_permits_an_undeclared_path() -> None:
    """A caller that genuinely owns a key has to say so."""
    result = apply_patch(BASE, [entry("test.workspace", "/tmp/x", allow_new=True)])
    assert result["test"]["workspace"] == "/tmp/x"


def test_allow_new_still_requires_an_explicit_opt_in() -> None:
    with pytest.raises(UnknownConfigPath):
        apply_patch(BASE, [entry("test.workspace", "/tmp/x")])


# ── Removal ──────────────────────────────────────────────────────────────────


def test_none_removes_a_key() -> None:
    """``null`` is how a data file says "drop this"."""
    result = apply_patch(BASE, [entry("channels.feishu")])
    assert "feishu" not in result["channels"]
    assert "web" in result["channels"]


def test_removing_a_whole_section_works() -> None:
    result = apply_patch(BASE, [entry("channels")])
    assert "channels" not in result


def test_removing_an_undeclared_path_is_refused() -> None:
    """Removal is still a statement about a path, so the typo rule applies."""
    with pytest.raises(UnknownConfigPath):
        apply_patch(BASE, [entry("channels.nothing")])


def test_removal_is_idempotent_when_the_path_exists() -> None:
    once = apply_patch(BASE, [entry("channels.feishu")])
    twice = apply_patch(once, [entry("channels.feishu", None, allow_new=True)])
    assert "feishu" not in twice["channels"]


def test_removal_does_not_touch_siblings() -> None:
    result = apply_patch(BASE, [entry("orchestration.sub_agent_retries")])
    assert result["orchestration"]["max_parallel_agents"] == 4


# ── Entries are data ─────────────────────────────────────────────────────────


def test_an_entry_is_frozen() -> None:
    with pytest.raises(Exception):
        PatchEntry(path="a").path = "b"  # type: ignore[misc]


def test_an_entry_serialises() -> None:
    """A table of them should be printable, not opaque."""
    import dataclasses

    assert dataclasses.asdict(entry("a.b", 1)) == {
        "path": "a.b",
        "value": 1,
        "allow_new": False,
    }
