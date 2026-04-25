# Memory Facts And Retrieval Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable fact assertions and resolved facts, bootstrap assistant identity deterministically, and make retrieval consult resolved facts before free-form memory search.

**Architecture:** Keep SQLite as the source of truth, but split exact facts into append-only `fact_assertions` plus derived `resolved_facts`. Route exact fact lookup through a lightweight query planner and resolved-fact retrieval, while preserving existing `conversation_turns` and `memory_items` behavior as fallback layers.

**Tech Stack:** Python 3.11, SQLite, pytest, existing `agent/memory/system.py` runtime

---

### Task 1: Add Fact Storage Primitives

**Files:**
- Create: `tests/test_fact_store.py`
- Modify: `agent/memory/system.py`
- Modify: `agent/memory/__init__.py`

- [ ] **Step 1: Write the failing store tests**

Add tests for:
- `fact_assertions` schema creation
- append-only assertion writes
- `resolved_facts` materialization for a simple `(subject, predicate, scope)` key
- conflict preservation when two assertions disagree

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest -q tests/test_fact_store.py`
Expected: FAIL because fact dataclasses, schema, and store APIs do not exist yet.

- [ ] **Step 3: Implement minimal fact dataclasses and store APIs**

Add to `agent/memory/system.py`:
- `FactAssertion`
- `ResolvedFact`
- schema creation for `fact_assertions` and `resolved_facts`
- minimal APIs:
  - `add_fact_assertion(...)`
  - `read_fact_assertions(...)`
  - `resolve_fact(...)`
  - `read_resolved_facts(...)`

Export new public types from `agent/memory/__init__.py`.

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `uv run pytest -q tests/test_fact_store.py`
Expected: PASS


### Task 2: Bootstrap Assistant Identity And Exact Fact Retrieval

**Files:**
- Modify: `tests/test_consolidation.py`
- Modify: `tests/test_output_dir.py`
- Modify: `config.example.json`
- Modify: `agent/bootstrap.py`
- Modify: `agent/memory/system.py`

- [ ] **Step 1: Write the failing bootstrap and retrieval tests**

Add tests for:
- optional assistant identity config bootstraps a resolved fact at startup
- exact fact lookup reads `resolved_facts` before free-form memory
- implicit retrieval includes assistant identity from resolved facts after restart

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `uv run pytest -q tests/test_consolidation.py -k "assistant_identity or resolved_fact" tests/test_output_dir.py -k "assistant_identity"`
Expected: FAIL because bootstrap wiring and resolved-fact retrieval are not implemented.

- [ ] **Step 3: Implement bootstrap wiring and exact fact query path**

Modify `agent/bootstrap.py` and `config.example.json` to support optional deterministic assistant identity config.

Modify `agent/memory/system.py` to add:
- a lightweight query plan object
- exact fact lookup over `resolved_facts`
- retrieval assembly that prefers resolved facts and only falls back to free-form memory when needed

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run: `uv run pytest -q tests/test_consolidation.py -k "assistant_identity or resolved_fact" tests/test_output_dir.py -k "assistant_identity"`
Expected: PASS


### Task 3: Preserve Existing Retrieval Guarantees While Adding Facts

**Files:**
- Modify: `tests/test_consolidation.py`
- Modify: `tests/test_background_memory.py`
- Modify: `agent/memory/system.py`

- [ ] **Step 1: Write the failing regression tests**

Add tests for:
- event recall remains session-scoped
- zero-hit implicit retrieval still returns empty
- conflicting facts are excluded from implicit injection
- free-form fallback still works for mixed queries when no exact fact exists

- [ ] **Step 2: Run the targeted regression tests to verify they fail**

Run: `uv run pytest -q tests/test_consolidation.py -k "conflict or mixed or event_recall or zero_hit" tests/test_background_memory.py`
Expected: FAIL for new fact-related regressions.

- [ ] **Step 3: Implement the minimal resolver and retrieval assembly rules**

In `agent/memory/system.py`:
- implement simple precedence-based resolution
- keep losing assertions in `fact_assertions`
- exclude conflicted fact keys from implicit context
- preserve existing event-recall isolation and free-form fallback behavior

- [ ] **Step 4: Run the targeted regression tests to verify they pass**

Run: `uv run pytest -q tests/test_consolidation.py -k "conflict or mixed or event_recall or zero_hit" tests/test_background_memory.py`
Expected: PASS


### Task 4: Verify End-To-End And Update Docs

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-04-25-memory-facts-and-retrieval-design.md`

- [ ] **Step 1: Update docs to match implemented behavior**

Document:
- `fact_assertions` / `resolved_facts`
- assistant identity bootstrap
- exact fact retrieval before free-form fallback

- [ ] **Step 2: Run focused memory suites**

Run: `uv run pytest -q tests/test_fact_store.py tests/test_consolidation.py tests/test_background_memory.py tests/test_memory_palace_store.py tests/test_staging.py tests/test_ltm_store.py`
Expected: PASS

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS with the existing warning count unchanged or lower.

- [ ] **Step 4: Review final diff for scope discipline**

Run: `git diff --stat`
Expected: only memory, bootstrap, config, docs, and related test files changed.
