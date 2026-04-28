# Turn Runtime Boundary Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish typed turn-runtime contracts that let CLI, channel, tools, memory, and provider orchestration converge on one execution boundary.

**Architecture:** Add a small `agent.runtime` package containing immutable input/result types and a typed dependency container. Keep behavior unchanged in this slice; the types are the stable seam for later `TurnRunner` extraction.

**Tech Stack:** Python dataclasses, pytest, existing `agent.core.output.OutputSink`.

---

### Task 1: Runtime Contract Types

**Files:**
- Create: `agent/runtime/__init__.py`
- Create: `agent/runtime/contracts.py`
- Modify: `agent/core/__init__.py`
- Test: `tests/test_runtime_contracts.py`

- [ ] **Step 1: Write the failing tests**

Define tests for:
- `TurnInput.from_text()` normalizes the most common CLI/channel input shape.
- `TurnResult.record_tool_use()` returns a new result without mutating the old result.
- `RuntimeComponents.require()` returns typed dependencies and raises a clear error for missing dependencies.

- [ ] **Step 2: Run focused tests to verify failure**

Run: `pytest tests/test_runtime_contracts.py -q`
Expected: import failure because `agent.runtime` does not exist.

- [ ] **Step 3: Add minimal runtime contracts**

Implement:
- `TurnInput`
- `TurnResult`
- `RuntimeComponents`

Keep the module free of provider/tool/memory imports except optional `OutputSink` typing.

- [ ] **Step 4: Export stable API**

Export the contracts from `agent.runtime` and lazy-export them from `agent.core`.

- [ ] **Step 5: Run verification**

Run:
- `pytest tests/test_runtime_contracts.py -q`
- `pytest tests/test_channel_layer.py -q`
- `pytest -q`
- `git diff --check`

- [ ] **Step 6: Commit**

```bash
git add agent/runtime agent/core/__init__.py tests/test_runtime_contracts.py docs/superpowers/plans/2026-04-28-turn-runtime-boundary.md
git commit -m "Add turn runtime boundary contracts"
```
