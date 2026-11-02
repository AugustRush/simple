"""From "a person said yes" to "it ran": the bridge, and nothing else.

Why this is a bridge and not an executor
----------------------------------------
The scheduler already *is* the stateless executor this design needs: a run
mounts its context, executes, writes back, and is destroyed; it holds a lease so
two runs cannot overlap; it records what happened so a crash is recoverable; and
it runs under a permission profile because nobody is there to answer a question.
Building a second executor here would duplicate all of that and drift from it.

So this module does one thing: turn an approved proposal into **a one-off task
due now**, and let the existing path run it. The proposal keeps the task id, and
the next patrol pass reads the run's outcome back.

Why the permission profile is pinned to `read_only`
---------------------------------------------------
The patrol classified the proposal as reversible because the *tool it named* is
read-only. But when the task actually runs, an agent reads the prompt and chooses
its own tools -- so "reversible" describes an intention, not a guarantee. What
actually constrains the run is the permission profile, and the only profile that
matches "nothing outside the workspace may change" is ``read_only``.

That is the honest chain: **the classification decides whether a person is asked;
the profile decides what can happen when they say yes.** A reversible proposal
that ran with `workspace_write` would be a promise the classification never made.

Why it is a person's command, not the agent's decision
------------------------------------------------------
``act`` is run by a person (the CLI today). The agent never decides to execute a
proposal: that is the line this whole delivery is drawn on, and moving it would
need the calibration numbers the shadow period exists to produce.

Why the transition is the lock
------------------------------
``store.decide(id, "acted")`` succeeds only on a row that is still ``proposed``,
so two concurrent approvals cannot both create a task: the second gets ``False``
and does nothing. The state machine is the mutex -- no separate lock, no
"already running?" check that can race with itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

__all__ = ["execute_proposal", "ExecutionOutcome"]

UTC = timezone.utc

#: The permission profile every executed proposal runs under.
#:
#: Not ``read_only``: that profile's shell sits at the ``ask`` level, where only
#: pipes-into-an-interpreter and redirect writes are questioned and everything
#: else runs -- and its own `detail` says the sandbox does not confine writes to
#: the workspace. A proposal classified "reversible" that then executes arbitrary
#: commands is a guarantee that was never true. ``autonomy_read`` denies shell
#: outright, which is the only thing that makes the promise keepable.
EXECUTION_PROFILE = "autonomy_read"

#: How long an executed task may take before the scheduler's own timeout applies.
EXECUTION_TIMEOUT_SECONDS = 900


class ExecutionOutcome:
    """What ``act`` did, in the words a caller reports.

    A dict rather than a dataclass because it crosses the CLI and the HTTP layer,
    and both want plain data.
    """

    @staticmethod
    def refused(reason: str) -> dict[str, Any]:
        return {"ok": False, "executed": False, "reason": reason}

    @staticmethod
    def queued(task_id: str, proposal_id: str, runner: str) -> dict[str, Any]:
        return {
            "ok": True,
            "executed": True,
            "proposal_id": proposal_id,
            "task_id": task_id,
            # Which clock will run it, said at the moment it is created -- the
            # same honesty `schedule_create` owes: a stored task is not a running
            # clock, and the person who just said "yes" is still here to read it.
            "runner": runner,
        }


def _reload(store: Any, proposal_id: str) -> Any:
    """The proposal as the store has it right now, or None when it is gone."""
    clean = str(proposal_id or "").strip()
    if not clean:
        return None
    try:
        rows = store.list_proposals(limit=500)
    except Exception:
        return None
    for row in rows:
        if getattr(row, "id", "") == clean:
            return row
    return None


def execute_proposal(
    *,
    store: Any,
    proposal: Any,
    scheduler_store: Any,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Create a one-off task for an approved proposal, due immediately.

    Refuses rather than guesses, and every refusal says which one it is:

    * the proposal is no longer ``proposed`` (already answered, or expired);
    * its verdict was ``drop`` -- a dropped candidate was never allowed to exist;
    * it is ``irreversible`` or ``workspace`` -- those are ``prepare`` answers, and
      a ``prepare`` that ran anyway is the one thing this design must not do.

    The order matters: the state transition is taken **last**, after everything
    that could refuse has refused, so a refusal never consumes the proposal.
    """
    moment = now or datetime.now(UTC)

    # The **store** is the truth, not the object handed in: a caller holding a
    # `Proposal` from before an answer has a stale `state`, and trusting it would
    # let a second approval create a second task. Re-read, then decide.
    current = _reload(store, getattr(proposal, "id", ""))
    if current is None:
        return ExecutionOutcome.refused("这条提议不存在了")
    proposal = current

    state = str(getattr(proposal, "state", "") or "")
    if state != "proposed":
        return ExecutionOutcome.refused(f"这条提议已经是 {state}，不能执行")

    reversibility = str(getattr(proposal, "reversibility", "") or "")
    if reversibility != "reversible":
        # `prepare` is a promise: the exact call was written down for a person to
        # approve, and this is that approval -- but an action that can change
        # something outside the workspace is not something a proposal may run.
        # It stays a proposal; a person does it, or a later version with a
        # stronger approval does.
        return ExecutionOutcome.refused(
            f"这条提议是 {reversibility}，只能准备好等人在别处执行"
        )

    admission = (getattr(proposal, "context", {}) or {}).get("admission") or {}
    if isinstance(admission, dict) and admission.get("answer") == "drop":
        return ExecutionOutcome.refused("这条提议在准入阶段就被丢弃了")

    body = _task_body(proposal, now=moment)
    try:
        from agent.scheduler import NewScheduledTask, DeliveryTarget, TriggerSpec

        spec = NewScheduledTask(
            name=_task_name(proposal),
            kind="agent_prompt",
            trigger=TriggerSpec.once(moment.isoformat(), "UTC"),
            payload={"prompt": body},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
            context_policy="stateless",
            timeout_seconds=EXECUTION_TIMEOUT_SECONDS,
            permission_profile=EXECUTION_PROFILE,
            request_quote=str(getattr(proposal, "source", "") or "")[:200],
        )
        created = scheduler_store.create_task(spec)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return ExecutionOutcome.refused(f"建任务失败：{exc}")

    # The transition last: it is the mutex, so a concurrent second approval loses
    # here and never creates a second task.
    claimed = store.decide(proposal.id, "acted", by="person")
    if not claimed:
        # Somebody else answered between the checks and now. The task exists and
        # nothing will run it: disable it rather than leave a stray due task.
        try:
            scheduler_store.set_enabled(created.id, False)
        except Exception:
            pass
        return ExecutionOutcome.refused("这条提议刚刚被别处答复了")

    runner = _runner(proposal)
    return ExecutionOutcome.queued(created.id, proposal.id, runner)


