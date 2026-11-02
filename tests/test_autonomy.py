"""The shadow patrol: proposals, not actions.

What this pins
--------------
The first delivery of autonomy is deliberately unable to *do* anything. It reads
the session directory, applies two silence gates, asks the rules for candidates,
and writes rows. The value of that is measurable only if the rules are
trustworthy, so the tests below are about the *rules* and the *bookkeeping*:

* every rule fires on a fact that is on disk and does not fire when it is absent;
* a proposal's reversibility comes from the tool's declared capabilities, not
  from anything the model said about itself;
* a suggestion somebody declined does not come back in new words -- the failure
  this table exists to prevent;
* one tick cannot flood the table.

What is deliberately not tested here: that the patrol calls a model. It does not,
and a test that mocked one would be describing a later version.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from agent.autonomy import (
    AUTONOMY_STATES,
    DEFAULT_EXPIRY_SECONDS,
    MAX_PROPOSALS_PER_TICK,
    PatrolOptions,
    Proposal,
    classify_reversibility,
    default_capabilities_of,
    patrol_tick,
)
from agent.autonomy.admission import AdmissionPolicy, decide_admission
from agent.autonomy.rules import RULE_NAMES, rule_candidates
from agent.autonomy.store import ProposalStore

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@pytest.fixture()
def store(tmp_path):
    store = ProposalStore(tmp_path / "proposals.db")
    yield store
    store.close()


def _session(session_id: str = "s1", **facts) -> dict:
    base = {
        "session_id": session_id,
        "title": "构建失败排查",
        "last_activity": (NOW - timedelta(hours=2)).isoformat(),
    }
    base.update(facts)
    return base


# ── reversibility comes from the declaration ────────────────────────────────


def test_a_read_tool_is_reversible() -> None:
    assert classify_reversibility({"read"}) == "reversible"
    assert classify_reversibility(frozenset()) == "reversible"


def test_a_file_write_is_reversible_but_leaves_a_trace() -> None:
    """``output_write`` is its own class: undoable, and visible to review."""
    assert classify_reversibility({"output_write"}) == "workspace"


def test_state_writes_and_shell_are_irreversible() -> None:
    """The three that can have done anything outside the workspace."""
    for capability in ("state_write", "side_effect", "shell"):
        assert classify_reversibility({capability}) == "irreversible"


def test_one_irreversible_capability_wins_over_the_rest() -> None:
    """A tool that can read *and* send is not reversible because it can read."""
    assert classify_reversibility({"read", "side_effect"}) == "irreversible"


def test_the_builtin_vocabulary_agrees_with_the_classifier() -> None:
    """The table the agent actually registers, read through the classifier."""
    assert default_capabilities_of("schedule_runs") == frozenset({"read"})
    assert classify_reversibility(default_capabilities_of("schedule_runs")) == "reversible"
    # An unknown tool answers "no capabilities", which the classifier reads as
    # reversible -- and the *patrol* is what turns that into "cannot be trusted".
    assert default_capabilities_of("no_such_tool") == frozenset()


# ── the rules fire on facts, and only on facts ──────────────────────────────


def test_a_rule_without_its_fact_does_not_fire() -> None:
    """An empty session proposes nothing. Rules do not guess."""
    assert rule_candidates(_session(), now=NOW) == []


def test_an_unreviewed_failure_becomes_a_proposal() -> None:
    proposals = rule_candidates(
        _session(attention=2), now=NOW, capabilities_of=default_capabilities_of
    )

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.context["rule"] == "failed-run-unreviewed"
    assert proposal.reversibility == "reversible", "reading is not a change"
    assert "2" in proposal.reason, "the reason names the fact it fired on"


def test_an_unfinished_task_becomes_a_proposal() -> None:
    proposals = rule_candidates(
        _session(task={"status": "interrupted", "active_goal": "把报告写完"}),
        now=NOW,
    )

    assert [p.context["rule"] for p in proposals] == ["session-ended-mid-task"]
    assert "把报告写完" in proposals[0].reason


def test_a_finished_task_does_not() -> None:
    assert rule_candidates(
        _session(task={"status": "done", "active_goal": "把报告写完"}), now=NOW
    ) == []


def test_a_queue_that_just_arrived_does_not_fire() -> None:
    """The wait is the fact, not the queue: a message two minutes old is normal."""
    assert rule_candidates(
        _session(
            queue={"pending": 1},
            last_activity=(NOW - timedelta(minutes=2)).isoformat(),
        ),
        now=NOW,
    ) == []


def test_a_queue_that_has_not_moved_does() -> None:
    proposals = rule_candidates(
        _session(
            last_activity=(NOW - timedelta(hours=2)).isoformat(),
            queue={"pending": 3},
        ),
        now=NOW,
    )

    assert [p.context["rule"] for p in proposals] == ["stale-queue"]
    assert "3" in proposals[0].reason


def test_every_rule_names_a_tool_that_can_be_classified() -> None:
    """A rule that cannot name what it would call cannot be allowed to act."""
    sessions = [
        _session(attention=1),
        _session(task={"status": "blocked", "active_goal": "x"}),
        _session(queue={"pending": 2}, last_activity=(NOW - timedelta(hours=3)).isoformat()),
    ]
    proposals = [p for s in sessions for p in rule_candidates(s, now=NOW)]

    assert len(proposals) == 3
    for proposal in proposals:
        assert proposal.tool, f"{proposal.context['rule']} names no tool"
        assert proposal.reversibility in {"reversible", "workspace", "irreversible"}
        assert proposal.context["rule"] in RULE_NAMES


# ── the patrol: gates, caps, dedupe ─────────────────────────────────────────


def test_a_warm_session_is_skipped(store) -> None:
    """A conversation somebody is in does not need a second voice."""
    sessions = [_session(last_activity=(NOW - timedelta(minutes=3)).isoformat())]

    result = patrol_tick(
        store=store, sessions=sessions, now=NOW, suggest=rule_candidates
    )

    assert result["skipped_warm"] == 1
    assert result["proposed"] == 0


def test_a_stale_session_is_skipped(store) -> None:
    """A week of silence is a person who left."""
    sessions = [_session(last_activity=(NOW - timedelta(days=10)).isoformat())]

    result = patrol_tick(
        store=store, sessions=sessions, now=NOW, suggest=rule_candidates
    )

    assert result["skipped_stale"] == 1
    assert result["proposed"] == 0


def test_the_two_gates_are_configurable_to_off(store) -> None:
    sessions = [_session(last_activity=(NOW - timedelta(minutes=3)).isoformat())]

    result = patrol_tick(
        store=store,
        sessions=sessions,
        now=NOW,
        options=PatrolOptions(warm_window_seconds=0, stale_window_seconds=0),
        suggest=rule_candidates,
    )

    assert result["eligible"] == 1


def test_a_tick_writes_its_proposals(store) -> None:
    result = patrol_tick(
        store=store,
        sessions=[_session(attention=2)],
        now=NOW,
        suggest=rule_candidates,
    )

    assert result["proposed"] == 1
    rows = store.list_proposals()
    assert len(rows) == 1
    assert rows[0].state == "proposed"
    assert rows[0].is_live(NOW) is True


def test_a_declined_proposal_does_not_come_back(store) -> None:
    """The failure this table exists to prevent: the same suggestion, reworded.

    ``already_decided`` answers it by source and action -- *not* by reason,
    because the reason is exactly what a model rephrases.
    """
    first = patrol_tick(
        store=store, sessions=[_session(attention=2)], now=NOW, suggest=rule_candidates
    )
    proposal_id = first["proposals"][0]["id"]
    assert store.decide(proposal_id, "declined", by="test") is True

    second = patrol_tick(
        store=store, sessions=[_session(attention=2)], now=NOW, suggest=rule_candidates
    )

    assert second["proposed"] == 0
    assert second["duplicates"] == 1


def test_a_live_proposal_is_not_written_twice(store) -> None:
    """Two identical live rows is the same suggestion counted twice."""
    assert store.record(Proposal(source="s1", action="读一遍失败的运行")) is True
    assert store.record(Proposal(source="s1", action="读一遍失败的运行")) is False


def test_one_tick_cannot_flood_the_table(store) -> None:
    """A tick that writes forty candidates is finding noise, not work."""
    sessions = [_session(f"s{i}", attention=1) for i in range(20)]

    result = patrol_tick(
        store=store,
        sessions=sessions,
        now=NOW,
        options=PatrolOptions(max_sessions_per_tick=20),
        suggest=rule_candidates,
    )

    assert result["proposed"] <= MAX_PROPOSALS_PER_TICK * 20
    # ...and the per-tick session cap is honoured too.
    result = patrol_tick(
        store=store,
        sessions=sessions,
        now=NOW,
        options=PatrolOptions(max_sessions_per_tick=3),
        suggest=rule_candidates,
    )
    assert result["examined"] == 3


# ── the store: expiry, decisions, counts ─────────────────────────────────────


def test_expiry_is_the_default_lifetime(store) -> None:
    proposal = Proposal(source="s1", action="a")
    assert store.record(proposal) is True

    rows = store.list_proposals()
    span = (rows[0].expires_at - rows[0].created_at).total_seconds()
    assert abs(span - DEFAULT_EXPIRY_SECONDS) < 5


def test_expired_proposals_stop_being_live(store) -> None:
    store.record(Proposal(source="s1", action="a"))
    later = NOW + timedelta(days=3)

    expired = store.expire_stale(now=later)

    assert expired == 1
    assert store.list_proposals(state="expired")
    assert store.list_proposals(state="proposed") == []


def test_a_decision_cannot_be_overwritten(store) -> None:
    """Who said no is the record; a later write must not erase it."""
    store.record(Proposal(source="s1", action="a"))
    proposal_id = store.list_proposals()[0].id

    assert store.decide(proposal_id, "declined", by="person") is True
    assert store.decide(proposal_id, "accepted", by="loop") is False
    assert store.list_proposals()[0].decided_by == "person"


def test_counts_are_the_page_s_number(store) -> None:
    store.record(Proposal(source="s1", action="a"))
    store.record(Proposal(source="s1", action="b"))
    store.decide(store.list_proposals()[0].id, "declined", by="test")

    counts = store.counts()

    assert counts == {"proposed": 1, "declined": 1}
    assert sum(counts.values()) == 2


def test_every_state_in_the_vocabulary_is_writable(store) -> None:
    """The five words are the contract; a typo in one is a silent dead row."""
    for state in AUTONOMY_STATES:
        if state == "proposed":
            continue
        proposal = Proposal(source=f"s-{state}", action="a")
        store.record(proposal)
        assert store.decide(proposal.id, state, by="test") is True
        assert store.list_proposals()[0].state == state


def test_a_store_is_plain_sqlite(tmp_path) -> None:
    """Inspectable with ``sqlite3``: the calibration needs to read these rows."""
    import sqlite3

    path = tmp_path / "p.db"
    store = ProposalStore(path)
    store.record(Proposal(source="s1", action="a"))
    store.close()

    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT source, action, state FROM proposals").fetchone()
    finally:
        conn.close()
    assert row == ("s1", "a", "proposed")


def test_without_a_capability_source_nothing_is_called_reversible() -> None:
    """The safe direction to be wrong in, pinned.

    ``rule_candidates`` classifies through whatever ``capabilities_of`` it is
    given. With none, a rule cannot prove its tool is harmless, so every proposal
    is ``irreversible`` -- which in a later version means "prepare it and ask",
    never "do it". The patrol always passes the registry lookup; a caller that
    forgets gets the conservative answer rather than a free pass.
    """
    proposals = rule_candidates(_session(attention=1), now=NOW)

    assert len(proposals) == 1
    assert proposals[0].reversibility == "irreversible"
    assert proposals[0].tool == "schedule_runs", (
        "the tool is still named, so a reader can see what would have been called"
    )


# ── admission: what may happen to a proposal ────────────────────────────────


def _proposal(reversibility: str = "reversible", confidence: float = 0.8) -> Proposal:
    return Proposal(
        source="s1",
        action="读一遍失败的运行",
        tool="schedule_runs",
        reversibility=reversibility,
        confidence=confidence,
    )


def test_shadow_mode_prepares_everything() -> None:
    """The default budget is zero: this delivery proposes, it does not act."""
    decision = decide_admission(_proposal(), policy=AdmissionPolicy())

    assert decision.answer == "prepare"
    assert decision.prepared["tool"] == "schedule_runs"
    assert "额度" in decision.reason


def test_a_reversible_action_within_budget_is_automatic() -> None:
    decision = decide_admission(
        _proposal(), policy=AdmissionPolicy(max_auto_actions_per_day=3)
    )

    assert decision.answer == "auto"
    assert decision.prepared is not None, "even an auto answer says what would run"


def test_an_irreversible_action_is_never_automatic() -> None:
    """No budget makes it automatic: there is no configuration for that."""
    decision = decide_admission(
        _proposal("irreversible"),
        policy=AdmissionPolicy(max_auto_actions_per_day=999),
    )

    assert decision.answer == "prepare"
    assert "工作区之外" in decision.reason


def test_a_workspace_write_is_prepared_not_automatic() -> None:
    """Writing files is undoable *by a person who can see them* -- so a person sees them."""
    decision = decide_admission(
        _proposal("workspace"),
        policy=AdmissionPolicy(max_auto_actions_per_day=999),
    )

    assert decision.answer == "prepare"


def test_a_low_confidence_proposal_is_dropped() -> None:
    decision = decide_admission(_proposal(confidence=0.2))

    assert decision.answer == "drop"
    assert "置信度" in decision.reason


def test_a_declined_proposal_is_dropped_even_when_it_would_be_automatic() -> None:
    """The order of the checks: "somebody said no" outranks every budget."""
    decision = decide_admission(
        _proposal(),
        policy=AdmissionPolicy(max_auto_actions_per_day=99),
        prior_decision="declined",
    )

    assert decision.answer == "drop"
    assert "拒过" in decision.reason


def test_the_daily_proposal_cap_drops_rather_than_shows() -> None:
    decision = decide_admission(
        _proposal(), policy=AdmissionPolicy(max_proposals_per_day=2), proposals_today=2
    )

    assert decision.answer == "drop"
    assert "上限" in decision.reason


def test_the_patrol_records_the_verdict_on_the_row(store) -> None:
    """A row without its admission answer cannot be audited."""
    patrol_tick(store=store, sessions=[_session(attention=1)], now=NOW, suggest=rule_candidates)

    row = store.list_proposals()[0]
    assert row.context["admission"]["answer"] in {"auto", "prepare"}
    assert row.context["admission"]["prepared"]["tool"] == row.tool


def test_the_patrol_does_not_write_dropped_candidates(store) -> None:
    """A dropped candidate is not written: the row would be the attention it saves."""
    result = patrol_tick(
        store=store,
        sessions=[_session(attention=1)],
        now=NOW,
        options=PatrolOptions(
            admission=AdmissionPolicy(min_confidence=0.99, max_proposals_per_day=10)
        ),
        suggest=rule_candidates,
    )

    assert result["proposed"] == 0
    assert result["dropped"] == 1
    assert store.list_proposals() == []
