"""Tests for the multi-agent orchestration runtime contracts."""

import asyncio

import pytest

from agent.orchestration import RendezvousDirective, SubtaskResult, SubtaskSpec
from agent.orchestration.planner import OrchestrationDecision
from agent.orchestration.runtime import (
    CANCELLED_ERROR,
    run_parallel_subtasks,
    run_pipeline_subtasks,
    run_rendezvous_round,
    validate_subtask_specs,
)


def _ok_result(spec: SubtaskSpec) -> SubtaskResult:
    return SubtaskResult(
        id=spec.id,
        ok=True,
        content=f"done:{spec.id}",
        summary=f"done:{spec.id}",
        tool_calls_made=[],
    )


def test_parallel_early_exit_cancels_queued_tasks_gracefully():
    """Early exit must not abort the batch when some tasks are still queued
    on the concurrency semaphore (regression: CancelledError escaped from
    semaphore acquisition and killed the whole run)."""

    async def executor(spec: SubtaskSpec) -> SubtaskResult:
        if spec.id == "fast":
            return _ok_result(spec)
        await asyncio.sleep(30)
        return _ok_result(spec)

    results = asyncio.run(
        asyncio.wait_for(
            run_parallel_subtasks(
                [
                    SubtaskSpec(id="fast", role="a", task="a", early_exit=True),
                    SubtaskSpec(id="slow1", role="b", task="b"),
                    SubtaskSpec(id="slow2", role="c", task="c"),
                    SubtaskSpec(id="slow3", role="d", task="d"),
                ],
                executor=executor,
                max_concurrency=2,
            ),
            timeout=2,
        )
    )

    by_id = {result.id: result for result in results}
    assert by_id["fast"].ok is True
    for spec_id in ("slow1", "slow2", "slow3"):
        assert by_id[spec_id].ok is False
        assert "cancelled" in by_id[spec_id].error


