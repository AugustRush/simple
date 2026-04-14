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
    # Default chars_per_token=4: (11+8) // 4 = 4
    assert tokens == (11 + 8) // 4


def test_estimate_tokens_respects_chars_per_token(tmp_path):
    from agent import ConsolidationEngine, LTMStore

    store = LTMStore(context_dir=tmp_path / "context")
    eng = ConsolidationEngine(store=store, chars_per_token=2.0)
    messages = [{"role": "user", "content": "abcdefgh"}]  # 8 non-CJK chars
    # 8 chars / 2.0 = 4 tokens
    assert eng.estimate_tokens(messages) == 4


def test_estimate_tokens_respects_cjk_chars_per_token(tmp_path):
    from agent import ConsolidationEngine, LTMStore

    store = LTMStore(context_dir=tmp_path / "context")
    eng_default = ConsolidationEngine(store=store, cjk_chars_per_token=1.0)
    eng_relaxed = ConsolidationEngine(store=store, cjk_chars_per_token=2.0)
    messages = [{"role": "user", "content": "你好世界"}]  # 4 CJK chars
    # default (1.0): 4 / 1.0 = 4 tokens
    assert eng_default.estimate_tokens(messages) == 4
    # relaxed (2.0): 4 / 2.0 = 2 tokens
    assert eng_relaxed.estimate_tokens(messages) == 2


def test_estimate_tokens_mixed_cjk_and_latin(tmp_path):
    from agent import ConsolidationEngine, LTMStore

    store = LTMStore(context_dir=tmp_path / "context")
    eng = ConsolidationEngine(store=store, chars_per_token=4.0, cjk_chars_per_token=1.0)
    # 4 CJK + 8 Latin chars
    messages = [{"role": "user", "content": "你好world!!!"}]
    # CJK: 2 chars / 1.0 = 2 tokens; Latin: 8 chars / 4.0 = 2 tokens → 4 total
    assert eng.estimate_tokens(messages) == 4


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


def test_consolidate_includes_full_staging_text_before_clearing(tmp_path):
    import asyncio
    from agent import LTMStore, ConsolidationEngine, StagingBuffer

    class _FakeClient:
        def __init__(self):
            self.prompts = []

            class _Chat:
                def __init__(self, outer):
                    self.completions = self
                    self.outer = outer

                async def create(self, **kwargs):
                    self.outer.prompts.append(kwargs["messages"][0]["content"])

                    class _Msg:
                        content = ""

                    class _Choice:
                        message = _Msg()

                    class _Resp:
                        choices = [_Choice()]

                    return _Resp()

            self.chat = _Chat(self)

    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")
    engine = ConsolidationEngine(store=store, max_source_tokens=700)
    staging = StagingBuffer(path=tmp_path / "staging.jsonl", session_id="session-1")
    staging.append("user", ("A" * 2500) + "FIRST_TAIL")
    staging.append("assistant", "reply one")
    staging.append("user", ("B" * 2500) + "SECOND_TAIL")
    staging.append("assistant", "reply two")

    client = _FakeClient()
    asyncio.run(
        engine.consolidate(
            [], client, "fake-model", api_format="openai", staging=staging
        )
    )

    assert len(client.prompts) >= 2
    assert any("FIRST_TAIL" in prompt for prompt in client.prompts)
    assert any("SECOND_TAIL" in prompt for prompt in client.prompts)
    assert staging.count() == 0


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


def test_route_categories_treats_conversation_history_as_episode_recall(tmp_path):
    from agent import LTMStore, ConsolidationEngine, LocalRetriever, ContextManager

    store = LTMStore(context_dir=tmp_path / "context")
    ctx_mgr = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
    )

    assert "episodes" in ctx_mgr._route_categories("对话历史 聊天内容")


def test_route_categories_does_not_treat_generic_history_queries_as_episode_recall(
    tmp_path,
):
    from agent import LTMStore, ConsolidationEngine, LocalRetriever, ContextManager

    store = LTMStore(context_dir=tmp_path / "context")
    ctx_mgr = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
    )

    assert "episodes" not in ctx_mgr._route_categories("git history")
    assert "episodes" not in ctx_mgr._route_categories("浏览器 history 怎么看")
    assert "episodes" not in ctx_mgr._route_categories("Python 历史是什么")


def test_retrieve_ltm_context_falls_back_to_recent_episodes_for_recall_queries(
    tmp_path,
):
    from agent import (
        LTMEntry,
        LTMStore,
        ConsolidationEngine,
        LocalRetriever,
        ContextManager,
    )

    store = LTMStore(context_dir=tmp_path / "context")
    store.add_entry(
        LTMEntry(
            id="episode-1",
            category="episodes",
            entity="session-1",
            memory_type="session_summary",
            content="USER: hi | USER: 你可以做什么 | USER: 写首诗",
            importance=0.7,
            created_at="2026-04-13 08:50 UTC",
            updated_at="2026-04-13 08:50 UTC",
        )
    )
    ctx_mgr = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
    )

    result = ctx_mgr.retrieve_ltm_context("我们刚刚才聊了什么", top_k=3)

    assert "episodes/session_1" in result
    assert "写首诗" in result


