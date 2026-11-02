"""The home page's one request: needs / running / activity.

The page answers "is anything waiting for me, and what has it been doing". Those
two questions have one answer each, computed here -- because the alternative is
each surface counting its own way, and the first place they would disagree is the
failure count the sidebar badge also shows.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from agent.autonomy import Proposal
from agent.autonomy.store import ProposalStore
from agent.channels import web_home
from agent.scheduler import (
    DeliveryTarget,
    NewScheduledTask,
    SchedulerStore,
    TriggerSpec,
)

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def tmp_path():
    path = Path("/tmp") / f"simple-web-home-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def stores(tmp_path):
    scheduler = SchedulerStore(db_path=tmp_path / "scheduler.db")
    proposals = ProposalStore(tmp_path / "proposals.db")
    yield scheduler, proposals
    scheduler.close()
    proposals.close()


class _Request:
    query_params: dict = {}

    async def json(self) -> dict:
        return {}


def _call(handler, request, **kwargs) -> tuple[int, dict]:
    response = asyncio.run(handler(request, authorized=lambda r: True, **kwargs))
    return response.status_code, json.loads(response.body)


def _task(scheduler, name: str, *, session_id: str = "", at: datetime = NOW):
    return scheduler.create_task(
        NewScheduledTask(
            name=name,
            kind="agent_prompt",
            trigger=TriggerSpec.once(at.isoformat(), "UTC"),
            payload={"prompt": "p"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
            session_id=session_id,
        )
    )


def _run(scheduler, task, *, status: str, summary: str = "", error: str = "", at: datetime = NOW):
    claimed = scheduler.claim_task_now(task.id, now=at)
    assert claimed is not None
    scheduler.complete_run(
        task.id, claimed.run.id, finished_at=at, status=status, summary=summary, error=error
    )
    return claimed.run


# ── what needs a person ────────────────────────────────────────────────────


def test_nothing_waiting_is_an_empty_needs_block(stores) -> None:
    """A healthy agent must not take up screen reporting that it is healthy."""
    scheduler, proposals = stores

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    assert payload["needs"] == {"proposals": 0, "failures": 0, "newest_failure": None}


def test_an_unanswered_proposal_is_counted(stores) -> None:
    scheduler, proposals = stores
    proposals.record(Proposal(source="s", action="读一遍失败的运行"))

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    assert payload["needs"]["proposals"] == 1


def test_an_answered_proposal_stops_counting(stores) -> None:
    """The number is "waiting for an answer", not "ever proposed"."""
    scheduler, proposals = stores
    proposal = Proposal(source="s", action="读一遍失败的运行")
    proposals.record(proposal)
    proposals.decide(proposal.id, "declined", by="test")

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    assert payload["needs"]["proposals"] == 0


def test_a_failed_run_counts_and_names_the_newest(stores) -> None:
    scheduler, proposals = stores
    task = _task(scheduler, "每天早上看日志")
    _run(scheduler, task, status="failed", error="provider 429")

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    assert payload["needs"]["failures"] == 1
    assert payload["needs"]["newest_failure"]["task_id"] == task.id


# ── what is happening now ──────────────────────────────────────────────────


def test_a_running_run_appears_in_flight(stores) -> None:
    scheduler, proposals = stores
    task = _task(scheduler, "收集")
    scheduler.claim_task_now(task.id, now=NOW)

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    assert [item["task_id"] for item in payload["running"]["in_flight"]] == [task.id]


def test_a_scheduled_task_appears_in_next_up(stores) -> None:
    scheduler, proposals = stores
    later = NOW + timedelta(hours=3)
    task = _task(scheduler, "每天整理并汇报", at=later)

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    assert [item["task_id"] for item in payload["running"]["next_up"]] == [task.id]
    assert payload["running"]["in_flight"] == []


def test_a_disabled_task_is_not_shown_as_coming(stores) -> None:
    """A paused task is not "next" -- it will not run until somebody says so."""
    scheduler, proposals = stores
    later = NOW + timedelta(hours=3)
    task = _task(scheduler, "暂停的任务", at=later)
    scheduler.set_enabled(task.id, False)

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    assert payload["running"]["next_up"] == []


# ── what happened ──────────────────────────────────────────────────────────


def test_a_finished_run_appears_in_the_timeline(stores) -> None:
    scheduler, proposals = stores
    task = _task(scheduler, "每天早上看日志")
    _run(scheduler, task, status="succeeded", summary="读了 3 条错误")

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    (entry,) = payload["activity"]
    assert entry["kind"] == "run"
    assert entry["title"] == "每天早上看日志"
    assert entry["status"] == "succeeded"
    assert entry["summary"] == "读了 3 条错误"


def test_a_thread_appears_in_the_same_timeline(stores) -> None:
    """A conversation is one of the things it did, so it sorts with the rest."""
    scheduler, proposals = stores

    payload = web_home.build_home_overview(
        scheduler_store=scheduler,
        proposal_store=proposals,
        sessions=[
            {
                "session_id": "s1",
                "title": "帮我看看昨天的构建",
                "last_activity": NOW.isoformat(),
                "turn_count": 2,
            }
        ],
        now=NOW,
    )

    (entry,) = payload["activity"]
    assert entry["kind"] == "thread"
    assert entry["title"] == "帮我看看昨天的构建"
    assert entry["turn_count"] == 2


def test_runs_and_threads_sort_together_by_time(stores) -> None:
    """The rhythm ("we talked in the morning, tasks ran after") needs one list."""
    scheduler, proposals = stores
    earlier = NOW - timedelta(hours=4)
    task = _task(scheduler, "早上的任务", at=earlier)
    _run(scheduler, task, status="succeeded", at=earlier)

    payload = web_home.build_home_overview(
        scheduler_store=scheduler,
        proposal_store=proposals,
        sessions=[
            {
                "session_id": "s1",
                "title": "刚才的对话",
                "last_activity": NOW.isoformat(),
                "turn_count": 1,
            }
        ],
        now=NOW,
    )

    kinds = [entry["kind"] for entry in payload["activity"]]
    assert kinds == ["thread", "run"], "newest first, across both kinds"


def test_the_timeline_is_bounded(stores) -> None:
    """A home page is glanced at; it must not be a scroll."""
    scheduler, proposals = stores
    for index in range(12):
        task = _task(scheduler, f"task {index}", at=NOW - timedelta(minutes=index))
        _run(scheduler, task, status="succeeded", at=NOW - timedelta(minutes=index))

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    assert len(payload["activity"]) == web_home.ACTIVITY_LIMIT


# ── the endpoint ───────────────────────────────────────────────────────────


def test_the_endpoint_returns_the_payload(stores) -> None:
    scheduler, proposals = stores

    status, body = _call(
        web_home._home_overview,
        _Request(),
        scheduler_store=scheduler,
        proposal_store=proposals,
    )

    assert status == 200
    assert set(body) == {"needs", "running", "activity", "at"}


def test_no_stores_is_an_empty_page_not_a_crash() -> None:
    """A process with no scheduler has "nothing scheduled", which is not an error."""
    status, body = _call(
        web_home._home_overview, _Request(), scheduler_store=None, proposal_store=None
    )

    assert status == 200
    assert body["needs"]["failures"] == 0
    assert body["running"] == {"in_flight": [], "next_up": []}
    assert body["activity"] == []


def test_unauthorized_is_401(stores) -> None:
    scheduler, proposals = stores
    response = asyncio.run(
        web_home._home_overview(
            _Request(),
            authorized=lambda r: False,
            scheduler_store=scheduler,
            proposal_store=proposals,
        )
    )
    assert response.status_code == 401
