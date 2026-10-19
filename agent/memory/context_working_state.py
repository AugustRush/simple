"""Working state: how an event is projected into the state the next turn reads, how relevant the stored state is to a query, and the recovery decision when it looks stale. Moved out of ContextManager (see the remaining-big-files survey work order). The store and the staging buffer arrive as collaborators."""

from __future__ import annotations
import re
import hashlib

from .query_terms import _weighted_term_overlap
from . import text as _render
from .working_state import _merge_artifacts, _working_state_candidates, _working_state_is_complete
from ._helpers import _lexical_terms
from .models import SessionWorkingState

from typing import Any

__all__ = [
    "_working_state_relevance_score",
    "_select_working_state",
    "_working_state_recovery_decision",
    "has_active_working_state",
    "_project_working_state_from_event",
]


TURN_OUTCOME_EVENTS = frozenset(
    {
        "turn_finished",
        "turn_failed",
        "turn_error_reported",
        "turn_interrupted",
    }
)


#: Lines that ask for a stopped task to be picked back up rather than naming
#: new work.  Matched against the whole line after punctuation is stripped, so
#: "继续，把机位改近" stays a new instruction.
_CONTINUATION_PHRASES = frozenset(
    {
        "继续",
        "继续吧",
        "继续任务",
        "继续当前任务",
        "继续做",
        "接着",
        "接着来",
        "接着做",
        "continue",
        "goon",
        "keepgoing",
        "resume",
        "carryon",
    }
)


def _is_continuation_message(text: str) -> bool:
    """Whether *text* means "carry on" and not "here is new work".

    The task card's 继续任务 button sends exactly ``继续当前任务``.  Scored like
    any other message it shares no terms with the goal it refers to, so it used
    to mint a *new* task whose goal was the word "继续当前任务" -- and every
    later failure then offered the prompt again under a fresh id, which is also
    why answering it never stuck.  A continuation is recognised by being *only*
    a continuation: an instruction attached to it is new work.
    """
    normalized = re.sub(
        r"[\s，。！？!?,.;；:：…~～、]+", "", str(text or "")
    ).casefold()
    return normalized in _CONTINUATION_PHRASES


def _nearest_real_goal(tasks: Any) -> str:
    """The newest goal that is actually a goal.

    A record written before continuations were matched can still carry
    ``继续当前任务`` as its goal -- the phrase the card's button sends.  That is
    not a goal, so the card's title falls back to the nearest earlier task that
    named one.  Without this the repair would only help *new* tasks, and the
    conversation the reader is looking at would keep the wrong title.
    """
    for task in reversed(list(tasks or ())):
        if not isinstance(task, dict):
            continue
        goal = str(task.get("active_goal") or "").strip()
        if goal and not _is_continuation_message(goal):
            return goal
    return ""


def _working_state_relevance_score(
    state: dict[str, Any],
    query: str,
) -> float:
    query_terms = set(_lexical_terms(query))
    if not query_terms:
        return 0.0

    score = 0.0
    score += _weighted_term_overlap(
        query_terms, state.get("active_goal"), weight=2.0
    )
    score += _weighted_term_overlap(
        query_terms, state.get("progress"), weight=1.0
    )
    score += _weighted_term_overlap(
        query_terms, state.get("last_error"), weight=0.35
    )
    artifacts = state.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            score += _weighted_term_overlap(query_terms, artifact, weight=2.5)
    recent_turns = state.get("recent_turns")
    if isinstance(recent_turns, list):
        for turn in recent_turns:
            if not isinstance(turn, dict):
                continue
            role = str(turn.get("role", "") or "").strip().lower()
            weight = 1.5 if role == "user" else 0.75
            score += _weighted_term_overlap(
                query_terms,
                turn.get("content", ""),
                weight=weight,
            )
    return score


def _select_working_state(
    state: dict[str, Any],
    query: str,
    *,
    include_completed: bool = False,
) -> dict[str, Any] | None:
    decision = _working_state_recovery_decision(
        state,
        query,
        include_completed=include_completed,
    )
    selected_task_id = str(decision.get("selected_task_id", "") or "")
    if not selected_task_id:
        return None
    for candidate in _working_state_candidates(state):
        if str(candidate.get("task_id", "") or "") == selected_task_id:
            return candidate
    return None