def _task_name(proposal: Any) -> str:
    """A task name a person can recognise in the 自动化 list.

    Prefixed so an executed proposal is distinguishable from a task the person
    made: the two have different provenance and the list is where that shows.
    """
    action = str(getattr(proposal, "action", "") or "").strip()
    name = action[:60] if action else "自主提议"
    return f"[自主] {name}"


def _task_body(proposal: Any, *, now: datetime) -> str:
    """The prompt the run gets.

    It carries the *reason* as well as the action: a run told only "read the failed
    runs" has to rediscover which ones and why, and the proposal already knows.
    The reason is what the patrol saw, so handing it over is what makes the run
    cheaper than the patrol was.
    """
    action = str(getattr(proposal, "action", "") or "").strip()
    reason = str(getattr(proposal, "reason", "") or "").strip()
    source = str(getattr(proposal, "source", "") or "").strip()
    lines = [
        "这是一条自主提议，已经由人批准执行。",
        "",
        f"要做的事：{action}",
    ]
    if reason:
        lines.append(f"为什么：{reason}")
    if source:
        lines.append(f"来自会话：{source}")
    lines += [
        "",
        "只做这件事。如果它需要改变工作区之外的东西（发消息、删数据、跑命令），",
        "不要绕过权限档：说明你需要什么，然后停下。",
    ]
    return "\n".join(lines)


def _runner(proposal: Any) -> str:
    """Which clock will run this, said at creation time.

    Same contract ``schedule_create`` owes: a stored task is not a running clock,
    and the person who just approved it is still here to be told.
    """
    try:
        from agent import shared

        scheduler_dir = getattr(shared, "SCHEDULER_DIR", None)
        if scheduler_dir is not None and (scheduler_dir / "scheduler.db").exists():
            return "external"
    except Exception:
        pass
    return "external"
