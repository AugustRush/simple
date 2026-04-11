# Skills Runtime Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Python skill modules with standard skill bundles loaded from `~/.agent/skills` and the repo `skills/` directory, with progressive disclosure and explicit invocation.

**Architecture:** A new catalog scans bundle directories for `SKILL.md`, resolves frontmatter metadata, and registers runtime skill tools. The system prompt receives a compact catalog, while full skill instructions and supporting files are only disclosed on activation. Explicit slash and natural-language invocations are normalized before agent execution.

**Tech Stack:** Python 3.11, single-file `agent.py`, `tool_runtime.py`, Typer CLI, existing tool loop, pytest.

---

## File Structure

- Modify: `agent.py`
- Modify: `tool_runtime.py`
- Modify: `README.md`
- Modify: `tests/test_agent_integration.py`
- Create: `skills/README.md`

### Task 1: Lock down bundle discovery in tests

**Files:**
- Modify: `/Users/shike/Desktop/simple/tests/test_agent_integration.py`

- [ ] **Step 1: Write failing tests for recursive `SKILL.md` discovery and metadata parsing**
- [ ] **Step 2: Write failing tests for built-in-vs-user precedence**
- [ ] **Step 3: Run the targeted tests and verify failure**
- [ ] **Step 4: Implement the catalog and metadata parsing**
- [ ] **Step 5: Re-run the targeted tests and verify pass**

### Task 2: Add progressive-disclosure runtime tools

**Files:**
- Modify: `/Users/shike/Desktop/simple/agent.py`
- Modify: `/Users/shike/Desktop/simple/tool_runtime.py`
- Modify: `/Users/shike/Desktop/simple/tests/test_agent_integration.py`

- [ ] **Step 1: Write failing tests for `activate_skill`, `list_skill_files`, and `read_skill_file`**
- [ ] **Step 2: Write a failing test that the system prompt includes the compact skill catalog instead of `*.py` tool text**
- [ ] **Step 3: Run the targeted tests and verify failure**
- [ ] **Step 4: Implement runtime skill activation and asset access**
- [ ] **Step 5: Re-run the targeted tests and verify pass**

### Task 3: Support explicit invocation forms

**Files:**
- Modify: `/Users/shike/Desktop/simple/agent.py`
- Modify: `/Users/shike/Desktop/simple/tests/test_agent_integration.py`

- [ ] **Step 1: Write failing tests for slash invocation parsing**
- [ ] **Step 2: Write failing tests for natural-language explicit invocation parsing**
- [ ] **Step 3: Run the targeted tests and verify failure**
- [ ] **Step 4: Implement invocation normalization and per-turn required skill activation**
- [ ] **Step 5: Re-run the targeted tests and verify pass**

### Task 4: Update docs and built-in skill scaffold

**Files:**
- Modify: `/Users/shike/Desktop/simple/README.md`
- Create: `/Users/shike/Desktop/simple/skills/README.md`

- [ ] **Step 1: Update the README to describe bundle-based skills**
- [ ] **Step 2: Add a repo `skills/` README for future built-in skills**
- [ ] **Step 3: Run the relevant test suite and verify the final behavior**
