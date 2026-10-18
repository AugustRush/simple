"""Taking the newest turn back: what is cut, what is deliberately left alone.

The feature exists so a reply can be asked again and a message can be edited
before it is sent.  There are three copies of a turn -- the durable transcript,
the provider replay tail (the checkpoint), and the live ``ctx.messages`` -- and
these tests are about cutting exactly the copies that hold it, at the request,
and not one message earlier or later.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.memory.store import LTMStore
from agent.memory.rewind import request_message_index
from agent.session_service import SessionBusyError, SessionService


def _store(tmp_path) -> LTMStore:
    return LTMStore(
        context_dir=tmp_path / "context",
        memory_dir=tmp_path / "memory",
    )


def _write_turn(store, *, session: str, question: str, answer: str, **kwargs) -> None:
    store.write_conversation_exchange(
        session_id=session,
        user_content=question,
        assistant_content=answer,
        channel="web",
        message_id=f"{session}-{question}",
        reply_to_id=f"{session}-{question}",
        assistant_message_id=f"{session}-{question}:reply",
        **kwargs,
    )


def test_truncating_the_transcript_removes_both_tables_and_keeps_the_head(tmp_path):
    store = _store(tmp_path)
    _write_turn(store, session="s", question="第一问", answer="第一答")
    _write_turn(store, session="s", question="第二问", answer="第二答")
    store.append_agent_event(
        session_id="s",
        turn_id="s-第二问",
        event_type="tool_started",
        payload={"operation_id": "op-2", "tool_name": "list_files"},
    )

    before = store.transcript_window(session_id="s")
    request_seq = min(
        int(turn.seq) for turn in before[0] if turn.role == "user" and turn.content == "第二问"
    )

    removed_turns, removed_events = store.truncate_transcript(
        session_id="s", from_seq=request_seq
    )

    # A turn is two positions: the request and the reply. Interjections add
    # more, so the count is "positions at or after the request", not "turns".
    assert removed_turns == 2, "the second request and its reply"
    assert removed_events == 1, "the tool event after it goes with the turn"
    turns, events, _ = store.transcript_window(session_id="s")
    assert [(turn.role, turn.content) for turn in turns] == [
        ("user", "第一问"),
        ("assistant", "第一答"),
    ]
    assert events == []


def test_rewind_returns_the_request_and_removes_the_newest_turn(tmp_path):
    store = _store(tmp_path)
    _write_turn(store, session="s", question="第一问", answer="第一答")
    _write_turn(store, session="s", question="第二问", answer="第二答")

    result = SessionService(store=store).rewind_last_turn("s")

    assert result is not None
    assert result["user_text"] == "第二问"
    assert result["removed_turns"] == 2, "the request and its reply"
    turns, _, _ = store.transcript_window(session_id="s")
    assert [turn.content for turn in turns] == ["第一问", "第一答"]


def test_rewind_cuts_the_checkpoint_at_the_request_message(tmp_path):
    store = _store(tmp_path)
    _write_turn(store, session="s", question="第一问", answer="第一答")
    _write_turn(store, session="s", question="第二问", answer="第二答")
    store.save_provider_checkpoint(
        "s",
        [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            # The request as the provider actually received it: the turn's
            # retrieved context rides in front of the user's words.
            {"role": "user", "content": "<retrieved_context>…</retrieved_context>\n\n第二问"},
            {"role": "assistant", "content": "第二答"},
        ],
    )

    result = SessionService(store=store).rewind_last_turn("s")

    assert result is not None
    assert result["checkpoint_messages_removed"] == 2
    checkpoint = store.load_provider_checkpoint("s")
    assert checkpoint is not None
    assert checkpoint["messages"] == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
    ]


def test_rewind_leaves_a_compacted_checkpoint_alone(tmp_path):
    """The turn is only inside the summary; cutting "the last user message"
    there would delete a *different* turn from the model's history."""
    store = _store(tmp_path)
    _write_turn(store, session="s", question="第一问", answer="第一答")
    _write_turn(store, session="s", question="第二问", answer="第二答")
    compacted = [
        {"role": "system", "content": "（此前对话已压缩为摘要）"},
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
    ]
    store.save_provider_checkpoint("s", compacted)

    result = SessionService(store=store).rewind_last_turn("s")

    assert result is not None
    assert result["checkpoint_messages_removed"] == 0
    assert store.load_provider_checkpoint("s")["messages"] == compacted
    turns, _, _ = store.transcript_window(session_id="s")
    assert [turn.content for turn in turns] == ["第一问", "第一答"]


