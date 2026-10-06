"""Bounding and assembling model-facing text, testable without an agent.

These four helpers were ``@staticmethod``/``@classmethod`` on ``BaseAgent`` and
were called from three unrelated places -- the sub-agent prompt builder, the
handoff summary, and the output-contract validation path.  That
reach is the reason they are now free functions: a helper used by three
concerns belongs to none of them.

The truncation markers differ on purpose, and the tests below pin each one,
because collapsing them would silently change what a model is shown.
"""

from __future__ import annotations

import json

import pytest

from agent.core.bounded_text import (
    append_named_block,
    bounded_json,
    bounded_structured,
    bounded_text,
)


# ── append_named_block ───────────────────────────────────────────────────────


def test_no_lines_returns_the_base_untouched() -> None:
    """What lets a caller pass an optional section without branching."""
    assert append_named_block("base", "Heading:", []) == "base"


def test_an_empty_base_returns_the_block_alone() -> None:
    """No leading blank line when there is nothing to separate it from."""
    assert append_named_block("", "Heading:", ["a", "b"]) == "Heading:\na\nb"


def test_a_base_and_a_block_are_separated_by_one_blank_line() -> None:
    assert append_named_block("base", "Heading:", ["a"]) == "base\n\nHeading:\na"


# ── bounded_json ─────────────────────────────────────────────────────────────


def test_short_json_is_serialized_and_sorted() -> None:
    assert bounded_json({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'


def test_long_json_is_truncated_within_the_limit() -> None:
    result = bounded_json({"k": "x" * 5000}, limit=200)
    assert len(result) <= 200
    assert result.endswith("... [truncated]")


def test_an_unserializable_leaf_does_not_raise() -> None:
    """``default=str``: a logging path must not die on an odd object."""

    class _Odd:
        def __repr__(self) -> str:
            return "<odd>"

    assert "<odd>" in bounded_json({"v": _Odd()})


# ── bounded_text ─────────────────────────────────────────────────────────────


def test_short_text_is_returned_as_is() -> None:
    assert bounded_text("hello", 10) == "hello"


def test_none_becomes_the_empty_string() -> None:
    assert bounded_text(None, 10) == ""


def test_long_text_is_truncated_within_the_limit() -> None:
    result = bounded_text("y" * 500, 50)
    assert len(result) <= 50
    assert result.endswith("... [truncated]")


def test_non_string_values_are_stringified() -> None:
    assert bounded_text(7, 10) == "7"


# ── bounded_structured ───────────────────────────────────────────────────────


def test_a_small_value_is_returned_unchanged_not_serialized() -> None:
    """Returns a *value*: a consumer should not have to parse back a string."""
    value = {"a": 1}
    assert bounded_structured(value) is value


def test_a_large_value_becomes_a_truncation_marker() -> None:
    marker = bounded_structured({"k": "x" * 5000}, limit=200)
    assert marker["_truncated"] is True
    assert len(marker["preview"]) <= 200


# ── the markers are not interchangeable ──────────────────────────────────────


def test_the_string_and_value_markers_differ() -> None:
    """The distinction the module docstring claims, asserted.

    Same input, same limit: ``bounded_json`` yields a string whose tail says it
    was cut, ``bounded_structured`` yields a value that says so structurally.
    """
    payload = {"k": "x" * 5000}
    as_text = bounded_json(payload, limit=200)
    as_value = bounded_structured(payload, limit=200)

    assert isinstance(as_text, str)
    assert as_text.endswith("... [truncated]")
    assert isinstance(as_value, dict)
    assert set(as_value) == {"_truncated", "preview"}
    assert not as_text.endswith("_truncated")


# ── the agent still delegates ────────────────────────────────────────────────


def test_the_agent_methods_agree_with_the_functions() -> None:
    """A wrapper that silently stops wrapping is the failure mode to catch."""
    import agent as agent_module

    agent = agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="fake-model", api_format="openai"
    )
    assert agent._bounded_json({"a": 1}) == bounded_json({"a": 1})
    assert agent._bounded_text("x", 5) == bounded_text("x", 5)
    assert agent._bounded_structured({"a": 1}) == bounded_structured({"a": 1})
    assert agent._append_named_block("b", "H:", ["l"]) == append_named_block("b", "H:", ["l"])


def test_the_string_list_method_still_delegates_to_contracts() -> None:
    """``_string_list`` stayed on the agent; its contract is unchanged."""
    from agent.orchestration import contracts

    import agent as agent_module

    agent = agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="fake-model", api_format="openai"
    )
    assert agent._string_list("a") == contracts.string_list("a")
