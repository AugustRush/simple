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
