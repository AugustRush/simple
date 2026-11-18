"""A replaced turn is moved aside, not deleted.

``rewind_last_turn`` used to DELETE the tail, so editing a sentence destroyed the
answer it replaced -- with no warning in the UI that it would. Its own docstring
already knew the right answer ("repairing that is a branch, not a truncation");
the implementation was a truncation.

These tests pin both halves of the fix:

* the **active** table afterwards holds exactly what a delete would have left --
  that is what keeps all 29 readers unchanged;
* the replaced rows are recoverable, with their tool trace, in both directions.
"""

from __future__ import annotations

import pathlib

import pytest


@pytest.fixture()
def store(tmp_path: pathlib.Path):
    from agent.memory.store import LTMStore

    created = LTMStore(tmp_path / "context")
    yield created
    created.close()


def _turn(store, session_id: str, user: str, assistant: str, *, mid: str) -> None:
    store.write_conversation_exchange(
        session_id=session_id,
        user_content=user,
        assistant_content=assistant,
        channel="web",
        message_id=mid,
    )


def _active(store, session_id: str) -> list[str]:
    """The transcript in reading order. ``recent_conversation_turns`` is chronological."""
    return [
        row.content
        for row in store.recent_conversation_turns(session_id=session_id, limit=50)
    ]


def _seq_of_last_request(store, session_id: str) -> int:
    """The newest request's position -- what ``rewind_last_turn`` cuts at."""
    for row in reversed(store.recent_conversation_turns(session_id=session_id, limit=50)):
        if row.role == "user":
            return int(row.seq)
    raise AssertionError("no user row")


def test_the_active_table_ends_up_exactly_as_a_delete_would_have_left_it(store) -> None:
    """The whole reason 29 readers did not change: nothing about them did."""
    session = "s1"
    _turn(store, session, "第一句", "第一答", mid="m1")
    _turn(store, session, "第二句", "第二答", mid="m2")

    before = _active(store, session)
    seq = _seq_of_last_request(store, session)
    store.archive_tail_from(session_id=session, from_seq=seq)
    after = _active(store, session)

    assert after == ["第一句", "第一答"]
    assert before == ["第一句", "第一答", "第二句", "第二答"]


def test_the_replaced_turn_can_be_read_back(store) -> None:
    """This is the data loss the change exists to stop."""
    session = "s1"
    _turn(store, session, "第一句", "第一答", mid="m1")
    _turn(store, session, "第二句", "第二答", mid="m2")

    seq = _seq_of_last_request(store, session)
    _turns, _events, branch_id, turn_index = store.archive_tail_from(
        session_id=session, from_seq=seq
    )

    versions = store.turn_versions(session_id=session, turn_index=turn_index)
    assert len(versions) == 1
    assert versions[0]["branch_id"] == branch_id
    assert versions[0]["request_text"] == "第二句"

    store.restore_turn_version(session_id=session, branch_id=branch_id)
    assert _active(store, session) == ["第一句", "第一答", "第二句", "第二答"]


def test_switching_back_keeps_the_version_you_switched_away_from(store) -> None:
    """Symmetric, so looking at an old answer cannot lose the new one.

    A one-way "restore" would be a second trap of the same shape as the delete it
    replaces: the person gets their old answer back and silently loses the new
    one.
    """
    session = "s1"
    _turn(store, session, "第一句", "第一答", mid="m1")
    _turn(store, session, "原提问", "原回答", mid="m2")

    seq = _seq_of_last_request(store, session)
    _t, _e, old_branch, turn_index = store.archive_tail_from(
        session_id=session, from_seq=seq
    )
    # The person edits and sends a different question at the same position.
    _turn(store, session, "新提问", "新回答", mid="m3")
    assert _active(store, session)[-2:] == ["新提问", "新回答"]

    # Switch to the old version: the new one is archived, not discarded.
    seq_now = _seq_of_last_request(store, session)
    _t, _e, new_branch, _i = store.archive_tail_from(
        session_id=session, from_seq=seq_now, archived_by="switch"
    )
    store.restore_turn_version(session_id=session, branch_id=old_branch)
    assert _active(store, session)[-2:] == ["原提问", "原回答"]

    # And back again.
    seq_old = _seq_of_last_request(store, session)
    _t, _e, _b, _i = store.archive_tail_from(
        session_id=session, from_seq=seq_old, archived_by="switch"
    )
    store.restore_turn_version(session_id=session, branch_id=new_branch)
    assert _active(store, session)[-2:] == ["新提问", "新回答"]


