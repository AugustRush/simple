from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass
class SubtaskSpec:
    id: str
    role: str
    task: str
    depends_on: list[str] = field(default_factory=list)
    expected_output: str = ""
    write_scope: list[str] = field(default_factory=list)


@dataclass
class SubtaskResult:
    id: str
    ok: bool
    content: str
    tool_calls_made: list[str]
    summary: str = ""
    error: str | None = None


async def run_parallel_subtasks(
    specs: list[SubtaskSpec],
    *,
    executor: Callable[[SubtaskSpec], Awaitable[SubtaskResult]],
    max_concurrency: int,
) -> list[SubtaskResult]:
    claimed_write_scopes: set[str] = set()
    for spec in specs:
        overlap = claimed_write_scopes.intersection(spec.write_scope)
        if overlap:
            raise ValueError(
                "overlapping write_scope detected: " + ", ".join(sorted(overlap))
            )
        claimed_write_scopes.update(spec.write_scope)

    sem = asyncio.Semaphore(max(1, int(max_concurrency)))

    async def _run(spec: SubtaskSpec) -> SubtaskResult:
        async with sem:
            try:
                return await executor(spec)
            except Exception as exc:
                return SubtaskResult(
                    id=spec.id,
                    ok=False,
                    content="",
                    tool_calls_made=[],
                    error=str(exc) or exc.__class__.__name__,
                )

    return await asyncio.gather(*[_run(spec) for spec in specs])


async def run_pipeline_subtasks(
    specs: list[SubtaskSpec],
    *,
    executor: Callable[[SubtaskSpec, dict[str, str]], Awaitable[SubtaskResult]],
) -> list[SubtaskResult]:
    pending = {spec.id: spec for spec in specs}
    summaries: dict[str, str] = {}
    results: list[SubtaskResult] = []

    while pending:
        progressed = False
        for spec_id, spec in list(pending.items()):
            if any(dep not in summaries for dep in spec.depends_on):
                continue
            upstream_summaries = {dep: summaries[dep] for dep in spec.depends_on}
            result = await executor(spec, upstream_summaries)
            summaries[spec.id] = result.summary
            results.append(result)
            pending.pop(spec_id)
            progressed = True
        if not progressed:
            raise ValueError("pipeline contains unresolved or cyclic dependencies")

    return results


async def run_rendezvous_round(
    specs: list[SubtaskSpec],
    *,
    executor: Callable[..., Awaitable[SubtaskResult]],
    summarize: Callable[[list[SubtaskResult]], str],
    max_rounds: int,
) -> list[SubtaskResult]:
    rounds = max(1, int(max_rounds))
    all_results: list[SubtaskResult] = []
    lead_summary = ""

    for round_index in range(1, rounds + 1):
        round_results: list[SubtaskResult] = []
        for spec in specs:
            result = await executor(
                spec,
                round_index=round_index,
                lead_summary=lead_summary,
            )
            round_results.append(result)
        all_results.extend(round_results)
        if round_index < rounds:
            lead_summary = summarize(round_results)

    return all_results
