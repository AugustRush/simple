"""Tests for ConsolidationEngine — sleep/decay/parse."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def make_engine(tmp_path):
    from agent import LTMStore, ConsolidationEngine

    store = LTMStore(context_dir=tmp_path / "context")
    return ConsolidationEngine(store=store)


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
    assert entries[0].category == "code_context"
    assert entries[1].importance == pytest.approx(0.7)
    assert entries[2].content == "Fix the auth bug"


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