def test_the_tool_trace_travels_with_its_version(store) -> None:
    """An answer whose tool calls were deleted shows a reply from nowhere."""
    session = "s1"
    _turn(store, session, "第一句", "第一答", mid="m1")
    _turn(store, session, "第二句", "第二答", mid="m2")
    store.append_agent_event(
        session_id=session,
        event_type="tool_started",
        payload={"tool_name": "list_files", "operation_id": "op1"},
        turn_id="m2",
    )

    seq = _seq_of_last_request(store, session)
    turns, events, branch_id, _i = store.archive_tail_from(
        session_id=session, from_seq=seq
    )
    assert events >= 1

    store.restore_turn_version(session_id=session, branch_id=branch_id)
    restored = [
        event
        for event in store.transcript_window(session_id=session, limit=50)[1]
        if getattr(event, "event_type", "") == "tool_started"
    ]
    assert restored, "the tool row must come back with its turn"


def test_versions_of_different_turns_do_not_mix(store) -> None:
    """``turn_index`` is what keeps two replaced turns apart."""
    session = "s1"
    _turn(store, session, "一", "答一", mid="m1")
    _turn(store, session, "二", "答二", mid="m2")
    seq2 = _seq_of_last_request(store, session)
    _t, _e, _b2, index2 = store.archive_tail_from(session_id=session, from_seq=seq2)

    _turn(store, session, "二改", "答二改", mid="m3")
    seq3 = _seq_of_last_request(store, session)
    _t, _e, _b3, index3 = store.archive_tail_from(session_id=session, from_seq=seq3)

    assert index2 == 2
    assert index3 == 2, "both replaced the same position"
    assert len(store.turn_versions(session_id=session, turn_index=2)) == 2
    assert store.turn_versions(session_id=session, turn_index=1) == []


def test_an_empty_session_archives_nothing(store) -> None:
    """No rows moved, and no version invented for a turn that never existed."""
    turns, events, _branch, _index = store.archive_tail_from(
        session_id="nobody", from_seq=5
    )

    assert (turns, events) == (0, 0)
    assert store.turn_versions(session_id="nobody", turn_index=1) == []


# ── the wiring: the conversation's own rewind creates a version ──────────────
#
# The store-level tests above pass just as well when `rewind_last_turn` still
# deletes -- they call `archive_tail_from` themselves. Measured: reverting the
# service to the delete path left all of them green. This is the test that
# would have caught it.


def _rewind(store, session_id: str):
    from agent.session_service import SessionService

    service = SessionService(store=store)
    service._store_for_session = lambda _sid: store  # type: ignore[attr-defined]
    return service.rewind_last_turn(session_id)


def test_the_conversations_own_rewind_keeps_the_turn_as_a_version(store) -> None:
    """The data loss the change exists to stop, asserted through the real path."""
    session = "s1"
    _turn(store, session, "第一句", "第一答", mid="m1")
    _turn(store, session, "打错了一字", "被替换的回答", mid="m2")

    result = _rewind(store, session)

    assert result is not None
    assert result["turn_index"] == 2
    branch = result["branch_id"]
    assert branch, "the rewind must report the version it just created"

    versions = store.turn_versions(session_id=session, turn_index=2)
    assert [item["branch_id"] for item in versions] == [branch]
    assert versions[0]["request_text"] == "打错了一字"

    store.restore_turn_version(session_id=session, branch_id=branch)
    assert _active(store, session) == ["第一句", "第一答", "打错了一字", "被替换的回答"]


# ── the service surface the web layer calls ─────────────────────────────────


def test_versions_list_marks_which_one_is_showing(store) -> None:
    from agent.session_service import SessionService

    session = "s1"
    _turn(store, session, "第一句", "第一答", mid="m1")
    _turn(store, session, "原提问", "原回答", mid="m2")
    service = SessionService(store=store)
    service._store_for_session = lambda _sid: store  # type: ignore[attr-defined]

    service.rewind_last_turn(session)
    _turn(store, session, "新提问", "新回答", mid="m3")

    versions = service.turn_versions(session, 2)

    assert len(versions) == 2
    assert [item["selected"] for item in versions] == [False, True]
    assert versions[0]["request_text"] == "原提问"
    assert versions[1]["request_text"] == "新提问"


