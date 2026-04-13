"""Tests for ConsolidationEngine and ContextManager — sleep/decay/parse/dirty-flag/idle."""

import time

import pytest


def make_engine(tmp_path):
    from agent import LTMStore, ConsolidationEngine

    store = LTMStore(context_dir=tmp_path / "context")
    return ConsolidationEngine(store=store)


def make_ctx_manager(tmp_path, idle_seconds=300, min_messages=4):
    from agent import LTMStore, ConsolidationEngine, LocalRetriever, ContextManager

    store = LTMStore(context_dir=tmp_path / "context")
    engine = ConsolidationEngine(store=store)
    return ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=engine,
        idle_seconds=idle_seconds,
        min_messages=min_messages,
    )


# ── ConsolidationEngine tests ─────────────────────────────────────────────────


def test_estimate_tokens(tmp_path):
    eng = make_engine(tmp_path)
    messages = [
        {"role": "user", "content": "Hello world"},  # 11 chars
        {"role": "assistant", "content": "Hi there"},  # 8 chars
    ]
    tokens = eng.estimate_tokens(messages)
    assert tokens == (11 + 8) // 4


def test_estimate_tokens_list_content(tmp_path):
    eng = make_engine(tmp_path)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "result data"}
            ],
        }
    ]
    tokens = eng.estimate_tokens(messages)
    assert tokens > 0


def test_should_sleep_true(tmp_path):
    eng = make_engine(tmp_path)
    # 1000 chars / 4 = 250 tokens; threshold = 300 * 0.7 = 210 → 250 > 210
    messages = [{"role": "user", "content": "x" * 1000}]
    assert eng.should_sleep(messages, max_tokens=300) is True


def test_should_sleep_false(tmp_path):
    eng = make_engine(tmp_path)
    messages = [{"role": "user", "content": "hello"}]  # 5 chars = 1 token
    assert eng.should_sleep(messages, max_tokens=8192) is False


def test_parse_entries(tmp_path):
    eng = make_engine(tmp_path)
    raw = (
        "Some preamble text here.\n"
        '{"category": "code_context", "content": "User uses Python 3.11", "importance": 0.8}\n'
        '{"category": "user_prefs", "content": "Prefers concise responses", "importance": 0.7}\n'
        "Non-JSON line skipped.\n"
        '{"category": "tasks", "content": "Fix the auth bug", "importance": 0.9}\n'
    )
    entries = eng._parse_entries(raw)
    assert len(entries) == 3
    assert entries[0].category == "concepts"
    assert entries[0].entity == "code_context"
    assert entries[1].importance == pytest.approx(0.7)
    assert entries[2].content == "Fix the auth bug"


def test_parse_entries_supports_fixed_loci_fields(tmp_path):
    eng = make_engine(tmp_path)
    raw = (
        '{"locus": "identity", "entity": "user", "memory_type": "preference", '
        '"content": "Prefers concise responses", "importance": 0.8, "confidence": 0.9}\n'
    )

    entries = eng._parse_entries(raw)

    assert len(entries) == 1
    assert entries[0].category == "identity"
    assert entries[0].entity == "user"
    assert entries[0].memory_type == "preference"
    assert entries[0].confidence == pytest.approx(0.9)


def test_parse_entries_skips_empty_content(tmp_path):
    eng = make_engine(tmp_path)
    raw = '{"category": "misc", "content": "", "importance": 0.5}\n'
    entries = eng._parse_entries(raw)
    assert len(entries) == 0


def test_parse_entries_invalid_json_ignored(tmp_path):
    eng = make_engine(tmp_path)
    raw = (
        "{invalid json}\n"
        '{"category": "ok", "content": "Valid entry", "importance": 0.6}\n'
    )
    entries = eng._parse_entries(raw)
    assert len(entries) == 1
    assert entries[0].content == "Valid entry"


def test_format_messages_for_llm(tmp_path):
    eng = make_engine(tmp_path)
    messages = [
        {"role": "user", "content": "What is async?"},
        {"role": "assistant", "content": "Async allows non-blocking IO."},
    ]
    text = eng._format_messages_for_llm(messages)
    assert "USER: What is async?" in text
    assert "ASSISTANT: Async allows non-blocking IO." in text


def test_format_messages_list_content(tmp_path):
    eng = make_engine(tmp_path)
    messages = [
        {
            "role": "user",
            "content": [{"type": "tool_result", "content": "file contents here"}],
        }
    ]
    text = eng._format_messages_for_llm(messages)
    assert "file contents here" in text


# ── ContextManager dirty flag tests ──────────────────────────────────────────


def test_dirty_flag_initially_false(tmp_path):
    ctx_mgr = make_ctx_manager(tmp_path)
    assert ctx_mgr._needs_consolidation is False


def test_mark_activity_sets_dirty_flag(tmp_path):
    ctx_mgr = make_ctx_manager(tmp_path)
    ctx_mgr.mark_activity()
    assert ctx_mgr._needs_consolidation is True


