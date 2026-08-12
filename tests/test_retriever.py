"""Tests for LocalRetriever — BM25-lite scoring."""

import pytest


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


def test_importance_does_not_influence_relevance_ranking():
    """Ranking is relevance only; importance is a retention prior.

    This test previously asserted the opposite. The multiplier was removed
    after measuring it: on the eval set, ablating it improved MRR
    0.587 -> 0.609, and a permutation test over 120 random reassignments of
    the same values put the real assignment at the 3rd percentile (p ~ 0.97
    that a random shuffle ranks at least as well). Importance anti-correlates
    with document length, and BM25 already applies a length prior, so the
    multiplier double-counted it.
    """
    from agent import LocalRetriever, LTMEntry

    r = LocalRetriever()
    entries = [
        LTMEntry("low", "python code function", 0.1, "c", "now", "now"),
        LTMEntry("high", "python code function", 0.9, "c", "now", "now"),
    ]
    scored = r.score("python code", entries)
    by_id = {entry.id: score for entry, score in scored}

    assert by_id["low"] == by_id["high"], (
        "equally relevant entries must score equally regardless of importance"
    )


def test_importance_still_orders_results_when_there_is_no_query():
    """With no relevance signal, the retention prior is all there is."""
    from agent import LocalRetriever, LTMEntry

    r = LocalRetriever()
    entries = [
        LTMEntry("low", "some memory", 0.1, "c", "now", "now"),
        LTMEntry("high", "another memory", 0.9, "c", "now", "now"),
    ]
    ranked = r.retrieve("", entries, top_k=2)
    assert [e.id for e in ranked] == ["high", "low"]


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


# ── Corpus-level IDF ───────────────────────────────────────────────────────
#
# The two-stage retrieval in `retrieve_ltm_context` feeds this scorer the
# output of an FTS query.  Those candidates all matched the query, so
# measuring IDF across them makes the query terms look maximally common and
# scores them ~0 — the re-ranker was ignoring the words the user asked about.


def _entry(entry_id, content, importance=0.5):
    from agent import LTMEntry

    return LTMEntry(entry_id, content, importance, "notes", "now", "now")


def test_candidate_local_idf_collapses_the_query_term():
    """Demonstrates the failure mode this fix addresses."""
    from agent.memory.retrieval import LocalRetriever

    # Every candidate matched "kubernetes" — which is exactly what an FTS
    # prefilter returns for that query.
    candidates = [_entry(str(i), f"kubernetes note number {i}") for i in range(20)]
    retriever = LocalRetriever()

    scored = retriever.score("kubernetes", candidates)

    # df == N, so IDF ≈ log(1 + 0.5/20.5) ≈ 0.024: effectively no signal.
    assert all(score < 0.2 for _entry_obj, score in scored)


def test_corpus_idf_restores_the_query_term_signal():
    from agent.memory.retrieval import CorpusStats, LocalRetriever

    candidates = [_entry(str(i), f"kubernetes note number {i}") for i in range(20)]
    retriever = LocalRetriever()

    # In the real store those 20 are rare: 20 of 10,000 documents.
    corpus = CorpusStats(
        total_documents=10_000, document_frequencies={"kubernetes": 20}
    )
    scored = retriever.score("kubernetes", candidates, corpus)

    assert all(score > 1.0 for _entry_obj, score in scored)


def test_corpus_idf_ranks_the_rarer_term_higher():
    """The point of IDF: a rare match should beat a ubiquitous one."""
    from agent.memory.retrieval import CorpusStats, LocalRetriever

    rare = _entry("rare", "deployment uses kubernetes")
    common = _entry("common", "deployment uses the thing")
    corpus = CorpusStats(
        total_documents=10_000,
        document_frequencies={"kubernetes": 5, "deployment": 9_000},
    )

    scored = LocalRetriever().score("kubernetes deployment", [rare, common], corpus)

    assert scored[0][0].id == "rare"


def test_score_without_corpus_still_ranks_within_the_set():
    """The fallback stays usable for standalone scoring over arbitrary lists."""
    from agent.memory.retrieval import LocalRetriever

    entries = [
        _entry("hit", "async await coroutine"),
        _entry("miss", "unrelated content here"),
    ]
    scored = LocalRetriever().score("coroutine", entries)

    assert scored[0][0].id == "hit"
    assert scored[1][1] == 0.0


def test_corpus_stats_idf_falls_back_when_corpus_is_empty():
    from agent.memory.retrieval import CorpusStats

    stats = CorpusStats(total_documents=0, document_frequencies={})
    assert stats.idf("anything", fallback_n=10) > 0