def test_switching_moves_both_directions(store) -> None:
    """The service-level version of the symmetry test, through the same calls
    the endpoints make."""
    from agent.session_service import SessionService

    session = "s1"
    _turn(store, session, "第一句", "第一答", mid="m1")
    _turn(store, session, "原提问", "原回答", mid="m2")
    service = SessionService(store=store)
    service._store_for_session = lambda _sid: store  # type: ignore[attr-defined]

    rewound = service.rewind_last_turn(session)
    _turn(store, session, "新提问", "新回答", mid="m3")

    switched = service.switch_turn_version(session, 2, rewound["branch_id"])
    assert switched is not None
    assert _active(store, session)[-2:] == ["原提问", "原回答"]

    # The version that was showing is now in the list, still reachable.
    versions = service.turn_versions(session, 2)
    assert [item["request_text"] for item in versions if not item["selected"]] == ["新提问"]

    parked = [item["branch_id"] for item in versions if not item["selected"]][0]
    back = service.switch_turn_version(session, 2, parked)
    assert back is not None
    assert _active(store, session)[-2:] == ["新提问", "新回答"]


def test_switching_to_a_version_that_is_gone_leaves_the_conversation_alone(store) -> None:
    """A failed switch must not leave an empty conversation behind it."""
    from agent.session_service import SessionService

    session = "s1"
    _turn(store, session, "第一句", "第一答", mid="m1")
    _turn(store, session, "第二句", "第二答", mid="m2")
    service = SessionService(store=store)
    service._store_for_session = lambda _sid: store  # type: ignore[attr-defined]

    before = _active(store, session)
    result = service.switch_turn_version(session, 2, "no-such-branch")

    assert result is None
    assert _active(store, session) == before


def test_the_order_is_stable_across_a_switch(store) -> None:
    """The counter must name a position, not "whatever is showing now".

    Measured in a browser: the archived versions always sorted first and the live
    one was appended last, so the switcher read "2 / 2" no matter which version
    was on screen -- the › button was permanently disabled and the ‹ button was
    the only one that ever worked.
    """
    from agent.session_service import SessionService

    session = "s1"
    _turn(store, session, "第一句", "第一答", mid="m1")
    _turn(store, session, "原提问", "原回答", mid="m2")
    service = SessionService(store=store)
    service._store_for_session = lambda _sid: store  # type: ignore[attr-defined]

    rewound = service.rewind_last_turn(session)
    _turn(store, session, "新提问", "新回答", mid="m3")

    before = [(v["request_text"], v["selected"]) for v in service.turn_versions(session, 2)]
    service.switch_turn_version(session, 2, rewound["branch_id"])
    after = [(v["request_text"], v["selected"]) for v in service.turn_versions(session, 2)]

    assert [text for text, _ in before] == [text for text, _ in after], (
        "the list order must not depend on which version is showing"
    )
    # And exactly one is selected, in both directions.
    assert [selected for _, selected in before] == [False, True]
    assert [selected for _, selected in after] == [True, False]


def test_the_archive_does_not_grow_forever(store) -> None:
    """A version is written on every replace and read only on request.

    Without a retention the archive is append-only for the life of the install --
    the same shape `patrol_ticks` had before it grew one.
    """
    session = "s1"
    _turn(store, session, "第一句", "第一答", mid="m1")
    _turn(store, session, "第二句", "第二答", mid="m2")
    seq = _seq_of_last_request(store, session)
    _t, _e, branch_id, _i = store.archive_tail_from(session_id=session, from_seq=seq)

    # Backdate the archived rows past the window, then run retention.
    from agent.memory import maintenance

    with store._connect() as conn:
        conn.execute(
            "UPDATE conversation_turns_archive SET archived_at = '2000-01-01 00:00:00'"
        )
        removed = maintenance.prune_archived_versions(conn)
        conn.commit()

    assert removed == 2
    assert store.turn_versions(session_id=session, turn_index=2) == []
