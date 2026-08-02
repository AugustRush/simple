from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import inspect
import math
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
    system_suffix: str = ""
    output_contract: dict[str, Any] = field(default_factory=dict)
    write_scope: list[str] = field(default_factory=list)
    capability_profile: str = "read_only"
    run_id: str = ""
    handoff: dict[str, Any] = field(default_factory=dict)
    early_exit: bool = False
    timeout_seconds: float = 0
    continue_on_failure: bool = False


@dataclass
class SubtaskResult:
    id: str
    ok: bool
    content: str
    tool_calls_made: list[str]
    summary: str = ""
    structured_content: Any = None
    error: str | None = None
    full_content: str = ""
    artifact_ref: str = ""


@dataclass(frozen=True)
class RendezvousDirective:
    summary: str = ""
    structured_context: dict[str, Any] | None = None
    continue_with: list[str] | None = None
    stop: bool = False
    summary_quality: str = "llm"


RuntimeProgressCallback = Callable[[str, dict[str, Any]], None]
CAPABILITY_PROFILES = frozenset({"read_only", "research", "implementation", "full"})

# The single error text produced by runtime-initiated cancellation.  Telemetry
# matches this exactly rather than substring-searching for "cancelled", which
# would also count a child's own error that merely mentions the word.
CANCELLED_ERROR = "cancelled: another agent triggered early exit"


def _emit_progress(
    progress_callback: RuntimeProgressCallback | None,
    kind: str,
    **payload: Any,
) -> None:
    if progress_callback is not None:
        progress_callback(kind, payload)


def _validate_write_scopes(
    specs: list[SubtaskSpec],
    canonicalize_write_scope: Callable[[str], Path] | None = None,
) -> tuple[int, float]:
    """Check for overlapping write scopes across specs.

    Returns (write_scope_count, duration_seconds).  Raises ValueError
    if two specs claim overlapping paths.
    """
    started = time.perf_counter()
    normalize = canonicalize_write_scope or (
        lambda raw_scope: canonicalize_user_path(raw_scope, base_dir=Path.cwd())
    )
    claimed: list[tuple[str, Path]] = []
    count = 0
    for spec in specs:
        for raw_scope in spec.write_scope:
            count += 1
            normalized = normalize(raw_scope)
            for claimed_raw, claimed_path in claimed:
                if paths_overlap(normalized, claimed_path):
                    raise ValueError(
                        "overlapping write_scope detected: "
                        + ", ".join(sorted({claimed_raw, raw_scope}))
                    )
            claimed.append((raw_scope, normalized))
    return count, time.perf_counter() - started


def _validate_concurrent_capabilities(
    specs: list[SubtaskSpec],
    *,
    max_concurrency: int,
) -> None:
    """Reject concurrent workers whose write surface cannot be isolated."""
    if len(specs) < 2 or max(1, int(max_concurrency)) == 1:
        return
    unrestricted = [
        spec.id
        for spec in specs
        if spec.capability_profile == "full" and not spec.write_scope
    ]
    if unrestricted:
        raise ValueError(
            "concurrent full-capability subtasks require an explicit write_scope: "
            + ", ".join(unrestricted)
        )


def validate_subtask_specs(
    specs: list[SubtaskSpec],
    *,
    mode: str = "",
) -> None:
    """Validate the execution graph before starting any child work."""
    seen: set[str] = set()
    by_id: dict[str, SubtaskSpec] = {}
    for spec in specs:
        spec_id = str(spec.id or "").strip()
        if not spec_id:
            raise ValueError("subtask id must be non-empty")
        if spec_id in seen:
            raise ValueError(f"duplicate subtask id: {spec_id}")
        seen.add(spec_id)
        by_id[spec_id] = spec
        if not str(spec.role or "").strip():
            raise ValueError(f"subtask {spec_id} has an empty role")
        if not str(spec.task or "").strip():
            raise ValueError(f"subtask {spec_id} has an empty task")
        profile = str(spec.capability_profile or "").strip().lower()
        if profile not in CAPABILITY_PROFILES:
            raise ValueError(
                f"subtask {spec_id} has unsupported capability_profile: {spec.capability_profile!r}"
            )
        if profile == "implementation" and not spec.write_scope:
            raise ValueError(
                f"subtask {spec_id} has capability_profile 'implementation' but no "
                "write_scope; workspace writes require an explicit write_scope"
            )
        if mode == "rendezvous" and spec.early_exit:
            raise ValueError(
                f"subtask {spec_id} uses early_exit, which is not supported for "
                "rendezvous subtasks (rendezvous is a bounded convergence protocol, "
                "not a winner-take-all fan-out)"
            )
        if spec.timeout_seconds and (
            not math.isfinite(float(spec.timeout_seconds))
            or float(spec.timeout_seconds) <= 0
        ):
            raise ValueError(f"subtask {spec_id} timeout_seconds must be positive")
    for spec in specs:
        spec_id = str(spec.id).strip()
        dependencies = [str(dep or "").strip() for dep in spec.depends_on]
        if len(dependencies) != len(set(dependencies)):
            raise ValueError(f"subtask {spec_id} contains duplicate dependencies")
        if spec_id in dependencies:
            raise ValueError(f"subtask {spec_id} cannot depend on itself")
        missing = [dep for dep in dependencies if dep not in by_id]
        if missing:
            raise ValueError(
                f"subtask {spec_id} depends on unknown subtask(s): {', '.join(missing)}"
            )
    if mode in {"parallel", "rendezvous"} and any(
        spec.depends_on for spec in specs
    ):
        raise ValueError(f"{mode} subtasks cannot declare dependencies")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(spec_id: str) -> None:
        if spec_id in visiting:
            raise ValueError("pipeline contains cyclic dependencies")
        if spec_id in visited:
            return
        visiting.add(spec_id)
        for dependency in by_id[spec_id].depends_on:
            visit(str(dependency).strip())
        visiting.remove(spec_id)
        visited.add(spec_id)

    for spec_id in by_id:
        visit(spec_id)


