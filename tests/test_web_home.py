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
    def __init__(self, query: dict | None = None) -> None:
        self.query_params = query or {}

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


def test_a_running_run_appears_in_the_running_column(stores) -> None:
    scheduler, proposals = stores
    task = _task(scheduler, "收集")
    scheduler.claim_task_now(task.id, now=NOW)

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    (card,) = payload["board"]["running"]
    assert card["task_id"] == task.id
    assert card["at_kind"] == "started"


def test_a_scheduled_task_appears_in_the_scheduled_column(stores) -> None:
    scheduler, proposals = stores
    later = NOW + timedelta(hours=3)
    task = _task(scheduler, "每天整理并汇报", at=later)

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    (card,) = payload["board"]["scheduled"]
    assert card["task_id"] == task.id
    assert card["at_kind"] == "next"
    assert payload["board"]["running"] == []


def test_a_failed_unread_run_appears_in_the_waiting_column(stores) -> None:
    scheduler, proposals = stores
    task = _task(scheduler, "构建检查")
    _run(scheduler, task, status="failed", error="provider 429")

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    (card,) = payload["board"]["waiting"]
    assert card["name"] == "构建检查"
    assert card["status"] == "failed"
    assert card["detail"].startswith("被限流"), card["detail"]
    assert card["at_kind"] == "finished"


def test_an_acknowledged_failure_leaves_the_waiting_column(stores) -> None:
    """Acknowledging is the act of having read it. A board that kept showing read
    failures would teach people to ignore the column."""
    scheduler, proposals = stores
    task = _task(scheduler, "构建检查")
    run = _run(scheduler, task, status="failed", error="429")
    scheduler.acknowledge_run(task.id, run.id)

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    assert payload["board"]["waiting"] == []


def test_a_proposal_waits_in_the_same_column_as_a_failure(stores) -> None:
    """Both are blocked on a person; the difference is what the person does."""
    scheduler, proposals = stores
    proposals.record(Proposal(source="s", action="读一遍失败的运行", reason="有 2 次"))

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    (card,) = payload["board"]["waiting"]
    assert card["kind"] == "proposal"
    assert card["name"] == "读一遍失败的运行"
    assert card["detail"] == "有 2 次", "the reason is what a decision needs"
    assert card["id"]


def test_proposals_are_not_counted_twice(stores) -> None:
    """The waiting column holds them; `needs` must not be a second copy.

    A strip above the board saying "1 次运行结束得不好" over a column showing that
    same failure is the page saying one thing twice, which is the exact fault the
    thread list had before it moved to the sidebar.
    """
    scheduler, proposals = stores
    proposals.record(Proposal(source="s", action="读一遍失败的运行"))

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    assert len(payload["board"]["waiting"]) == 1
    # `needs` stays as the count for anything that wants a badge, but the board is
    # the one place that renders the rows.
    assert payload["needs"]["proposals"] == 1


def test_the_scheduled_column_is_a_preview(stores) -> None:
    """A board is glanced at; thirty cards in the third column is a list."""
    scheduler, proposals = stores
    for index in range(6):
        _task(scheduler, f"task {index}", at=NOW + timedelta(hours=index + 1))

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    assert len(payload["board"]["scheduled"]) == web_home.BOARD_SCHEDULED_PREVIEW
    assert payload["board"]["scheduled_total"] == 6, "the rest is one click away"


def test_a_disabled_task_is_not_shown_as_coming(stores) -> None:
    """A paused task is not "next" -- it will not run until somebody says so."""
    scheduler, proposals = stores
    later = NOW + timedelta(hours=3)
    task = _task(scheduler, "暂停的任务", at=later)
    scheduler.set_enabled(task.id, False)

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    assert payload["board"]["scheduled"] == []


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
    assert set(body) == {"needs", "board", "activity", "activity_next", "at"}


def test_no_stores_is_an_empty_page_not_a_crash() -> None:
    """A process with no scheduler has "nothing scheduled", which is not an error."""
    status, body = _call(
        web_home._home_overview, _Request(), scheduler_store=None, proposal_store=None
    )

    assert status == 200
    assert body["needs"]["failures"] == 0
    assert body["board"] == {
        "running": [], "waiting": [], "scheduled": [], "scheduled_total": 0,
        # No Ralph store in this composition is a configuration, not a gap:
        # nothing is being worked on, and that is the honest answer.
        "working": [],
    }
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


