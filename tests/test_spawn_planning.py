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

from agent.core.agent import _active_orchestration_runs
from agent.core.spawn_planning import (
    MAX_PROMOTED_SUMMARY_CHARS,
    PlannerDeps,
    SpawnPlanner,
    promote_orchestration_runs,
)
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


def test_tool_calls_are_capped_at_a_hundred() -> None:
    """A runaway sub-agent must not hand the parent an unbounded list."""
    result = SubtaskResult(
        id="s", ok=True, content="c", tool_calls_made=[f"t{i}" for i in range(250)]
    )
    payload = json.loads(_planner().spawn_result_payload(_spec(), result))
    assert len(payload["tool_calls_made"]) == 100


def test_content_is_bounded_by_the_configured_limit() -> None:
    result = SubtaskResult(id="s", ok=True, content="x" * 10_000, tool_calls_made=[])
    payload = json.loads(_planner().spawn_result_payload(_spec(), result))
    assert len(payload["content"]) <= 4000


def test_an_artifact_reference_is_carried_through() -> None:
    result = SubtaskResult(
        id="s", ok=True, content="c", tool_calls_made=[], artifact_ref="out/x.md"
    )
    payload = json.loads(_planner().spawn_result_payload(_spec(), result))
    assert payload["artifact_ref"] == "out/x.md"


def test_an_absent_artifact_reference_is_omitted() -> None:
    payload = json.loads(
        _planner().spawn_result_payload(
            _spec(), SubtaskResult(id="s", ok=True, content="c", tool_calls_made=[])
        )
    )
    assert "artifact_ref" not in payload


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


# ── Rendezvous directive parsing ─────────────────────────────────────────────
#
# The model's output is free-form, so every field is normalized rather than
# trusted.  These cases are the whole contract.


def _parse(text, known=("a", "b")):
    return SpawnPlanner.parse_rendezvous_directive(text, known_ids=set(known))


def test_a_plain_json_object_parses() -> None:
    directive = _parse('{"summary": "done", "stop": true}')
    assert directive is not None
    assert directive.summary == "done"
    assert directive.stop is True


def test_a_fenced_code_block_is_unwrapped() -> None:
    """Models emit markdown fences around JSON; that is not a parse error."""
    directive = _parse('```json\n{"summary": "x", "stop": false}\n```')
    assert directive is not None
    assert directive.summary == "x"


def test_a_bare_fence_is_unwrapped() -> None:
    directive = _parse('```\n{"summary": "x"}\n```')
    assert directive is not None


@pytest.mark.parametrize("text", ["", "   ", "not json", "[1,2,3]", '"a string"'])
def test_unparseable_input_returns_none(text: str) -> None:
    """None rather than a guess, so the caller uses its deterministic fallback."""
    assert _parse(text) is None


def test_stop_accepts_only_true_like_values() -> None:
    """``stop`` decides whether the loop continues, so it must not be truthy-loose."""
    assert _parse('{"stop": true}').stop is True
    assert _parse('{"stop": "yes"}').stop is True
    assert _parse('{"stop": "1"}').stop is True
    assert _parse('{"stop": 1}').stop is True
    assert _parse('{"stop": false}').stop is False
    assert _parse('{"stop": "maybe"}').stop is False
    assert _parse('{"stop": null}').stop is False
    assert _parse('{"summary": "x"}').stop is False


def test_continue_with_is_filtered_to_known_ids() -> None:
    """An unknown id would ask a runner to continue something that does not exist."""
    assert _parse('{"continue_with": ["a", "zzz"]}').continue_with == ["a"]


def test_an_unknown_single_id_becomes_none() -> None:
    assert _parse('{"continue_with": "zzz"}').continue_with is None


def test_an_empty_selection_becomes_none() -> None:
    """None means "keep everyone", which is safer than an empty continue list."""
    assert _parse('{"continue_with": []}').continue_with is None
    assert _parse('{"continue_with": ["  "]}').continue_with is None


def test_a_single_known_string_is_accepted() -> None:
    assert _parse('{"continue_with": "b"}').continue_with == ["b"]


def test_an_unsupported_continue_type_becomes_none() -> None:
    assert _parse('{"continue_with": 7}').continue_with is None


# ── Subtask result summarization ─────────────────────────────────────────────


def test_short_content_is_its_own_summary() -> None:
    assert SpawnPlanner.summarize_subtask_result("short") == "short"


def test_long_content_is_truncated_with_an_ellipsis() -> None:
    summary = SpawnPlanner.summarize_subtask_result("x" * 100, limit=40)
    assert len(summary) <= 40
    assert summary.endswith("...")


