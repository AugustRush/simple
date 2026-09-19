"""Tests for StagingBuffer — append/read/clear/persistence."""

import sqlite3

import pytest


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


def test_default_staging_persists_in_sqlite_without_jsonl_file(tmp_path):
    from agent import StagingBuffer

    context_dir = tmp_path / "context"
    buf1 = StagingBuffer(context_dir=context_dir, session_id="session-1")
    buf1.append("user", "sqlite turn")
    buf1.append("assistant", "sqlite reply")

    assert (context_dir / "palace.db").exists()
    assert not buf1.path.exists()

    buf2 = StagingBuffer(context_dir=context_dir, session_id="session-1")

    assert buf2.count() == 2
    assert [msg["content"] for msg in buf2.read_all()] == [
        "sqlite turn",
        "sqlite reply",
    ]


def test_sqlite_staging_close_releases_connections(tmp_path):
    from agent import StagingBuffer

    buf = StagingBuffer(context_dir=tmp_path / "context", session_id="session-1")
    connection = buf._connect()

    buf.close()

    assert buf._thread_connections == {}
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


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
            raise AssertionError(
                "count should use cached state instead of reopening staging"
            )
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


def test_drop_prefix_uses_atomic_write(tmp_path, monkeypatch):
    import agent as agent_module

    buf = make_staging(tmp_path)
    buf.append("user", "first")
    buf.append("assistant", "second")

    calls = []
    real_atomic_write = agent_module._atomic_write_text

    def recording_atomic_write(path, content, encoding="utf-8"):
        calls.append((path, content))
        real_atomic_write(path, content, encoding=encoding)

    monkeypatch.setattr(agent_module, "_atomic_write_text", recording_atomic_write)

    buf.drop_prefix(1)

    assert calls
    assert [msg["content"] for msg in buf.read_all()] == ["second"]


def test_should_session_end_sleep_uses_staging(tmp_path):
    """ContextManager.should_session_end_sleep fires when staging has at least
    one complete user+assistant turn (count >= 2)."""
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

    # Only one message staged (bare user message, no assistant reply) → still False.
    ctx_mgr.mark_activity()
    staging.append("user", "some conversation")
    assert ctx_mgr.should_session_end_sleep() is False

    # Complete user+assistant turn staged → True
    staging.append("assistant", "some reply")
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


def test_retrieve_implicit_context_skips_current_session_for_non_recall_queries(
    tmp_path,
):
    from agent import (
        LTMEntry,
        LTMStore,
        ConsolidationEngine,
        LocalRetriever,
        ContextManager,
        StagingBuffer,
    )

    store = LTMStore(context_dir=tmp_path / "context")
    store.add_entry(
        LTMEntry(
            id="pref-1",
            category="identity",
            entity="user",
            content="Prefers concise responses",
            importance=0.8,
            memory_type="preference",
            created_at="2026-04-13",
            updated_at="2026-04-13",
        )
    )
    staging = StagingBuffer(path=tmp_path / "staging.jsonl")
    staging.append("user", "We just talked about decorators.")
    staging.append("assistant", "Right, and wrappers too.")

    ctx_mgr = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        staging=staging,
    )

    result = ctx_mgr.retrieve_implicit_context("concise responses")

    assert "Prefers concise responses" in result
    assert "Current Session" not in result


def test_retrieve_implicit_context_includes_recent_staging_after_compaction(
    tmp_path,
):
    from agent import (
        LTMStore,
        ConsolidationEngine,
        LocalRetriever,
        ContextManager,
        StagingBuffer,
    )

    store = LTMStore(context_dir=tmp_path / "context")
    staging = StagingBuffer(path=tmp_path / "staging.jsonl")
    staging.append("user", "We decided to keep retries enabled after failures.")
    staging.append("assistant", "Noted, retries must remain armed.")
    staging.append("user", "Also keep the auth fix and retry worker as separate tasks.")
    staging.append("assistant", "Separate tasks, same project.")

    ctx_mgr = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        staging=staging,
    )

    compacted_messages = ctx_mgr.compact_messages(
        [{"role": "user", "content": f"older turn {i}"} for i in range(10)],
        input_token_budget=64,
    )
    result = ctx_mgr.retrieve_implicit_context(
        "What did we decide about retries?",
        current_messages=compacted_messages,
    )

    assert "Current Session" in result
    assert "keep retries enabled after failures" in result


def test_retrieve_implicit_context_recovers_missing_turn_before_visible_tail(tmp_path):
    from agent import ConsolidationEngine, ContextManager, LocalRetriever, LTMStore, StagingBuffer

    store = LTMStore(context_dir=tmp_path / "context")
    staging = StagingBuffer(path=tmp_path / "staging.jsonl")
    staging.append("user", "important decision before the visible tail")
    for index in range(6):
        staging.append("assistant", f"visible tail {index}")
    manager = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        staging=staging,
    )
    current = [
        {"role": "assistant", "content": f"visible tail {index}"}
        for index in range(6)
    ]

    result = manager.retrieve_implicit_context("continue", current_messages=current)

    assert "important decision before the visible tail" in result


