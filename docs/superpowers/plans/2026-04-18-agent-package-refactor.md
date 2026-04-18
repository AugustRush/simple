# Agent Package Refactor Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the monolithic `agent.py` implementation with a package layout that separates stable runtime responsibilities.

**Architecture:** Create an `agent/` package with domain, core, tools, memory, skills, plugins, and channel modules, then move the existing implementation into those modules while keeping runtime behavior working through a single composition root and Typer entrypoint.

**Tech Stack:** Python, asyncio, Typer, Rich, pytest, setuptools

---

### Task 1: Encode the target package layout in tests

**Files:**
- Modify: `tests/test_single_file_layout.py`
- Test: `tests/test_single_file_layout.py`

- [ ] **Step 1: Write failing tests for the package layout**

```python
def test_agent_imports_from_package():
    import agent
    assert Path(agent.__file__).name == "__init__.py"
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `pytest tests/test_single_file_layout.py -v`
Expected: FAIL because `agent` still resolves to `agent.py`.

- [ ] **Step 3: Add a package-oriented assertion for stable entrypoints**

```python
def test_agent_exports_typer_app():
    import agent
    assert agent.app is not None
```

- [ ] **Step 4: Re-run the focused test target after implementation**

Run: `pytest tests/test_single_file_layout.py -v`
Expected: PASS

### Task 2: Create the package skeleton and migrate shared/core modules

**Files:**
- Create: `agent/__init__.py`
- Create: `agent/domain/__init__.py`
- Create: `agent/domain/constants.py`
- Create: `agent/domain/events.py`
- Create: `agent/domain/models.py`
- Create: `agent/core/__init__.py`
- Create: `agent/core/output.py`
- Create: `agent/core/agent.py`
- Create: `agent/config.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Move constants, dataclasses, and event models into package modules**

```python
from .cli import app
from .bootstrap import build_components
```

- [ ] **Step 2: Update imports so the moved code still resolves**

Run: `pytest tests/test_single_file_layout.py tests/test_channel_layer.py -v`
Expected: At least the package-layout tests pass; channel tests may still expose remaining missing moves.

- [ ] **Step 3: Keep `agent.__init__` as the public re-export surface**

```python
__all__ = ["app", "build_components", "BaseAgent"]
```

### Task 3: Extract tools, memory, skills, and plugins into focused packages

**Files:**
- Create: `agent/tools/__init__.py`
- Create: `agent/tools/registry.py`
- Create: `agent/tools/builtin.py`
- Create: `agent/tools/mcp.py`
- Create: `agent/tools/user_tools.py`
- Create: `agent/memory/__init__.py`
- Create: `agent/memory/index.py`
- Create: `agent/memory/palace.py`
- Create: `agent/memory/staging.py`
- Create: `agent/memory/ltm.py`
- Create: `agent/memory/retrieval.py`
- Create: `agent/memory/consolidation.py`
- Create: `agent/memory/background.py`
- Create: `agent/skills/__init__.py`
- Create: `agent/skills/bundles.py`
- Create: `agent/skills/catalog.py`
- Create: `agent/skills/runtime.py`
- Create: `agent/plugins/__init__.py`
- Create: `agent/plugins/base.py`
- Create: `agent/plugins/catalog.py`
- Create: `agent/plugins/manifest.py`
- Modify: tests that import moved symbols

- [ ] **Step 1: Run a focused integration subset to establish the red baseline**

Run: `pytest tests/test_agent_integration.py -k 'build_components or skill or spawn_agent or plugin' -v`
Expected: FAIL while imports still point at the monolith.

- [ ] **Step 2: Move each subsystem without changing behavior**

```python
class ToolRegistry:
    ...

class MemoryPalace:
    ...
```

- [ ] **Step 3: Re-run the same subset until it passes**

Run: `pytest tests/test_agent_integration.py -k 'build_components or skill or spawn_agent or plugin' -v`
Expected: PASS

### Task 4: Extract channels, bootstrap, and CLI, then remove the monolith implementation

**Files:**
- Create: `agent/channels/__init__.py`
- Create: `agent/channels/base.py`
- Create: `agent/channels/cli.py`
- Create: `agent/bootstrap.py`
- Create: `agent/cli.py`
- Delete: `agent.py`
- Modify: `channels/feishu.py`
- Modify: `README.md`
- Modify: `pyproject.toml`

- [ ] **Step 1: Move channel abstractions and runtime assembly behind the package**

```python
app = typer.Typer(...)
```

- [ ] **Step 2: Update the packaging entrypoint**

Run: `pytest tests/test_channel_layer.py tests/test_feishu_channel.py -v`
Expected: PASS

- [ ] **Step 3: Delete the monolithic implementation once all imports are updated**

Run: `pytest tests/test_single_file_layout.py tests/test_channel_layer.py tests/test_agent_integration.py -v`
Expected: PASS

### Task 5: Run full verification and polish docs

**Files:**
- Modify: `README.md`
- Modify: any remaining tests or package exports touched by migration

- [ ] **Step 1: Run the full test suite**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 2: Sanity-check the CLI entrypoint**

Run: `python -m agent --help`
Expected: Exit 0 with Typer help output.

- [ ] **Step 3: Update docs that still describe the repo as single-file**

Run: `rg -n "Single-file|single-file|agent.py" README.md docs tests`
Expected: Remaining references are intentional or updated.
