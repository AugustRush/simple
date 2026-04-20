# Durable Conversation History Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable append-only conversation event history so history recall can cite actual turns instead of semantic memory summaries.

**Architecture:** Store user/assistant events in SQLite `conversation_turns`, keep `memory_items` as semantic memory, and let `ContextManager.retrieve_context()` combine evidence based on query intent. The existing `context_retrieve` tool remains the public API.

**Tech Stack:** Python, SQLite, pytest.

---

## File Structure

- Modify `agent/memory/system.py`: add turn dataclass/store methods, event-recall retrieval, and context retrieval composition.
- Modify `agent/cli.py`: record completed CLI turns to conversation history.
- Modify `agent/channels/base.py`: record completed channel turns to conversation history with channel metadata.
- Modify `tests/test_consolidation.py`: cover store persistence, staging independence, and retrieval composition.
- Modify `tests/test_builtin_tools.py`: cover `context_retrieve` returning turn history through the public tool.

## Task 1: Durable Turn Store

**Files:**
- Modify: `agent/memory/system.py`
- Test: `tests/test_consolidation.py`

- [ ] **Step 1: Write failing tests**

Add tests proving `LTMStore.append_conversation_turn()` persists ordered turns and that clearing staging does not delete them.

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest -q tests/test_consolidation.py::test_conversation_turns_persist_in_order tests/test_consolidation.py::test_conversation_history_survives_staging_clear`

Expected: fail because the store has no conversation history API.

- [ ] **Step 3: Implement minimal store API**

Add `ConversationTurn`, `conversation_turns` schema, `append_conversation_turn()`, and `recent_conversation_turns()`.

- [ ] **Step 4: Run tests to verify pass**

Run the same focused tests. Expected: pass.

## Task 2: Retrieval Composition

**Files:**
- Modify: `agent/memory/system.py`
- Test: `tests/test_consolidation.py`

- [ ] **Step 1: Write failing tests**

Add tests proving event-recall queries include actual recent turns and generic history queries do not incorrectly trigger conversation recall.

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest -q tests/test_consolidation.py::test_retrieve_context_prefers_conversation_turns_for_event_recall tests/test_consolidation.py::test_retrieve_context_keeps_generic_history_semantic`

Expected: fail because retrieval has no history section.

- [ ] **Step 3: Implement minimal retrieval**

Add `retrieve_history_context()` and compose it in `retrieve_context()` before semantic memory when query intent requires event evidence.

- [ ] **Step 4: Run tests to verify pass**

Run the same focused tests. Expected: pass.

## Task 3: Public Tool And Write Path

**Files:**
- Modify: `agent/cli.py`
- Modify: `agent/channels/base.py`
- Modify: `tests/test_builtin_tools.py`

- [ ] **Step 1: Write failing tests**

Add a public `context_retrieve` test proving the tool returns conversation history sections through `ContextManager`.

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest -q tests/test_builtin_tools.py::test_context_retrieve_returns_conversation_history_sections`

Expected: fail until retrieval is wired.

- [ ] **Step 3: Implement write path**

Call `ctx_mgr.record_turn()` after completed user/assistant turns in CLI and channel runners. Keep staging writes unchanged.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest -q tests/test_consolidation.py tests/test_builtin_tools.py`

Expected: pass.

## Task 4: Full Verification And Commit

- [ ] **Step 1: Run full verification**

Run: `uv run pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Commit**

```bash
git add agent/memory/system.py agent/cli.py agent/channels/base.py tests/test_consolidation.py tests/test_builtin_tools.py docs/superpowers/specs/2026-04-20-durable-conversation-history-design.md docs/superpowers/plans/2026-04-20-durable-conversation-history.md
git commit -m "feat: add durable conversation history"
```
