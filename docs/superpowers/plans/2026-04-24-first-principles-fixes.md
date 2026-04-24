# First Principles Fixes Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the confirmed high-value correctness and safety issues without pulling the repo into a large refactor.

**Architecture:** Keep behavior changes narrowly scoped: add regression tests first, fix each root cause with minimal code, and preserve existing public interfaces unless the prior behavior was unsafe. Memory export becomes on-demand instead of write-through, legacy cleanup becomes allowlisted, and scheduler schema setup grows a minimal migration seam.

**Tech Stack:** Python, SQLite, pytest, Typer.

---

## File Structure

- Modify `agent/cli.py`: fix the `evolve` cleanup call.
- Modify `agent/memory/system.py`: decouple write path from full JSONL export and narrow legacy cleanup.
- Modify `agent/skills/catalog.py`: harden instruction updates against accidental erasure.
- Modify `agent/scheduler/store.py`: add schema versioning and migration entrypoint.
- Modify `agent/tools/runtime.py`: remove duplicate web constants.
- Modify `agent/config.py`: remove dead `DEFAULT_CONFIG = None`.
- Modify `tests/test_agent_integration.py`: add regressions for CLI/skill update behavior.
- Modify `tests/test_memory_palace_store.py`: add regressions for on-demand JSONL export and safe cleanup.
- Modify `tests/test_scheduler.py`: add regression for scheduler schema version initialization/migration.

## Task 1: Write Regression Tests

**Files:**
- Modify: `tests/test_agent_integration.py`
- Modify: `tests/test_memory_palace_store.py`
- Modify: `tests/test_scheduler.py`

- [ ] **Step 1: Add a failing test for the `evolve` cleanup path**
- [ ] **Step 2: Add a failing test proving `MemoryPalace.write()` does not rewrite JSONL eagerly**
- [ ] **Step 3: Add a failing test proving legacy cleanup preserves unrelated `.json`/`.md` files**
- [ ] **Step 4: Add a failing test proving `update_skill(instructions=\"\")` preserves the existing body**
- [ ] **Step 5: Add a failing test proving `SchedulerStore` sets `PRAGMA user_version`**
- [ ] **Step 6: Run the focused tests to verify RED**

Run: `uv run pytest -q tests/test_memory_palace_store.py tests/test_scheduler.py tests/test_agent_integration.py -k 'evolve or update_skill or jsonl or legacy or schema_version'`

## Task 2: Implement Minimal Fixes

**Files:**
- Modify: `agent/cli.py`
- Modify: `agent/memory/system.py`
- Modify: `agent/skills/catalog.py`
- Modify: `agent/scheduler/store.py`
- Modify: `agent/tools/runtime.py`
- Modify: `agent/config.py`

- [ ] **Step 1: Fix the `evolve` cleanup call**
- [ ] **Step 2: Make memory JSONL export dirty/on-demand instead of write-through**
- [ ] **Step 3: Replace broad legacy cleanup with explicit known-artifact cleanup**
- [ ] **Step 4: Preserve skill instructions when update payload passes an empty string**
- [ ] **Step 5: Add scheduler schema version initialization and migration hook**
- [ ] **Step 6: Remove duplicate constants and dead assignment**

## Task 3: Verify Green

**Files:**
- Modify: `tests/test_agent_integration.py`
- Modify: `tests/test_memory_palace_store.py`
- Modify: `tests/test_scheduler.py`

- [ ] **Step 1: Re-run focused regression tests**

Run: `uv run pytest -q tests/test_memory_palace_store.py tests/test_scheduler.py tests/test_agent_integration.py -k 'evolve or update_skill or jsonl or legacy or schema_version'`

- [ ] **Step 2: Run broader affected suites**

Run: `uv run pytest -q tests/test_memory_palace_store.py tests/test_scheduler.py tests/test_evolution.py tests/test_agent_integration.py`

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest -q`

