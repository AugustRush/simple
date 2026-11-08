"""v1 of "what would be worth doing": rules over what the store already knows.

Why rules first
---------------
The patrol's job is to find candidates cheaply.  A model asked "is anything
worth doing here" costs money per session per tick and answers in prose that
cannot be audited; a rule costs nothing, and its answer is the same every time
the same facts appear -- which is what makes a proposal's *reason* checkable.

Every rule below fires on a fact that is already on disk.  None of them calls a
model, and none of them guesses: if a rule cannot see the fact, it does not
fire.  That is the opposite of "the model thinks something might be wrong", and
it is what lets the funnel's numbers be trusted.

What the rules are not
----------------------
They are not the decision.  A rule firing produces a **proposal** -- "here is
something I would do, and here is why" -- and admission, in the same pass,
decides whether that may run or has to wait for a person.  A person reads the
rows too, and that is the calibration.

Why each rule names a tool
--------------------------
The reversibility class comes from the tool's declared capabilities, so a
proposal without a tool cannot be classified and is treated as irreversible.
That is deliberate: a rule that cannot name what it would call has not thought
far enough to be allowed to act.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import (
    Proposal,
    classify_reversibility,
    MAX_PROPOSAL_ACTION_CHARS,
    MAX_PROPOSAL_REASON_CHARS,
)

__all__ = ["rule_candidates", "RULE_NAMES", "RULE_PRIORITY", "rule_priority"]

UTC = timezone.utc

#: The rules, by name, so a proposal's ``context`` can say which one fired.
#: What to do first when several proposals are waiting.
#:
#: Ranked by **who is blocked**, not by how alarming the words sound:
#:
#: 0. ``stale-queue`` -- a person is waiting on a message that is not moving.
#:    Somebody is stuck right now, and the queue is the only rule that describes a
#:    human currently blocked rather than work that already finished failing.
#: 1. ``failed-run-unreviewed`` -- work was lost and nobody has read why. No one is
#:    blocked; the cost is that the same failure can repeat unseen.
#: 2. ``session-ended-mid-task`` -- work stopped halfway. The next turn in that
#:    session may well pick it up by itself, so acting on it is the most optional
#:    of the three.
#:
#: Deliberately an order, not a score. A weighted sum would need weights nobody
#: can justify, and the first thing a made-up weight does is make two different
#: situations look equally urgent. Ties inside a rank are broken by age -- the
#: thing that has been waiting longest goes first.
RULE_PRIORITY: dict[str, int] = {
    "stale-queue": 0,
    "repeated-task-failure": 1,
    "failed-run-unreviewed": 2,
    "session-ended-mid-task": 3,
}


#: The order `patrol_tick` acts in when several proposals were admitted at once.
def rule_priority(rule: str) -> int:
    return RULE_PRIORITY.get(str(rule or ""), len(RULE_PRIORITY))


RULE_NAMES = (
    "repeated-task-failure",
    "failed-run-unreviewed",
    "session-ended-mid-task",
    "stale-queue",
)


def _iso(value: Optional[datetime]) -> str:
    return value.astimezone(UTC).isoformat() if value is not None else ""


def _ago(now: datetime, seconds: float) -> datetime:
    return now - timedelta(seconds=seconds)


#: Which failures get which remedy, and why each is reversible.
#:
#: A retry count and a timeout are settings, not commitments: raising either can
#: be put back with the same call, and neither changes what the task is *for*.
#: A broken credential is different -- no setting fixes it, so the honest move is
#: to stop the task repeating a failure that cannot succeed until somebody looks,
#: and saying so in the proposal. Both classes are "workspace": they change this
#: machine, not the world.
_REMEDY_BY_CAUSE: dict[str, str] = {
    "超时": "timeout",
    "连接中断": "retry",
    "被限流": "retry",
    "服务端错误": "retry",
    "鉴权失败": "pause",
    "额度用尽": "pause",
}

#: The tools a "yes" may apply. A stored proposal is data, and data that names a
#: tool is a call waiting to happen -- so the set of calls is enumerated here
#: rather than taken from the row.
APPLICABLE_PREPARED_TOOLS = ("schedule_update", "schedule_set_enabled")


def _remedy_for(
    cause: str,
    *,
    task_id: str,
    timeout_seconds: int,
    retry_policy: dict[str, Any],
) -> tuple[Optional[str], dict[str, Any], str]:
    """The exact call this cause deserves, or ``None`` when there is no safe one."""
    kind = _REMEDY_BY_CAUSE.get(cause)
    if kind == "timeout":
        target = max(int(timeout_seconds or 1800) * 2, 600)
        return (
            "schedule_update",
            {"task_id": task_id, "timeout_seconds": target},
            f"把超时从 {int(timeout_seconds or 1800) // 60} 分钟调到 {target // 60} 分钟",
        )
    if kind == "retry":
        attempts = max(3, int(retry_policy.get("max_attempts") or 1) + 2)
        return (
            "schedule_update",
            {
                "task_id": task_id,
                "retry_policy": {
                    "max_attempts": attempts,
                    "backoff_seconds": int(retry_policy.get("backoff_seconds") or 30),
                },
            },
            f"把这个任务的重试次数从 {int(retry_policy.get('max_attempts') or 1)} 提到 {attempts}",
        )
    if kind == "pause":
        return (
            "schedule_set_enabled",
            {"task_id": task_id, "enabled": False},
            "先把它停掉，等凭证或额度修好再开",
        )
    return None, {}, ""


def rule_candidates(
    session: dict[str, Any],
    *,
    now: datetime,
    capabilities_of: Any = None,
) -> list[Proposal]:
    """Every candidate the rules can see for one session.

    ``session`` is the row the session list already publishes -- ``session_id``,
    ``title``, ``last_activity``, ``turn_count``, ``status`` -- plus two optional
    facts the caller may have: ``attention`` (unacknowledged failed runs) and
    ``queue`` (pending items).  Missing facts mean the matching rule cannot
    fire; nothing is invented.
    """
    session_id = str(session.get("session_id") or "").strip()
    if not session_id:
        return []
    label = str(session.get("title") or "").strip() or session_id[:8]
    found: list[Proposal] = []

    def reversibility_for(tool: str) -> str:
        if capabilities_of is None:
            return "irreversible"
        try:
            return classify_reversibility(capabilities_of(tool))
        except Exception:
            return "irreversible"

    # ── 1. runs that failed and nobody looked at ───────────────────────────
    # "Attention" is a *task*-level fact (an unacknowledged failed run belongs to a
    # task). A session carries it only when its tasks do, so the row is asked for
    # the tasks it knows about, and the count is theirs. A session with no tasks
    # has no failures to read -- which is different from zero failures.
    attention = int(session.get("attention") or 0)
    task_rows = [
        task for task in (session.get("tasks") or []) if isinstance(task, dict)
    ]
    for task in task_rows:
        attention += int(task.get("attention") or 0)
    if attention > 0:
        # The call has to name *which* task failed. Without an argument the
        # executor is told "read the failed runs" and has to guess -- a real run
        # answered that those runs "are no longer in the store, there is no error
        # record to read". The largest count wins; ties keep the first, because
        # the row order is the only ordering the caller gave us.
        failed_tasks = [
            task for task in task_rows if int(task.get("attention") or 0) > 0
        ]
        target = max(
            failed_tasks,
            key=lambda task: int(task.get("attention") or 0),
            default=None,
        )
        arguments: dict[str, Any] = {}
        target_id = str((target or {}).get("task_id") or "").strip()
        if target_id:
            arguments["task_id"] = target_id
        found.append(
            Proposal(
                source=session_id,
                reason=(
                    f"「{label}」有 {attention} 次运行结束得不好，还没有人看过。"
                )[:MAX_PROPOSAL_REASON_CHARS],
                action=(
                    f"读一遍「{label}」那几次失败的运行，写一句它们为什么失败"
                )[:MAX_PROPOSAL_ACTION_CHARS],
                # Reading is the whole of it: the proposal does not fix anything,
                # which is what keeps it in the reversible class.
                tool="schedule_runs",
                reversibility=reversibility_for("schedule_runs"),
                confidence=0.8,
                context={
                    "rule": "failed-run-unreviewed",
                    "priority": rule_priority("failed-run-unreviewed"),
                    "attention": attention,
                    # The exact call admission writes down for a "prepare" row,
                    # and the prompt the executor gets when it runs.
                    "arguments": arguments,
                },
            )
        )

    # ── 2. the same task failing the same way, twice ───────────────────────
    #
    # Rule 1 says "somebody read the failure". This one says "reading it was not
    # enough": the same task has now failed twice for the same classified
    # reason inside a week, so the explanation exists and the situation anyway
    # repeats. The two are different questions and they get different actions --
    # 1 asks for an explanation, this asks for a decision.
    #
    # Measured on this machine: `汇报` and `每周五写周报` both failed with
    # "connection lost mid-reply", were diagnosed by the rule-1 tasks, and then
    # nothing happened for two days. A diagnosis nobody acts on is a diary.
    for task in task_rows:
        if not isinstance(task, dict):
            continue
        task_name = str(task.get("name") or task.get("title") or "").strip()
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            continue
        by_label: dict[str, list[dict[str, Any]]] = {}
        for failure in task.get("failures") or ():
            if not isinstance(failure, dict):
                continue
            cause = str(failure.get("label") or "").strip()
            if not cause:
                continue
            by_label.setdefault(cause, []).append(failure)
        # The most repeated cause wins; ties by the most recent failure, because
        # that is the one still happening.
        cause, occurrences = max(
            by_label.items(),
            key=lambda item: (len(item[1]), str(item[1][-1].get("at") or "")),
            default=("", []),
        )
        if len(occurrences) < 2:
            continue
        remedy_tool, remedy_arguments, remedy_text = _remedy_for(
            cause,
            task_id=task_id,
            timeout_seconds=int(task.get("timeout_seconds") or 0),
            retry_policy=task.get("retry_policy") or {},
        )
        if remedy_tool is None:
            continue
        found.append(
            Proposal(
                source=session_id,
                reason=(
                    f"「{task_name}」最近 {len(occurrences)} 次都是同一个原因失败"
                    f"（{cause}）。上一次是 {str(occurrences[-1].get('at') or '')[:10]}。"
                )[:MAX_PROPOSAL_REASON_CHARS],
                action=remedy_text[:MAX_PROPOSAL_ACTION_CHARS],
                # A task definition is the person's own sentence, so changing it
                # is a "prepare": the agent names the exact change and waits.
                tool=remedy_tool,
                reversibility="workspace",
                confidence=0.75,
                context={
                    "rule": "repeated-task-failure",
                    "priority": rule_priority("repeated-task-failure"),
                    "task_id": task_id,
                    "task_name": task_name,
                    "cause": cause,
                    "occurrences": len(occurrences),
                    # The exact call a "yes" applies. Without it, agreeing to a
                    # prepare proposal is agreeing to a sentence -- which is
                    # what it was until now: the row said "accepted" and
                    # nothing on the machine changed.
                    "arguments": remedy_arguments,
                },
            )
        )

    # ── 2. a session that ended mid-task ───────────────────────────────────
    task = session.get("task") or {}
    if isinstance(task, dict):
        status = str(task.get("status") or "").strip().lower()
        goal = str(task.get("active_goal") or "").strip()
        if status in {"running", "interrupted", "blocked"} and goal:
            found.append(
                Proposal(
                    source=session_id,
                    reason=(
                        f"「{label}」上次停在「{goal}」，状态是 {status}，没有收尾。"
                    )[:MAX_PROPOSAL_REASON_CHARS],
                    action=(
                        f"看一眼「{label}」停在哪一步，判断是接着做完还是该问一句"
                    )[:MAX_PROPOSAL_ACTION_CHARS],
                    tool="memory_read",
                    reversibility=reversibility_for("memory_read"),
                    confidence=0.6,
                    context={
                        "rule": "session-ended-mid-task",
                        "priority": rule_priority("session-ended-mid-task"),
                        "status": status,
                    },
                )
            )

    # ── 3. something has been waiting in the queue too long ────────────────
    queue = session.get("queue") or {}
    if isinstance(queue, dict):
        pending = int(queue.get("pending") or 0)
        if pending > 0:
            # The wait is the *queue's* age, not the session's: a conversation
            # somebody is typing in has a recent session and a queue that has not
            # moved, and reporting the session's age as the wait would be a
            # different claim. `queued_since` is when the oldest item arrived.
            last_active = _parse(
                queue.get("queued_since")
                or session.get("last_activity")
            )
            waited = now - last_active if last_active is not None else timedelta(0)
            if waited >= timedelta(minutes=30):
                found.append(
                    Proposal(
                        source=session_id,
                        reason=(
                            f"「{label}」有 {pending} 条排队消息等了 "
                            f"{int(waited.total_seconds() // 60)} 分钟没动。"
                        )[:MAX_PROPOSAL_REASON_CHARS],
                        action=(
                            f"检查「{label}」的队列，确认那 {pending} 条是不是卡住了"
                        )[:MAX_PROPOSAL_ACTION_CHARS],
                        tool="list_tools",
                        reversibility=reversibility_for("list_tools"),
                        confidence=0.5,
                        context={
                            "rule": "stale-queue",
                            "priority": rule_priority("stale-queue"),
                            "pending": pending,
                        },
                    )
                )

    return found


def _parse(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
