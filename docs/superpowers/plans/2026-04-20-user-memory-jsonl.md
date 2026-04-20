# User Memory JSONL Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove write-path memory projections and expose one user-facing JSONL memory export.

**Architecture:** Keep internal memory loci and retrieval unchanged, but stop maintaining `_meta.json`, category JSON snapshots, and markdown entity projections. Derive category stats directly from SQLite and export user-visible memory only through `memory/memory.jsonl`.

**Tech Stack:** Python, SQLite, pytest

---

### Task 1: Lock new user-facing behavior with tests

**Files:**
- Modify: `tests/test_memory_palace_store.py`
- Modify: `tests/test_ltm_store.py`

- [ ] **Step 1: Replace projection/snapshot expectations with JSONL expectations**
- [ ] **Step 2: Run focused tests and confirm failures**

### Task 2: Implement no-projection write path

**Files:**
- Modify: `agent/memory/system.py`

- [ ] **Step 1: Stop writing `_meta.json`, category JSON, and markdown projections**
- [ ] **Step 2: Derive category stats from SQLite queries**
- [ ] **Step 3: Add `MemoryPalace.export_jsonl()` and route `read_index()` to it**
- [ ] **Step 4: Clean up legacy derived artifacts on store initialization**

### Task 3: Verify

**Files:**
- Verify: `agent/memory/system.py`
- Verify: `tests/test_memory_palace_store.py`
- Verify: `tests/test_ltm_store.py`

- [ ] **Step 1: Run focused suites**

Run: `pytest -q tests/test_memory_palace_store.py tests/test_ltm_store.py`

- [ ] **Step 2: Run full suite**

Run: `pytest -q`
