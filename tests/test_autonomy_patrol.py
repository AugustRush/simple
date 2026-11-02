"""The patrol on a clock: one pass per interval, and nothing else.

The service exists so the patrol runs during *silence* -- the state it is looking
for. These tests pin the properties that make it safe to leave running in a
gateway:

* it never raises into the loop (a patrol failure is not a gateway failure);
* one pass is bounded (the caps live in the store and the policy, not here);
* stopping actually stops it;
* a pass with no sessions is a pass, not an error.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from agent.autonomy import Proposal
from agent.autonomy.admission import AdmissionPolicy
from agent.autonomy.patrol import PatrolService, PatrolSettings
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


# ── settings ───────────────────────────────────────────────────────────────


def test_a_missing_section_means_enabled_shadow_defaults() -> None:
    """The first version is harmless, so it may be on by default.

    When a later version can *act*, this default has to flip -- and the test that
    fails then is this one, which is the point of writing it down.
    """
    settings = PatrolSettings.from_config({})

    assert settings.enabled is True
    assert settings.max_auto_actions_per_day == 0, "shadow mode: nothing is executed"


def test_a_configured_section_is_read() -> None:
    settings = PatrolSettings.from_config(
        {"autonomy": {"interval_minutes": 3, "max_auto_actions_per_day": 2}}
    )

    assert settings.interval_minutes == 3
    assert settings.max_auto_actions_per_day == 2


def test_unreadable_values_keep_their_defaults() -> None:
    """A gateway must not fail to start because one number is a word."""
    settings = PatrolSettings.from_config(
        {"autonomy": {"interval_minutes": "often", "min_confidence": "high"}}
    )

    assert settings.interval_minutes == 10
    assert settings.min_confidence == 0.5


def test_the_interval_has_a_floor() -> None:
    """Zero minutes is a busy loop, which is the one thing a patrol must not be."""
    assert PatrolSettings.from_config({"autonomy": {"interval_minutes": 0}}).interval_minutes == 1


# ── the pass ───────────────────────────────────────────────────────────────


def test_one_pass_proposes_and_writes(store) -> None:
    service = PatrolService(store=store, sessions_provider=lambda: [_session(attention=2)])

    result = asyncio.run(service.tick(now=NOW))

    assert result["proposed"] == 1
    assert store.list_proposals()[0].state == "proposed"


def test_a_pass_expires_what_nobody_answered(store) -> None:
    """Expiry belongs to the pass: the table is what the next pass reads."""
    store.record(Proposal(source="old", action="a"))
    service = PatrolService(store=store, sessions_provider=lambda: [])

    result = asyncio.run(service.tick(now=NOW + timedelta(days=2)))

    assert result["expired"] == 1
    assert store.list_proposals(state="proposed") == []


def test_a_broken_session_list_is_an_empty_pass(store) -> None:
    """A patrol that cannot read the list is off, not crashed."""
    def broken():
        raise RuntimeError("the index is locked")

    service = PatrolService(store=store, sessions_provider=broken)

    result = asyncio.run(service.tick(now=NOW))

    assert result["examined"] == 0
    assert result["proposed"] == 0


def test_no_provider_means_no_start(tmp_path) -> None:
    store = ProposalStore(tmp_path / "p.db")
    try:
        service = PatrolService(store=store)

        async def run() -> bool:
            return service.start()

        assert asyncio.run(run()) is False
    finally:
        store.close()


def test_a_listener_that_raises_does_not_break_the_pass(store) -> None:
    def listener(result):
        raise RuntimeError("listener is broken")

    service = PatrolService(
        store=store,
        sessions_provider=lambda: [_session(attention=1)],
        on_result=listener,
    )

    result = asyncio.run(service.tick(now=NOW))

    assert result["proposed"] == 1, "the row is written before the listener runs"


# ── the clock ──────────────────────────────────────────────────────────────


def test_starting_twice_does_not_start_two_loops(store) -> None:
    service = PatrolService(store=store, sessions_provider=lambda: [_session()])

    async def run() -> bool:
        assert service.start() is True
        assert service.start() is True, "a second start is a no-op, not a second task"
        await service.stop()
        return True

    assert asyncio.run(run()) is True


def test_stopping_actually_stops(store) -> None:
    """A stopped service leaves no task behind: a gateway shutdown must be clean."""
    service = PatrolService(
        store=store,
        settings=PatrolSettings(interval_minutes=1),
        sessions_provider=lambda: [_session()],
    )

    async def run() -> None:
        assert service.start() is True
        task = service._task
        await asyncio.sleep(0)
        await service.stop()
        assert task is not None and task.done(), "the patrol task must be finished"

    asyncio.run(run())


def test_a_pass_that_raises_does_not_kill_the_loop(store, monkeypatch) -> None:
    """The loop's contract: a failing pass is logged and the next one runs."""
    service = PatrolService(
        store=store,
        settings=PatrolSettings(interval_minutes=1),
        sessions_provider=lambda: [_session()],
    )
    calls = {"n": 0}

    async def broken_tick(now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first pass fails")
        return {}

    monkeypatch.setattr(service, "tick", broken_tick)
    # The real interval is a minute; a test that waits one is a test nobody runs.
    monkeypatch.setattr(service, "interval_seconds", lambda: 0.02)

    async def run() -> None:
        assert service.start() is True
        for _ in range(200):
            await asyncio.sleep(0.01)
            if calls["n"] >= 2:
                break
        await service.stop()
        assert calls["n"] >= 2, "the loop stopped after the failing pass"

    asyncio.run(run())


# ── the funnel's numbers ────────────────────────────────────────────────────


def test_every_pass_records_itself_even_when_it_finds_nothing(store) -> None:
    """A funnel that examines nothing looks exactly like one with nothing to do."""
    service = PatrolService(store=store, sessions_provider=lambda: [])

    asyncio.run(service.tick(now=NOW))

    stats = store.tick_stats()
    assert stats["passes"] == 1
    assert stats["examined"] == 0
    assert stats["proposed"] == 0


def test_the_stats_sum_the_window(store) -> None:
    service = PatrolService(
        store=store,
        sessions_provider=lambda: [_session("a", attention=1), _session("b")],
    )
    asyncio.run(service.tick(now=NOW))
    asyncio.run(service.tick(now=NOW + timedelta(minutes=10)))

    stats = store.tick_stats(since=NOW - timedelta(minutes=1))

    assert stats["passes"] == 2
    assert stats["examined"] == 4, "both passes looked at both sessions"
    # One proposal, not two: only session `a` has the fact the rule fires on, and
    # the second pass finds the same one already live.
    assert stats["proposed"] == 1


def test_the_window_excludes_older_passes(store) -> None:
    service = PatrolService(store=store, sessions_provider=lambda: [_session()])
    asyncio.run(service.tick(now=NOW))

    stats = store.tick_stats(since=NOW + timedelta(hours=1))

    assert stats["passes"] == 0
