"""The fixed replies the tool loop sends when it cannot make progress: a context restart and a stuck loop. Moved out of BaseAgent (see the remaining-big-files survey work order)."""

from __future__ import annotations
import json

from agent.core.tool_watchdog import ToolLoopWatchdog

__all__ = [
    "_clear_context_restart_message",
    "_build_tool_loop_stuck_response",
]


def _clear_context_restart_message(
    tool_calls: list[dict],
    results: list[str],
) -> str | None:
    """Return a deferred clear-context restart message from a tool batch."""
    for tool_call, raw_result in zip(tool_calls, results):
        if tool_call.get("name") != "clear_context":
            continue
        try:
            payload = json.loads(str(raw_result or ""))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("ok") is False:
            continue
        if payload.get("clear_context_requested") is True:
            restart_message = str(payload.get("restart_message", "")).strip()
            if restart_message:
                return restart_message
    return None


def _build_tool_loop_stuck_response(
    *,
    user_message: str,
    reason: str,
    tool_uses: list[dict],
    results: list[str],
) -> str:
    """See :meth:`agent.core.tool_watchdog.ToolLoopWatchdog.build_stuck_response`."""
    return ToolLoopWatchdog.build_stuck_response(
        user_message=user_message,
        reason=reason,
        tool_uses=tool_uses,
        results=results,
    )
