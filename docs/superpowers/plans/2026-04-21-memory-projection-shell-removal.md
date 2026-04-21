# Memory Projection Shell Removal Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the remaining chapter/index projection shell from the memory layer while keeping SQLite loci internally and a single chronological JSONL export externally.

**Architecture:** Delete `MemoryIndex` and chapter-directory behavior, keep `MemoryPalace` as a thin compatibility facade over SQLite plus `memory.jsonl`, and update CLI/tests so user-facing memory is no longer described as chapter folders.

**Tech Stack:** Python, SQLite, pytest.

---

## File Structure

- Modify `agent/memory/system.py`: remove `MemoryIndex`, stop chapter-shell behavior, sort JSONL export chronologically.
- Modify `agent/memory/__init__.py` and `agent/__init__.py`: remove `MemoryIndex` export.
- Modify `agent/cli.py`: replace chapter-based memory display with JSONL/stat-based display.
- Modify `tests/test_memory_palace_store.py`: cover no chapter shell and chronological JSONL export.
- Modify `tests/test_agent_integration.py`: align fake memory objects and CLI expectations if needed.

## Task 1: Regression Tests

**Files:**
- Modify: `tests/test_memory_palace_store.py`

- [ ] **Step 1: Write failing tests**
- [ ] **Step 2: Run focused tests to verify they fail**

Run: `uv run pytest -q tests/test_memory_palace_store.py::test_memory_palace_does_not_create_chapter_dirs tests/test_memory_palace_store.py::test_memory_palace_exports_jsonl_in_updated_at_order`

Expected: fail until chapter shell is removed and export order is made chronological.

## Task 2: Remove Chapter Shell

**Files:**
- Modify: `agent/memory/system.py`
- Modify: `agent/memory/__init__.py`
- Modify: `agent/__init__.py`

- [ ] **Step 1: Remove `MemoryIndex` usage and chapter dir initialization**
- [ ] **Step 2: Sort `export_jsonl()` by `updated_at`, then `id`**
- [ ] **Step 3: Re-run focused tests**

## Task 3: CLI Simplification

**Files:**
- Modify: `agent/cli.py`
- Modify: `tests/test_agent_integration.py`

- [ ] **Step 1: Replace chapter-based memory display with JSONL/stat-based display**
- [ ] **Step 2: Re-run related CLI tests**

## Task 4: Full Verification

- [ ] **Step 1: Run targeted suites**

Run: `uv run pytest -q tests/test_memory_palace_store.py tests/test_agent_integration.py`

- [ ] **Step 2: Run full suite**

Run: `uv run pytest -q`

- [ ] **Step 3: Commit**

```bash
git add agent/memory/system.py agent/memory/__init__.py agent/__init__.py agent/cli.py tests/test_memory_palace_store.py tests/test_agent_integration.py docs/superpowers/specs/2026-04-21-memory-projection-shell-removal-design.md docs/superpowers/plans/2026-04-21-memory-projection-shell-removal.md
git commit -m "refactor: remove memory projection shell"
```
