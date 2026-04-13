# Memory Benchmark Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a standalone benchmark script that measures memory search and write latency across configurable dataset sizes.

**Architecture:** Keep benchmark logic out of `agent.py` by adding a dedicated script under `scripts/`. The script will create isolated temporary stores, seed synthetic memory items, run repeatable search/write workloads, and print machine-readable JSON so results can be compared across runs.

**Tech Stack:** Python 3.11, stdlib (`argparse`, `json`, `statistics`, `tempfile`, `time`), existing `agent.LTMStore` / `agent.LTMEntry`, `pytest`

---

### Task 1: Add benchmark tests

**Files:**
- Create: `tests/test_memory_benchmark.py`
- Test: `tests/test_memory_benchmark.py`

- [ ] **Step 1: Write the failing test**

```python
def test_run_benchmarks_returns_metrics_for_requested_sizes():
    results = run_benchmarks(sizes=[10], search_runs=2, write_runs=2)
    assert results["sizes"][0]["entries"] == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/test_memory_benchmark.py`
Expected: FAIL because the benchmark module does not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
def run_benchmarks(...):
    return {"sizes": [...]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q tests/test_memory_benchmark.py`
Expected: PASS

### Task 2: Implement the benchmark script

**Files:**
- Create: `scripts/benchmark_memory.py`
- Modify: `README.md`
- Test: `tests/test_memory_benchmark.py`

- [ ] **Step 1: Implement isolated search/write benchmark helpers**

```python
def benchmark_search(...): ...
def benchmark_write(...): ...
```

- [ ] **Step 2: Add CLI entrypoint**

Run: `python scripts/benchmark_memory.py --sizes 100 --search-runs 2 --write-runs 2`
Expected: JSON output with search/write metrics

- [ ] **Step 3: Document usage**

Add a short README section showing the command and JSON output purpose.

- [ ] **Step 4: Run targeted tests**

Run: `uv run pytest -q tests/test_memory_benchmark.py`
Expected: PASS

### Task 3: Verify end-to-end

**Files:**
- Modify: `scripts/benchmark_memory.py`
- Modify: `README.md`
- Test: `tests/test_memory_benchmark.py`

- [ ] **Step 1: Run sample benchmark**

Run: `python scripts/benchmark_memory.py --sizes 100 --search-runs 2 --write-runs 2`
Expected: exit 0 with JSON containing one benchmark record

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest -q`
Expected: all tests pass