def _failed_result(spec: SubtaskSpec, error: str) -> SubtaskResult:
    return SubtaskResult(
        id=spec.id,
        ok=False,
        content="",
        tool_calls_made=[],
        error=error,
    )


def _cancelled_result(spec: SubtaskSpec) -> SubtaskResult:
    return _failed_result(spec, CANCELLED_ERROR)


async def _run_cancellable(
    semaphore: asyncio.Semaphore,
    early_exit_event: asyncio.Event | None,
    spec: SubtaskSpec,
    operation: Callable[[], Awaitable[SubtaskResult]],
) -> SubtaskResult:
    """Run one subtask under the semaphore with a uniform cancellation contract.

    Early-exit cancellation must be translated into a graceful cancelled
    result instead of a propagated CancelledError.  The try/except has to wrap
    semaphore acquisition too: a task cancelled while queued on the semaphore
    would otherwise raise CancelledError from ``sem.acquire()`` and abort the
    whole batch.  A genuine external cancellation (no early-exit event) is
    re-raised unchanged.
    """
    try:
        async with semaphore:
            if early_exit_event is not None and early_exit_event.is_set():
                return _cancelled_result(spec)
            try:
                return await operation()
            except asyncio.CancelledError:
                if early_exit_event is not None and early_exit_event.is_set():
                    return _cancelled_result(spec)
                raise
    except asyncio.CancelledError:
        if early_exit_event is not None and early_exit_event.is_set():
            return _cancelled_result(spec)
        raise


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
    validate_subtask_specs(specs, mode="parallel")
    _validate_concurrent_capabilities(specs, max_concurrency=max_concurrency)
    write_scope_count, write_scope_check_seconds = _validate_write_scopes(
        specs, canonicalize_write_scope
    )

    sem = asyncio.Semaphore(max(1, int(max_concurrency)))
    early_exit_event = asyncio.Event()
    early_exit_triggered = False

    async def _run(index: int, spec: SubtaskSpec) -> tuple[int, SubtaskResult]:
        async def operation() -> SubtaskResult:
            try:
                if spec.timeout_seconds and spec.timeout_seconds > 0:
                    result = await asyncio.wait_for(executor(spec), timeout=spec.timeout_seconds)
                else:
                    result = await executor(spec)
            except asyncio.TimeoutError:
                result = SubtaskResult(
                    id=spec.id,
                    ok=False,
                    content="",
                    tool_calls_made=[],
                    error=f"subtask timed out after {spec.timeout_seconds}s",
                )
            except Exception as exc:
                result = SubtaskResult(
                    id=spec.id,
                    ok=False,
                    content="",
                    tool_calls_made=[],
                    error=str(exc) or exc.__class__.__name__,
                )
            return result

        result = await _run_cancellable(sem, early_exit_event, spec, operation)
        return index, result

    results: list[SubtaskResult | None] = [None] * len(specs)
    completed_count = 0
    cancelled_count = 0
    task_objects = [
        asyncio.create_task(_run(index, spec))
        for index, spec in enumerate(specs)
    ]
    try:
        for task in asyncio.as_completed(task_objects):
            index, result = await task
            results[index] = result
            completed_count = len([r for r in results if r is not None])
            if result.ok and specs[index].early_exit and not early_exit_triggered:
                early_exit_triggered = True
                early_exit_event.set()
                for t in task_objects:
                    if not t.done():
                        t.cancel()
            cancelled_count = sum(
                1 for r in results if r is not None and r.error == CANCELLED_ERROR
            )
            _emit_progress(
                progress_callback,
                "batch_progress",
                execution_mode="parallel",
                completed=completed_count,
                total=len(specs),
                spec_count=len(specs),
                max_concurrency=max(1, int(max_concurrency)),
                cancelled=cancelled_count,
            )
    except BaseException:
        for task in task_objects:
            if not task.done():
                task.cancel()
        await asyncio.gather(*task_objects, return_exceptions=True)
        raise
    if telemetry is not None:
        telemetry.update(
            {
                "execution_mode": "parallel",
                "spec_count": len(specs),
                "max_concurrency": max(1, int(max_concurrency)),
                "write_scope_count": write_scope_count,
                "write_scope_check_seconds": write_scope_check_seconds,
                "cancelled_count": cancelled_count,
                "early_exit_triggered": early_exit_triggered,
                "duration_seconds": time.perf_counter() - started_at,
            }
        )
    return [result for result in results if result is not None]


