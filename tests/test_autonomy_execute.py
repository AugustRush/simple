"""From "a person said yes" to "it ran": the bridge, and the four things it refuses.

The execution path is the scheduler's, not a second one built here. What these
tests pin is the *bridge*: the state transition that makes it single-shot, the
permission profile that makes "reversible" true at run time, and the four
refusals -- because a bridge that lets one wrong thing through is worse than no
bridge at all.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from agent.autonomy import Proposal
from agent.autonomy.admission import AdmissionPolicy, decide_admission
from agent.autonomy.execute import EXECUTION_PROFILE, execute_proposal
from agent.autonomy.store import ProposalStore

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def tmp_path():
    path = Path("/tmp") / f"simple-autonomy-exec-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def stores(tmp_path):
    from agent.scheduler import SchedulerStore

    proposals = ProposalStore(tmp_path / "proposals.db")
    scheduler = SchedulerStore(db_path=tmp_path / "scheduler.db")
    yield proposals, scheduler
    proposals.close()
    scheduler.close()


def _approved(proposals, *, reversibility="reversible", answer="prepare"):
    proposal = Proposal(
        source="s1",
        reason="有 2 次运行结束得不好，还没有人看过",
        action="读一遍那 2 次失败的运行，写一句它们为什么失败",
        tool="schedule_runs",
        reversibility=reversibility,
        confidence=0.8,
    )
    proposal.context["admission"] = {
        "answer": answer,
        "reason": "今天自动执行的额度已用完",
        "prepared": {"tool": "schedule_runs", "arguments": {}},
    }
    proposals.record(proposal)
    return proposal


# ── the happy path ─────────────────────────────────────────────────────────


def test_an_approved_reversible_proposal_becomes_a_one_off_task(stores) -> None:
    proposals, scheduler = stores
    proposal = _approved(proposals)

    outcome = execute_proposal(
        store=proposals, proposal=proposal, scheduler_store=scheduler, now=NOW
    )

    assert outcome["ok"] is True
    assert outcome["executed"] is True
    assert outcome["runner"] == "external", "a stored task is not a running clock"

    task = scheduler.list_tasks()[0]
    assert task.id == outcome["task_id"]
    assert task.name.startswith("[自主] "), "provenance shows in the automation list"
    assert task.trigger.trigger_type == "once"
    # The prompt carries the reason, so the run does not rediscover what the
    # patrol already knew.
    assert "读一遍" in task.payload["prompt"]
    assert "还没有人看过" in task.payload["prompt"]


def test_the_run_is_pinned_to_a_profile_that_cannot_run_commands(stores) -> None:
    """The classification decides whether a person is asked; the profile decides
    what can happen when they say yes.

    Not ``read_only``: its shell sits at ``ask``, where only some commands are
    questioned -- and its own detail says the sandbox does not confine writes to
    the workspace. A "reversible" proposal that can then execute commands is a
    promise that was never true.
    """
    proposals, scheduler = stores
    proposal = _approved(proposals)

    execute_proposal(store=proposals, proposal=proposal, scheduler_store=scheduler, now=NOW)

    assert scheduler.list_tasks()[0].permission_profile == EXECUTION_PROFILE
    assert EXECUTION_PROFILE == "autonomy_read"
    # ...and that profile really denies shell, rather than only saying so.
    from agent.scheduler.profiles import resolve_permission_profile

    assert resolve_permission_profile(EXECUTION_PROFILE).shell_level == "deny"


def test_the_proposal_records_that_it_was_acted_on(stores) -> None:
    proposals, scheduler = stores
    proposal = _approved(proposals)

    execute_proposal(store=proposals, proposal=proposal, scheduler_store=scheduler, now=NOW)

    row = proposals.list_proposals()[0]
    assert row.state == "acted"
    assert row.decided_by == "person"


# ── the refusals ───────────────────────────────────────────────────────────


def test_a_proposal_that_was_already_answered_is_refused(stores) -> None:
    proposals, scheduler = stores
    proposal = _approved(proposals)
    proposals.decide(proposal.id, "declined", by="person")

    outcome = execute_proposal(
        store=proposals,
        proposal=proposals.list_proposals()[0],
        scheduler_store=scheduler,
        now=NOW,
    )

    assert outcome["ok"] is False
    assert "declined" in outcome["reason"]
    assert scheduler.list_tasks() == [], "a refusal must not create a task"


def test_an_irreversible_proposal_is_refused(stores) -> None:
    """`prepare` is a promise: the exact call is written down for a person."""
    proposals, scheduler = stores
    proposal = _approved(proposals, reversibility="irreversible")

    outcome = execute_proposal(
        store=proposals, proposal=proposal, scheduler_store=scheduler, now=NOW
    )

    assert outcome["ok"] is False
    assert "irreversible" in outcome["reason"]
    assert scheduler.list_tasks() == []


def test_a_workspace_write_is_refused_too(stores) -> None:
    """Undoable by a person who can see it -- so a person sees it first."""
    proposals, scheduler = stores
    proposal = _approved(proposals, reversibility="workspace")

    outcome = execute_proposal(
        store=proposals, proposal=proposal, scheduler_store=scheduler, now=NOW
    )

    assert outcome["ok"] is False
    assert scheduler.list_tasks() == []


def test_a_dropped_proposal_is_refused(stores) -> None:
    """A candidate the admission stage threw away was never allowed to exist."""
    proposals, scheduler = stores
    proposal = _approved(proposals, answer="drop")

    outcome = execute_proposal(
        store=proposals, proposal=proposal, scheduler_store=scheduler, now=NOW
    )

    assert outcome["ok"] is False
    assert scheduler.list_tasks() == []


# ── the mutex ──────────────────────────────────────────────────────────────


def test_a_second_approval_cannot_create_a_second_task(stores) -> None:
    """The state transition is the lock: two approvals, one task."""
    proposals, scheduler = stores
    proposal = _approved(proposals)

    first = execute_proposal(
        store=proposals, proposal=proposal, scheduler_store=scheduler, now=NOW
    )
    second = execute_proposal(
        store=proposals, proposal=proposal, scheduler_store=scheduler, now=NOW
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert len(scheduler.list_tasks()) == 1, "the second approval must not add a task"


# ── the budget the admission stage promised ────────────────────────────────


def test_execution_does_not_bypass_the_admission_answer(stores) -> None:
    """A proposal whose verdict was `auto` still executes; the verdict is not a gate.

    ``auto`` means "this may run without asking" -- and a person clicking approve
    is the asking. What must not happen is the reverse: a ``drop`` running.
    """
    proposals, scheduler = stores
    proposal = _approved(proposals)
    # No budget argument: the answer is about permission, and a reversible
    # proposal is permitted. Passing a cap here would be passing something that no
    # longer decides anything.
    decision = decide_admission(proposal, policy=AdmissionPolicy())
    assert decision.answer == "auto"

    outcome = execute_proposal(
        store=proposals, proposal=proposal, scheduler_store=scheduler, now=NOW
    )

    assert outcome["ok"] is True


# ── the loop closes ────────────────────────────────────────────────────────


def test_the_task_id_lands_on_the_proposal(stores) -> None:
    """A task id that lives only in a CLI's stdout is a loop that never closes."""
    proposals, scheduler = stores
    proposal = _approved(proposals)

    outcome = execute_proposal(
        store=proposals, proposal=proposal, scheduler_store=scheduler, now=NOW
    )

    row = proposals.list_proposals()[0]
    assert row.context["outcome"]["task_id"] == outcome["task_id"]