def test_sleep_clears_staging(tmp_path):
    """After sleep(), the staging file is cleared."""
    import asyncio
    from agent import (
        LTMStore,
        ConsolidationEngine,
        ContextManager,
        LocalRetriever,
        ModelEndpoint,
        StagingBuffer,
    )

    store = LTMStore(context_dir=tmp_path / "context")

    class FakeEngine(ConsolidationEngine):
        async def consolidate(
            self,
            messages,
            endpoint,
            model,
            keep_last=None,
            staging=None,
            project_scope="",
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
    asyncio.run(ctx_mgr.sleep(messages, ModelEndpoint(None, "openai"), "x"))

    assert ctx_mgr.staging.count() == 0
    assert ctx_mgr._needs_consolidation is False


# ── Orphan discovery (SQLite backend) ─────────────────────────────────────────


def test_discover_sqlite_sessions_finds_every_partition(tmp_path):
    """The SQLite backend keeps each session's turns in one shared database.

    Nothing about a stranded session is visible on the filesystem, so this
    query is the only way a later run can find turns an interrupted one left.
    """
    from agent import StagingBuffer

    first = StagingBuffer(context_dir=tmp_path, session_id="alpha")
    first.append("user", "one")
    first.append("assistant", "two")
    second = StagingBuffer(context_dir=tmp_path, session_id="beta")
    second.append("user", "solo")
    first.close()
    second.close()

    found = dict(StagingBuffer.discover_sqlite_sessions(tmp_path))

    assert found == {"alpha": 2, "beta": 1}


def test_discover_sqlite_sessions_skips_consolidated_partitions(tmp_path):
    """A session whose turns were consumed is not an orphan any more."""
    from agent import StagingBuffer

    buf = StagingBuffer(context_dir=tmp_path, session_id="done")
    buf.append("user", "hello")
    buf.clear_all()
    buf.close()

    assert StagingBuffer.discover_sqlite_sessions(tmp_path) == []


def test_discover_sqlite_sessions_tolerates_a_missing_database(tmp_path):
    """A first run has nothing to recover and must still start."""
    from agent import StagingBuffer

    assert StagingBuffer.discover_sqlite_sessions(tmp_path / "absent") == []


def test_discover_sqlite_sessions_tolerates_a_database_without_the_table(tmp_path):
    """A home written before staging moved into SQLite has no such table."""
    from agent import StagingBuffer

    conn = sqlite3.connect(tmp_path / "palace.db")
    conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    assert StagingBuffer.discover_sqlite_sessions(tmp_path) == []


# ── Orphan recovery enqueue ───────────────────────────────────────────────────


class _RecordingManager:
    """A ContextManager stand-in that records what recovery queued."""

    def __init__(self, staging):
        self.staging = staging
        self.queued = []

    def enqueue_staging_job(self, reason, staging):
        self.queued.append((reason, staging.session_id))


def test_orphan_recovery_queues_stranded_sqlite_sessions(tmp_path, monkeypatch):
    """The regression this exists for: stranded rows had no collector at all.

    Staged turns are only deleted by a successful consolidation, so a session
    that dies in between leaves rows that no later run would ever look at.
    """
    from agent import StagingBuffer, enqueue_orphan_staging_recovery
    from agent import shared

    monkeypatch.setattr(shared, "STAGING_DIR", tmp_path / "_staging")

    stranded = StagingBuffer(context_dir=tmp_path, session_id="stranded")
    stranded.append("user", "never consolidated")
    stranded.close()

    current = StagingBuffer(context_dir=tmp_path, session_id="current")
    manager = _RecordingManager(current)

    assert enqueue_orphan_staging_recovery(manager) == 1
    assert manager.queued == [("orphan_recovery", "stranded")]
    current.close()


def test_orphan_recovery_leaves_the_live_session_alone(tmp_path, monkeypatch):
    """Consolidating the running session's own turns would race its writer."""
    from agent import StagingBuffer, enqueue_orphan_staging_recovery
    from agent import shared

    monkeypatch.setattr(shared, "STAGING_DIR", tmp_path / "_staging")

    current = StagingBuffer(context_dir=tmp_path, session_id="current")
    current.append("user", "in flight")
    manager = _RecordingManager(current)

    assert enqueue_orphan_staging_recovery(manager) == 0
    assert manager.queued == []
    current.close()


def test_orphan_recovery_still_collects_legacy_jsonl_files(tmp_path, monkeypatch):
    """Homes written before the SQLite switch still have files to recover."""
    from agent import StagingBuffer, enqueue_orphan_staging_recovery
    from agent import shared

    legacy_dir = tmp_path / "_staging"
    legacy_dir.mkdir()
    (legacy_dir / "old-session.jsonl").write_text(
        '{"role":"user","content":"old turn","ts":"2026-04-13 00:00 UTC"}\n',
        encoding="utf-8",
    )
    (legacy_dir / "empty-session.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(shared, "STAGING_DIR", legacy_dir)

    current = StagingBuffer(context_dir=tmp_path, session_id="current")
    manager = _RecordingManager(current)

    # The empty file is not a stranded turn, so it must not be queued.
    assert enqueue_orphan_staging_recovery(manager) == 1
    assert manager.queued == [("orphan_recovery", "old-session")]
    current.close()


def test_orphan_recovery_does_not_scan_a_db_for_a_jsonl_session(tmp_path, monkeypatch):
    """A JSONL buffer names no database, so the sweep must not invent one.

    Falling back to the global context dir here would make a test or a legacy
    home reach into whatever palace.db the machine happens to have.
    """
    from agent import StagingBuffer, enqueue_orphan_staging_recovery
    from agent import shared

    monkeypatch.setattr(shared, "STAGING_DIR", tmp_path / "_staging")

    # A populated database sitting in the same directory the JSONL buffer uses.
    stranded = StagingBuffer(context_dir=tmp_path, session_id="stranded")
    stranded.append("user", "rows in the shared db")
    stranded.close()

    jsonl = StagingBuffer(path=tmp_path / "current.jsonl", session_id="current")
    manager = _RecordingManager(jsonl)

    assert enqueue_orphan_staging_recovery(manager) == 0
    assert manager.queued == []
