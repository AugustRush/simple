from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import inspect
from pathlib import Path
import time
from typing import Any, Awaitable, Callable

from agent.pathing import canonicalize_user_path, paths_overlap


@dataclass
class SubtaskSpec:
    id: str
    role: str
    task: str
    depends_on: list[str] = field(default_factory=list)
    expected_output: str = ""
    output_contract: dict[str, Any] = field(default_factory=dict)
    write_scope: list[str] = field(default_factory=list)
    capability_profile: str = "full"


@dataclass
class SubtaskResult:
    id: str
    ok: bool
    content: str
    tool_calls_made: list[str]
    summary: str = ""
    structured_content: Any = None
    error: str | None = None


@dataclass(frozen=True)
class RendezvousDirective:
    summary: str = ""
    structured_context: dict[str, Any] | None = None
    continue_with: list[str] | None = None
    stop: bool = False


RuntimeProgressCallback = Callable[[str, dict[str, Any]], None]


def _emit_progress(
    progress_callback: RuntimeProgressCallback | None,
    kind: str,
    **payload: Any,
) -> None:
    if progress_callback is not None:
        progress_callback(kind, payload)


async def run_parallel_subtasks(
    specs: list[SubtaskSpec],
    *,
    executor: Callable[[SubtaskSpec], Awaitable[SubtaskResult]],
    max_concurrency: int,
    canonicalize_write_scope: Callable[[str], Path] | None = None,
    telemetry: dict[str, Any] | None = None,
    progress_callback: RuntimeProgressCallback | None = None,
) -> list[SubtaskResult]:
    started_at = time.perf_counter()
    scope_check_started_at = time.perf_counter()
    normalize_write_scope = canonicalize_write_scope or (
        lambda raw_scope: canonicalize_user_path(raw_scope, base_dir=Path.cwd())
    )
    claimed_write_scopes: list[tuple[str, Path]] = []
    write_scope_count = 0
    for spec in specs:
        for raw_scope in spec.write_scope:
            write_scope_count += 1
            normalized_scope = normalize_write_scope(raw_scope)
            for claimed_raw_scope, claimed_scope in claimed_write_scopes:
                if paths_overlap(normalized_scope, claimed_scope):
                    raise ValueError(
                        "overlapping write_scope detected: "
                        + ", ".join(sorted({claimed_raw_scope, raw_scope}))
                    )
            claimed_write_scopes.append((raw_scope, normalized_scope))
    write_scope_check_seconds = time.perf_counter() - scope_check_started_at

    sem = asyncio.Semaphore(max(1, int(max_concurrency)))

    async def _run(index: int, spec: SubtaskSpec) -> tuple[int, SubtaskResult]:
        async with sem:
            try:
                result = await executor(spec)
            except Exception as exc:
                result = SubtaskResult(
                    id=spec.id,
                    ok=False,
                    content="",
                    tool_calls_made=[],
                    error=str(exc) or exc.__class__.__name__,
                )
            return index, result

    results: list[SubtaskResult | None] = [None] * len(specs)
    completed_count = 0
    tasks = [
        asyncio.create_task(_run(index, spec))
        for index, spec in enumerate(specs)
    ]
    for task in asyncio.as_completed(tasks):
        index, result = await task
        results[index] = result
        completed_count += 1
        _emit_progress(
            progress_callback,
            "batch_progress",
            execution_mode="parallel",
            completed=completed_count,
            total=len(specs),
            spec_count=len(specs),
            max_concurrency=max(1, int(max_concurrency)),
        )
    if telemetry is not None:
        telemetry.update(
            {
                "execution_mode": "parallel",
                "spec_count": len(specs),
                "max_concurrency": max(1, int(max_concurrency)),
                "write_scope_count": write_scope_count,
                "write_scope_check_seconds": write_scope_check_seconds,
                "duration_seconds": time.perf_counter() - started_at,
            }
        )
    return [result for result in results if result is not None]


async def run_pipeline_subtasks(
    specs: list[SubtaskSpec],
    *,
    executor: Callable[[SubtaskSpec, dict[str, str]], Awaitable[SubtaskResult]],
    telemetry: dict[str, Any] | None = None,
    progress_callback: RuntimeProgressCallback | None = None,
) -> list[SubtaskResult]:
    started_at = time.perf_counter()
    pending = {spec.id: spec for spec in specs}
    summaries: dict[str, str] = {}
    successful_results: dict[str, SubtaskResult] = {}
    results: list[SubtaskResult] = []
    stage_count = 0

    while pending:
        ready = [
            (spec_id, spec)
            for spec_id, spec in pending.items()
            if all(dep in summaries for dep in spec.depends_on)
        ]
        if not ready:
            raise ValueError("pipeline contains unresolved or cyclic dependencies")
        stage_count += 1
        _emit_progress(
            progress_callback,
            "phase_started",
            execution_mode="pipeline",
            phase_kind="stage",
            phase_index=stage_count,
            ready_count=len(ready),
            ready_ids=[spec.id for _, spec in ready],
            ready_roles=[spec.role for _, spec in ready],
            spec_count=len(specs),
        )
        stage_results = await asyncio.gather(
            *[
                _invoke_pipeline_executor(
                    executor,
                    spec,
                    {dep: summaries[dep] for dep in spec.depends_on},
                    {dep: successful_results[dep] for dep in spec.depends_on},
                )
                for _, spec in ready
            ]
        )
        succeeded_count = sum(1 for result in stage_results if result.ok)
        failed_count = len(stage_results) - succeeded_count
        _emit_progress(
            progress_callback,
            "phase_finished",
            execution_mode="pipeline",
            phase_kind="stage",
            phase_index=stage_count,
            ready_count=len(ready),
            ready_ids=[spec.id for _, spec in ready],
            ready_roles=[spec.role for _, spec in ready],
            succeeded_count=succeeded_count,
            failed_count=failed_count,
            halted=failed_count > 0,
            spec_count=len(specs),
        )
        stage_failed = False
        for (spec_id, spec), result in zip(ready, stage_results):
            results.append(result)
            pending.pop(spec_id)
            if not result.ok:
                stage_failed = True
                continue
            summaries[spec.id] = result.summary
            successful_results[spec.id] = result
        if stage_failed:
            if telemetry is not None:
                telemetry.update(
                    {
                        "execution_mode": "pipeline",
                        "spec_count": len(specs),
                        "stage_count": stage_count,
                        "completed_count": len(results),
                        "duration_seconds": time.perf_counter() - started_at,
                    }
                )
            return results

    if telemetry is not None:
        telemetry.update(
            {
                "execution_mode": "pipeline",
                "spec_count": len(specs),
                "stage_count": stage_count,
                "completed_count": len(results),
                "duration_seconds": time.perf_counter() - started_at,
            }
        )
    return results


