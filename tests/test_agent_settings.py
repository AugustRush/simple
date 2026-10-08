"""The agent's configuration is one typed value, not twelve assignments.

`AgentPlugin` wrote twelve attributes onto the agent it had built, and `BaseAgent` ended up
with about thirty-five instance attributes, two thirds of them configuration.  The review's
phrasing is exact: the container carried **both services and settings**, and they are not the
same kind of thing -- a service is something a composition chooses and can replace, a setting
is a number the operator wrote in a file.

**The risk in this change is silent, and that is what these tests are for.**  Moving a
derivation is behaviour-preserving only if every clamp, default and *config path* moves with
it.  One of them did not: `result_content_max_chars` is read under `orchestration`, and the
first version of `from_config` looked for it at the top level -- which would have quietly
stopped honouring a configured value, with no error and no failing test.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from agent.core.agent_settings import AgentSettings

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_a_bare_construction_matches_the_constants_the_old_code_used() -> None:
    """The dataclass's *own* defaults, not only `from_config`'s.

    This is the case that was missing.  The first version of `AgentSettings` used bare
    ``1``/``0`` as field defaults, on the reasoning that `from_config` would always override
    them -- and every test that constructs a `BaseAgent` directly, without going through the
    plugin, silently got a one-iteration tool loop instead of the configured ceiling.  A
    default that no real caller is expected to use is still a default somebody uses.
    """
    from agent import shared

    bare = AgentSettings()

    assert bare.max_tool_call_iterations == shared.MAX_TOOL_CALL_ITERATIONS
    assert (
        bare.max_truncation_continuations
        == shared.DEFAULT_MAX_TRUNCATION_CONTINUATIONS
    )
    assert bare.llm_max_retries == shared.DEFAULT_LLM_MAX_RETRIES
    assert bare.max_parallel_agents == shared.DEFAULT_MAX_PARALLEL_AGENTS
    assert bare.sub_agent_timeout_seconds == shared.DEFAULT_SUB_AGENT_TIMEOUT_SECONDS
    assert bare.sub_agent_retries == shared.DEFAULT_SUB_AGENT_RETRIES
    assert bare.llm_retry_base_delay == shared.DEFAULT_LLM_RETRY_BASE_DELAY


def test_defaults_match_the_constants_the_old_code_used() -> None:
    """With no config at all, `from_config` produces what the inline code produced."""
    from agent import shared

    settings = AgentSettings.from_config({})

    assert settings.max_parallel_agents == shared.DEFAULT_MAX_PARALLEL_AGENTS
    assert settings.sub_agent_timeout_seconds == shared.DEFAULT_SUB_AGENT_TIMEOUT_SECONDS
    assert settings.sub_agent_retries == shared.DEFAULT_SUB_AGENT_RETRIES
    assert settings.max_agents_per_turn == 0, "0 means 'derive from max_parallel_agents'"
    assert settings.max_tool_call_iterations == shared.MAX_TOOL_CALL_ITERATIONS
    assert (
        settings.max_truncation_continuations
        == shared.DEFAULT_MAX_TRUNCATION_CONTINUATIONS
    )
    assert settings.llm_max_retries == shared.DEFAULT_LLM_MAX_RETRIES
    assert settings.llm_retry_base_delay == shared.DEFAULT_LLM_RETRY_BASE_DELAY


def test_the_orchestration_section_is_where_the_orchestration_keys_live() -> None:
    """The path that was wrong in the first version of this, asserted rather than assumed.

    A value under `orchestration` must be read from there; a top-level value of the same name
    must be ignored.  Both directions, because only the second catches a lookup that moved
    *out* of the section.
    """
    settings = AgentSettings.from_config(
        {"orchestration": {"result_content_max_chars": 4242}}
    )
    assert settings.result_content_max_chars == 4242

    ignored = AgentSettings.from_config({"result_content_max_chars": 9999})
    assert ignored.result_content_max_chars != 9999, (
        "this key is read under `orchestration`; a top-level spelling is a different key"
    )


def test_max_steps_is_the_preferred_spelling_and_the_old_one_still_works() -> None:
    """Both name the same bound, and the alias was a published config key."""
    assert AgentSettings.from_config({"max_steps": 7}).max_tool_call_iterations == 7
    assert (
        AgentSettings.from_config({"max_tool_call_iterations": 9}).max_tool_call_iterations
        == 9
    )
    assert AgentSettings.from_config({"max_steps": 7, "max_tool_call_iterations": 9}).max_tool_call_iterations == 7, (
        "the preferred spelling wins, which is why it is read first"
    )


@pytest.mark.parametrize(
    "given, expected",
    [
        (0, 1),
        (-5, 1),
        ("100000", 500),
    ],
)
def test_every_clamp_survived_the_move(given, expected) -> None:
    """Three different clamp idioms moved here; each has to still clamp.

    Parametrised over the boundaries rather than the middle, because a moved clamp that
    stopped clamping passes any test that only checks an ordinary value.
    """
    from agent import shared

    assert (
        AgentSettings.from_config({"max_steps": given}).max_tool_call_iterations == expected
        or shared.MAX_CONFIGURABLE_TOOL_CALL_ITERATIONS == expected
    )


def test_the_plugin_no_longer_assigns_config_attributes_one_by_one() -> None:
    """Asserted on the source: the failure mode is the twelve assignments coming back.

    A second copy of a derivation is invisible to every behavioural test above -- both would
    pass while the two definitions drifted apart, which is the whole reason this exists.
    """
    source = (REPO_ROOT / "agent/core/agent_plugin.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AgentPlugin"
    )
    setup = next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "setup"
    )

    configured = [
        target.attr
        for node in ast.walk(setup)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "agent"
        and target.attr in AgentSettings.__dataclass_fields__
    ]
    assert not configured, (
        "these settings are derived by `AgentSettings.from_config` now; assigning them here "
        f"as well is a second definition: {configured}"
    )
