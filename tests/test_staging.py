"""Tests for StagingBuffer — append/read/clear/persistence."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def make_staging(tmp_path):
    from agent import StagingBuffer

    return StagingBuffer(path=tmp_path / "staging.jsonl")


def test_append_and_read(tmp_path):
    buf = make_staging(tmp_path)
    buf.append("user", "Hello there")
    buf.append("assistant", "Hi! How can I help?")
    msgs = buf.read_all()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "Hello there"
    assert msgs[1]["role"] == "assistant"


def test_count(tmp_path):
    buf = make_staging(tmp_path)
    assert buf.count() == 0
    buf.append("user", "msg1")
    buf.append("assistant", "reply1")
    assert buf.count() == 2


def test_empty_content_skipped(tmp_path):
    buf = make_staging(tmp_path)
    buf.append("user", "")
    buf.append("user", "   ")
    assert buf.count() == 0


def test_clear_all(tmp_path):
    buf = make_staging(tmp_path)
    buf.append("user", "remember this")
    assert buf.count() == 1
    buf.clear_all()
    assert buf.count() == 0
    assert not (tmp_path / "staging.jsonl").exists()


def test_read_all_empty_file(tmp_path):
    buf = make_staging(tmp_path)
    msgs = buf.read_all()
    assert msgs == []


def test_timestamp_recorded(tmp_path):
    buf = make_staging(tmp_path)
    buf.append("user", "test message")
    msgs = buf.read_all()
    assert "ts" in msgs[0]
    assert len(msgs[0]["ts"]) > 0


def test_persistence_across_instances(tmp_path):
    """Buffer survives process restart (new instance reads same file)."""
    from agent import StagingBuffer

    path = tmp_path / "staging.jsonl"
    buf1 = StagingBuffer(path=path)
    buf1.append("user", "turn 1")
    buf1.append("assistant", "response 1")

    buf2 = StagingBuffer(path=path)
    msgs = buf2.read_all()
    assert len(msgs) == 2
    assert msgs[0]["content"] == "turn 1"


def test_multiple_sessions_append(tmp_path):
    """Subsequent session appends to existing staging file."""
    from agent import StagingBuffer

    path = tmp_path / "staging.jsonl"
    StagingBuffer(path=path).append("user", "session 1 msg")
    StagingBuffer(path=path).append("user", "session 2 msg")

    buf = StagingBuffer(path=path)
    assert buf.count() == 2


def test_default_staging_isolated_per_session(tmp_path):
    from agent import StagingBuffer

    buf1 = StagingBuffer(context_dir=tmp_path / "context")
    buf2 = StagingBuffer(context_dir=tmp_path / "context")

    buf1.append("user", "session one")
    buf2.append("user", "session two")

    assert buf1.path != buf2.path
    assert [m["content"] for m in buf1.read_all()] == ["session one"]
    assert [m["content"] for m in buf2.read_all()] == ["session two"]


def test_count_does_not_depend_on_read_all(tmp_path, monkeypatch):
    buf = make_staging(tmp_path)
    buf.append("user", "one")
    buf.append("assistant", "two")

    def fail_read_all():
        raise AssertionError("count should not reparse the whole file")

    monkeypatch.setattr(buf, "read_all", fail_read_all)

    assert buf.count() == 2


def test_count_uses_cached_value_without_reopening_file(tmp_path, monkeypatch):
    import builtins

    buf = make_staging(tmp_path)
    buf.append("user", "one")
    buf.append("assistant", "two")

    original_open = builtins.open

    def guarded_open(*args, **kwargs):
        path = str(args[0]) if args else ""
        mode = kwargs.get("mode") or (args[1] if len(args) > 1 else "r")
        if path.endswith("staging.jsonl") and "r" in mode:
            raise AssertionError("count should use cached state instead of reopening staging")
        return original_open(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)

    assert buf.count() == 2


def test_clear_all_then_append(tmp_path):
    buf = make_staging(tmp_path)
    buf.append("user", "before clear")
    buf.clear_all()
    buf.append("user", "after clear")
    msgs = buf.read_all()
    assert len(msgs) == 1
    assert msgs[0]["content"] == "after clear"


def test_should_session_end_sleep_uses_staging(tmp_path):
    """ContextManager.should_session_end_sleep fires when staging has content."""
    from agent import (
        LTMStore,
        ConsolidationEngine,
        LocalRetriever,
        ContextManager,
        StagingBuffer,
    )

    store = LTMStore(context_dir=tmp_path / "context")
    engine = ConsolidationEngine(store=store)
    staging = StagingBuffer(path=tmp_path / "staging.jsonl")
    ctx_mgr = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=engine,
        staging=staging,
    )

    # Nothing staged, not dirty → False
    assert ctx_mgr.should_session_end_sleep() is False

    # Mark activity + stage a turn → True
    ctx_mgr.mark_activity()
    staging.append("user", "some conversation")
    assert ctx_mgr.should_session_end_sleep() is True


def test_retrieve_context_includes_current_session_staging(tmp_path):
    from agent import (
        LTMStore,
        ConsolidationEngine,
        LocalRetriever,
        ContextManager,
        StagingBuffer,
    )

    store = LTMStore(context_dir=tmp_path / "context")
    staging = StagingBuffer(path=tmp_path / "staging.jsonl")
    staging.append("user", "Explain decorators in Python")
    staging.append("assistant", "We discussed Python decorators and wrappers.")

    ctx_mgr = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        staging=staging,
    )

    result = ctx_mgr.retrieve_context("我们刚才聊了什么")

    assert "Current Session" in result
    assert "Explain decorators in Python" in result
    assert "Python decorators and wrappers" in result


def test_sleep_clears_staging(tmp_path):
    """After sleep(), the staging file is cleared."""
    import asyncio
    from agent import (
        LTMStore,
        ConsolidationEngine,
        LocalRetriever,
        ContextManager,
        StagingBuffer,
    )

    store = LTMStore(context_dir=tmp_path / "context")

    class FakeEngine(ConsolidationEngine):
        async def consolidate(
            self,
            messages,
            client,
            model,
            api_format="anthropic",
            keep_last=None,
            staging=None,
        ):
            if staging:
                staging.clear_all()
            return messages

    staging = StagingBuffer(path=tmp_path / "staging.jsonl")
    staging.append("user", "hello")
    staging.append("assistant", "hi there")

    ctx_mgr = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=FakeEngine(store=store),
        staging=staging,
    )
    ctx_mgr.mark_activity()
    assert ctx_mgr.staging.count() == 2

    messages = [{"role": "user", "content": "hello"}] * 2
    asyncio.run(ctx_mgr.sleep(messages, client=None, model="x"))

    assert ctx_mgr.staging.count() == 0
    assert ctx_mgr._needs_consolidation is False
