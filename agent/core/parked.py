"""Reading parked tasks back: into the prompt, and into the next turn.

Two halves, and both are needed.

**The block** is what stops a parked task from being forgotten. It rides in the
turn's message next to the outstanding-work block, so the agent sees "the person
asked for this while I was busy" without having to remember to look.

**The pickup** is what makes the block a promise rather than a note. A block
alone would be the `set_goal` mistake again: a reminder that depends on the model
choosing to act. So the turn loop, when a turn ends with tasks still waiting,
starts the next one from the queue -- deterministic, no judgement required.

Why the block still exists if the pickup is automatic
-----------------------------------------------------
Because the pickup only fires when a turn *ends*. If the person keeps talking,
the queue keeps waiting, and the agent should know it is there -- "还有两件事排着"
is information that changes how it answers ("I'll do that after the two you
already asked for"). The block is the awareness; the pickup is the guarantee.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["parked_block", "take_next_parked", "finish_parked", "MAX_SHOWN"]

#: A prompt block is read, not searched. Five is a reminder; twenty is a list
#: nobody finishes, and the conversation is where the rest can be asked about.
MAX_SHOWN = 5


def _store(store: Optional[Any]) -> Any:
    if store is not None:
        return store
    try:
        from agent import shared
        from agent.memory.store import LTMStore

        return LTMStore(shared.AGENT_HOME / "context")
    except Exception:
        return None


def parked_block(
    *,
    store: Optional[Any] = None,
    session_id: str = "",
) -> str:
    """The parked-work block, or ``""`` when the queue is empty.

    Best-effort like every other prompt block: a store that cannot be read means
    "nothing parked", never a turn that fails to start.
    """
    clean = str(session_id or "").strip()
    if not clean:
        return ""
    reader = _store(store)
    list_tasks = getattr(reader, "list_parked_tasks", None)
    if not callable(list_tasks):
        return ""
    try:
        # The full waiting set, so "还有 N 件没列出" is the real number rather
        # than "at least one". Cheap: this is a per-session queue, not a table
        # scan, and the count is what makes the line honest.
        tasks = list(list_tasks(session_id=clean, statuses=("waiting",), limit=200))
    except Exception:
        return ""
    if not tasks:
        return ""
    shown = tasks[:MAX_SHOWN]
    lines = [
        "## 排着的事",
        "这些是你在忙别的事时我记下的，还没开始。它们不会打断手头的工作。",
    ]
    for index, task in enumerate(shown, start=1):
        text = str(getattr(task, "text", "") or "").strip()
        if not text:
            continue
        lines.append(f"{index}. {text}")
    if len(tasks) > MAX_SHOWN:
        lines.append(f"（还有 {len(tasks) - MAX_SHOWN} 件没列出。）")
    lines.append("")
    lines.append(
        "手头这件事做完之后接着办这些；如果哪件已经不必做了，用 `drop_parked_task` "
        "说明原因，别让列表越堆越长。不要因为看到这个就停下现在做的事。"
    )
    return "\n".join(lines)


def take_next_parked(
    *,
    store: Optional[Any] = None,
    session_id: str = "",
) -> Optional[dict[str, str]]:
    """Claim the oldest waiting task, mark it active, and return it.

    Returns ``{"id", "text"}`` or ``None``. Claiming is what makes the queue a
    queue rather than a list: a task marked `active` is not handed out twice,
    even if two turns end at once.
    """
    clean = str(session_id or "").strip()
    if not clean:
        return None
    reader = _store(store)
    list_tasks = getattr(reader, "list_parked_tasks", None)
    mark = getattr(reader, "set_parked_status", None)
    if not callable(list_tasks) or not callable(mark):
        return None
    try:
        tasks = list(list_tasks(session_id=clean, statuses=("waiting",), limit=1))
    except Exception:
        return None
    if not tasks:
        return None
    task = tasks[0]
    task_id = str(getattr(task, "id", "") or "")
    text = str(getattr(task, "text", "") or "").strip()
    if not task_id or not text:
        return None
    try:
        if not mark(task_id=task_id, status="active"):
            return None
    except Exception:
        return None
    return {"id": task_id, "text": text}


def finish_parked(
    *,
    store: Optional[Any] = None,
    task_id: str,
    outcome: str = "",
    status: str = "done",
) -> bool:
    """Close a claimed task. Best-effort: a lost close must not fail a turn."""
    reader = _store(store)
    mark = getattr(reader, "set_parked_status", None)
    if not callable(mark) or not str(task_id or "").strip():
        return False
    try:
        return bool(mark(task_id=task_id, status=status, outcome=outcome))
    except Exception:
        return False