async def run_rendezvous_round(
    specs: list[SubtaskSpec],
    *,
    executor: Callable[..., Awaitable[SubtaskResult]],
    summarize: Callable[[list[SubtaskResult]], str | RendezvousDirective],
    max_rounds: int,
    telemetry: dict[str, Any] | None = None,
    progress_callback: RuntimeProgressCallback | None = None,
) -> list[SubtaskResult]:
    started_at = time.perf_counter()
    rounds = max(1, int(max_rounds))
    all_results: list[SubtaskResult] = []
    lead_summary = ""
    lead_structured_context: dict[str, Any] | None = None
    active_specs = list(specs)
    rounds_completed = 0

    for round_index in range(1, rounds + 1):
        rounds_completed = round_index
        _emit_progress(
            progress_callback,
            "phase_started",
            execution_mode="rendezvous",
            phase_kind="round",
            phase_index=round_index,
            phase_total=rounds,
            participant_count=len(active_specs),
            participant_ids=[spec.id for spec in active_specs],
            participant_roles=[spec.role for spec in active_specs],
            spec_count=len(specs),
        )
        round_results = await asyncio.gather(
            *[
                _invoke_rendezvous_executor(
                    executor,
                    spec,
                    round_index=round_index,
                    lead_summary=lead_summary,
                    lead_structured_context=lead_structured_context,
                )
                for spec in active_specs
            ]
        )
        all_results.extend(round_results)
        succeeded_count = sum(1 for result in round_results if result.ok)
        failed_count = len(round_results) - succeeded_count
        _emit_progress(
            progress_callback,
            "phase_finished",
            execution_mode="rendezvous",
            phase_kind="round",
            phase_index=round_index,
            phase_total=rounds,
            participant_count=len(active_specs),
            participant_ids=[spec.id for spec in active_specs],
            participant_roles=[spec.role for spec in active_specs],
            succeeded_count=succeeded_count,
            failed_count=failed_count,
            spec_count=len(specs),
        )
        if round_index < rounds:
            directive = summarize(round_results)
            if isinstance(directive, str):
                directive = RendezvousDirective(summary=directive)
            lead_summary = directive.summary
            lead_structured_context = directive.structured_context
            if directive.continue_with is None:
                next_specs = list(specs)
            else:
                selected_ids = set(directive.continue_with)
                next_specs = [spec for spec in specs if spec.id in selected_ids]
            _emit_progress(
                progress_callback,
                "phase_note",
                execution_mode="rendezvous",
                phase_kind="lead_summary",
                phase_index=round_index,
                phase_total=rounds,
                continue_count=len(next_specs),
                continue_ids=[spec.id for spec in next_specs],
                continue_roles=[spec.role for spec in next_specs],
                stop=directive.stop,
                spec_count=len(specs),
            )
            if directive.stop:
                break
            active_specs = next_specs
            if not active_specs:
                break

    if telemetry is not None:
        telemetry.update(
            {
                "execution_mode": "rendezvous",
                "spec_count": len(specs),
                "rounds_completed": rounds_completed,
                "result_count": len(all_results),
                "duration_seconds": time.perf_counter() - started_at,
            }
        )
    return all_results


async def _invoke_pipeline_executor(
    executor: Callable[[SubtaskSpec, dict[str, str]], Awaitable[SubtaskResult]],
    spec: SubtaskSpec,
    upstream_summaries: dict[str, str],
    upstream_results: dict[str, SubtaskResult],
) -> SubtaskResult:
    if "upstream_results" in inspect.signature(executor).parameters:
        return await executor(
            spec,
            upstream_summaries,
            upstream_results=upstream_results,
        )
    return await executor(spec, upstream_summaries)


async def _invoke_rendezvous_executor(
    executor: Callable[..., Awaitable[SubtaskResult]],
    spec: SubtaskSpec,
    *,
    round_index: int,
    lead_summary: str,
    lead_structured_context: dict[str, Any] | None,
) -> SubtaskResult:
    if "lead_structured_context" in inspect.signature(executor).parameters:
        return await executor(
            spec,
            round_index=round_index,
            lead_summary=lead_summary,
            lead_structured_context=lead_structured_context,
        )
    return await executor(
        spec,
        round_index=round_index,
        lead_summary=lead_summary,
    )
