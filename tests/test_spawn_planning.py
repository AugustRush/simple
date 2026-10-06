"""Spawn-batch planning, testable without an agent.

Six methods on ``BaseAgent`` decided the whole shape of a spawn batch -- profiles,
sizing, specs, result payloads, coordination mode -- and none of them performed
I/O.  They were reachable only through an agent, so testing a profile fallback
meant building one.

``SpawnPlanner`` takes a :class:`PlannerDeps` of configuration and helpers, so
the decision rules can be driven directly.  The rules themselves moved by
mechanical rewrite; these tests pin them.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.core.spawn_planning import PlannerDeps, SpawnPlanner
from agent.orchestration.planner import OrchestrationDecision
from agent.orchestration.runtime import SubtaskResult, SubtaskSpec


def _planner(**settings) -> SpawnPlanner:
    ns = SimpleNamespace(
        max_agents_per_turn=settings.get("max_agents_per_turn", 0),
        **{k: v for k, v in settings.items() if k != "max_agents_per_turn"},
    )
    return SpawnPlanner(
        PlannerDeps(
            settings=ns,
            max_parallel_agents=settings.get("max_parallel_agents", 3),
            string_list=lambda v: [str(x) for x in (v or []) if str(x).strip()],
            normalize_output_contract=lambda v: dict(v or {}),
            mapping_dict=lambda v: dict(v or {}),
            bounded_text=lambda v, limit: str(v or "")[:limit],
            bounded_structured=lambda v: v,
            result_content_limit=lambda: 4000,
        )
    )


def _decision(mode: str) -> OrchestrationDecision:
    return OrchestrationDecision(mode=mode)


# ── Capability profiles ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("role", "task"),
    [
        ("engineer", "fix the parser"),
        ("assistant", "refactor this module"),
        ("assistant", "修改这个文件"),
        ("developer", "patch the config"),
    ],
)
def test_implementation_wording_is_recognised(role: str, task: str) -> None:
    assert SpawnPlanner.looks_like_implementation_work(role, task) is True


@pytest.mark.parametrize(
    ("role", "task"),
    [("researcher", "summarize the docs"), ("assistant", "count the rows")],
)
def test_other_wording_is_not_implementation(role: str, task: str) -> None:
    assert SpawnPlanner.looks_like_implementation_work(role, task) is False


def test_the_implementation_vocabulary_is_deliberately_broad() -> None:
    """``file`` counts as implementation wording, so "list the files" does too.

    Recorded rather than narrowed: the profile is a safety *floor*, and the cost
    of treating a read task as implementation work is a wider toolset, while the
    cost of the reverse is a sub-agent that cannot do what it was asked.  The
    asymmetry is why the broad token is acceptable here.
    """
    assert SpawnPlanner.looks_like_implementation_work("assistant", "list the files") is True


def test_an_explicit_profile_always_wins() -> None:
    planner = _planner()
    assert planner.default_capability_profile(
        "engineer", "implement it", "  Research ", orchestration_mode="parallel"
    ) == "research"


def test_direct_mode_gets_the_full_toolset() -> None:
    """No coordination to protect, so the profile does not narrow the tools."""
    planner = _planner()
    assert planner.default_capability_profile(
        "engineer", "implement it", "", orchestration_mode="direct"
    ) == "full"


def test_implementation_wording_earns_the_implementation_profile() -> None:
    planner = _planner()
    assert planner.default_capability_profile(
        "engineer", "fix it", "", orchestration_mode="parallel"
    ) == "implementation"


def test_a_read_only_task_stays_read_only() -> None:
    planner = _planner()
    assert planner.default_capability_profile(
        "researcher", "summarize the docs", "", orchestration_mode="parallel"
    ) == "read_only"


# ── Sizing ───────────────────────────────────────────────────────────────────


def test_the_per_turn_ceiling_is_four_times_concurrency() -> None:
    assert _planner(max_parallel_agents=3).max_agents_per_turn() == max(8, 12)


def test_a_configured_ceiling_wins() -> None:
    planner = _planner(max_agents_per_turn=50, max_parallel_agents=3)
    assert planner.max_agents_per_turn() == 50


def test_the_configured_ceiling_is_read_live() -> None:
    """Assigned on a live agent in more than one place, so not copied."""
    ns = SimpleNamespace(max_agents_per_turn=0)
    planner = SpawnPlanner(
        PlannerDeps(
            settings=ns,
            max_parallel_agents=1,
            string_list=lambda v: list(v or []),
            normalize_output_contract=lambda v: dict(v or {}),
            mapping_dict=lambda v: dict(v or {}),
            bounded_text=lambda v, n: str(v or ""),
            bounded_structured=lambda v: v,
            result_content_limit=lambda: 100,
        )
    )
    assert planner.max_agents_per_turn() == 8
    ns.max_agents_per_turn = 12
    assert planner.max_agents_per_turn() == 12


# ── Spec construction ────────────────────────────────────────────────────────


def test_a_minimal_spawn_call_becomes_a_spec() -> None:
    spec = _planner().spawn_tool_use_to_spec(
        {"id": "call-1", "input": {"task": "do it"}},
        index=0,
        orchestration_decision=_decision("parallel"),
    )
    assert isinstance(spec, SubtaskSpec)
    assert spec.task == "do it"
    assert spec.role == "assistant"


def test_a_missing_id_falls_back_to_the_index() -> None:
    spec = _planner().spawn_tool_use_to_spec(
        {"input": {"task": "t"}},
        index=4,
        orchestration_decision=_decision("parallel"),
    )
    assert spec.id == "spawn-4"


def test_the_id_prefers_the_input_over_the_envelope() -> None:
    spec = _planner().spawn_tool_use_to_spec(
        {"id": "envelope", "input": {"id": "inner", "task": "t"}},
        index=0,
        orchestration_decision=_decision("parallel"),
    )
    assert spec.id == "inner"


def test_declared_fields_reach_the_spec() -> None:
    spec = _planner().spawn_tool_use_to_spec(
        {
            "input": {
                "task": "t",
                "role": "engineer",
                "depends_on": ["a"],
                "write_scope": ["src"],
                "expected_output": "a patch",
                "timeout_seconds": 30,
                "early_exit": True,
                "continue_on_failure": True,
            }
        },
        index=0,
        orchestration_decision=_decision("parallel"),
    )
    assert spec.depends_on == ["a"]
    assert spec.write_scope == ["src"]
    assert spec.expected_output == "a patch"
    assert spec.timeout_seconds == 30
    assert spec.early_exit is True
    assert spec.continue_on_failure is True


def test_the_decision_mode_reaches_the_profile() -> None:
    """The spec's profile comes from the decision, which is why it is a param."""
    spec = _planner().spawn_tool_use_to_spec(
        {"input": {"task": "summarize"}},
        index=0,
        orchestration_decision=_decision("direct"),
    )
    assert spec.capability_profile == "full"