def test_none_summarizes_to_empty() -> None:
    assert SpawnPlanner.summarize_subtask_result(None) == ""


# ── The agent still delegates these too ──────────────────────────────────────


def test_the_agent_still_parses_directives_and_summarizes() -> None:
    import agent as agent_module

    agent = agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="fake-model", api_format="openai"
    )
    assert agent._summarize_subtask_result("x" * 100, limit=40).endswith("...")
    parsed = agent._parse_rendezvous_directive('{"stop": true}', known_ids={"a"})
    assert parsed is not None and parsed.stop is True


# ── Promoting run summaries into memory ──────────────────────────────────────
#
# Three filters, each of which exists because the alternative put something
# wrong into memory a later turn reads as fact.


class _Entry:
    """Records what was promoted, standing in for the memory layer's class."""

    def __init__(self, **fields) -> None:
        self.__dict__.update(fields)


class _Store:
    def __init__(self) -> None:
        self.entries: list = []

    def add_entry(self, entry) -> None:
        self.entries.append(entry)


class _Manager:
    def __init__(self, session_id: str = "s-1") -> None:
        self.staging = SimpleNamespace(session_id=session_id)
        self.store = _Store()


def _promote(manager, runs):
    return promote_orchestration_runs(
        manager,
        runs,
        now="2026-01-01 00:00 UTC",
        new_id=lambda: "id-1",
        entry_type=_Entry,
    )


def _ran(result_id: str, ok: bool = True, **kwargs) -> SubtaskResult:
    return SubtaskResult(
        id=result_id,
        ok=ok,
        content=kwargs.pop("content", "the finding"),
        tool_calls_made=[],
        **kwargs,
    )


def _run(*results) -> dict:
    return {"run_id": "r-1", "results": list(results)}


def test_a_successful_summary_is_promoted() -> None:
    manager = _Manager()
    written = _promote(manager, [_run(_ran("a"))])
    assert written == 1
    entry = manager.store.entries[0]
    assert "the finding" in entry.content
    assert entry.scope == "session:s-1"
    assert entry.memory_type == "orchestration_summary"


def test_a_failed_result_is_not_promoted() -> None:
    """A failed sub-agent's text is not a finding.

    Promoting it would put a former error into the memory a later turn reads as
    fact -- the most damaging thing this function can do.
    """
    manager = _Manager()
    assert _promote(manager, [_run(_ran("a", ok=False))]) == 0
    assert manager.store.entries == []


def test_an_empty_summary_is_not_promoted() -> None:
    """It would occupy a retrieval slot while carrying no information."""
    manager = _Manager()
    assert _promote(manager, [_run(_ran("a", content="   "))]) == 0


def test_a_long_summary_is_bounded() -> None:
    """This text joins a later turn's context, so it cannot be unbounded."""
    manager = _Manager()
    _promote(manager, [_run(_ran("a", content="x" * 9000))])
    entry = manager.store.entries[0]
    body = entry.content.split("\n", 1)[1]
    assert len(body) == MAX_PROMOTED_SUMMARY_CHARS


def test_the_summary_is_read_from_summary_or_content() -> None:
    manager = _Manager()
    _promote(manager, [_run(_ran("a", content="fallback", summary="preferred"))])
    assert "preferred" in manager.store.entries[0].content


def test_a_non_subtask_result_is_skipped() -> None:
    manager = _Manager()
    assert _promote(manager, [_run("not a result")]) == 0


def test_the_run_id_and_result_id_appear_in_the_entry() -> None:
    """So a later reader can trace a memory back to the run that produced it."""
    manager = _Manager()
    _promote(manager, [_run(_ran("subtask-7"))])
    content = manager.store.entries[0].content
    assert "r-1" in content
    assert "subtask-7" in content


def test_several_results_in_one_run_are_all_promoted() -> None:
    manager = _Manager()
    assert _promote(manager, [_run(_ran("a"), _ran("b"))]) == 2


def test_no_context_manager_promotes_nothing() -> None:
    """The no-session case: nothing to attribute the memory to."""
    assert promote_orchestration_runs(
        None, [_run(_ran("a"))], now="t", new_id=lambda: "i", entry_type=_Entry
    ) == 0


def test_the_agent_still_delegates_promotion() -> None:
    import agent as agent_module

    agent = agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="fake-model", api_format="openai"
    )
    manager = _Manager()
    agent._context_manager_for = lambda ctx=None: manager  # type: ignore[assignment]
    token = _active_orchestration_runs.set([_run(_ran("a"))])
    try:
        agent._promote_orchestration_runs()
    finally:
        _active_orchestration_runs.reset(token)
    assert len(manager.store.entries) == 1