def test_retrieve_ltm_context_does_not_fallback_episode_for_generic_history_queries(
    tmp_path,
):
    from agent import (
        LTMEntry,
        LTMStore,
        ConsolidationEngine,
        LocalRetriever,
        ContextManager,
    )

    store = LTMStore(context_dir=tmp_path / "context")
    store.add_entry(
        LTMEntry(
            id="episode-1",
            category="episodes",
            entity="session-1",
            memory_type="session_summary",
            content="USER: hi | USER: 写首诗",
            importance=0.7,
            created_at="2026-04-13 08:50 UTC",
            updated_at="2026-04-13 08:50 UTC",
        )
    )
    ctx_mgr = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
    )

    result = ctx_mgr.retrieve_ltm_context("git history", top_k=3)

    assert result == ""


# ── Regression: per-turn consolidation bug ────────────────────────────────────


def test_should_enqueue_requires_min_messages_before_token_slow_path(tmp_path):
    """Slow-path (token) check must not fire with fewer than min_messages staged
    entries, even when a single verbose response would exceed the token threshold.
    This prevents per-turn consolidation firing on every CJK response.
    """
    ctx_mgr = make_ctx_manager(tmp_path)
    ctx_mgr.mark_activity()

    # Append only 2 entries (1 complete turn) — below min_messages default of 4.
    # Use a very large payload that would otherwise easily cross the token
    # slow-path threshold (8000 estimated tokens).
    long_response = "这" * 10000  # 10000 Chinese chars >> 8000 token threshold
    ctx_mgr.staging.append("user", "hello")
    ctx_mgr.staging.append("assistant", long_response)

    # count=2, min_messages=4: slow path must not fire.
    assert ctx_mgr.staging.count() == 2
    assert not ctx_mgr.should_enqueue_consolidation()


def test_should_enqueue_slow_path_fires_once_min_messages_reached(tmp_path):
    """Slow-path fires when staging has >= min_messages entries AND tokens exceed
    the threshold — even if the turn count is below staging_turn_threshold.
    """
    ctx_mgr = make_ctx_manager(tmp_path)
    ctx_mgr.mark_activity()

    long_response = "这" * 10000  # 10000 Chinese chars >> 8000 token threshold
    # Append 4 entries (2 complete turns) = min_messages.
    ctx_mgr.staging.append("user", "turn 1")
    ctx_mgr.staging.append("assistant", long_response)
    ctx_mgr.staging.append("user", "turn 2")
    ctx_mgr.staging.append("assistant", long_response)

    # count=4 == min_messages, tokens >> threshold, count < staging_turn_threshold(6)
    # → slow path should now fire.
    assert ctx_mgr.staging.count() == 4
    assert ctx_mgr.should_enqueue_consolidation()


def test_should_enqueue_fast_path_unchanged(tmp_path):
    """Fast path (turn count >= staging_turn_threshold) must still fire regardless
    of response length, unaffected by the min_messages guard.
    """
    ctx_mgr = make_ctx_manager(tmp_path)
    ctx_mgr.mark_activity()

    # 6 short entries (3 complete turns) = fast-path threshold, tiny responses.
    for i in range(6):
        ctx_mgr.staging.append("user", f"m{i}")

    assert ctx_mgr.should_enqueue_consolidation()


def test_consolidate_suppresses_messages_compressed_print_when_no_messages(
    tmp_path, capsys
):
    """consolidate() must not print 'Messages compressed: 0 → 0' when called
    with an empty messages list (the normal background-job path).
    """
    import asyncio
    from agent import ConsolidationEngine, LTMStore, StagingBuffer

    store = LTMStore(context_dir=tmp_path / "context")
    engine = ConsolidationEngine(store=store)
    staging = StagingBuffer(path=tmp_path / "staging.jsonl")
    staging.append("user", "hello")
    staging.append("assistant", "world")

    asyncio.run(
        engine.consolidate(
            messages=[],
            client=None,
            model="x",
            api_format="openai",
            staging=staging,
        )
    )

    captured = capsys.readouterr()
    assert "Messages compressed" not in captured.out


def test_consolidate_still_prints_messages_compressed_when_messages_present(
    tmp_path, capsys
):
    """consolidate() should still print 'Messages compressed' when actual
    working-memory messages are passed (the compaction-triggered path).
    """
    import asyncio
    from agent import ConsolidationEngine, LTMStore, StagingBuffer

    store = LTMStore(context_dir=tmp_path / "context")
    engine = ConsolidationEngine(store=store)
    staging = StagingBuffer(path=tmp_path / "staging.jsonl")

    messages = [{"role": "user", "content": f"msg {i}"} for i in range(8)]

    asyncio.run(
        engine.consolidate(
            messages=[],
            client=None,
            model="x",
            api_format="openai",
            staging=staging,
        )
    )

    captured = capsys.readouterr()
    assert "Messages compressed" not in captured.out


def test_consolidate_still_prints_messages_compressed_when_messages_present(
    tmp_path, capsys
):
    """consolidate() should still print 'Messages compressed' when actual
    working-memory messages are passed (the compaction-triggered path).
    """
    import asyncio
    from agent import ConsolidationEngine, LTMStore, StagingBuffer

    store = LTMStore(context_dir=tmp_path / "context")
    engine = ConsolidationEngine(store=store)
    staging = StagingBuffer(path=tmp_path / "staging.jsonl")

    messages = [{"role": "user", "content": f"msg {i}"} for i in range(8)]

    asyncio.run(
        engine.consolidate(
            messages=messages,
            client=None,
            model="x",
            api_format="openai",
            staging=staging,
        )
    )

    captured = capsys.readouterr()
    assert "Messages compressed" in captured.out