def test_the_patrol_reads_the_run_result_back(stores) -> None:
    """Without this, "acted on" is the last thing anybody knows."""
    import asyncio

    from agent.autonomy.patrol import PatrolService

    proposals, scheduler = stores
    proposal = _approved(proposals)
    execute_proposal(
        store=proposals, proposal=proposal, scheduler_store=scheduler, now=NOW
    )

    # The run has to reach a terminal status before the read-back is worth doing.
    service = PatrolService(
        store=proposals,
        sessions_provider=lambda: [],
        scheduler_store_provider=lambda: scheduler,
    )
    assert service.read_back_outcomes() == 0, "a queued run is not an outcome yet"

    task_id = proposals.list_proposals()[0].context["outcome"]["task_id"]
    # A run exists once the task is claimed -- that is the scheduler's first step,
    # and a test that skipped it would be testing a state production never has.
    claimed = scheduler.claim_task_now(task_id, now=NOW)
    assert claimed is not None
    scheduler.complete_run(
        task_id,
        claimed.run.id,
        finished_at=NOW,
        status="succeeded",
        summary="读完了",
    )

    assert service.read_back_outcomes() == 1
    row = proposals.list_proposals()[0]
    assert row.context["outcome"]["status"] == "succeeded"
    assert row.context["outcome"]["summary"] == "读完了"


def test_reading_back_happens_once(stores) -> None:
    """The second pass must not rewrite an outcome that has already been read."""
    from agent.autonomy.patrol import PatrolService

    proposals, scheduler = stores
    proposal = _approved(proposals)
    execute_proposal(
        store=proposals, proposal=proposal, scheduler_store=scheduler, now=NOW
    )
    task_id = proposals.list_proposals()[0].context["outcome"]["task_id"]
    claimed = scheduler.claim_task_now(task_id, now=NOW)
    assert claimed is not None
    scheduler.complete_run(
        task_id, claimed.run.id, finished_at=NOW, status="succeeded", summary="第一次"
    )

    service = PatrolService(
        store=proposals,
        sessions_provider=lambda: [],
        scheduler_store_provider=lambda: scheduler,
    )
    assert service.read_back_outcomes() == 1
    assert service.read_back_outcomes() == 0
    assert proposals.list_proposals()[0].context["outcome"]["summary"] == "第一次"
