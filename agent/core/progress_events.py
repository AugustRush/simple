"""Progress events and the human-readable text that goes with them: the runtime progress line, the sub-agent event, and the role list. Moved out of BaseAgent (see the remaining-big-files survey work order)."""

from __future__ import annotations

from agent import shared

from agent.core.turn_state import SubAgentProgressEvent

from agent.core.output import CliOutputSink, _active_sink

from typing import Any

__all__ = [
    "_emit_runtime_progress_event",
    "_emit_subagent_event",
    "_format_role_list",
    "_runtime_progress_message",
]


def _emit_subagent_event(
    event: SubAgentProgressEvent,
) -> None:
    sink = _active_sink.get()
    if sink is not None:
        sink.on_subagent_event(event)
        return
    CliOutputSink(shared.CONSOLE).on_subagent_event(event)


def _format_role_list(roles: list[str], *, limit: int = 3) -> str:
    cleaned = [str(role).strip() for role in roles if str(role).strip()]
    if not cleaned:
        return "sub-agents"
    if len(cleaned) <= limit:
        return ", ".join(cleaned)
    remaining = len(cleaned) - limit
    return f"{', '.join(cleaned[:limit])}, +{remaining} more"


def _runtime_progress_message(
    kind: str,
    payload: dict[str, Any],
) -> str:
    mode = str(payload.get("execution_mode", "") or "").strip().lower()
    if kind == "batch_progress" and mode == "parallel":
        completed = int(payload.get("completed", 0) or 0)
        total = int(payload.get("total", 0) or 0)
        running = max(0, total - completed)
        return (
            f"Parallel batch running: {completed}/{total} completed, "
            f"{running} still running"
        )

    if kind == "phase_started" and mode == "pipeline":
        stage_index = int(payload.get("phase_index", 0) or 0)
        ready_count = int(payload.get("ready_count", 0) or 0)
        roles = _format_role_list(list(payload.get("ready_roles", [])))
        return (
            f"Pipeline stage {stage_index} started: "
            f"{ready_count} ready ({roles})"
        )

    if kind == "phase_finished" and mode == "pipeline":
        stage_index = int(payload.get("phase_index", 0) or 0)
        succeeded = int(payload.get("succeeded_count", 0) or 0)
        failed = int(payload.get("failed_count", 0) or 0)
        if failed:
            return (
                f"Pipeline stage {stage_index} finished: "
                f"{succeeded} succeeded, {failed} failed"
            )
        return f"Pipeline stage {stage_index} finished: {succeeded} succeeded"

    if kind == "phase_note" and mode == "pipeline":
        if payload.get("phase_kind") == "skipped":
            skipped = len(payload.get("skipped_ids") or [])
            roles = _format_role_list(list(payload.get("skipped_roles", [])))
            reason = str(payload.get("reason", "") or "")
            suffix = f" ({reason})" if reason else ""
            return f"Pipeline: {skipped} blocked agents skipped{suffix}: {roles}"

    if kind == "phase_started" and mode == "rendezvous":
        round_index = int(payload.get("phase_index", 0) or 0)
        round_total = int(payload.get("phase_total", 0) or 0)
        participants = int(payload.get("participant_count", 0) or 0)
        roles = _format_role_list(list(payload.get("participant_roles", [])))
        return (
            f"Debate round {round_index}/{round_total} started: "
            f"{participants} participants ({roles})"
        )

    if kind == "phase_finished" and mode == "rendezvous":
        round_index = int(payload.get("phase_index", 0) or 0)
        round_total = int(payload.get("phase_total", 0) or 0)
        succeeded = int(payload.get("succeeded_count", 0) or 0)
        failed = int(payload.get("failed_count", 0) or 0)
        if failed:
            return (
                f"Debate round {round_index}/{round_total} finished: "
                f"{succeeded} succeeded, {failed} failed"
            )
        return (
            f"Debate round {round_index}/{round_total} finished: "
            f"{succeeded} views collected"
        )

    if kind == "phase_note" and mode == "rendezvous":
        round_index = int(payload.get("phase_index", 0) or 0)
        round_total = int(payload.get("phase_total", 0) or 0)
        continue_count = int(payload.get("continue_count", 0) or 0)
        if bool(payload.get("final")):
            return (
                f"Lead synthesis complete after round "
                f"{round_index}/{round_total}"
            )
        if bool(payload.get("stop")):
            return (
                f"Lead summary after round {round_index}/{round_total}: "
                    "no further round needed"
            )
        roles = _format_role_list(list(payload.get("continue_roles", [])))
        next_round = min(round_total, round_index + 1)
        return (
            f"Lead summary ready for round {next_round}/{round_total}: "
            f"{continue_count} continue ({roles})"
        )

    return ""


def _emit_runtime_progress_event(
    kind: str,
    payload: dict[str, Any],
) -> None:
    _emit_subagent_event(
        SubAgentProgressEvent(
            kind=kind,
            completed=int(payload.get("completed", 0) or 0),
            total=int(payload.get("total", 0) or 0),
            message=_runtime_progress_message(kind, payload),
            metrics=dict(payload),
        )
    )
