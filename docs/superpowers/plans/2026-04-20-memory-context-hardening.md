# Memory Context Hardening Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix memory retry semantics, task coexistence, and post-compaction recent-context retrieval without rewriting the whole memory subsystem.

**Architecture:** Keep the current memory pipeline intact, but make consolidation return an explicit success signal, loosen task deduplication so distinct tasks can coexist, and unify recent staged-context injection behind a bounded helper. Retrieval routing remains keyword-informed, but acts as a ranking bias rather than a hard prefilter.

**Tech Stack:** Python, pytest, SQLite FTS5

---

### Task 1: Lock retry semantics with tests

**Files:**
- Modify: `tests/test_background_memory.py`
- Modify: `tests/test_consolidation.py`
- Modify: `agent/memory/system.py`

- [ ] **Step 1: Write the failing tests**

- [ ] **Step 2: Run targeted tests to verify the failures**

Run: `pytest -q tests/test_background_memory.py tests/test_consolidation.py`
Expected: FAIL on retry-semantics assertions

- [ ] **Step 3: Implement minimal consolidation success plumbing**

- [ ] **Step 4: Re-run targeted tests**

Run: `pytest -q tests/test_background_memory.py tests/test_consolidation.py`
Expected: PASS

### Task 2: Preserve distinct tasks under one entity

**Files:**
- Modify: `tests/test_ltm_store.py`
- Modify: `agent/memory/system.py`

- [ ] **Step 1: Write the failing task coexistence test**

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_ltm_store.py`
Expected: FAIL on task coexistence assertion

- [ ] **Step 3: Implement minimal task deduplication change**

- [ ] **Step 4: Re-run task store tests**

Run: `pytest -q tests/test_ltm_store.py`
Expected: PASS

### Task 3: Restore recent context after compaction

**Files:**
- Modify: `tests/test_staging.py`
- Modify: `tests/test_consolidation.py`
- Modify: `agent/memory/system.py`

- [ ] **Step 1: Write the failing retrieval tests**

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q tests/test_staging.py tests/test_consolidation.py`
Expected: FAIL on recent-context assertions

- [ ] **Step 3: Implement bounded recent-context helper and soft-biased retrieval**

- [ ] **Step 4: Re-run targeted tests**

Run: `pytest -q tests/test_staging.py tests/test_consolidation.py`
Expected: PASS

### Task 4: Final verification

**Files:**
- Verify: `agent/memory/system.py`
- Verify: `tests/test_background_memory.py`
- Verify: `tests/test_consolidation.py`
- Verify: `tests/test_ltm_store.py`
- Verify: `tests/test_staging.py`

- [ ] **Step 1: Run the focused regression suite**

Run: `pytest -q tests/test_background_memory.py tests/test_consolidation.py tests/test_ltm_store.py tests/test_staging.py`
Expected: PASS

- [ ] **Step 2: Run any adjacent relevant suite if needed**

Run: `pytest -q tests/test_memory_palace_store.py tests/test_retriever.py`
Expected: PASS