def test_parallel_propagates_genuine_external_cancellation():
    """A real cancellation (not an early exit) must still surface as
    CancelledError instead of being masked as a cancelled subtask."""

    async def executor(spec: SubtaskSpec) -> SubtaskResult:
        await asyncio.sleep(30)
        return _ok_result(spec)

    async def run():
        task = asyncio.create_task(
            run_parallel_subtasks(
                [
                    SubtaskSpec(id="a", role="x", task="a"),
                    SubtaskSpec(id="b", role="x", task="b"),
                ],
                executor=executor,
                max_concurrency=2,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_pipeline_early_exit_cancels_stage_siblings_and_skips_downstream():
    async def executor(spec: SubtaskSpec, upstream_summaries: dict[str, str]):
        if spec.id == "find":
            return _ok_result(spec)
        await asyncio.sleep(30)
        return _ok_result(spec)

    results = asyncio.run(
        asyncio.wait_for(
            run_pipeline_subtasks(
                [
                    SubtaskSpec(id="find", role="search", task="find", early_exit=True),
                    SubtaskSpec(id="sibling", role="worker", task="sibling"),
                    SubtaskSpec(
                        id="downstream",
                        role="worker",
                        task="downstream",
                        depends_on=["find"],
                    ),
                ],
                executor=executor,
                max_concurrency=2,
            ),
            timeout=2,
        )
    )

    by_id = {result.id: result for result in results}
    assert by_id["find"].ok is True
    assert by_id["sibling"].ok is False
    assert "cancelled" in by_id["sibling"].error
    assert by_id["downstream"].ok is False
    assert "cancelled" in by_id["downstream"].error


def test_rendezvous_rejects_early_exit():
    with pytest.raises(ValueError, match="not supported for rendezvous"):
        validate_subtask_specs(
            [SubtaskSpec(id="a", role="debater", task="debate", early_exit=True)],
            mode="rendezvous",
        )


def test_rendezvous_round_stops_on_directive():
    async def executor(spec, *, round_index, lead_summary, lead_structured_context=None):
        return SubtaskResult(
            id=spec.id,
            ok=True,
            content=f"round:{round_index}",
            summary=f"round:{round_index}",
            tool_calls_made=[],
        )

    async def summarize(results):
        return RendezvousDirective(summary="consensus", stop=True)

    results = asyncio.run(
        run_rendezvous_round(
            [
                SubtaskSpec(id="a", role="debater", task="t"),
                SubtaskSpec(id="b", role="critic", task="t"),
            ],
            executor=executor,
            summarize=summarize,
            max_rounds=3,
        )
    )

    assert [result.id for result in results] == ["a", "b"]
    assert all(result.content == "round:1" for result in results)


def test_implementation_profile_requires_write_scope():
    with pytest.raises(ValueError, match="explicit write_scope"):
        validate_subtask_specs(
            [
                SubtaskSpec(
                    id="a",
                    role="implementer",
                    task="edit the file",
                    capability_profile="implementation",
                )
            ]
        )
    # Declaring a scope satisfies the contract.
    validate_subtask_specs(
        [
            SubtaskSpec(
                id="a",
                role="implementer",
                task="edit the file",
                capability_profile="implementation",
                write_scope=["src/a.py"],
            )
        ]
    )


def test_direct_single_spawn_validates_before_dispatch():
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )

    async def run():
        with pytest.raises(ValueError, match="explicit write_scope"):
            await agent._run_orchestrated_spawn_calls(
                [
                    (
                        0,
                        {
                            "input": {
                                "role": "implementer",
                                "task": "edit a file",
                                "capability_profile": "implementation",
                            }
                        },
                    )
                ],
                OrchestrationDecision(mode="explicit"),
            )

    asyncio.run(run())


def test_spawn_tool_use_spec_carries_system_suffix():
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    spec = agent._spawn_tool_use_to_spec(
        {
            "id": "s1",
            "input": {
                "role": "critic",
                "task": "inspect",
                "system_suffix": "Be strict.",
            },
        },
        index=1,
        orchestration_decision=OrchestrationDecision(mode="explicit"),
    )
    assert spec.system_suffix == "Be strict."


def test_rendezvous_directive_parse_normalizes_model_output():
    from agent.core.agent import BaseAgent

    parse = BaseAgent._parse_rendezvous_directive
    known = {"a", "b"}

    # String "false" must not be treated as truthy.
    directive = parse(
        '{"summary": "s", "stop": "false", "continue_with": null}',
        known_ids=known,
    )
    assert directive is not None
    assert directive.stop is False

    directive = parse(
        '{"summary": "s", "stop": true, "continue_with": ["a"]}',
        known_ids=known,
    )
    assert directive.stop is True
    assert directive.continue_with == ["a"]

    # Unknown ids fall back to "keep everyone" instead of narrowing to nothing.
    directive = parse(
        '{"summary": "s", "continue_with": ["nope"]}',
        known_ids=known,
    )
    assert directive.continue_with is None

    # A bare string selection is accepted when it names a known id.
    directive = parse(
        '{"summary": "s", "continue_with": "b"}',
        known_ids=known,
    )
    assert directive.continue_with == ["b"]

    # Fenced JSON is handled, malformed output returns None for the fallback.
    directive = parse(
        '```json\n{"summary": "s", "stop": false, "continue_with": null}\n```',
        known_ids=known,
    )
    assert directive is not None
    assert directive.stop is False
    assert parse("not json at all", known_ids=known) is None


def test_rendezvous_round_scopes_run_id_per_round():
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    observed: list[str] = []

    async def fake_execute(spec: SubtaskSpec) -> SubtaskResult:
        observed.append(spec.run_id)
        return _ok_result(spec)

    async def summarize(results):
        return RendezvousDirective(summary="s", stop=False)

    agent._execute_subtask_spec = fake_execute
    asyncio.run(
        agent.run_rendezvous_subtasks(
            [SubtaskSpec(id="a", role="debater", task="t", run_id="run1")],
            max_rounds=2,
        )
    )

    assert observed == ["run1#r1", "run1#r2"]


def test_rendezvous_always_produces_a_reachable_synthesis():
    """The lead synthesis is the deliverable of a rendezvous, so it must run
    after the final round and be published to the caller (regression: it ran
    only between rounds, so the last synthesis was never produced and an
    intermediate one was computed then discarded)."""
    rounds_summarized: list[list[str]] = []

    async def executor(spec, *, round_index, lead_summary, lead_structured_context=None):
        return SubtaskResult(
            id=spec.id,
            ok=True,
            content=f"position:{spec.id}:r{round_index}",
            summary=f"position:{spec.id}:r{round_index}",
            tool_calls_made=[],
        )

    async def summarize(results):
        rounds_summarized.append([result.id for result in results])
        return RendezvousDirective(summary="CONSENSUS", stop=False)

    telemetry: dict = {}
    asyncio.run(
        run_rendezvous_round(
            [
                SubtaskSpec(id="a", role="pro", task="t"),
                SubtaskSpec(id="b", role="con", task="t"),
            ],
            executor=executor,
            summarize=summarize,
            max_rounds=2,
            telemetry=telemetry,
        )
    )
    assert len(rounds_summarized) == 2, "final round must also be synthesized"
    assert telemetry["lead_summary"] == "CONSENSUS"
    assert telemetry["summary_quality"] == "llm"


def test_rendezvous_single_round_still_synthesizes():
    """A one-round rendezvous must still converge; otherwise it is just a
    parallel run wearing a debate label."""
    calls: list[int] = []

    async def executor(spec, *, round_index, lead_summary, lead_structured_context=None):
        return _ok_result(spec)

    async def summarize(results):
        calls.append(len(results))
        return RendezvousDirective(summary="single-round synthesis")

    telemetry: dict = {}
    asyncio.run(
        run_rendezvous_round(
            [SubtaskSpec(id="a", role="pro", task="t")],
            executor=executor,
            summarize=summarize,
            max_rounds=1,
            telemetry=telemetry,
        )
    )
    assert calls == [1]
    assert telemetry["lead_summary"] == "single-round synthesis"


def test_cancelled_count_ignores_child_errors_mentioning_cancellation():
    """cancelled_count must count runtime cancellations, not any child error
    whose text happens to contain the word (regression: substring match)."""

    async def executor(spec):
        if spec.id == "victim":
            return SubtaskResult(
                id=spec.id,
                ok=False,
                content="",
                tool_calls_made=[],
                error="upstream API returned: request cancelled by peer",
            )
        return _ok_result(spec)

    telemetry: dict = {}
    asyncio.run(
        run_parallel_subtasks(
            [
                SubtaskSpec(id="victim", role="r", task="t"),
                SubtaskSpec(id="other", role="r", task="t"),
            ],
            executor=executor,
            max_concurrency=2,
            telemetry=telemetry,
        )
    )
    assert telemetry["early_exit_triggered"] is False
    assert telemetry["cancelled_count"] == 0


def test_cancelled_count_counts_real_early_exit_cancellations():
    async def executor(spec):
        if spec.id == "winner":
            return _ok_result(spec)
        await asyncio.sleep(5)
        return _ok_result(spec)

    telemetry: dict = {}
    results = asyncio.run(
        run_parallel_subtasks(
            [
                SubtaskSpec(id="winner", role="r", task="t", early_exit=True),
                SubtaskSpec(id="loser", role="r", task="t"),
            ],
            executor=executor,
            max_concurrency=2,
            telemetry=telemetry,
        )
    )
    assert telemetry["early_exit_triggered"] is True
    assert telemetry["cancelled_count"] == 1
    assert any(result.error == CANCELLED_ERROR for result in results)


def test_sub_agent_timeout_bounds_the_whole_subtask_not_each_attempt():
    """sub_agent_timeout_seconds is a total budget: retries must not each get
    a fresh full timeout (regression: wall clock was attempts x timeout while
    the error still reported the single-attempt figure)."""
    import time

    import agent as agent_module

    registry = agent_module.ToolRegistry()
    parent = agent_module.BaseAgent(
        object(), registry, model="m", api_format="openai"
    )
    parent.sub_agent_timeout_seconds = 0.2
    parent.sub_agent_retries = 2  # would previously allow 3 x 0.2s
    parent._base_system_prompt = "sys"
    attempts: list[float] = []

    class _Hanging:
        async def send_message(self, ctx, task):
            attempts.append(time.monotonic())
            await asyncio.sleep(10)

    parent._create_sub_agent = lambda sub_registry: _Hanging()

    started = time.monotonic()
    payload = asyncio.run(
        parent._execute_agent(
            role="researcher", task="read docs", capability_profile="read_only"
        )
    )
    elapsed = time.monotonic() - started

    assert payload["ok"] is False
    assert payload["timed_out"] is True
    assert elapsed < 0.5, f"total budget overrun: {elapsed:.2f}s for a 0.2s budget"
    assert len(attempts) == 1, "a timeout consumes the budget; no retry is possible"
    assert "budget" in payload["error"]


def test_retry_still_happens_for_transient_failures_within_budget():
    """A non-timeout failure must still retry while budget remains."""
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    parent = agent_module.BaseAgent(
        object(), registry, model="m", api_format="openai"
    )
    parent.sub_agent_timeout_seconds = 30
    parent.sub_agent_retries = 2
    parent._base_system_prompt = "sys"
    calls: list[int] = []

    class _FlakyThenOk:
        async def send_message(self, ctx, task):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("transient upstream 503")
            return agent_module.AgentResult(
                agent_id="sub", content="finally ok", tool_calls_made=[], error=None
            )

    parent._create_sub_agent = lambda sub_registry: _FlakyThenOk()
    payload = asyncio.run(
        parent._execute_agent(
            role="researcher", task="read docs", capability_profile="read_only"
        )
    )
    assert len(calls) == 3
    assert payload["ok"] is True
    assert payload["content"] == "finally ok"


def _spawn_parent(**attrs):
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    parent = agent_module.BaseAgent(
        object(), registry, model="m", api_format="openai"
    )
    for key, value in attrs.items():
        setattr(parent, key, value)

    async def fake_exec(spec):
        return _ok_result(spec)

    parent._execute_subtask_spec = fake_exec
    return parent


def test_total_sub_agents_per_turn_is_bounded():
    """max_parallel_agents bounds concurrency only; one turn must not be able
    to fan out to an unbounded total number of sub-agents."""
    parent = _spawn_parent(max_parallel_agents=3)
    budget = parent._max_agents_per_turn()
    spawn_calls = [
        (i, {"input": {"role": f"w{i}", "task": "t", "id": f"w{i}"}})
        for i in range(budget + 1)
    ]
    with pytest.raises(ValueError, match="too many sub-agents"):
        asyncio.run(
            parent._run_orchestrated_spawn_calls(
                spawn_calls, OrchestrationDecision(mode="explicit")
            )
        )
    # Exactly at the limit is allowed.
    payloads, _ = asyncio.run(
        parent._run_orchestrated_spawn_calls(
            spawn_calls[:budget], OrchestrationDecision(mode="explicit")
        )
    )
    assert len(payloads) == budget


def test_agent_cap_is_configurable():
    parent = _spawn_parent(max_parallel_agents=3, max_agents_per_turn=2)
    assert parent._max_agents_per_turn() == 2


def test_rejected_batch_is_reported_as_tool_results_not_a_dead_turn():
    """A batch rejected before dispatch is a correctable model error, so it
    must come back as tool results instead of aborting the whole turn."""
    import json

    parent = _spawn_parent(max_parallel_agents=3, max_agents_per_turn=1)
    parent.registry.register(
        "spawn_agent",
        "spawn",
        {"type": "object", "properties": {}},
        lambda **kw: "",
        capabilities={"orchestration"},
    )
    tool_uses = [
        {"id": "t1", "name": "spawn_agent", "input": {"role": "a", "task": "t", "id": "a"}},
        {"id": "t2", "name": "spawn_agent", "input": {"role": "b", "task": "t", "id": "b"}},
    ]
    results = asyncio.run(parent._run_tool_uses(tool_uses))

    assert len(results) == len(tool_uses), "every tool_use must get a tool_result"
    for raw in results:
        payload = json.loads(raw)
        assert payload["ok"] is False
        assert "batch rejected" in payload["error"]
        assert "too many sub-agents" in payload["error"]


def test_missing_result_reason_names_the_actual_mode():
    """A rendezvous participant without a result must not be described as a
    failed pipeline stage."""
    import json

    parent = _spawn_parent(max_parallel_agents=3)

    async def fake_rendezvous(specs, *, max_rounds=2, telemetry=None):
        if telemetry is not None:
            telemetry["lead_summary"] = "synthesis text"
        return [
            SubtaskResult(
                id="a", ok=True, content="ok", summary="ok", tool_calls_made=[]
            )
        ]

    parent.run_rendezvous_subtasks = fake_rendezvous
    spawn_calls = [
        (0, {"input": {"role": "pro", "task": "t", "id": "a",
                       "coordination_mode": "rendezvous"}}),
        (1, {"input": {"role": "con", "task": "t", "id": "b",
                       "coordination_mode": "rendezvous"}}),
    ]
    payloads, _ = asyncio.run(
        parent._run_orchestrated_spawn_calls(
            spawn_calls, OrchestrationDecision(mode="explicit")
        )
    )
    missing = json.loads(payloads[1])
    assert "pipeline" not in missing["error"]
    assert "rendezvous" in missing["error"]
    # The synthesis reaches the parent through the first payload.
    assert json.loads(payloads[0])["run_synthesis"] == "synthesis text"
