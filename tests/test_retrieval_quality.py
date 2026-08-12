"""Retrieval quality is a regression surface, so it gets a test.

The eval set lives in `tests/eval/` and the runner in
`scripts/eval_retrieval.py`; this file is the automated guard that keeps a
ranking change from silently degrading recall. It asserts against the pinned
baseline rather than against absolute numbers, because the absolute numbers
are not good — they are simply what the system does today, and the point is
that they must not get worse without someone deciding they should.

Run `uv run python scripts/eval_retrieval.py --save-baseline` to re-pin after
an intentional change, and read the tag breakdown before you do.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval_retrieval import (  # noqa: E402
    BASELINE_PATH,
    DEFAULT_CASES,
    build_filler,
    evaluate,
)

# Metrics move in discrete jumps on a set this small (one case out of ~23 is
# ~0.043), so the tolerance admits noise from an unrelated change while still
# catching a real regression of one or more cases.
TOLERANCE = 0.05


@pytest.fixture(scope="module")
def report():
    return evaluate(DEFAULT_CASES)


@pytest.fixture(scope="module")
def baseline():
    if not BASELINE_PATH.exists():
        pytest.skip("no pinned baseline; run --save-baseline")
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def test_overall_metrics_do_not_regress(report, baseline):
    higher_is_better = ("candidate_recall", "recall@1", "recall@3", "recall@5", "mrr")
    failures = []
    for key in higher_is_better:
        now = report["overall"][key]
        was = baseline["overall"][key]
        if now < was - TOLERANCE:
            failures.append(f"{key}: {was:.3f} -> {now:.3f}")
    now_miss = report["overall"]["miss_rate"]
    was_miss = baseline["overall"]["miss_rate"]
    if now_miss > was_miss + TOLERANCE:
        failures.append(f"miss_rate: {was_miss:.3f} -> {now_miss:.3f} (higher is worse)")

    assert not failures, (
        "retrieval quality regressed against tests/eval/retrieval_baseline.json:\n  "
        + "\n  ".join(failures)
        + "\n\nRun `python scripts/eval_retrieval.py` to see which cases broke. "
        "If the change is intentional, re-pin with --save-baseline."
    )


def test_no_tag_regresses_badly(report, baseline):
    """Overall averages can hide one capability collapsing."""
    failures = []
    for tag, metrics in report["by_tag"].items():
        if tag == "negative" or "mrr" not in metrics:
            continue
        was = baseline.get("by_tag", {}).get(tag, {}).get("mrr")
        if was is None:
            continue
        if metrics["mrr"] < was - TOLERANCE:
            failures.append(f"{tag}: mrr {was:.3f} -> {metrics['mrr']:.3f}")
    assert not failures, "per-tag regression:\n  " + "\n  ".join(failures)


def test_lexical_retrieval_is_perfect(report):
    """The one capability that must never break.

    When the query and the memory share surface words, BM25 has no excuse.
    A drop here means the pipeline is broken, not that the corpus is hard.
    """
    lexical = report["by_tag"]["lexical"]
    assert lexical["mrr"] == pytest.approx(1.0), (
        "exact-word-match retrieval degraded — this is not a semantic-difficulty "
        "case, something in the pipeline is wrong"
    )


def test_candidate_recall_bounds_final_recall(report):
    """Stage 1 is a ceiling: ranking cannot retrieve what was never fetched."""
    assert report["overall"]["recall@5"] <= report["overall"]["candidate_recall"] + 1e-9


def test_eval_corpus_is_large_enough_to_exercise_prefiltering(report):
    """Guard the flaw that made the first version of this eval blind.

    With a corpus smaller than the FTS window (top_k * 6), every entry is a
    candidate, stage 1 never filters, and any bug that only appears when
    candidates << corpus scores identically to its own fix.
    """
    assert report["corpus_size"] > 5 * 6 * 3


def test_filler_is_deterministic():
    """Baselines are only comparable if the corpus is byte-identical."""
    assert build_filler(50) == build_filler(50)
    assert build_filler(50)[0]["id"] == "filler-00000"


def test_filler_never_answers_a_labeled_case(report):
    """Filler must be a distractor, never accidentally the correct answer."""
    labeled_ids = {r for case in report["results"] for r in case["relevant"]}
    assert not any(entry_id.startswith("filler-") for entry_id in labeled_ids)


# ── Multi-query retrieval ──────────────────────────────────────────────────
#
# The dominant retrieval failure is not ranking. It is that a single
# unmodified user message is a poor query against a lexical index: measured
# here, 7 of 7 stage-1 misses were recoverable by re-asking in the memory's
# own wording or language. Hence `queries` is plural.


@pytest.fixture(scope="module")
def multi_report():
    return evaluate(DEFAULT_CASES, multi_query=True)


def test_reformulation_lifts_the_stage_one_ceiling(report, multi_report):
    """Stage 1 was the ceiling; re-asking removes it.

    This is the measurement behind making `context_retrieve` take a list.
    Note it is an upper bound: the reformulations are authored, so it shows
    what the pull model can reach, not what a given model will produce.
    """
    assert report["overall"]["candidate_recall"] < 0.80
    assert multi_report["overall"]["candidate_recall"] > 0.95
    assert multi_report["overall"]["miss_rate"] < report["overall"]["miss_rate"]


def test_reformulation_fixes_the_semantic_tags_specifically(multi_report):
    """cross-lingual and paraphrase are where lexical search fails alone."""
    for tag in ("cross-lingual", "paraphrase"):
        assert multi_report["by_tag"][tag]["mrr"] > 0.9, tag


def _tools_with_memory(tmp_path):
    """BuiltinTools wired to a real ContextManager, as production has it."""
    from agent import BuiltinTools, LTMStore, MemoryPalace, ToolRegistry
    from agent.memory.consolidation import ConsolidationEngine
    from agent.memory.context import ContextManager
    from agent.memory.retrieval import LocalRetriever
    from agent.memory.staging import StagingBuffer

    store = LTMStore(context_dir=tmp_path / "context")
    manager = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        staging=StagingBuffer(session_id="t", context_dir=tmp_path / "context"),
    )
    memory = MemoryPalace(
        base_dir=tmp_path / "memory", context_dir=tmp_path / "context"
    )
    return BuiltinTools(
        memory=memory, registry=ToolRegistry(), context_manager=manager
    )


def test_a_bare_string_is_one_query_not_a_bag_of_characters(tmp_path):
    """A model filling an array parameter with a string is routine.

    Iterating a str yields characters, silently turning one good query into
    dozens of one-character queries that match nearly everything.
    """
    tools = _tools_with_memory(tmp_path)

    result = tools._context_retrieve(queries="kubernetes deployment")
    assert result["queries"] == ["kubernetes deployment"]

    result = tools._context_retrieve(queries=["a", "b"], query="c")
    assert result["queries"] == ["a", "b", "c"]


def test_empty_search_tells_the_model_to_reformulate(tmp_path):
    """An empty result is more often a bad query than an absent memory.

    Without saying so, the model concludes "no record" and tells the user.
    """
    tools = _tools_with_memory(tmp_path)

    result = tools._context_retrieve(queries=["zzzz nonexistent qqqq"])
    assert result["count"] == 0
    assert "lexical" in result["hint"]


def test_missing_queries_is_an_explicit_error(tmp_path):
    tools = _tools_with_memory(tmp_path)

    result = tools._context_retrieve()
    assert result["ok"] is False


def test_multi_query_keeps_each_entry_best_score(tmp_path):
    """Union with max-per-entry, not concatenation.

    Concatenating phrasings dilutes IDF across terms belonging to different
    formulations and can rank worse than any single phrasing alone.
    """
    from agent import LTMEntry, LTMStore
    from agent.memory.consolidation import ConsolidationEngine
    from agent.memory.context import ContextManager
    from agent.memory.retrieval import LocalRetriever
    from agent.memory.staging import StagingBuffer

    store = LTMStore(context_dir=tmp_path / "context")
    store.add_entries(
        [
            LTMEntry("zh", "用户在大疆做 iOS 开发", 0.5, "identity",
                     created_at="2026-01-01", updated_at="2026-01-01"),
            LTMEntry("en", "The user works on camera firmware", 0.5, "identity",
                     created_at="2026-01-01", updated_at="2026-01-01"),
        ]
    )
    manager = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        staging=StagingBuffer(session_id="t", context_dir=tmp_path / "context"),
    )

    # Neither phrasing alone finds both; together they do.
    assert {e.id for e in manager.rank_ltm_entries("大疆")} == {"zh"}
    assert {e.id for e in manager.rank_ltm_entries("camera firmware")} == {"en"}
    both = {e.id for e in manager.rank_ltm_entries(["大疆", "camera firmware"])}
    assert both == {"zh", "en"}