def _working_state_recovery_decision(
    state: dict[str, Any],
    query: str,
    *,
    include_completed: bool = False,
) -> dict[str, Any]:
    threshold = 2.0
    continuation_request = bool(
        re.search(
            r"(?:^|\s)(?:continue|resume)(?:\s|$)|继续|接着(?:做|来|完成)?|恢复(?:任务|工作)?",
            str(query or "").strip().lower(),
        )
    )
    candidates: list[dict[str, Any]] = []
    scored: list[tuple[float, dict[str, Any]]] = []
    for candidate in _working_state_candidates(state):
        complete = _working_state_is_complete(candidate)
        dismissed = (
            str(candidate.get("status", "") or "").strip().lower()
            == "dismissed"
        )
        score = _working_state_relevance_score(candidate, query)
        # An explicit dismissal is durable user intent.  Even a generic
        # "continue" request must not resurrect that task automatically.
        eligible = False if dismissed else (
            include_completed or continuation_request or not complete
        )
        candidate_trace = {
            "task_id": str(candidate.get("task_id", "") or ""),
            "status": str(candidate.get("status", "") or ""),
            "score": round(score, 3),
            "eligible": bool(eligible),
            "active_goal": _render._clip_working_state_text(
                candidate.get("active_goal", ""), 120
            ),
        }
        candidates.append(candidate_trace)
        if eligible and (score >= threshold or continuation_request):
            scored.append((score, candidate))
    selected: dict[str, Any] | None = None
    if not scored:
        selected = None
    else:
        scored.sort(
            key=lambda item: (
                item[0],
                str(item[1].get("updated_at", "")),
                str(item[1].get("task_id", "")),
            ),
            reverse=True,
        )
        selected = scored[0][1]
    selected_score = (
        _working_state_relevance_score(selected, query)
        if selected is not None
        else 0.0
    )
    return {
        "query": query,
        "threshold": threshold,
        "selected": selected is not None,
        "selected_task_id": str((selected or {}).get("task_id", "") or ""),
        "selected_goal": str((selected or {}).get("active_goal", "") or ""),
        "selected_status": str((selected or {}).get("status", "") or ""),
        "selected_score": round(selected_score, 3),
        "candidates": candidates,
    }


def has_active_working_state(
    *,
    store,
    staging,
) -> bool:
    """Whether durable state still describes an unfinished task."""
    snapshot = store.load_session_working_state(staging.session_id)
    if snapshot is None or not isinstance(snapshot.state, dict):
        return False
    tasks = snapshot.state.get("tasks")
    if isinstance(tasks, list):
        return any(
            isinstance(task, dict) and not _working_state_is_complete(task)
            for task in tasks
        )
    return not _working_state_is_complete(snapshot.state)


