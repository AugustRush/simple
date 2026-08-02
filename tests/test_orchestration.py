"""Tests for the multi-agent orchestration runtime contracts."""

import asyncio

import pytest

from agent.orchestration import RendezvousDirective, SubtaskResult, SubtaskSpec
from agent.orchestration.planner import OrchestrationDecision
from agent.orchestration.runtime import (
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