def test_a_task_whose_later_run_succeeded_still_waits_if_a_failure_is_unread(
    stores,
) -> None:
    """Acknowledging is the act of having read it -- not of the next run working.

    This is what made the badge and the board disagree: the badge counts unread
    *runs*, and the board counted only tasks whose *latest* run failed. A retry
    that succeeded left its earlier failure unread and unlisted, so the number on
    the sidebar could not be cleared from the page that claims to show what is
    waiting.
    """
    scheduler, proposals = stores
    task = _task(scheduler, "每天早上看日志")
    _run(scheduler, task, status="failed", error="429", at=NOW - timedelta(hours=1))
    # A retry that worked. The failure above is still unread.
    claimed = scheduler.claim_task_now(task.id, now=NOW)
    assert claimed is not None
    scheduler.complete_run(
        task.id, claimed.run.id, finished_at=NOW, status="succeeded", summary="ok"
    )

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    (card,) = payload["board"]["waiting"]
    assert card["name"] == "每天早上看日志"
    assert card["unread"] == 1
    # The card quotes the failure it is about, not the run that replaced it --
    # in the classified form a reader can act on.
    assert card["detail"].startswith("被限流"), card["detail"]
    # ...and its time is that failure's, not the next run's. A card in the column
    # called "等你处理" that reads "22 小时后" is answering a question nobody
    # asked while hiding the one being asked.
    assert card["at_kind"] == "finished"
    assert card["at"] == (NOW - timedelta(hours=1)).isoformat()


def test_the_waiting_column_adds_up_to_the_badge(stores) -> None:
    """Two numbers on one screen answering one question must agree.

    The badge counts runs; the column counts tasks. The card carries its own
    count so the sum is the badge's number -- otherwise opening the app shows "6"
    above a column holding two cards and nobody can tell which is lying.
    """
    scheduler, proposals = stores
    for name, failures in (("任务甲", 3), ("任务乙", 2)):
        task = _task(scheduler, name)
        claimed = scheduler.claim_task_now(task.id, now=NOW)
        assert claimed is not None
        scheduler.complete_run(
            task.id, claimed.run.id, finished_at=NOW, status="failed", error="429"
        )
        # Plus older unread failures on the same task.
        for index in range(failures - 1):
            run = scheduler.claim_task_now(task.id, now=NOW)
            if run is None:
                break
            scheduler.complete_run(
                task.id,
                run.run.id,
                finished_at=NOW - timedelta(minutes=index + 1),
                status="failed",
                error="429",
            )

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    total = sum(card["unread"] for card in payload["board"]["waiting"])
    assert total == payload["needs"]["failures"], (
        f"the column says {total}, the badge says {payload['needs']['failures']}"
    )


def test_a_waiting_card_does_not_say_it_succeeded(stores) -> None:
    """The column and the card must not contradict each other.

    A task whose retry succeeded has a waiting failure; a card reading "成功" in
    the column called "等你处理" is the page arguing with itself, and the reader
    has no way to tell which half is wrong.
    """
    scheduler, proposals = stores
    task = _task(scheduler, "每天早上看日志")
    _run(scheduler, task, status="failed", error="429", at=NOW - timedelta(hours=1))
    claimed = scheduler.claim_task_now(task.id, now=NOW)
    assert claimed is not None
    scheduler.complete_run(
        task.id, claimed.run.id, finished_at=NOW, status="succeeded", summary="ok"
    )

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    (card,) = payload["board"]["waiting"]
    assert card["status"] == "failed", "the card is about the unread failure"


# ── the timeline is a feed, not a fixed window ─────────────────────────────


def test_the_first_page_publishes_a_cursor(stores) -> None:
    """Without one, "加载更早" can never appear on the first visit.

    The link was written to show only when the client held a cursor -- and the
    client only got one *after* loading a second page, which it could not ask
    for. A full first page has to say so.
    """
    scheduler, proposals = stores
    for index in range(web_home.ACTIVITY_LIMIT + 2):
        task = _task(scheduler, f"task {index}", at=NOW - timedelta(minutes=index))
        _run(scheduler, task, status="succeeded", at=NOW - timedelta(minutes=index))

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    assert payload["activity_next"], "a full page must carry a cursor"


def test_a_short_page_publishes_no_cursor(stores) -> None:
    """Nothing older to ask for, so the link must not be offered."""
    scheduler, proposals = stores
    task = _task(scheduler, "只跑过一次")
    _run(scheduler, task, status="succeeded")

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW
    )

    assert payload["activity_next"] == ""


def test_the_feed_pages_backwards_without_repeating(stores) -> None:
    """A cursor, not an offset: the feed grows at the front, and an offset would
    repeat or skip rows whenever a run finished between two page loads."""
    scheduler, proposals = stores
    for index in range(6):
        task = _task(scheduler, f"task {index}", at=NOW - timedelta(minutes=index))
        _run(scheduler, task, status="succeeded", at=NOW - timedelta(minutes=index))

    first = asyncio.run(
        web_home._activity_feed(
            _Request(query={"limit": "3"}),
            authorized=lambda r: True,
            scheduler_store=scheduler,
        )
    )
    first_body = json.loads(first.body)
    assert len(first_body["activity"]) == 3
    assert first_body["next_before"]

    second = asyncio.run(
        web_home._activity_feed(
            _Request(query={"limit": "3", "before": first_body["next_before"]}),
            authorized=lambda r: True,
            scheduler_store=scheduler,
        )
    )
    second_body = json.loads(second.body)

    first_titles = [item["title"] for item in first_body["activity"]]
    second_titles = [item["title"] for item in second_body["activity"]]
    assert not set(first_titles) & set(second_titles), "pages must not overlap"
    assert len(second_titles) == 3


