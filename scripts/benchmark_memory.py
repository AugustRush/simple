#!/usr/bin/env python3
"""Benchmark memory search and write latency for the local LTM store."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import LTMEntry, LTMStore  # noqa: E402


def _build_entry(index: int, *, category: str = "identity", entity: str = "user") -> LTMEntry:
    return LTMEntry(
        id=f"entry-{index}",
        category=category,
        entity=entity,
        memory_type="preference",
        content=f"Prefers concise responses and stable benchmarks {index}",
        importance=0.5 + ((index % 5) * 0.1),
        status="active",
        created_at="2026-04-13",
        updated_at="2026-04-13",
    )


def _measure_ms(fn) -> float:
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000.0


def _summarize(samples_ms: list[float]) -> dict[str, float | int]:
    return {
        "runs": len(samples_ms),
        "avg_ms": round(statistics.mean(samples_ms), 3),
        "min_ms": round(min(samples_ms), 3),
        "max_ms": round(max(samples_ms), 3),
        "p50_ms": round(statistics.median(samples_ms), 3),
        "total_ms": round(sum(samples_ms), 3),
    }


def _seed_store(store: LTMStore, count: int) -> None:
    if count <= 0:
        return
    entries = [_build_entry(idx) for idx in range(count)]
    affected_categories: set[str] = set()
    memory_rows = []
    fts_rows = []
    for entry in entries:
        category = store.normalize_category_name(entry.category)
        entity = store._normalize_entity(entry.entity, category)
        affected_categories.add(category)
        memory_rows.append(
            (
                entry.id,
                entry.content,
                float(entry.importance),
                category,
                entity,
                entry.memory_type or "fact",
                entry.scope or "global",
                entry.status or "active",
                entry.source_session or "",
                float(entry.confidence or 1.0),
                entry.created_at,
                entry.updated_at,
            )
        )
        fts_rows.append((entry.id, entry.content, entity, category))
    with store._connect() as conn:
        conn.executemany(
            """
            INSERT INTO memory_items (
                id, content, importance, category, entity, memory_type, scope,
                status, source_session, confidence, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            memory_rows,
        )
        conn.executemany(
            """
            INSERT INTO memory_items_fts (memory_id, content, entity, category)
            VALUES (?, ?, ?, ?)
            """,
            fts_rows,
        )
    store._sync_after_mutation(affected_categories)


def benchmark_search(entry_count: int, runs: int, query: str = "concise responses") -> dict:
    with tempfile.TemporaryDirectory(prefix="memory-bench-search-") as tmp:
        base = Path(tmp)
        store = LTMStore(
            context_dir=base / "context",
            memory_dir=base / "memory",
        )
        _seed_store(store, entry_count)
        samples_ms = [
            _measure_ms(lambda: store.search_entries(query, limit=5))
            for _ in range(runs)
        ]
    return _summarize(samples_ms)


def benchmark_write(entry_count: int, runs: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="memory-bench-write-") as tmp:
        base = Path(tmp)
        store = LTMStore(
            context_dir=base / "context",
            memory_dir=base / "memory",
        )
        _seed_store(store, entry_count)
        next_index = entry_count
        samples_ms: list[float] = []
        for _ in range(runs):
            entry = _build_entry(next_index, category="tasks", entity=f"task_{next_index}")
            samples_ms.append(_measure_ms(lambda e=entry: store.add_entry(e)))
            next_index += 1
    return _summarize(samples_ms)


def run_benchmarks(
    sizes: list[int],
    search_runs: int,
    write_runs: int,
    query: str = "concise responses",
) -> dict:
    return {
        "query": query,
        "sizes": [
            {
                "entries": size,
                "search": benchmark_search(size, search_runs, query=query),
                "write": benchmark_write(size, write_runs),
            }
            for size in sizes
        ],
    }