def test_should_sleep_requires_dirty_flag(tmp_path):
    ctx_mgr = make_ctx_manager(tmp_path, min_messages=1)
    # Big message list but no activity marked → should NOT sleep
    big_messages = [{"role": "user", "content": "x" * 2000}] * 5
    assert ctx_mgr.should_sleep(big_messages, max_tokens=300) is False
    # After mark_activity → should sleep
    ctx_mgr.mark_activity()
    assert ctx_mgr.should_sleep(big_messages, max_tokens=300) is True


def test_should_sleep_respects_min_messages(tmp_path):
    ctx_mgr = make_ctx_manager(tmp_path, min_messages=4)
    ctx_mgr.mark_activity()
    # Only 2 messages → below min_messages threshold
    small = [{"role": "user", "content": "x" * 2000}] * 2
    assert ctx_mgr.should_sleep(small, max_tokens=300) is False
    # 5 messages → above threshold
    big = [{"role": "user", "content": "x" * 2000}] * 5
    assert ctx_mgr.should_sleep(big, max_tokens=300) is True


def test_idle_elapsed_zero_before_activity(tmp_path):
    ctx_mgr = make_ctx_manager(tmp_path)
    assert ctx_mgr.idle_elapsed() == 0.0


def test_idle_elapsed_increases_after_activity(tmp_path):
    ctx_mgr = make_ctx_manager(tmp_path)
    ctx_mgr.mark_activity()
    time.sleep(0.05)
    assert ctx_mgr.idle_elapsed() >= 0.05


def test_should_idle_sleep_requires_dirty_and_elapsed(tmp_path):
    # idle_seconds=0 means any elapsed time qualifies
    ctx_mgr = make_ctx_manager(tmp_path, idle_seconds=0, min_messages=1)
    messages = [{"role": "user", "content": "hello"}]
    # Not dirty → False
    assert ctx_mgr.should_idle_sleep(messages) is False
    ctx_mgr.mark_activity()
    # Dirty + idle=0 + 1 message ≥ min_messages=1 → True
    assert ctx_mgr.should_idle_sleep(messages) is True


def test_should_idle_sleep_respects_min_messages(tmp_path):
    ctx_mgr = make_ctx_manager(tmp_path, idle_seconds=0, min_messages=4)
    ctx_mgr.mark_activity()
    few = [{"role": "user", "content": "hi"}] * 2
    assert ctx_mgr.should_idle_sleep(few) is False


def test_sleep_clears_dirty_flag(tmp_path, monkeypatch):
    """sleep() must clear _needs_consolidation even if consolidation raises."""
    import asyncio
    from agent import LTMStore, ConsolidationEngine, LocalRetriever, ContextManager

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
            return messages[-2:] if len(messages) > 2 else messages

    engine = FakeEngine(store=store)
    ctx_mgr = ContextManager(
        store=store, retriever=LocalRetriever(), consolidation=engine
    )
    ctx_mgr.mark_activity()
    assert ctx_mgr._needs_consolidation is True

    messages = [{"role": "user", "content": "hello"}] * 4
    result = asyncio.run(ctx_mgr.sleep(messages, client=None, model="x"))
    assert ctx_mgr._needs_consolidation is False
    assert len(result) <= 4


def test_sleep_clears_dirty_flag_when_consolidation_raises(tmp_path):
    import asyncio
    from agent import LTMStore, ConsolidationEngine, LocalRetriever, ContextManager

    store = LTMStore(context_dir=tmp_path / "context")

    class FailingEngine(ConsolidationEngine):
        async def consolidate(
            self,
            messages,
            client,
            model,
            api_format="anthropic",
            keep_last=None,
            staging=None,
        ):
            raise RuntimeError("boom")

    ctx_mgr = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=FailingEngine(store=store),
    )
    ctx_mgr.mark_activity()

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(ctx_mgr.sleep([{"role": "user", "content": "hello"}], None, "x"))

    assert ctx_mgr._needs_consolidation is False


def test_stats_report_dynamic_category_count_against_limit(tmp_path):
    from agent import LTMEntry

    ctx_mgr = make_ctx_manager(tmp_path)
    ctx_mgr.store.max_categories = 1
    ctx_mgr.store.add_entry(
        LTMEntry(
            id="alpha",
            category="alpha",
            content="First dynamic category",
            importance=0.5,
            created_at="2026-04-11",
            updated_at="2026-04-11",
        )
    )
    ctx_mgr.store.add_entry(
        LTMEntry(
            id="identity",
            category="identity",
            entity="user",
            content="Prefers concise responses",
            importance=0.8,
            created_at="2026-04-11",
            updated_at="2026-04-11",
        )
    )

    stats = ctx_mgr.stats()

    assert stats["dynamic_categories"] == 1
    assert stats["max_categories"] == 1


def test_route_categories_uses_configurable_keyword_map(tmp_path):
    from agent import LTMStore, ConsolidationEngine, LocalRetriever, ContextManager

    store = LTMStore(context_dir=tmp_path / "context")
    ctx_mgr = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        route_keywords={
            "episodes": ["recent"],
            "identity": ["style"],
            "projects": ["workspace"],
        },
    )

    assert ctx_mgr._route_categories("recent style workspace") == [
        "episodes",
        "identity",
        "projects",
        "tasks",
    ]
