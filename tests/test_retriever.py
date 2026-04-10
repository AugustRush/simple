"""Tests for LocalRetriever — BM25-lite scoring."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def make_entries():
    from agent import LTMEntry

    return [
        LTMEntry(
            "1", "Python async await coroutine patterns", 0.9, "code", "now", "now"
        ),
        LTMEntry(
            "2",
            "User prefers concise responses without emojis",
            0.8,
            "prefs",
            "now",
            "now",
        ),
        LTMEntry(
            "3", "Database connection pooling with asyncpg", 0.7, "code", "now", "now"
        ),
        LTMEntry(
            "4", "Project deadline is end of April 2026", 0.6, "tasks", "now", "now"
        ),
        LTMEntry(
            "5", "Machine learning model training pipeline", 0.5, "code", "now", "now"
        ),
    ]


def test_tokenize():
    from agent import LocalRetriever

    r = LocalRetriever()
    tokens = r.tokenize("Hello World async patterns")
    assert "hello" in tokens
    assert "world" in tokens
    assert "async" in tokens
    assert "patterns" in tokens


def test_tokenize_chinese():
    from agent import LocalRetriever

    r = LocalRetriever()
    tokens = r.tokenize("Python 异步模式")
    assert "python" in tokens
    assert "异步模式" in tokens or any("异" in t for t in tokens)


def test_retrieve_top_k():
    from agent import LocalRetriever, LTMEntry

    r = LocalRetriever()
    entries = [
        LTMEntry(
            "1", "Python async await coroutine patterns", 0.9, "code", "now", "now"
        ),
        LTMEntry(
            "2",
            "Async Python generator expressions and iteration",
            0.8,
            "code",
            "now",
            "now",
        ),
        LTMEntry(
            "3", "Database connection pooling with psycopg2", 0.7, "code", "now", "now"
        ),
    ]
    result = r.retrieve("async python patterns", entries, top_k=2)
    assert len(result) == 2
    ids = [e.id for e in result]
    assert "1" in ids  # highest importance + most term matches


def test_retrieve_returns_empty_for_no_match():
    from agent import LocalRetriever

    r = LocalRetriever()
    entries = make_entries()
    result = r.retrieve("xyzzy foobar nonsense123", entries, top_k=3)
    assert len(result) == 0


def test_importance_boosts_ranking():
    from agent import LocalRetriever, LTMEntry

    r = LocalRetriever()
    entries = [
        LTMEntry("low", "python code function", 0.1, "c", "now", "now"),
        LTMEntry("high", "python code function", 0.9, "c", "now", "now"),
    ]
    result = r.retrieve("python code", entries, top_k=2)
    assert result[0].id == "high"


def test_retrieve_respects_top_k():
    from agent import LocalRetriever

    r = LocalRetriever()
    entries = make_entries()
    result = r.retrieve("the", entries, top_k=2)
    assert len(result) <= 2


def test_score_empty_entries():
    from agent import LocalRetriever

    r = LocalRetriever()
    scored = r.score("query", [])
    assert scored == []


def test_retrieve_empty_entries():
    from agent import LocalRetriever

    r = LocalRetriever()
    result = r.retrieve("python", [], top_k=5)
    assert result == []
