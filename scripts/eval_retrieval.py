#!/usr/bin/env python3
"""Measure long-term-memory retrieval quality.

Why this exists
---------------
The repo had a latency benchmark (`benchmark_memory.py`) and a behavioural
test suite, but nothing that measured whether the agent *remembers the right
thing*.  That is the system's core value, and it was the one axis with no
instrument: a change to the ranking function could be shown to be faster and
still pass every test while silently recalling worse.

This runner drives the production path — `ContextManager.rank_ltm_entries`,
the same call the agent makes on every turn — against a labeled set, and
reports recall@k and MRR broken down by capability tag, so a regression can
be attributed rather than merely noticed.

Usage
-----
    uv run python scripts/eval_retrieval.py                  # run + print
    uv run python scripts/eval_retrieval.py --save-baseline  # pin current
    uv run python scripts/eval_retrieval.py --compare        # diff vs baseline
    uv run python scripts/eval_retrieval.py --harvest        # build a local
                                                             # set from your
                                                             # real store
    uv run python scripts/eval_retrieval.py --cases PATH     # custom set
    uv run python scripts/eval_retrieval.py --verbose        # per-case detail

Reading the numbers
-------------------
The set is small (tens of cases), so treat single-point differences as noise
and look at the tag breakdown.  `cross-lingual` and `paraphrase` are where a
purely lexical retriever is expected to struggle; if those are near zero, the
honest conclusion is that lexical matching is not sufficient for this corpus,
not that the ranking constants need another tweak.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import LTMEntry, LTMStore  # noqa: E402
from agent.memory.consolidation import ConsolidationEngine  # noqa: E402
from agent.memory.context import ContextManager  # noqa: E402
from agent.memory.retrieval import LocalRetriever  # noqa: E402
from agent.memory.staging import StagingBuffer  # noqa: E402

DEFAULT_CASES = ROOT / "tests" / "eval" / "retrieval_cases.json"
BASELINE_PATH = ROOT / "tests" / "eval" / "retrieval_baseline.json"
LOCAL_CASES = ROOT / "tests" / "eval" / "retrieval_cases.local.json"
CUTOFFS = (1, 3, 5)
#: Filler entries added so the FTS stage actually prefilters. Fixed by
#: default so baselines stay comparable; --pad 0 gives the unpadded set.
DEFAULT_PAD = 500


# ── Filler corpus ──────────────────────────────────────────────────────────
#
# A labeled set is necessarily small — every entry costs hand-judgement.  But
# evaluating on 23 documents does not measure retrieval, it measures ranking
# of everything: the FTS stage fetches top_k*6 candidates, so when the corpus
# is smaller than that window nothing is ever filtered, and any bug that only
# appears when candidates << corpus is invisible.
#
# That is not hypothetical.  The candidate-local-IDF bug fixed earlier scores
# *identically* to the fix on an unpadded set, for exactly this reason.  A
# real store has hundreds to thousands of entries, so the eval pads with
# deterministic filler that shares the corpus vocabulary — genuine
# distractors, never the right answer for any case.

_FILLER_SUBJECTS = (
    "缓存", "日志", "配置", "超时", "重试", "队列", "索引", "会话",
    "cache", "logging", "config", "timeout", "retry", "queue", "index",
)
_FILLER_ACTIONS = (
    "调整了参数", "补了单测", "改了默认值", "加了注释", "重命名了变量",
    "tuned the parameters", "added a test", "renamed a helper",
    "adjusted the default", "clarified a comment",
)
_FILLER_OBJECTS = (
    "在网关路径上", "在调度器里", "在传输层", "在插件加载器里",
    "in the gateway path", "in the scheduler", "in the transport layer",
)


def build_filler(count: int, seed: int = 20260812) -> list[dict[str, Any]]:
    """Deterministic distractor entries; same set on every run."""
    import random

    rng = random.Random(seed)
    filler: list[dict[str, Any]] = []
    for i in range(count):
        subject = rng.choice(_FILLER_SUBJECTS)
        action = rng.choice(_FILLER_ACTIONS)
        obj = rng.choice(_FILLER_OBJECTS)
        filler.append(
            {
                "id": f"filler-{i:05d}",
                "category": rng.choice(["episodes", "concepts", "tasks"]),
                "entity": "session",
                "memory_type": "session_summary",
                "importance": round(rng.uniform(0.2, 0.6), 2),
                "content": f"第 {i} 次维护记录：{action}{obj}，涉及{subject}。"
                f" Routine maintenance note {i} touching {subject}.",
            }
        )
    return filler


# ── Harness ────────────────────────────────────────────────────────────────


def build_context_manager(corpus: list[dict[str, Any]], tmp_dir: Path):
    """Load *corpus* into a throwaway store wired like the real one."""
    store = LTMStore(context_dir=tmp_dir / "context")
    store.add_entries(
        [
            LTMEntry(
                id=item["id"],
                content=item["content"],
                importance=float(item.get("importance", 0.5)),
                category=item.get("category", "concepts"),
                entity=item.get("entity", ""),
                memory_type=item.get("memory_type", "note"),
                scope=item.get("scope", "global"),
                created_at="2026-01-01",
                updated_at="2026-01-01",
            )
            for item in corpus
        ]
    )
    staging = StagingBuffer(
        session_id="eval", context_dir=tmp_dir / "context"
    )
    return ContextManager(
        store=store,
        staging=staging,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
    )


def evaluate(cases_path: Path, *, pad: int = DEFAULT_PAD) -> dict[str, Any]:
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    labeled = payload["corpus"]
    cases = payload["cases"]
    corpus = labeled + build_filler(pad)

    with tempfile.TemporaryDirectory() as raw_tmp:
        manager = build_context_manager(corpus, Path(raw_tmp))
        results = [_run_case(manager, case) for case in cases]

    return {
        "cases_file": str(cases_path.relative_to(ROOT)),
        "corpus_size": len(corpus),
        "labeled_size": len(labeled),
        "filler_size": pad,
        "case_count": len(cases),
        "overall": _aggregate([r for r in results if not r["is_negative"]]),
        "negative": _negative_metrics([r for r in results if r["is_negative"]]),
        "by_tag": _by_tag(results),
        "results": results,
    }


def _run_case(manager: ContextManager, case: dict[str, Any]) -> dict[str, Any]:
    query = case["query"]
    relevant = set(case.get("relevant", []))
    # The production call.  Ask for more than the largest cutoff so recall@5
    # is not truncated by the default top_k.
    ranked = manager.rank_ltm_entries(query, top_k=max(CUTOFFS))
    retrieved = [entry.id for entry in ranked]
    # Stage-1 ceiling: an entry the candidate fetch misses can never be
    # recovered by ranking, so a miss here and a miss in the ranking are
    # different bugs with different fixes.
    candidate_ids = {e.id for e in manager.ltm_candidates(query, top_k=max(CUTOFFS))}
    reachable = bool(relevant) and bool(relevant & candidate_ids)

    hits = {k: len(relevant & set(retrieved[:k])) for k in CUTOFFS}
    recall = {
        k: (hits[k] / len(relevant)) if relevant else 0.0 for k in CUTOFFS
    }
    reciprocal_rank = 0.0
    for position, entry_id in enumerate(retrieved, start=1):
        if entry_id in relevant:
            reciprocal_rank = 1.0 / position
            break

    return {
        "query": query,
        "tags": case.get("tags", []),
        "is_negative": not relevant,
        "relevant": sorted(relevant),
        "retrieved": retrieved,
        "recall": recall,
        "reciprocal_rank": reciprocal_rank,
        "reachable": reachable,
        "candidate_count": len(candidate_ids),
        "first_hit_rank": (
            next(
                (i for i, e in enumerate(retrieved, 1) if e in relevant),
                None,
            )
        ),
    }


def _aggregate(results: list[dict[str, Any]]) -> dict[str, float]:
    if not results:
        return {}
    metrics = {
        f"recall@{k}": statistics.mean(r["recall"][k] for r in results)
        for k in CUTOFFS
    }
    metrics["mrr"] = statistics.mean(r["reciprocal_rank"] for r in results)
    metrics["candidate_recall"] = statistics.mean(
        1.0 if r["reachable"] else 0.0 for r in results
    )
    metrics["miss_rate"] = statistics.mean(
        1.0 for r in results if r["first_hit_rank"] is None
    ) * (
        sum(1 for r in results if r["first_hit_rank"] is None) / len(results)
    ) if any(r["first_hit_rank"] is None for r in results) else 0.0
    # Simpler and honest: fraction of cases where nothing relevant was found.
    metrics["miss_rate"] = sum(
        1 for r in results if r["first_hit_rank"] is None
    ) / len(results)
    return metrics


def _negative_metrics(results: list[dict[str, Any]]) -> dict[str, float]:
    """For queries with no correct answer, retrieving nothing is the goal."""
    if not results:
        return {}
    return {
        "count": len(results),
        "false_positive_rate": sum(1 for r in results if r["retrieved"])
        / len(results),
        "avg_spurious_results": statistics.mean(
            len(r["retrieved"]) for r in results
        ),
    }


def _by_tag(results: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    tags = sorted({tag for r in results for tag in r["tags"]})
    out: dict[str, dict[str, float]] = {}
    for tag in tags:
        tagged = [r for r in results if tag in r["tags"]]
        positives = [r for r in tagged if not r["is_negative"]]
        if tag == "negative":
            out[tag] = _negative_metrics(tagged)
        elif positives:
            out[tag] = {"count": len(positives), **_aggregate(positives)}
    return out


# ── Reporting ──────────────────────────────────────────────────────────────


def _fmt(metrics: dict[str, float]) -> str:
    order = (
        "candidate_recall", "recall@1", "recall@3", "recall@5", "mrr", "miss_rate",
    )
    return "  ".join(
        f"{key}={metrics[key]:.3f}" for key in order if key in metrics
    )


def print_report(report: dict[str, Any], *, verbose: bool = False) -> None:
    print(
        f"\ncorpus={report['corpus_size']} entries "
        f"({report['labeled_size']} labeled + {report['filler_size']} filler)   "
        f"cases={report['case_count']}   ({report['cases_file']})\n"
    )
    print(f"OVERALL   {_fmt(report['overall'])}")
    negative = report.get("negative") or {}
    if negative:
        print(
            f"NEGATIVE  n={negative['count']}  "
            f"false_positive_rate={negative['false_positive_rate']:.3f}  "
            f"avg_spurious={negative['avg_spurious_results']:.1f}"
        )
    print("\nBY TAG")
    for tag, metrics in sorted(report["by_tag"].items()):
        if tag == "negative":
            continue
        print(f"  {tag:14} n={metrics['count']:<3} {_fmt(metrics)}")

    misses = [r for r in report["results"] if not r["is_negative"] and r["first_hit_rank"] is None]
    if misses:
        stage1 = [r for r in misses if not r["reachable"]]
        stage2 = [r for r in misses if r["reachable"]]
        print(
            f"\nCOMPLETE MISSES ({len(misses)})   "
            f"stage-1 recall failures: {len(stage1)}   "
            f"stage-2 ranking failures: {len(stage2)}"
        )
        print("  (stage-1 = the candidate fetch never returned the answer, so no")
        print("   amount of re-ranking could have found it)")
        for label, group in (("STAGE-1", stage1), ("STAGE-2", stage2)):
            for r in group:
                print(f"  {label}  [{','.join(r['tags']):28}] {r['query']}")
                print(
                    f"      expected {r['relevant']}  got {r['retrieved'][:3]}"
                    f"  (candidates={r['candidate_count']})"
                )

    if verbose:
        print("\nPER CASE")
        for r in report["results"]:
            rank = r["first_hit_rank"]
            mark = "—" if r["is_negative"] else (f"#{rank}" if rank else "MISS")
            print(f"  {mark:>5}  {r['query']}")


def compare(current: dict[str, Any], baseline: dict[str, Any]) -> int:
    """Print a metric-by-metric diff. Returns 1 when anything regressed."""
    print("\nCOMPARISON vs baseline")
    regressed = False
    for key in (
        "candidate_recall", "recall@1", "recall@3", "recall@5", "mrr", "miss_rate",
    ):
        now = current["overall"].get(key)
        was = baseline.get("overall", {}).get(key)
        if now is None or was is None:
            continue
        delta = now - was
        # miss_rate is the one metric where lower is better.
        worse = delta > 1e-9 if key == "miss_rate" else delta < -1e-9
        arrow = "→" if abs(delta) < 1e-9 else ("↓" if delta < 0 else "↑")
        flag = "  REGRESSED" if worse else ""
        regressed = regressed or worse
        print(f"  {key:12} {was:.3f} {arrow} {now:.3f}  ({delta:+.3f}){flag}")

    for tag in sorted(current["by_tag"]):
        now = current["by_tag"][tag].get("mrr")
        was = baseline.get("by_tag", {}).get(tag, {}).get("mrr")
        if now is None or was is None:
            continue
        delta = now - was
        if abs(delta) >= 0.05:
            print(f"    tag {tag:14} mrr {was:.3f} → {now:.3f} ({delta:+.3f})")
    return 1 if regressed else 0


# ── Harvest from a real store ──────────────────────────────────────────────


def harvest(db_path: Path, out_path: Path) -> None:
    """Write a local eval set seeded from a real palace.db.

    The corpus is copied verbatim, so the output contains personal content and
    is gitignored.  Cases are left empty on purpose: ground truth is a
    judgement about what *should* have been recalled, and only the person who
    lived those sessions can supply it.  Fill in `relevant` by hand.
    """
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, content, importance, category, entity, memory_type, scope
        FROM memory_items
        WHERE status NOT IN ('archived', 'superseded')
        """
    ).fetchall()
    conn.close()

    payload = {
        "_readme": [
            "Harvested from a real store — contains personal content, gitignored.",
            "`relevant` lists are intentionally empty: fill them in by hand with",
            "the entry ids each query SHOULD return, then rerun with --cases.",
        ],
        "corpus": [dict(row) for row in rows],
        "cases": [
            {"query": "<your question here>", "relevant": [], "tags": ["fact"]}
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"harvested {len(rows)} entries -> {out_path}")
    print("Now fill in the `cases` list with real questions and their answers.")


# ── Entry point ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument(
        "--pad",
        type=int,
        default=DEFAULT_PAD,
        help="deterministic filler entries so the FTS stage prefilters "
        f"(default {DEFAULT_PAD}; 0 disables)",
    )
    parser.add_argument("--save-baseline", action="store_true")
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit raw JSON")
    parser.add_argument(
        "--harvest",
        nargs="?",
        const=str(Path.home() / ".agent" / "context" / "palace.db"),
        help="seed a local eval set from a real palace.db",
    )
    args = parser.parse_args(argv)

    if args.harvest:
        harvest(Path(args.harvest), LOCAL_CASES)
        return 0

    report = evaluate(args.cases, pad=args.pad)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print_report(report, verbose=args.verbose)

    if args.save_baseline:
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(
            json.dumps(
                {k: report[k] for k in ("overall", "negative", "by_tag")},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nbaseline written to {BASELINE_PATH.relative_to(ROOT)}")
        return 0

    if args.compare:
        if not BASELINE_PATH.exists():
            print("\nno baseline yet — run with --save-baseline first")
            return 1
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        return compare(report, baseline)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
