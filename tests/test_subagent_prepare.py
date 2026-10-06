"""Preparing a sub-agent, testable without one.

Everything here happens before any model call, so none of it needed an agent --
it needed the eight things it reads, passed in.  The three normalizations are
where the interest is: each input arrives in whatever shape a tool call used, and
two consumers have to agree on the canonical form afterwards.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.core.subagent_prepare import SubAgentPreparation, prepare_sub_agent


class _Catalog:
    def __init__(self, definitions: dict | None = None) -> None:
        self.definitions = definitions or {}

    def get_agent_definition(self, role: str):
        return self.definitions.get(role)


def _deps(**overrides):
    """The five injected callables, each recording what it was given."""
    seen: dict[str, list] = {
        "normalize": [],
        "contract": [],
        "registry": [],
        "agent": [],
        "prompt": [],
    }

    def normalize(value):
        seen["normalize"].append(value)
        return {"format": "json"} if value else {}

    def with_contract(task, expected_output, contract):
        seen["contract"].append((task, expected_output, contract))
        return task + " [contract]" if (expected_output or contract) else task

    def create_registry(*, capability_profile, write_scope):
        seen["registry"].append((capability_profile, list(write_scope)))
        return SimpleNamespace(name="sub-registry")

    def create_agent(registry):
        seen["agent"].append(registry)
        return SimpleNamespace(name="sub-agent")

    def compose(registry, suffix):
        seen["prompt"].append((registry, suffix))
        return (f"prompt[{suffix}]", SimpleNamespace(metadata={"active": True}))

    return seen, dict(
        normalize_output_contract=normalize,
        with_output_contract=with_contract,
        create_sub_registry=create_registry,
        create_sub_agent=create_agent,
        compose_system_prompt=compose,
    )


def _prepare(**overrides):
    seen, deps = _deps()
    kwargs = dict(
        role="researcher",
        task="read the docs",
        system_suffix="be terse",
        expected_output="",
        output_contract=None,
        write_scope=None,
        capability_profile="read_only",
        plugin_catalog=None,
    )
    kwargs.update(overrides)
    result = prepare_sub_agent(**kwargs, **deps)
    return result, seen


# ── The three normalizations ─────────────────────────────────────────────────


def test_the_write_scope_drops_blanks_and_stringifies() -> None:
    """The registry view and the profile must agree on what may be touched."""
    result, seen = _prepare(write_scope=["src", "  ", 7])
    assert result.normalized_scope == ("src", "7")
    assert seen["registry"][0][1] == ["src", "7"]


def test_the_write_scope_filter_is_string_shaped_not_value_shaped() -> None:
    """``None`` becomes the string ``"None"`` rather than being dropped.

    The filter is ``if str(item).strip()``, so it drops blanks *after*
    stringification -- which means a non-string non-blank value survives as its
    representation.  Recorded rather than changed: this is pre-existing
    behaviour moved verbatim, and narrowing the filter is a decision about what a
    tool call may declare, not part of an extraction.  The test exists so the
    behaviour is visible the next time someone reads this normalization.
    """
    result, _ = _prepare(write_scope=["src", None, "", "  "])
    assert result.normalized_scope == ("src", "None")


def test_an_absent_write_scope_becomes_empty() -> None:
    result, _ = _prepare(write_scope=None)
    assert result.normalized_scope == ()


def test_the_output_contract_is_normalized_once_and_reused() -> None:
    """Two consumers read it -- the prompt builder and the validator.

    If they read different shapes, one of them silently sees an empty contract
    and the sub-agent is never held to it.
    """
    result, seen = _prepare(output_contract={"format": "JSON"})
    assert result.output_contract == {"format": "json"}
    # Normalized once, then handed to the contract renderer.
    assert len(seen["normalize"]) == 1
    assert seen["contract"][0][2] == {"format": "json"}


def test_the_task_carries_its_contract_instructions() -> None:
    """A model cannot satisfy a contract it was never told about."""
    result, _ = _prepare(expected_output="a patch")
    assert result.shaped_task.endswith("[contract]")


def test_a_task_without_a_contract_is_left_alone() -> None:
    result, _ = _prepare(expected_output="", output_contract=None)
    assert result.shaped_task == "read the docs"


# ── Plugin-declared roles ────────────────────────────────────────────────────


def test_a_plugin_role_body_is_prepended_to_the_suffix() -> None:
    """Prepended, so a per-spawn suffix still comes last and wins."""
    catalog = _Catalog({"plugin:p:a": {"body": "You are the author's role."}})
    result, _ = _prepare(
        role="plugin:p:a", system_suffix="be terse", plugin_catalog=catalog
    )
    assert result.system_prompt.startswith("prompt[You are the author's role.")
    assert "be terse" in result.system_prompt


def test_a_plugin_role_with_no_suffix_uses_the_body_alone() -> None:
    catalog = _Catalog({"plugin:p:a": {"body": "Role text."}})
    result, _ = _prepare(role="plugin:p:a", system_suffix="", plugin_catalog=catalog)
    assert result.system_prompt == "prompt[Role text.]"


def test_an_unknown_plugin_role_is_ignored() -> None:
    catalog = _Catalog({})
    result, _ = _prepare(role="plugin:p:missing", plugin_catalog=catalog)
    assert result.system_prompt == "prompt[be terse]"


def test_a_plugin_role_body_that_is_blank_is_ignored() -> None:
    catalog = _Catalog({"plugin:p:a": {"body": "   "}})
    result, _ = _prepare(role="plugin:p:a", plugin_catalog=catalog)
    assert result.system_prompt == "prompt[be terse]"


def test_a_plain_role_never_consults_the_catalog() -> None:
    class _Boom:
        def get_agent_definition(self, role):
            raise AssertionError("a non-plugin role must not be looked up")

    result, _ = _prepare(role="researcher", plugin_catalog=_Boom())
    assert result.system_prompt == "prompt[be terse]"


def test_no_catalog_is_fine() -> None:
    """A composition without plugins must still spawn sub-agents."""
    result, _ = _prepare(role="plugin:p:a", plugin_catalog=None)
    assert result.system_prompt == "prompt[be terse]"


# ── The bundle ───────────────────────────────────────────────────────────────


def test_everything_is_resolved_once_and_returned() -> None:
    """Built before the retry loop: two attempts must not see two registries."""
    result, seen = _prepare()
    assert isinstance(result, SubAgentPreparation)
    assert len(seen["registry"]) == 1
    assert len(seen["agent"]) == 1
    assert len(seen["prompt"]) == 1
    assert result.sub_agent.name == "sub-agent"
    assert result.sub_registry.name == "sub-registry"


def test_the_prompt_composer_receives_the_sub_registry() -> None:
    """Not the parent's: the prompt must describe the tools the child has."""
    result, seen = _prepare()
    assert seen["prompt"][0][0] is result.sub_registry


def test_the_active_context_is_carried_through() -> None:
    result, _ = _prepare()
    assert result.active_context.metadata == {"active": True}


# ── The preparation is frozen ────────────────────────────────────────────────


def test_the_preparation_is_frozen() -> None:
    result, _ = _prepare()
    with pytest.raises(Exception):
        result.shaped_task = "other"  # type: ignore[misc]


def test_the_scope_is_a_tuple_not_a_mutable_list() -> None:
    """A caller mutating it must not change what the registry was built from."""
    result, _ = _prepare(write_scope=["src"])
    assert isinstance(result.normalized_scope, tuple)
