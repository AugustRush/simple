"""How Feishu words progress: tool lines and sub-agent batch lines.

Why this module exists
----------------------
These ten functions were methods on ``FeishuOutputSink``.  They are the part of
it that turns structured progress into the sentence a person reads -- a tool's
status with its percentage and byte counts, and a sub-agent batch's mode, stage
count and duration.  They touch no client, no thread and no event loop, but
before this move they had **no direct test at all**: the only coverage was
indirect, through ``on_subagent_event`` and ``_render_primary_markdown``
asserting the finished text.  A formatting rule could therefore be changed and
only be caught by a test about something else.

The rules worth stating, because each is a case the code refuses to guess:

- **A boolean is not a number here.**  ``True`` is an ``int`` in Python, so
  ``positive_int`` and ``format_seconds`` reject ``bool`` explicitly rather
  than reporting "1 subtask" or "1.00s" for a flag.
- **A missing denominator means no percentage**, not a division error: the
  percentage is only added when ``total`` is truthy.
- **A batch line is only produced when its facts are known.**  A parallel batch
  with no ``max_parallel_agents`` returns ``None`` and falls back to the
  runtime's own message, rather than advertising a concurrency nobody declared;
  a pipeline that lost subtasks says "ended early" instead of "finished".
- **An unknown event with no message renders as the empty string**, which
  ``_append_progress_text`` (still on the sink, where the buffer lives) drops --
  so an unrecognised event cannot append a blank line to the card.

Behaviour is preserved exactly; the fallback chain
(``format_mode_aware_subagent_event`` trying the batch formatter first, then the
generic one) is unchanged.
"""

from __future__ import annotations

from typing import Any

from agent.core import SubAgentProgressEvent


def format_byte_count(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    size = float(value)
    units = ("B", "KB", "MB", "GB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return None


def format_tool_progress_line(name: str, progress: dict[str, Any]) -> str:
    status = str(progress.get("status") or "running").replace("_", " ")
    message = str(progress.get("message") or "").strip()
    current = progress.get("current")
    total = progress.get("total")
    suffix = ""
    if isinstance(current, (int, float)) and isinstance(total, (int, float)) and total:
        suffix = f" {float(current) / float(total) * 100:.0f}%"

    bytes_done = format_byte_count(progress.get("bytes_done"))
    bytes_total = format_byte_count(progress.get("bytes_total"))
    byte_part = ""
    if bytes_done and bytes_total:
        byte_part = f" ({bytes_done}/{bytes_total})"
    elif bytes_done:
        byte_part = f" ({bytes_done})"

    detail = f" - {message}" if message else ""
    return f"- **{name}**: {status}{suffix}{byte_part}{detail}"


def format_mode_aware_subagent_event(event: SubAgentProgressEvent) -> str:
    if event.kind == "batch_started":
        formatted = format_batch_started_event(event)
        if formatted:
            return formatted
    if event.kind == "batch_finished":
        formatted = format_batch_finished_event(event)
        if formatted:
            return formatted
    return format_subagent_event(event)


def positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        candidate = int(value)
        return candidate if candidate > 0 else None
    return None


def event_spec_count(event: SubAgentProgressEvent) -> int | None:
    return positive_int(event.metrics.get("spec_count")) or positive_int(
        event.total
    )


def format_seconds(value: Any, *, precision: int) -> str | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    return f"{float(value):.{precision}f}s"


def pluralize(value: int, singular: str) -> str:
    return singular if value == 1 else f"{singular}s"


def format_batch_started_event(event: SubAgentProgressEvent) -> str | None:
    metrics = event.metrics or {}
    mode = str(metrics.get("execution_mode", "") or "").strip().lower()
    spec_count = event_spec_count(event)
    if mode == "parallel" and spec_count is not None:
        max_parallel_agents = positive_int(metrics.get("max_parallel_agents"))
        if max_parallel_agents is None:
            return None
        return (
            f"Parallel batch: {spec_count} subtasks, "
            f"max concurrency {max_parallel_agents}"
        )
    if mode == "pipeline" and spec_count is not None:
        return (
            f"Pipeline batch: {spec_count} subtasks, "
            "dependency-driven execution"
        )
    if mode == "rendezvous" and spec_count is not None:
        max_rounds = positive_int(
            metrics.get("max_rounds") or metrics.get("max_rendezvous_rounds")
        )
        if max_rounds is not None:
            return (
                f"Rendezvous batch: {spec_count} subtasks, "
                f"max {max_rounds} {pluralize(max_rounds, 'round')}"
            )
        return f"Rendezvous batch: {spec_count} subtasks"
    return None


def format_batch_finished_event(event: SubAgentProgressEvent) -> str | None:
    metrics = event.metrics or {}
    mode = str(metrics.get("execution_mode", "") or "").strip().lower()
    spec_count = event_spec_count(event)
    completed = positive_int(event.completed) or 0
    duration = format_seconds(metrics.get("duration_seconds"), precision=2)

    if mode == "parallel" and spec_count is not None and completed and duration:
        line = f"Parallel batch finished: {completed}/{spec_count} in {duration}"
        scope_check = format_seconds(
            metrics.get("write_scope_check_seconds"),
            precision=3,
        )
        if scope_check is not None:
            line += f" (scope check {scope_check})"
        return line

    if mode == "pipeline" and spec_count is not None and completed and duration:
        stage_count = positive_int(metrics.get("stage_count"))
        if stage_count is None:
            return None
        prefix = (
            "Pipeline batch ended early"
            if completed < spec_count
            else "Pipeline batch finished"
        )
        return (
            f"{prefix}: {completed}/{spec_count} across {stage_count} "
            f"{pluralize(stage_count, 'stage')} in {duration}"
        )

    if mode == "rendezvous" and spec_count is not None and duration:
        rounds_completed = positive_int(metrics.get("rounds_completed"))
        if rounds_completed is None:
            return None
        return (
            f"Rendezvous batch finished: {spec_count} subtasks, "
            f"{rounds_completed} {pluralize(rounds_completed, 'round')} "
            f"in {duration}"
        )

    return None


def format_subagent_event(event: SubAgentProgressEvent) -> str:
    role = event.role or "agent"
    kind = event.kind
    # Phase events: runtime generates good messages; prefix with an
    # emoji for visual hierarchy.
    if kind == "phase_started":
        prefix = "▸ "
        return f"{prefix}{event.message}" if event.message else f"{prefix}Started"
    if kind == "phase_finished":
        prefix = "▾ "
        return f"{prefix}{event.message}" if event.message else f"{prefix}Complete"
    if kind == "phase_note":
        return f"💬 {event.message}" if event.message else ""
    # Agent lifecycle events: always use the new structured format
    # since it includes role, status, and duration.
    if kind == "agent_started":
        return f"→ **{role}** started"
    if kind == "agent_finished":
        dur = event.metrics.get("duration_seconds")
        dur_str = f" ({dur:.1f}s)" if isinstance(dur, (int, float)) else ""
        return f"✓ **{role}** finished{dur_str}"
    if kind == "agent_failed":
        return f"✗ **{role}** failed"
    if kind == "agent_retry":
        return f"↻ **{role}** retry"
    # Batch-level events: use the runtime's pre-computed message
    # when available, otherwise a minimal fallback.
    if kind == "batch_started":
        return event.message or f"Starting {event.total} sub-agents"
    if kind == "batch_progress":
        return event.message or f"Running: {event.completed}/{event.total}"
    if kind == "batch_finished":
        return event.message or f"Batch finished: {event.completed}/{event.total}"
    return event.message or ""