def test_the_feed_stops_at_the_end(stores) -> None:
    scheduler, proposals = stores
    task = _task(scheduler, "只有一个")
    _run(scheduler, task, status="succeeded")

    response = asyncio.run(
        web_home._activity_feed(
            _Request(query={"limit": "10"}),
            authorized=lambda r: True,
            scheduler_store=scheduler,
        )
    )

    body = json.loads(response.body)
    assert len(body["activity"]) == 1
    assert body["next_before"] == "", "a short page is the last page"


def test_the_ledger_says_when_the_agent_did_it_itself(stores) -> None:
    """An autonomous errand reads as the agent acting, not as one of your tasks.

    The provenance travels as a field, not as a prefix on the name, so the ledger
    can mark the row without asking a reader to decode its title.
    """
    scheduler, proposals = stores
    mine = _task(scheduler, "每天早上看日志")
    _run(scheduler, mine, status="succeeded")

    theirs = scheduler.create_task(
        NewScheduledTask(
            name="读一遍那 2 次失败的运行",
            kind="agent_prompt",
            trigger=TriggerSpec.once(NOW.isoformat(), "UTC"),
            payload={"prompt": "p", "origin": "autonomy"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        )
    )
    _run(scheduler, theirs, status="succeeded", at=NOW + timedelta(minutes=1))

    payload = web_home.build_home_overview(
        scheduler_store=scheduler, proposal_store=proposals, now=NOW + timedelta(minutes=2)
    )

    by_title = {entry["title"]: entry for entry in payload["activity"]}
    assert by_title["读一遍那 2 次失败的运行"]["origin"] == "autonomy"
    assert by_title["每天早上看日志"]["origin"] == "", "a person's task carries no marker"


# ── goals in progress ───────────────────────────────────────────────────────


def test_the_board_shows_a_goal_that_is_being_worked_on():
    """A Ralph goal has no run row while it runs: it is *between* runs.

    The panel answered "is anything happening" from runs alone, so a goal being
    iterated on looked exactly like an idle agent. The goal is the thing the
    person asked for; the runs are how it is being delivered.
    """
    from types import SimpleNamespace

    from agent.channels import web_home

    finished = SimpleNamespace(
        id="done", goal="已经做完的事", status=SimpleNamespace(value="complete"),
        current_iteration=3, max_iterations=5, created_at="2026-11-01T00:00:00+00:00",
    )
    running = SimpleNamespace(
        id="live", goal="整理项目", status=SimpleNamespace(value="running"),
        current_iteration=2, max_iterations=8, created_at="2026-11-02T00:00:00+00:00",
    )

    class _Store:
        def list_tasks(self):
            return [finished, running]

    payload = web_home.build_home_overview(work_store=_Store())

    working = payload["board"]["working"]
    assert [row["id"] for row in working] == ["live"], "a finished goal is a result, not work in flight"
    assert working[0]["goal"] == "整理项目"
    assert (working[0]["iteration"], working[0]["max_iterations"]) == (2, 8)


def test_a_board_with_no_work_store_still_answers():
    """No Ralph in this composition is a configuration, not an error."""
    from agent.channels import web_home

    assert web_home.build_home_overview()["board"]["working"] == []


def test_a_proposal_about_a_task_says_which_task(monkeypatch, tmp_path):
    """The card names the task and opens it: a recommendation you cannot reach
    is a sentence, not a working queue item."""
    from agent import shared
    from agent.autonomy import Proposal
    from agent.autonomy.store import open_proposal_store
    from agent.channels import web_home

    monkeypatch.setattr(shared, "AGENT_HOME", tmp_path)
    store = open_proposal_store()
    try:
        store.record(
            Proposal(
                source="task:t1",
                reason="「汇报」最近 2 次都是同一个原因失败（连接中断）。",
                action="决定「汇报」怎么办：调大超时或重试次数，还是先停掉",
                tool="schedule_create",
                reversibility="workspace",
                confidence=0.75,
                context={"rule": "repeated-task-failure", "task_id": "t1"},
            )
        )
        (card,) = web_home._proposal_cards(store)
    finally:
        store.close()

    assert card["target_task_id"] == "t1"
    assert card["kind"] == "proposal"