def test_rewind_skips_an_interjection_when_looking_for_the_request(tmp_path):
    """The newest user row can be an interjection; the turn starts at the
    request before it, and cutting at the interjection would leave the request
    stranded in a turn that no longer has an answer."""
    store = _store(tmp_path)
    _write_turn(store, session="s", question="第一问", answer="第一答")
    _write_turn(
        store,
        session="s",
        question="第二问",
        answer="第二答",
        interjections=[{"text": "等等，换个说法", "message_id": "i-1"}],
    )

    result = SessionService(store=store).rewind_last_turn("s")

    assert result is not None
    assert result["user_text"] == "第二问"
    turns, _, _ = store.transcript_window(session_id="s")
    assert [turn.content for turn in turns] == ["第一问", "第一答"]


def test_rewind_cuts_the_live_context_too(tmp_path):
    store = _store(tmp_path)
    _write_turn(store, session="s", question="第一问", answer="第一答")
    _write_turn(store, session="s", question="第二问", answer="第二答")
    ctx = SimpleNamespace(
        messages=[
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "第二问"},
            {"role": "assistant", "content": "第二答"},
        ]
    )
    live = SimpleNamespace(ctx=ctx, operation_state="idle", restart_queue=[])
    service = SessionService(store=store, live_states={"s": live})

    result = service.rewind_last_turn("s")

    assert result is not None
    assert result["live_messages_removed"] == 2
    assert ctx.messages == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
    ]


def test_rewind_refuses_while_a_turn_is_running(tmp_path):
    store = _store(tmp_path)
    _write_turn(store, session="s", question="问题", answer="答案")
    live = SimpleNamespace(
        ctx=SimpleNamespace(messages=[]),
        operation_state="active",
        restart_queue=[],
    )

    with pytest.raises(SessionBusyError):
        SessionService(store=store, live_states={"s": live}).rewind_last_turn("s")
    turns, _, _ = store.transcript_window(session_id="s")
    assert [turn.content for turn in turns] == ["问题", "答案"], "nothing was cut"


def test_rewind_refuses_when_queued_work_is_waiting(tmp_path):
    store = _store(tmp_path)
    _write_turn(store, session="s", question="问题", answer="答案")
    live = SimpleNamespace(
        ctx=SimpleNamespace(messages=[]),
        operation_state="idle",
        restart_queue=[{"message_id": "m-2", "text": "下一条"}],
    )

    with pytest.raises(SessionBusyError):
        SessionService(store=store, live_states={"s": live}).rewind_last_turn("s")


def test_rewind_returns_none_for_a_session_with_no_turns(tmp_path):
    store = _store(tmp_path)
    assert SessionService(store=store).rewind_last_turn("empty") is None


def test_the_request_index_skips_tool_results_and_interjections():
    messages = [
        {"role": "user", "content": "甲"},
        {"role": "assistant", "content": "甲的回答"},
        {"role": "user", "content": "乙"},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "…"},
                {"type": "text", "text": "乙"},
            ],
        },
        {"role": "assistant", "content": "乙的回答"},
    ]
    # Newest-first: the tool-result message carries the words but is not the
    # request; the plain "乙" message is.
    assert request_message_index(messages, "乙") == 2
    assert request_message_index(messages, "甲") == 0
    assert request_message_index(messages, "丙") is None
