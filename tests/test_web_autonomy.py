"""The autonomy page's HTTP surface: what it shows, and the one thing it may change.

The page exists so reading a proposal and answering it are one visit. These tests
pin the two halves that make that safe:

* **it cannot act.** ``decide`` accepts only ``accepted`` / ``declined``; a form
  that could write ``acted`` would be a second way to start work, beside the
  patrol's admission check and the executor's walls.
* **a decline is kept.** The answer is the fact the table exists to remember.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from agent.autonomy import Proposal
from agent.autonomy.store import ProposalStore
from agent.channels import web_autonomy

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


class _Request:
    """The two things a handler reads off a request."""

    def __init__(self, body: dict | None = None, query: dict | None = None) -> None:
        self._body = body
        self.query_params = query or {}

    async def json(self) -> dict:
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest.fixture()
def store(tmp_path):
    store = ProposalStore(tmp_path / "proposals.db")
    yield store
    store.close()


def _authorized(request) -> bool:
    return True


def _call(handler, request, store) -> tuple[int, dict]:
    response = asyncio.run(handler(request, authorized=_authorized, store=store))
    return response.status_code, json.loads(response.body)


def _seed(store, action: str = "读一遍失败的运行", **facts) -> str:
    proposal = Proposal(
        source="s1",
        reason="有 2 次运行结束得不好",
        action=action,
        tool="schedule_runs",
        reversibility="reversible",
        confidence=0.8,
        **facts,
    )
    proposal.context["admission"] = {
        "answer": "prepare",
        "reason": "这个动作只能准备好等确认",
        "prepared": {"tool": "schedule_runs", "arguments": {}},
    }
    store.record(proposal)
    return proposal.id


# ── the overview ───────────────────────────────────────────────────────────


def test_the_overview_is_the_rows_and_their_counts_from_one_read(store) -> None:
    _seed(store)
    _seed(store, action="另一条")

    status, body = _call(web_autonomy._autonomy_overview, _Request(), store)

    assert status == 200
    assert len(body["live"]) == 2
    assert body["counts"]["proposed"] == 2, "the header number comes from the same rows"


def test_a_proposal_carries_its_verdict_flat(store) -> None:
    """The page's whole job is "what would happen and why"; the client must not
    have to reach into a context bag for the verdict."""
    _seed(store)

    _status, body = _call(web_autonomy._autonomy_overview, _Request(), store)

    row = body["live"][0]
    assert row["admission"]["answer"] == "prepare"
    assert row["reversibility"] == "reversible"
    assert row["rule"] == "" or isinstance(row["rule"], str)
    assert row["tool"] == "schedule_runs"


def test_an_empty_store_is_an_empty_page(store) -> None:
    status, body = _call(web_autonomy._autonomy_overview, _Request(), store)

    assert status == 200
    assert body["live"] == []
    assert body["counts"] == {}


def test_no_store_is_503_not_a_crash() -> None:
    """The patrol may never have run; that is "nothing yet", not an error."""
    status, body = _call(web_autonomy._autonomy_overview, _Request(), None)

    assert status == 503
    assert "not available" in body["error"]


def test_unauthorized_is_401(store) -> None:
    response = asyncio.run(
        web_autonomy._autonomy_overview(
            _Request(), authorized=lambda request: False, store=store
        )
    )
    assert response.status_code == 401


# ── the decision ───────────────────────────────────────────────────────────


def test_declining_is_recorded(store) -> None:
    proposal_id = _seed(store)

    status, body = _call(
        web_autonomy._autonomy_decide,
        _Request({"id": proposal_id, "state": "declined"}),
        store,
    )

    assert status == 200
    assert body["ok"] is True
    assert store.list_proposals(state="declined")[0].decided_by == "page"


def test_accepting_is_recorded(store) -> None:
    proposal_id = _seed(store)

    status, _body = _call(
        web_autonomy._autonomy_decide,
        _Request({"id": proposal_id, "state": "accepted"}),
        store,
    )

    assert status == 200
    assert store.list_proposals(state="accepted")


def test_the_page_cannot_claim_something_was_acted_on(store) -> None:
    """`acted` is a state this process cannot reach; a form must not write it."""
    proposal_id = _seed(store)

    status, body = _call(
        web_autonomy._autonomy_decide,
        _Request({"id": proposal_id, "state": "acted"}),
        store,
    )

    assert status == 400
    assert "cannot act" in body["error"]
    assert store.list_proposals(state="proposed"), "the row is untouched"


def test_an_unknown_state_is_refused(store) -> None:
    proposal_id = _seed(store)

    status, _body = _call(
        web_autonomy._autonomy_decide,
        _Request({"id": proposal_id, "state": "maybe"}),
        store,
    )

    assert status == 400


def test_deciding_twice_is_a_conflict_not_an_overwrite(store) -> None:
    """Who said no is the record; a second write must not erase it."""
    proposal_id = _seed(store)
    _call(
        web_autonomy._autonomy_decide,
        _Request({"id": proposal_id, "state": "declined"}),
        store,
    )

    status, body = _call(
        web_autonomy._autonomy_decide,
        _Request({"id": proposal_id, "state": "accepted"}),
        store,
    )

    assert status == 409
    assert body["ok"] is False
    assert store.list_proposals(state="declined")[0].decided_by == "page"


def test_a_missing_id_is_a_bad_request(store) -> None:
    status, _body = _call(
        web_autonomy._autonomy_decide, _Request({"state": "declined"}), store
    )
    assert status == 400


def test_a_malformed_body_is_a_bad_request(store) -> None:
    status, _body = _call(web_autonomy._autonomy_decide, _Request(), store)
    assert status == 400


# ── the list ───────────────────────────────────────────────────────────────


def test_the_list_filters_by_state(store) -> None:
    declined = _seed(store, action="a")
    _seed(store, action="b")
    store.decide(declined, "declined", by="test")

    _status, body = _call(
        web_autonomy._autonomy_proposals, _Request(query={"state": "declined"}), store
    )

    assert len(body["proposals"]) == 1
    assert body["proposals"][0]["state"] == "declined"
    assert body["counts"]["proposed"] == 1


def test_the_overview_carries_what_was_answered_and_what_came_of_it(store) -> None:
    """A page that only shows the queue makes approving a leap of faith repeated."""
    from agent.autonomy import Proposal

    proposal = Proposal(
        source="s1", reason="r", action="读一遍失败的运行",
        tool="schedule_runs", reversibility="reversible", confidence=0.8,
    )
    proposal.context["admission"] = {"answer": "prepare", "reason": "x"}
    store.record(proposal)
    store.decide(proposal.id, "acted", by="person")
    store.note_outcome(
        proposal.id, {"task_id": "t1", "status": "succeeded", "summary": "读完了"}
    )

    status, body = _call(web_autonomy._autonomy_overview, _Request(), store)

    assert status == 200
    assert body["live"] == [], "nothing is waiting"
    (answered,) = body["answered"]
    assert answered["state"] == "acted"
    assert answered["outcome"]["status"] == "succeeded"
    assert answered["outcome"]["summary"] == "读完了"


def test_answered_rows_outside_the_panel_window_do_not_load_the_rail(store) -> None:
    """The answered list is a *recent* history, not a scroll.

    The panel's funnel line says 近 7 天 -- the answered rows now say the same
    thing. Before this, three states × 20 rows each rendered every answered
    proposal ever kept, which turned a one-glance rail into a third screen of
    history that only grows.
    """
    from datetime import datetime, timedelta, timezone

    from agent.autonomy import Proposal

    old = Proposal(
        source="s1", reason="r", action="很久以前读过的运行",
        tool="schedule_runs", reversibility="reversible", confidence=0.8,
    )
    store.record(old)
    with store._transaction() as conn:
        stamp = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        conn.execute(
            "UPDATE proposals SET state='acted', decided_at=? WHERE id=?",
            (stamp, old.id),
        )

    fresh = Proposal(
        source="s1", reason="r", action="这周读过的运行",
        tool="schedule_runs", reversibility="reversible", confidence=0.8,
    )
    store.record(fresh)
    store.decide(fresh.id, "acted", by="person")

    _status, body = _call(web_autonomy._autonomy_overview, _Request(), store)

    actions = [row["action"] for row in body["answered"]]
    assert actions == ["这周读过的运行"], "a 30-day-old answer is history, not the rail"


def test_an_answered_row_that_cannot_be_dated_is_shown_rather_than_hidden(store) -> None:
    """Undatable is not "outside the window": hiding is the stronger claim."""
    from agent.autonomy import Proposal

    proposal = Proposal(
        source="s1", reason="r", action="没有时间的回答",
        tool="schedule_runs", reversibility="reversible", confidence=0.8,
    )
    store.record(proposal)
    with store._transaction() as conn:
        conn.execute(
            "UPDATE proposals SET state='acted', decided_at='' WHERE id=?",
            (proposal.id,),
        )

    _status, body = _call(web_autonomy._autonomy_overview, _Request(), store)

    assert [row["action"] for row in body["answered"]] == ["没有时间的回答"]


# ── a "yes" carries out the call the proposal wrote down ───────────────────


def _seed_prepared(store, *, tool="schedule_update", arguments=None) -> str:
    proposal = Proposal(
        source="task:t1",
        reason="「汇报」最近 2 次都是同一个原因失败（连接中断）。",
        action="把这个任务的重试次数从 1 提到 3",
        tool=tool,
        reversibility="workspace",
        confidence=0.75,
    )
    proposal.context["admission"] = {
        "answer": "prepare",
        "reason": "会改任务定义",
        "prepared": {
            "tool": tool,
            "arguments": arguments
            if arguments is not None
            else {"task_id": "t1", "retry_policy": {"max_attempts": 3}},
        },
    }
    store.record(proposal)
    return proposal.id


def test_accepting_applies_the_recorded_call(store) -> None:
    """同意 used to set a state and change nothing; the call was already written."""
    proposal_id = _seed_prepared(store)
    calls: list[tuple[str, dict]] = []

    class _Registry:
        async def call(self, name, arguments):
            calls.append((name, arguments))
            return {"ok": True}

    response = asyncio.run(
        web_autonomy._autonomy_decide(
            _Request({"id": proposal_id, "state": "accepted"}),
            authorized=_authorized,
            store=store,
            registry=_Registry(),
        )
    )
    body = json.loads(response.body)

    assert response.status_code == 200
    assert body["applied"] is True
    assert calls == [
        (
            "schedule_update",
            {
                "task_id": "t1",
                "retry_policy": {"max_attempts": 3},
                "intent": "把这个任务的重试次数从 1 提到 3",
            },
        )
    ]


def test_declining_applies_nothing(store) -> None:
    proposal_id = _seed_prepared(store)
    calls: list[str] = []

    class _Registry:
        async def call(self, name, arguments):  # pragma: no cover - must not run
            calls.append(name)
            return {}

    response = asyncio.run(
        web_autonomy._autonomy_decide(
            _Request({"id": proposal_id, "state": "declined"}),
            authorized=_authorized,
            store=store,
            registry=_Registry(),
        )
    )

    assert response.status_code == 200
    assert calls == []
    assert json.loads(response.body).get("applied") is None
