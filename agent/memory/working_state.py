"""Working state as data: which tasks it holds, whether one is finished, how artifacts combine.

Moved out of ``ContextManager`` (see the remaining-big-files survey).  All three take the state
dict and read it -- none of them touches the store, the session or the engine, so the rules
about what counts as a live task and a completed one can be read in one place.
"""

from __future__ import annotations

from typing import Any

__all__ = ['_working_state_candidates', '_working_state_is_complete', '_merge_artifacts']


@staticmethod
def _working_state_is_complete(state: dict[str, Any]) -> bool:
    status = str(state.get("status", "") or "").strip().lower()
    return status in {"completed", "updated", "done", "success", "dismissed"}


def _working_state_candidates(
    state: dict[str,
    Any],
) -> list[dict[str, Any]]:
    tasks = state.get("tasks")
    if isinstance(tasks, list):
        candidates = [task for task in tasks if isinstance(task, dict)]
        if candidates:
            return candidates
    return [state] if isinstance(state, dict) and state else []


@staticmethod
def _merge_artifacts(*groups: Any, limit: int = 12) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            value = str(item or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            merged.append(value)
            if len(merged) >= limit:
                return merged
    return merged