def _flatten_rows(payload: dict) -> list[dict[str, int | float | str]]:
    rows: list[dict[str, int | float | str]] = []
    for size_result in payload.get("sizes", []):
        entries = int(size_result["entries"])
        for metric_name in ("search", "write"):
            metric = size_result[metric_name]
            rows.append(
                {
                    "entries": entries,
                    "metric": metric_name,
                    "runs": int(metric["runs"]),
                    "avg_ms": metric["avg_ms"],
                    "min_ms": metric["min_ms"],
                    "max_ms": metric["max_ms"],
                    "p50_ms": metric["p50_ms"],
                    "total_ms": metric["total_ms"],
                }
            )
    return rows


def _write_output(payload: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = output_path.suffix.lower()
    if suffix == ".json":
        output_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return
    if suffix == ".jsonl":
        with output_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return
    if suffix == ".csv":
        rows = _flatten_rows(payload)
        fieldnames = [
            "entries",
            "metric",
            "runs",
            "avg_ms",
            "min_ms",
            "max_ms",
            "p50_ms",
            "total_ms",
        ]
        with output_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return
    raise ValueError("Unsupported output format. Use .json, .jsonl, or .csv")


def _load_payload(path: Path) -> dict:
    suffix = path.suffix.lower()
    raw = path.read_text(encoding="utf-8")
    if suffix == ".jsonl":
        lines = [line for line in raw.splitlines() if line.strip()]
        if not lines:
            raise ValueError(f"No benchmark payloads found in {path}")
        return json.loads(lines[-1])
    return json.loads(raw)


def compare_payloads(current: dict, baseline: dict) -> dict:
    baseline_by_size = {
        int(item["entries"]): item
        for item in baseline.get("sizes", [])
    }
    comparison_sizes = []
    for current_item in current.get("sizes", []):
        entries = int(current_item["entries"])
        baseline_item = baseline_by_size.get(entries)
        if baseline_item is None:
            continue
        metrics: dict[str, dict[str, float | int]] = {"entries": entries}  # type: ignore[assignment]
        comparison_row: dict[str, object] = {"entries": entries}
        for metric_name in ("search", "write"):
            current_metric = current_item[metric_name]
            baseline_metric = baseline_item[metric_name]
            current_avg = float(current_metric["avg_ms"])
            baseline_avg = float(baseline_metric["avg_ms"])
            comparison_row[metric_name] = {
                "current_avg_ms": round(current_avg, 3),
                "baseline_avg_ms": round(baseline_avg, 3),
                "delta_avg_ms": round(current_avg - baseline_avg, 3),
                "ratio_vs_baseline": round(current_avg / baseline_avg, 3)
                if baseline_avg
                else None,
            }
        comparison_sizes.append(comparison_row)
    return {
        "baseline": str(baseline.get("source", "")) if baseline.get("source") else None,
        "sizes": comparison_sizes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=[1000, 10000],
        help="Dataset sizes to benchmark.",
    )
    parser.add_argument(
        "--search-runs",
        type=int,
        default=10,
        help="Number of repeated search measurements per dataset size.",
    )
    parser.add_argument(
        "--write-runs",
        type=int,
        default=10,
        help="Number of repeated write measurements per dataset size.",
    )
    parser.add_argument(
        "--query",
        type=str,
        default="concise responses",
        help="Search query used for retrieval measurements.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional output path. Supported suffixes: .json, .jsonl, .csv",
    )
    parser.add_argument(
        "--compare",
        type=Path,
        help="Optional previous benchmark result (.json or .jsonl) to compare against.",
    )
    args = parser.parse_args(argv)

    payload = run_benchmarks(
        sizes=args.sizes,
        search_runs=args.search_runs,
        write_runs=args.write_runs,
        query=args.query,
    )
    if args.compare:
        baseline = _load_payload(args.compare)
        baseline["source"] = str(args.compare)
        payload["comparison"] = compare_payloads(payload, baseline)
    if args.output:
        _write_output(payload, args.output)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
