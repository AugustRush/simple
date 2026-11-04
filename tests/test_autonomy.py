"""The autonomy funnel: what it finds, what it may do, and what it records.

What this pins
--------------
The patrol reads the row sources (the session index, tasks with unreviewed
failures, live queues), applies two silence gates, asks the rules for candidates,
and has admission decide what may happen to each: a reversible one may run, and
everything else is written down and waits for a person. The value of that is
measurable only if the rules and the bookkeeping are trustworthy, so the tests
below are about those:

* every rule fires on a fact that is on disk and does not fire when it is absent;
* a proposal's reversibility comes from the tool's declared capabilities, not
  from anything the model said about itself -- and no declaration is not a clean
  bill of health;
* a task with a failure nobody read gets a row even when it belongs to no
  session, because the rule fires per row;
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
    unowned_task_rows,
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
        "title": f"会话 {session_id}",
        "last_activity": (NOW - timedelta(hours=2)).isoformat(),
    }
    base.update(facts)
    return base


# ── reversibility comes from the declaration ────────────────────────────────


def test_a_read_tool_is_reversible() -> None:
    assert classify_reversibility({"read"}) == "reversible"
    # "No capabilities declared" is not "declared safe": an unknown tool is
    # treated as if it could do anything, which is the safe direction.
    assert classify_reversibility(frozenset()) == "irreversible"


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
    # irreversible: the declaration it needs to say "reversible" is missing, and
    # missing is not the same as safe.
    assert default_capabilities_of("no_such_tool") == frozenset()


def test_an_unknown_tool_is_never_admitted_automatically() -> None:
    """The fail-open this closes: no declaration used to mean reversible, and the
    auto path is exactly "run it without asking"."""
    proposal = Proposal(
        source="s1",
        action="用一个没人分类的插件工具做点什么",
        tool="no_such_tool",
        reversibility=classify_reversibility(default_capabilities_of("no_such_tool")),
        confidence=0.9,
    )

    assert proposal.reversibility == "irreversible"
    assert decide_admission(proposal).answer == "prepare", (
        "an undeclared tool must reach a person, never the auto path"
    )


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


# ── a task with no session: its own row ─────────────────────────────────────


def test_a_task_without_a_session_gets_its_own_row() -> None:
    """The join needs a row to attach attention to; this task has no session.

    A task created without a session id -- which is every task the patrol
    executes -- has nothing for ``failed-run-unreviewed`` to fire on, so its
    failures were permanently invisible.  The row it gets must carry real
    activity (the newest run's time), because the warm/stale gates drop rows by
    it.
    """
    created = (NOW - timedelta(days=3)).isoformat()
    latest_run = (NOW - timedelta(minutes=40)).isoformat()

    rows = unowned_task_rows(
        [
            {
                "task_id": "t1",
                "name": "日报",
                "last_run_at": created,
                "created_at": created,
            },
            {
                "task_id": "t2",
                "name": "周报",
                "last_run_at": "",
                "created_at": created,
            },
            {
                "task_id": "t3",
                "name": "已经挂在会话下",
                "last_run_at": "",
                "created_at": created,
            },
            {
                "task_id": "t4",
                "name": "没有失败",
                "last_run_at": "",
                "created_at": created,
            },
        ],
        attention_counts={"t1": 2, "t2": 1, "t3": 5},
        placed_task_ids={"t3"},
        latest_run_at={"t1": latest_run},
    )

    # t3 is already shown under its session (never twice), t4 has no failures.
    assert [row["session_id"] for row in rows] == ["task:t1", "task:t2"]
    assert rows[0]["title"] == "日报", "the row is named by the task, not a session"
    assert rows[0]["status"] == "task"
    assert rows[0]["last_activity"] == latest_run, (
        "the newest run's time is real activity; the task's creation time is not"
    )
    assert rows[1]["last_activity"] == created, "no run time: fall back to the task's own"
    assert rows[0]["tasks"] == [{"task_id": "t1", "attention": 2}]


# ── the exact call reaches the proposal and the run ────────────────────────


def test_the_admitted_call_reaches_the_proposal_and_the_run_prompt(store) -> None:
    """A run told only "read the failed runs" had to guess which ones -- and a
    real one guessed that they were no longer in the store.

    So the rule names the task, admission writes the arguments down, and the
    prompt the executor gets carries them.
    """
    from agent.autonomy.execute import _task_body

    patrol_tick(
        store=store,
        sessions=[_session("s1", tasks=[{"task_id": "t-failed", "attention": 2}])],
        now=NOW,
        suggest=rule_candidates,
        capabilities_of=default_capabilities_of,
    )

    proposal = store.list_proposals()[0]
    prepared = proposal.context["admission"]["prepared"]
    assert prepared["tool"] == "schedule_runs"
    assert prepared["arguments"] == {"task_id": "t-failed"}

    body = _task_body(proposal, now=NOW)
    assert "t-failed" in body, "the prompt names the task the run has to read"
    assert "schedule_runs" in body, "and the tool to read it with"


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


def test_a_reversible_action_is_automatic() -> None:
    """Permission, not budget: reversibility is the whole answer."""
    decision = decide_admission(_proposal(reversibility="reversible"))

    assert decision.answer == "auto"


def test_an_irreversible_action_is_never_automatic() -> None:
    """No configuration makes this automatic -- there is no configuration left."""
    decision = decide_admission(_proposal(reversibility="irreversible"))

    assert decision.answer == "prepare"
    assert decision.prepared, "a prepare that cannot say what it would do is not one"


def test_a_workspace_write_is_prepared_not_automatic() -> None:
    """Undoable by a person who can see it -- so a person sees it first."""
    decision = decide_admission(_proposal(reversibility="workspace"))

    assert decision.answer == "prepare"


def test_a_declined_proposal_is_dropped_even_when_reversible() -> None:
    decision = decide_admission(
        _proposal(reversibility="reversible"), prior_decision="declined"
    )

    assert decision.answer == "drop"


def test_a_weak_guess_is_dropped_rather_than_shown() -> None:
    from agent.autonomy.admission import AdmissionPolicy

    decision = decide_admission(
        _proposal(confidence=0.1), policy=AdmissionPolicy(min_confidence=0.5)
    )

    assert decision.answer == "drop"


def test_there_is_no_daily_action_cap_left_to_hit() -> None:
    """The counter is gone, so nothing can be "over budget".

    It used to prepare anything past a daily count. That decided *which* finite
    set of work to skip, in arrival order -- and the set is already bounded by the
    facts (a rule fires once per session per fact, dedupe blocks repeats, an
    answered proposal never returns).
    """
    from agent.autonomy.admission import AdmissionPolicy

    policy = AdmissionPolicy()
    assert not hasattr(policy, "max_auto_actions_per_day")
    assert not hasattr(policy, "max_proposals_per_day")

    decision = decide_admission(_proposal(reversibility="reversible"), policy=policy)
    assert decision.answer == "auto"


def test_priority_orders_a_pass_by_who_is_blocked() -> None:
    """A stalled queue blocks a person; an unread failure does not.

    The ranking is an order, not a score: weights nobody can justify would make
    two different situations look equally urgent.
    """
    from agent.autonomy.rules import rule_priority

    assert rule_priority("stale-queue") < rule_priority("failed-run-unreviewed")
    assert rule_priority("failed-run-unreviewed") < rule_priority(
        "session-ended-mid-task"
    )
    assert rule_priority("something-new") > rule_priority("session-ended-mid-task")


def test_a_pass_acts_in_priority_order(store) -> None:
    """Two candidates, one callback order: the blocked person goes first."""
    store_seen: list[str] = []

    if True:
        # One session with a stalled queue (rank 0) and one with unread failures
        # (rank 1). The queue came later but outranks.
        sessions = [
            _session(
                "failing",
                attention=1,
                last_activity=(NOW - timedelta(hours=2)).isoformat(),
            ),
            _session(
                "blocked",
                last_activity=(NOW - timedelta(hours=2)).isoformat(),
                queue={
                    "pending": 2,
                    "queued_since": (NOW - timedelta(hours=1)).isoformat(),
                },
            ),
        ]
        patrol_tick(
            store=store,
            sessions=sessions,
            now=NOW,
            options=PatrolOptions(),
            suggest=rule_candidates,
            on_auto=lambda proposal: store_seen.append(proposal.context["rule"]) or True,
        )

    assert store_seen == ["stale-queue", "failed-run-unreviewed"], (
        "the pass must act on the blocked person before the unread failure"
    )


def test_a_pass_stamps_its_findings_with_its_own_time(store) -> None:
    """Not with the wall clock.

    The rules build proposals with `datetime.now()`, which is right when a rule is
    called directly and wrong when a pass runs at a time somebody chose: a pass
    replaying "yesterday" would carry timestamps from whenever it happened to
    execute, and a proposal is an assertion about *when* the fact was seen.

    This is what caught it: a daily-window test around these timestamps passed on
    the day it was written and failed the next morning, which is the signature of
    a clock leaking into policy.
    """
    long_ago = NOW - timedelta(days=30)
    session = _session(
        "a",
        attention=1,
        last_activity=(long_ago - timedelta(hours=2)).isoformat(),
    )

    patrol_tick(
        store=store,
        sessions=[session],
        now=long_ago,
        options=PatrolOptions(),
        suggest=rule_candidates,
    )

    row = store.list_proposals()[0]
    assert row.created_at == long_ago, "the pass's own time, not the wall clock's"
    assert row.expires_at == long_ago + timedelta(seconds=DEFAULT_EXPIRY_SECONDS)


# ── what the pass spends its budget on ──────────────────────────────────────


def _task_row(task_id: str = "t1", attention: int = 2) -> dict:
    """The row `unowned_task_rows` builds for a failure with no conversation."""
    return {
        "session_id": f"task:{task_id}",
        "title": "构建检查",
        "last_activity": (NOW - timedelta(hours=2)).isoformat(),
        "status": "task",
        "tasks": [{"task_id": task_id, "attention": attention}],
    }


def test_the_pass_reads_its_own_errands_before_conversations(store) -> None:
    """A capped pass must not spend the cap on rows that carry no fact.

    `max_sessions_per_tick` bounds the work, and a plain slice ranks by list
    order -- in which the task rows come last. On an install with more
    conversations than the cap, every row that exists *because something failed*
    would be dropped before it was read, while quiet conversations took its place.
    """
    quiet = [
        _session(f"chat{index}", last_activity=(NOW - timedelta(hours=2)).isoformat())
        for index in range(5)
    ]

    patrol_tick(
        store=store,
        sessions=[*quiet, _task_row()],
        now=NOW,
        options=PatrolOptions(max_sessions_per_tick=2),
        suggest=rule_candidates,
    )

    rows = store.list_proposals()
    assert [row.source for row in rows] == ["task:t1"], (
        "the row that named a failure is the one the pass looked at"
    )


def test_the_agent_s_own_errands_are_recognisable() -> None:
    """Provenance the funnel can read, and a permissive default for anything else.

    Reading a person's task as the agent's own would hide a real failure; the
    opposite mistake only costs one redundant proposal.
    """
    from agent.autonomy import is_own_errand

    from agent.autonomy import OWN_ERRAND_PREFIX

    assert is_own_errand({"prompt": "go", "origin": "autonomy"})
    assert not is_own_errand({"prompt": "go"})
    assert not is_own_errand({"origin": "person"})
    assert not is_own_errand(None)
    assert not is_own_errand("origin=autonomy")
    # The name is the same fact, spelled by the same constant -- for rows created
    # before the payload carried the marker.
    assert is_own_errand({}, f"{OWN_ERRAND_PREFIX}读一遍")
    assert not is_own_errand({}, "每天早上看日志")