async def run_pipeline_subtasks(
    specs: list[SubtaskSpec],
    *,
    executor: Callable[[SubtaskSpec, dict[str, str]], Awaitable[SubtaskResult]],
    max_concurrency: int | None = None,
    canonicalize_write_scope: Callable[[str], Path] | None = None,
    telemetry: dict[str, Any] | None = None,
    progress_callback: RuntimeProgressCallback | None = None,
) -> list[SubtaskResult]:
    started_at = time.perf_counter()
    validate_subtask_specs(specs, mode="pipeline")
    concurrency = max(1, int(max_concurrency or len(specs) or 1))
    semaphore = asyncio.Semaphore(concurrency)
    early_exit_event = asyncio.Event()
    early_exit_triggered = False
    pending = {spec.id: spec for spec in specs}
    summaries: dict[str, str] = {}
    successful_results: dict[str, SubtaskResult] = {}
    failed_ids: set[str] = set()
    results: list[SubtaskResult] = []
    stage_count = 0
    write_scope_count = 0
    write_scope_check_seconds = 0.0

    while pending:
        blocked_ids = [
            spec_id
            for spec_id, spec in pending.items()
            if any(dep in failed_ids for dep in spec.depends_on)
        ]
        if blocked_ids:
            blocked_specs = [pending[spec_id] for spec_id in blocked_ids]
            _emit_progress(
                progress_callback,
                "phase_note",
                execution_mode="pipeline",
                phase_kind="skipped",
                skipped_ids=blocked_ids,
                skipped_roles=[spec.role for spec in blocked_specs],
                reason="upstream stage failed",
                spec_count=len(specs),
            )
            for spec_id in blocked_ids:
                pending.pop(spec_id)
                failed_ids.add(spec_id)
            continue
        ready = [
            (spec_id, spec)
            for spec_id, spec in pending.items()
            if all(dep in summaries for dep in spec.depends_on)
        ]
        if not ready:
            raise ValueError("pipeline contains unresolved or cyclic dependencies")
        ready_specs = [spec for _, spec in ready]
        _validate_concurrent_capabilities(
            ready_specs,
            max_concurrency=concurrency,
        )
        stage_scope_count, stage_scope_seconds = _validate_write_scopes(
            ready_specs,
            canonicalize_write_scope,
        )
        write_scope_count += stage_scope_count
        write_scope_check_seconds += stage_scope_seconds
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
        async def run_ready(index: int, spec: SubtaskSpec) -> tuple[int, SubtaskResult]:
            async def operation() -> SubtaskResult:
                try:
                    invoked = _invoke_pipeline_executor(
                        executor,
                        spec,
                        {dep: summaries[dep] for dep in spec.depends_on},
                        {dep: successful_results[dep] for dep in spec.depends_on},
                    )
                    if spec.timeout_seconds:
                        return await asyncio.wait_for(invoked, spec.timeout_seconds)
                    return await invoked
                except asyncio.TimeoutError:
                    return _failed_result(
                        spec,
                        f"subtask timed out after {spec.timeout_seconds}s",
                    )
                except Exception as exc:
                    return _failed_result(spec, str(exc) or exc.__class__.__name__)

            result = await _run_cancellable(
                semaphore,
                early_exit_event,
                spec,
                operation,
            )
            return index, result

        stage_tasks = [
            asyncio.create_task(run_ready(index, spec))
            for index, (_, spec) in enumerate(ready)
        ]
        stage_results: list[SubtaskResult | None] = [None] * len(ready)
        try:
            for future in asyncio.as_completed(stage_tasks):
                index, result = await future
                stage_results[index] = result
                if (
                    result.ok
                    and ready[index][1].early_exit
                    and not early_exit_triggered
                ):
                    early_exit_triggered = True
                    early_exit_event.set()
                    for task in stage_tasks:
                        if not task.done():
                            task.cancel()
        except BaseException:
            for task in stage_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*stage_tasks, return_exceptions=True)
            raise
        stage_results = [result for result in stage_results if result is not None]
        succeeded_count = sum(1 for result in stage_results if result.ok)
        failed_count = len(stage_results) - succeeded_count
        hard_failed_count = sum(
            1
            for (_, spec), result in zip(ready, stage_results)
            if not result.ok and not spec.continue_on_failure
        )
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
            halted=hard_failed_count > 0,
            early_exit_triggered=early_exit_triggered,
            spec_count=len(specs),
        )
        for (spec_id, spec), result in zip(ready, stage_results):
            results.append(result)
            pending.pop(spec_id)
            if not result.ok:
                if spec.continue_on_failure:
                    summaries[spec.id] = f"[failed] {result.error or 'unknown error'}"
                    successful_results[spec.id] = result
                    continue
                failed_ids.add(spec.id)
                continue
            summaries[spec.id] = result.summary
            successful_results[spec.id] = result
        if early_exit_triggered:
            for spec_id, spec in pending.items():
                results.append(_cancelled_result(spec))
            pending.clear()
            break
    if telemetry is not None:
        telemetry.update(
            {
                "execution_mode": "pipeline",
                "spec_count": len(specs),
                "stage_count": stage_count,
                "completed_count": len(results),
                "skipped_count": len(specs) - len(results),
                "max_concurrency": concurrency,
                "early_exit_triggered": early_exit_triggered,
                "write_scope_count": write_scope_count,
                "write_scope_check_seconds": write_scope_check_seconds,
                "duration_seconds": time.perf_counter() - started_at,
            }
        )
    return results


