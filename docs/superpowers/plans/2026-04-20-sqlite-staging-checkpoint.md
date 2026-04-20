# SQLite Staging Checkpoint Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store default conversation staging in SQLite while preserving JSONL compatibility.

**Architecture:** Keep `StagingBuffer` as the public abstraction. Use SQLite when no explicit path is supplied, and preserve JSONL behavior when callers pass `path=...`. Add backend metadata to queued consolidation jobs so background workers reconstruct the correct staging buffer.

**Tech Stack:** Python, SQLite, pytest

---

### Task 1: Add SQLite-backed staging regression tests

**Files:**
- Modify: `tests/test_staging.py`
- Modify: `tests/test_background_memory.py`

- [ ] **Step 1: Write failing tests for default SQLite persistence and background reconstruction**
- [ ] **Step 2: Run those tests and confirm they fail**

Run: `pytest -q tests/test_staging.py::test_default_staging_persists_in_sqlite_without_jsonl_file tests/test_background_memory.py::test_process_one_job_reconstructs_sqlite_staging_from_job_metadata`

### Task 2: Implement dual-backend staging

**Files:**
- Modify: `agent/memory/system.py`

- [ ] **Step 1: Add SQLite table creation for default staging**
- [ ] **Step 2: Route append/read/count/clear/drop_prefix to SQLite for default staging**
- [ ] **Step 3: Keep explicit path-based JSONL behavior unchanged**
- [ ] **Step 4: Add job backend metadata and reconstruct SQLite staging in `_job_staging()`**

### Task 3: Verify

**Files:**
- Verify: `agent/memory/system.py`
- Verify: `tests/test_staging.py`
- Verify: `tests/test_background_memory.py`
- Verify: `tests/test_consolidation.py`

- [ ] **Step 1: Run focused suites**

Run: `pytest -q tests/test_staging.py tests/test_background_memory.py tests/test_consolidation.py`

- [ ] **Step 2: Run full suite**

Run: `pytest -q`