# ── Result payload ───────────────────────────────────────────────────────────


def _spec() -> SubtaskSpec:
    return SubtaskSpec(id="s", role="researcher", task="t")


def test_a_successful_result_serializes_with_its_content() -> None:
    payload = json.loads(
        _planner().spawn_result_payload(
            _spec(), SubtaskResult(id="s", ok=True, content="the answer", tool_calls_made=[])
        )
    )
    assert payload["ok"] is True
    assert payload["content"] == "the answer"
    assert payload["role"] == "researcher"


def test_missing_content_says_so_rather_than_being_empty() -> None:
    payload = json.loads(
        _planner().spawn_result_payload(_spec(), SubtaskResult(id="s", ok=True, content="", tool_calls_made=[]))
    )
    assert payload["content"] == "(no output)"


def test_an_error_is_carried_through() -> None:
    payload = json.loads(
        _planner().spawn_result_payload(
            _spec(), SubtaskResult(id="s", ok=False, content="", error="boom", tool_calls_made=[])
        )
    )
    assert payload["error"] == "boom"


def test_structured_content_only_appears_when_present() -> None:
    plain = json.loads(
        _planner().spawn_result_payload(_spec(), SubtaskResult(id="s", ok=True, content="", tool_calls_made=[]))
    )
    assert "structured_content" not in plain

    structured = json.loads(
        _planner().spawn_result_payload(
            _spec(),
            SubtaskResult(id="s", ok=True, content="", tool_calls_made=[], structured_content={"a": 1}),
        )
    )
    assert structured["structured_content"] == {"a": 1}


def test_extra_fields_are_merged_last() -> None:
    """Last, so a caller can override what the planner produced."""
    payload = json.loads(
        _planner().spawn_result_payload(
            _spec(), SubtaskResult(id="s", ok=True, content="x", tool_calls_made=[]), extra={"ok": "override"}
        )
    )
    assert payload["ok"] == "override"


# ── Coordination mode ────────────────────────────────────────────────────────


def _calls(*modes: str, depends: bool = False) -> list[tuple[int, dict]]:
    out = []
    for i, mode in enumerate(modes):
        payload = {"task": f"t{i}"}
        if mode:
            payload["coordination_mode"] = mode
        if depends:
            payload["depends_on"] = ["prev"]
        out.append((i, {"input": payload}))
    return out


def test_one_call_is_direct() -> None:
    assert _planner().execution_mode_from_spawn_calls(_calls("")) == "direct"


def test_several_calls_are_parallel() -> None:
    assert _planner().execution_mode_from_spawn_calls(_calls("", "")) == "parallel"


def test_dependencies_make_it_a_pipeline() -> None:
    assert _planner().execution_mode_from_spawn_calls(
        _calls("", "", depends=True)
    ) == "pipeline"


def test_an_explicit_mode_wins() -> None:
    assert _planner().execution_mode_from_spawn_calls(
        _calls("rendezvous", "rendezvous")
    ) == "rendezvous"


def test_an_unsupported_mode_is_refused_by_name() -> None:
    """Loudly, because silently running it as parallel drops the intent."""
    with pytest.raises(ValueError, match="unsupported coordination_mode"):
        _planner().execution_mode_from_spawn_calls(_calls("sequential", "sequential"))


def test_conflicting_modes_are_refused() -> None:
    """Rather than a silent winner: the caller asked for two different things."""
    with pytest.raises(ValueError, match="conflicting coordination_mode"):
        _planner().execution_mode_from_spawn_calls(_calls("parallel", "pipeline"))


# ── The agent still delegates ────────────────────────────────────────────────


def test_the_agent_methods_agree_with_the_planner() -> None:
    """A wrapper that silently stops wrapping is the failure mode to catch."""
    import agent as agent_module

    agent = agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="fake-model", api_format="openai"
    )
    assert agent._looks_like_implementation_work("engineer", "fix it") is True
    assert agent._default_capability_profile(
        "x", "summarize", "", orchestration_mode="parallel"
    ) == "read_only"
    assert agent._max_agents_per_turn() == max(8, agent.max_parallel_agents * 4)
    assert agent._derive_execution_mode_from_spawn_calls(_calls("", "")) == "parallel"
    assert json.loads(
        agent._spawn_result_payload(_spec(), SubtaskResult(id="s", ok=True, content="c", tool_calls_made=[]))
    )["content"] == "c"