async def run_rendezvous_round(
    specs: list[SubtaskSpec],
    *,
    executor: Callable[..., Awaitable[SubtaskResult]],
    summarize: Callable[
        [list[SubtaskResult]],
        str | RendezvousDirective | Awaitable[str | RendezvousDirective],
    ],
    max_rounds: int,
    max_concurrency: int | None = None,
    canonicalize_write_scope: Callable[[str], Path] | None = None,
    telemetry: dict[str, Any] | None = None,
    progress_callback: RuntimeProgressCallback | None = None,
) -> list[SubtaskResult]:
    started_at = time.perf_counter()
    validate_subtask_specs(specs, mode="rendezvous")
    concurrency = max(1, int(max_concurrency or len(specs) or 1))
    _validate_concurrent_capabilities(specs, max_concurrency=concurrency)
    write_scope_count, write_scope_check_seconds = _validate_write_scopes(
        specs, canonicalize_write_scope
    )
    rounds = max(1, int(max_rounds))
    all_results: list[SubtaskResult] = []
    lead_summary = ""
    summary_quality = ""
    lead_structured_context: dict[str, Any] | None = None
    active_specs = list(specs)
    rounds_completed = 0
    semaphore = asyncio.Semaphore(concurrency)

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
        async def run_participant(spec: SubtaskSpec) -> SubtaskResult:
            async with semaphore:
                try:
                    operation = _invoke_rendezvous_executor(
                        executor,
                        spec,
                        round_index=round_index,
                        lead_summary=lead_summary,
                        lead_structured_context=lead_structured_context,
                    )
                    if spec.timeout_seconds:
                        return await asyncio.wait_for(operation, spec.timeout_seconds)
                    return await operation
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    return _failed_result(
                        spec,
                        f"subtask timed out after {spec.timeout_seconds}s",
                    )
                except Exception as exc:
                    return _failed_result(spec, str(exc) or exc.__class__.__name__)

        round_tasks = [
            asyncio.create_task(run_participant(spec)) for spec in active_specs
        ]
        try:
            round_results = await asyncio.gather(*round_tasks)
        except BaseException:
            for task in round_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*round_tasks, return_exceptions=True)
            raise
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
        # Synthesis runs after EVERY round, including the last one.  The lead
        # summary is the deliverable of a rendezvous — a convergence protocol
        # that returned only the raw final-round positions would be an
        # expensive parallel run.  On non-final rounds it also primes the
        # next round.
        directive = summarize(round_results)
        if inspect.isawaitable(directive):
            directive = await directive
        if isinstance(directive, str):
            directive = RendezvousDirective(summary=directive)
        lead_summary = directive.summary
        lead_structured_context = directive.structured_context
        summary_quality = directive.summary_quality
        is_final_round = round_index >= rounds or directive.stop
        if is_final_round:
            next_specs = []
        elif directive.continue_with is None:
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
            stop=directive.stop or is_final_round,
            final=is_final_round,
            spec_count=len(specs),
        )
        if is_final_round:
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
                # The synthesis is the run's deliverable; publish it so the
                # caller can forward it to the parent model.
                "lead_summary": lead_summary,
                "summary_quality": summary_quality,
                "lead_structured_context": lead_structured_context,
                "write_scope_count": write_scope_count,
                "write_scope_check_seconds": write_scope_check_seconds,
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
