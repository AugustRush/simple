# Spawn-Agent Orchestration Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an internal orchestration layer and orchestration skill on top of `spawn_agent` without introducing any new public delegation tool.

**Architecture:** Keep `spawn_agent` as the only public delegation primitive. Add a small internal orchestration runtime for `parallel`, `pipeline`, and `rendezvous`, plus an internal skill that teaches the lead agent when to choose each strategy.

**Tech Stack:** Python 3.11, asyncio, existing `BaseAgent` / `AgentContext`, pytest

---

### Task 1: Add Internal Orchestration Data Model

**Files:**
- Create: `agent/orchestration/runtime.py`
- Create: `agent/orchestration/__init__.py`
- Test: `tests/test_agent_integration.py`

- [ ] **Step 1: Write the failing tests**

Add tests for a minimal internal data model and import surface:
- `SubtaskSpec`
- `SubtaskResult`

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/test_agent_integration.py::test_orchestration_runtime_exports_minimal_types`
Expected: FAIL because module/types do not exist

- [ ] **Step 3: Write minimal implementation**

Create `agent/orchestration/runtime.py` with small dataclasses only.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q tests/test_agent_integration.py::test_orchestration_runtime_exports_minimal_types`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/orchestration/__init__.py agent/orchestration/runtime.py tests/test_agent_integration.py
git commit -m "feat: add orchestration runtime types"
```

### Task 2: Add Parallel Execution Runtime

**Files:**
- Modify: `agent/orchestration/runtime.py`
- Test: `tests/test_agent_integration.py`

- [ ] **Step 1: Write the failing tests**

Add tests that prove:
- parallel subtasks run concurrently
- failures do not cancel independent siblings

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/test_agent_integration.py::test_run_parallel_subtasks_executes_independent_specs`
Expected: FAIL because function does not exist

- [ ] **Step 3: Write minimal implementation**

Implement `run_parallel_subtasks(...)` using `asyncio.gather(..., return_exceptions=True)` and bounded concurrency.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q tests/test_agent_integration.py::test_run_parallel_subtasks_executes_independent_specs tests/test_agent_integration.py::test_run_parallel_subtasks_preserves_sibling_results_on_failure`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/orchestration/runtime.py tests/test_agent_integration.py
git commit -m "feat: add parallel orchestration runtime"
```

### Task 3: Add Pipeline Execution Runtime

**Files:**
- Modify: `agent/orchestration/runtime.py`
- Test: `tests/test_agent_integration.py`

- [ ] **Step 1: Write the failing tests**

Add tests that prove:
- dependent subtasks execute in dependency order
- downstream task receives only summarized upstream result, not raw full context

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/test_agent_integration.py::test_run_pipeline_subtasks_executes_in_dependency_order`
Expected: FAIL because function does not exist

- [ ] **Step 3: Write minimal implementation**

Implement `run_pipeline_subtasks(...)` with topological staged execution and summary passing.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q tests/test_agent_integration.py::test_run_pipeline_subtasks_executes_in_dependency_order tests/test_agent_integration.py::test_run_pipeline_subtasks_passes_summary_only`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/orchestration/runtime.py tests/test_agent_integration.py
git commit -m "feat: add pipeline orchestration runtime"
```

### Task 4: Add Rendezvous Execution Runtime

**Files:**
- Modify: `agent/orchestration/runtime.py`
- Test: `tests/test_agent_integration.py`

- [ ] **Step 1: Write the failing tests**

Add tests that prove:
- rendezvous runs an initial independent round
- lead summary is the only input to the second round
- round limit is enforced

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/test_agent_integration.py::test_run_rendezvous_round_uses_lead_summary_for_followup`
Expected: FAIL because function does not exist

- [ ] **Step 3: Write minimal implementation**

Implement `run_rendezvous_round(...)` with explicit round limit and lead summary callback.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q tests/test_agent_integration.py::test_run_rendezvous_round_uses_lead_summary_for_followup tests/test_agent_integration.py::test_run_rendezvous_round_enforces_round_limit`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/orchestration/runtime.py tests/test_agent_integration.py
git commit -m "feat: add rendezvous orchestration runtime"
```

### Task 5: Add Write-Scope Conflict Guard

**Files:**
- Modify: `agent/orchestration/runtime.py`
- Test: `tests/test_agent_integration.py`

- [ ] **Step 1: Write the failing tests**

Add a test that rejects parallel specs with overlapping `write_scope`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/test_agent_integration.py::test_parallel_orchestration_rejects_overlapping_write_scope`
Expected: FAIL because guard is missing

- [ ] **Step 3: Write minimal implementation**

Validate `write_scope` before parallel execution and fail early with a clear error.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q tests/test_agent_integration.py::test_parallel_orchestration_rejects_overlapping_write_scope`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/orchestration/runtime.py tests/test_agent_integration.py
git commit -m "fix: guard parallel orchestration write conflicts"
```

### Task 6: Add Internal Orchestration Skill

**Files:**
- Create: `agent/_builtin/skills/multi-agent-orchestration/SKILL.md`
- Modify: `tests/test_agent_integration.py`

- [ ] **Step 1: Write the failing tests**

Add tests that prove the skill bundle is discoverable and can be surfaced through the existing skills runtime.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/test_agent_integration.py::test_build_components_exposes_multi_agent_orchestration_skill`
Expected: FAIL because skill does not exist

- [ ] **Step 3: Write minimal implementation**

Create a built-in skill that teaches `direct / parallel / pipeline / rendezvous` selection rules and explicitly forbids introducing new public delegation tools.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q tests/test_agent_integration.py::test_build_components_exposes_multi_agent_orchestration_skill`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/_builtin/skills/multi-agent-orchestration/SKILL.md tests/test_agent_integration.py
git commit -m "feat: add orchestration decision skill"
```

### Task 7: Integrate Orchestration Runtime Behind Spawn-Agent Flows

**Files:**
- Modify: `agent/core/agent.py`
- Modify: `agent/__init__.py`
- Modify: `tests/test_agent_integration.py`

- [ ] **Step 1: Write the failing tests**

Add tests that prove:
- `spawn_agent` remains the only public delegation tool
- sub-agents do not see orchestration internals as public tools
- orchestration runtime can be invoked internally without changing the public tool surface

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/test_agent_integration.py::test_system_prompt_keeps_spawn_agent_as_only_public_delegation_tool`
Expected: FAIL because integration is incomplete

- [ ] **Step 3: Write minimal implementation**

Wire the runtime in a way that preserves the public surface. Do not register any new public tool names.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q tests/test_agent_integration.py::test_system_prompt_keeps_spawn_agent_as_only_public_delegation_tool tests/test_agent_integration.py::test_sub_agents_do_not_receive_orchestration_public_surface`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/core/agent.py agent/__init__.py tests/test_agent_integration.py
git commit -m "refactor: internalize orchestration behind spawn_agent"
```

### Task 8: Full Verification

**Files:**
- Modify: none unless failures are found

- [ ] **Step 1: Run focused tests**

Run: `uv run pytest -q tests/test_agent_integration.py`
Expected: PASS

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 3: Review git diff**

Run:
```bash
git diff --stat
git diff --check
```
Expected: no whitespace issues, only intended files changed

- [ ] **Step 4: Commit final polish if needed**

```bash
git add <files>
git commit -m "chore: finalize spawn-agent orchestration"
```
