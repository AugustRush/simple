"""Running subtasks as a debate: the executor that gives each participant the lead summary after a round and asks the summarizer what to do next. Moved out of BaseAgent (see the remaining-big-files survey work order). The two seams a test swaps out -- the per-subtask executor and the round summarizer -- arrive as capabilities, resolved from the instance at call time so a patch on the agent still intercepts."""

from __future__ import annotations

from dataclasses import replace

from agent.orchestration.runtime import SubtaskResult, SubtaskSpec, run_rendezvous_round as _run_rendezvous_round

from typing import Any

__all__ = [
    "run_rendezvous_subtasks",
]


async def run_rendezvous_subtasks(
    specs: list[SubtaskSpec],
    *,
    max_rounds: int = 2,
    telemetry: dict[str, Any] | None = None,
    max_parallel_agents,
    execute_subtask_spec,
    summarize_rendezvous_round,
    canonicalize_write_scope,
    emit_runtime_progress_event,
    with_lead_summary,
) -> list[SubtaskResult]:
    async def _executor(
        spec: SubtaskSpec,
        *,
        round_index: int,
        lead_summary: str,
        lead_structured_context: dict[str, Any] | None = None,
    ) -> SubtaskResult:
        adjusted = (
            with_lead_summary(
                spec,
                lead_summary,
                lead_structured_context,
            )
            if round_index > 1
            else spec
        )
        # Scope per-round LTM observations so repeated rounds of the same
        # agent do not collide under one run id.
        if adjusted.run_id:
            adjusted = replace(
                adjusted,
                run_id=f"{adjusted.run_id}#r{round_index}",
            )
        return await execute_subtask_spec(adjusted)

    return await _run_rendezvous_round(
        specs,
        executor=_executor,
        summarize=summarize_rendezvous_round,
        max_rounds=max_rounds,
        max_concurrency=max_parallel_agents,
        canonicalize_write_scope=canonicalize_write_scope,
        telemetry=telemetry,
        progress_callback=emit_runtime_progress_event,
    )
