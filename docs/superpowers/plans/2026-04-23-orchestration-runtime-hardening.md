# Orchestration Runtime Hardening Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move multi-agent orchestration from prompt guidance into runtime-enforced execution and restore the missing task-boundary guarantees.

**Architecture:** Keep `spawn_agent` as the only public delegation primitive, but stop using planner output as prose injected into the model prompt. Instead, build an explicit internal orchestration plan, execute it through `BaseAgent` runtime helpers, and propagate worker constraints such as expected output, write scope, and capability profile into private subagent spawning.

**Tech Stack:** Python 3.11, asyncio, pytest

---

### Task 1: Replace Prompt Guidance With Explicit Plan Data

**Files:**
- Modify: `agent/orchestration/planner.py`
- Modify: `agent/orchestration/__init__.py`
- Test: `tests/test_agent_integration.py`

- [ ] **Step 1: Write the failing tests**

Add tests that prove planner decisions are returned as structured plan data instead of prose guidance only.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/test_agent_integration.py -k 'orchestration_planner'`
Expected: FAIL because the planner does not yet expose the new structure.

- [ ] **Step 3: Write minimal implementation**

Introduce explicit orchestration plan/result types in `agent/orchestration/planner.py` and export them.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q tests/test_agent_integration.py -k 'orchestration_planner'`
Expected: PASS

### Task 2: Execute Internal Orchestration From `send_message()`

**Files:**
- Modify: `agent/core/agent.py`
- Test: `tests/test_agent_integration.py`

- [ ] **Step 1: Write the failing tests**

Add tests that prove `send_message()` executes internal parallel/pipeline/rendezvous orchestration without relying on planner prose injected into the system prompt.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/test_agent_integration.py -k 'internal_parallel_orchestration or orchestrates'`
Expected: FAIL because `send_message()` still only injects prompt guidance.

- [ ] **Step 3: Write minimal implementation**

Teach `BaseAgent` to recognize explicit orchestration plans, build subtask specs, run the internal runtime helpers, and synthesize the final response from those results.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q tests/test_agent_integration.py -k 'internal_parallel_orchestration or orchestrates'`
Expected: PASS

### Task 3: Propagate Worker Constraints Into Private Spawning

**Files:**
- Modify: `agent/core/agent.py`
- Modify: `agent/orchestration/runtime.py`
- Test: `tests/test_agent_integration.py`

- [ ] **Step 1: Write the failing tests**

Add tests that prove `expected_output`, `write_scope`, and capability profile reach private subagent spawning and are enforced in the sub-agent registry.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/test_agent_integration.py -k 'write_scope or capability_profile or expected_output'`
Expected: FAIL because those constraints are not enforced yet.

- [ ] **Step 3: Write minimal implementation**

Add private subagent spawning from `SubtaskSpec`, restrict tools by capability profile, and enforce write-scope-aware file/shell permissions in subagents.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q tests/test_agent_integration.py -k 'write_scope or capability_profile or expected_output'`
Expected: PASS

### Task 4: Align Runtime Semantics And Prompt Policy

**Files:**
- Modify: `agent/orchestration/runtime.py`
- Modify: `agent/__init__.py`
- Modify: `agent/_builtin/skills/multi-agent-orchestration/SKILL.md`
- Test: `tests/test_agent_integration.py`

- [ ] **Step 1: Write the failing tests**

Add tests that prove pipeline executes ready stages in parallel, rendezvous can stop or narrow follow-up rounds, and the base prompt matches bounded rendezvous policy.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/test_agent_integration.py -k 'pipeline or rendezvous or only_public_delegation_tool'`
Expected: FAIL because runtime semantics and prompt text are still inconsistent.

- [ ] **Step 3: Write minimal implementation**

Make pipeline execute stage-by-stage, let rendezvous choose follow-up participants and stop early, and rewrite the base prompt to match lead-controlled bounded coordination.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q tests/test_agent_integration.py -k 'pipeline or rendezvous or only_public_delegation_tool'`
Expected: PASS

### Task 5: Final Verification

**Files:**
- Verify only

- [ ] **Step 1: Run focused orchestration tests**

Run: `uv run pytest -q tests/test_agent_integration.py -k 'orchestration or spawn_agent or parallel or pipeline or rendezvous'`
Expected: PASS

- [ ] **Step 2: Run the full integration test file**

Run: `uv run pytest -q tests/test_agent_integration.py`
Expected: PASS
