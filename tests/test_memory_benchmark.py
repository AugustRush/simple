import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "benchmark_memory.py"


def test_benchmark_script_exists():
    assert SCRIPT.exists()


def test_run_benchmarks_returns_metrics_for_requested_sizes():
    from scripts.benchmark_memory import run_benchmarks

    results = run_benchmarks(sizes=[10], search_runs=2, write_runs=2)

    assert results["sizes"][0]["entries"] == 10
    assert results["sizes"][0]["search"]["runs"] == 2
    assert results["sizes"][0]["write"]["runs"] == 2
    assert results["sizes"][0]["search"]["avg_ms"] >= 0
    assert results["sizes"][0]["write"]["avg_ms"] >= 0


def test_seed_store_bypasses_per_entry_add_entry(monkeypatch, tmp_path):
    from agent import LTMStore
    from scripts.benchmark_memory import _seed_store

    store = LTMStore(
        context_dir=tmp_path / "context",
        memory_dir=tmp_path / "memory",
    )

    def fail_add_entry(entry):
        raise AssertionError("_seed_store should not call add_entry for every seed row")

    monkeypatch.setattr(store, "add_entry", fail_add_entry)

    _seed_store(store, 5)

    assert len(store.all_entries()) == 5


def test_seed_store_bypasses_per_entry_write_entry_row(monkeypatch, tmp_path):
    from agent import LTMStore
    from scripts.benchmark_memory import _seed_store

    store = LTMStore(
        context_dir=tmp_path / "context",
        memory_dir=tmp_path / "memory",
    )

    def fail_write_entry_row(conn, entry):
        raise AssertionError(
            "_seed_store should bulk insert instead of calling _write_entry_row"
        )

    monkeypatch.setattr(store, "_write_entry_row", fail_write_entry_row)

    _seed_store(store, 5)

    assert len(store.all_entries()) == 5


def test_benchmark_script_cli_outputs_json():
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--sizes",
            "10",
            "--search-runs",
            "1",
            "--write-runs",
            "1",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )

    payload = json.loads(proc.stdout)

    assert payload["sizes"][0]["entries"] == 10
    assert "search" in payload["sizes"][0]
    assert "write" in payload["sizes"][0]


def test_benchmark_script_cli_writes_jsonl_output(tmp_path):
    output_path = tmp_path / "bench.jsonl"

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--sizes",
            "10",
            "--search-runs",
            "1",
            "--write-runs",
            "1",
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )

    lines = output_path.read_text(encoding="utf-8").splitlines()

    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["sizes"][0]["entries"] == 10


def test_benchmark_script_cli_writes_csv_output(tmp_path):
    output_path = tmp_path / "bench.csv"

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--sizes",
            "10",
            "--search-runs",
            "1",
            "--write-runs",
            "1",
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )

    content = output_path.read_text(encoding="utf-8")

    assert "entries,metric,runs,avg_ms,min_ms,max_ms,p50_ms,total_ms" in content
    assert "10,search,1," in content
    assert "10,write,1," in content


def test_benchmark_script_cli_compares_against_previous_run(tmp_path):
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps(
            {
                "query": "concise responses",
                "sizes": [
                    {
                        "entries": 10,
                        "search": {
                            "runs": 1,
                            "avg_ms": 2.0,
                            "min_ms": 2.0,
                            "max_ms": 2.0,
                            "p50_ms": 2.0,
                            "total_ms": 2.0,
                        },
                        "write": {
                            "runs": 1,
                            "avg_ms": 3.0,
                            "min_ms": 3.0,
                            "max_ms": 3.0,
                            "p50_ms": 3.0,
                            "total_ms": 3.0,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--sizes",
            "10",
            "--search-runs",
            "1",
            "--write-runs",
            "1",
            "--compare",
            str(baseline_path),
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )

    payload = json.loads(proc.stdout)

    assert "comparison" in payload
    assert payload["comparison"]["sizes"][0]["entries"] == 10
    assert "delta_avg_ms" in payload["comparison"]["sizes"][0]["search"]