def _project_working_state_from_event(
    *,
    event_type: str,
    payload: dict[str, Any],
    created_at: str,
    store,
    staging,
) -> SessionWorkingState:
    previous = store.load_session_working_state(staging.session_id)
    prior_state = previous.state if previous is not None else {}
    payload = payload if isinstance(payload, dict) else {}
    user_content = str(payload.get("user_content", "") or "")
    assistant_content = str(payload.get("assistant_content", "") or "")
    # How the task stands is how its **turn** ended, and only a described
    # error may say so.  Both halves matter, because an ``error`` key on a
    # recorded event is not a task-level fact:
    #
    # * ``agent_result_ready`` reports a *boolean* -- did this one model call
    #   fail -- and a failed call inside a turn that then continued is not the
    #   task's outcome.  Stringified, it also landed in the durable
    #   ``last_error`` as the word "True", which is what the model then read
    #   as the task's last error.
    # * consolidation runs in the background and reports its own failures
    #   under the same key; a memory-maintenance failure is not the task's.
    error = ""
    raw_error = payload.get("error")
    if event_type in TURN_OUTCOME_EVENTS and isinstance(raw_error, str):
        error = raw_error
    clean_user = _render._clip_working_state_text(user_content, 1000)
    clean_assistant = _render._clip_working_state_text(assistant_content, 1000)
    clean_error = _render._clip_working_state_text(error, 1000)

    error_lower = clean_error.lower()
    if clean_error and "cancel" in error_lower:
        status = "cancelled"
    elif clean_error:
        status = "failed"
    elif clean_assistant:
        status = "completed"
    else:
        status = "in_progress"

    prior_tasks = [
        dict(task)
        for task in _working_state_candidates(prior_state)
        if isinstance(task, dict) and task
    ]
    if (
        prior_state
        and not isinstance(prior_state.get("tasks"), list)
        and str(prior_state.get("active_goal", "") or "").strip()
    ):
        prior_tasks = [dict(prior_state)]

    matched_task: dict[str, Any] | None = None
    continuation = _is_continuation_message(clean_user)
    if clean_user and prior_tasks:
        open_tasks = [
            task for task in prior_tasks if not _working_state_is_complete(task)
        ]
        if continuation:
            # "Carry on" refers to the task we were just doing -- including one
            # whose last *turn* finished, because a turn's "completed" is not
            # the goal's: "继续" after a finished step means the next step.  The
            # newest task is the one it refers to; taking only *open* ones
            # matched nothing, since every successful turn closes its task, and
            # that is how the phrase became a goal of its own.
            matched_task = dict(prior_tasks[-1])
        else:
            scored_matches = [
                (_working_state_relevance_score(task, clean_user), task)
                for task in open_tasks
            ]
            scored_matches = [
                (score, task) for score, task in scored_matches if score >= 2.0
            ]
            if scored_matches:
                scored_matches.sort(
                    key=lambda item: (
                        item[0],
                        str(item[1].get("updated_at", "")),
                        str(item[1].get("task_id", "")),
                    ),
                    reverse=True,
                )
                matched_task = dict(scored_matches[0][1])
    elif prior_tasks:
        open_tasks = [
            task for task in prior_tasks if not _working_state_is_complete(task)
        ]
        if open_tasks:
            matched_task = dict(open_tasks[-1])

    task_fingerprint = hashlib.sha1(
        f"{staging.session_id}\0{clean_user}\0{created_at}".encode("utf-8")
    ).hexdigest()[:12]
    task_id = str(
        (matched_task or {}).get("task_id")
        or f"task-{task_fingerprint}"
    )
    task_recent_turns = (
        list((matched_task or {}).get("recent_turns", []))
        if isinstance((matched_task or {}).get("recent_turns"), list)
        else []
    )
    if clean_user:
        task_recent_turns.append({"role": "user", "content": clean_user})
    if clean_assistant:
        task_recent_turns.append({"role": "assistant", "content": clean_assistant})
    task_recent_turns = task_recent_turns[-8:]

    progress = clean_assistant or str((matched_task or {}).get("progress", "") or "")
    next_action = str((matched_task or {}).get("next_action", "") or "")
    if clean_error:
        next_action = "Recover from the last error and continue the active goal if the user's next message is related."
    elif clean_assistant:
        next_action = "Use this working context only when it is relevant to the user's next message."

    artifacts = _merge_artifacts(
        payload.get("artifacts"),
        (matched_task or {}).get("artifacts"),
    )
    # A continuation never overwrites the goal it continues: the goal is what
    # the user asked for, and "继续当前任务" is not that.
    if continuation:
        active_goal = str((matched_task or {}).get("active_goal", "") or "").strip()
        if not active_goal or _is_continuation_message(active_goal):
            active_goal = _nearest_real_goal(prior_tasks) or clean_user
    else:
        active_goal = clean_user or str(
            (matched_task or {}).get("active_goal", "") or ""
        )
    current_task = {
        "task_id": task_id,
        "active_goal": active_goal,
        "status": status,
        "progress": progress,
        "next_action": next_action,
        "last_error": clean_error,
        "artifacts": artifacts,
        "recent_turns": task_recent_turns,
        "last_event_type": event_type,
        "updated_at": created_at,
    }
    tasks_by_id: dict[str, dict[str, Any]] = {}
    for task in prior_tasks:
        existing_id = str(task.get("task_id") or "")
        if not existing_id:
            existing_id = f"legacy-{len(tasks_by_id)}"
            task["task_id"] = existing_id
        tasks_by_id[existing_id] = task
    tasks_by_id[task_id] = current_task
    tasks = sorted(
        tasks_by_id.values(),
        key=lambda task: str(task.get("updated_at", "")),
    )[-12:]
    state = dict(current_task)
    state["tasks"] = tasks
    return store.save_session_working_state(
        staging.session_id,
        state,
        updated_at=created_at,
    )
