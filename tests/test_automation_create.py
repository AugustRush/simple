"""One creation verb: a task is a chain of one step, and `steps` is the whole difference.

Why this file exists
--------------------
The model used to choose between `schedule_create` and `workflow_create`, and the choice
carried no information the request did not already have: "remind me at 9" has no steps in
it, "fetch, then summarise, then post" has nothing but. A tool surface that asks the model
to classify the request by picking a tool is a surface that produces the wrong pick
sometimes, and the cost of the wrong pick is a chain built out of signals -- which has no
recorded edges, so nothing knows which steps a failure must stop.

`automation_create` is that decision moved into the body: no `steps` means one task, and
`steps` means a chain. What the tests below pin:

1. **The surface shrank.** One creation verb; the two it replaced are gone from the
   registry, not merely deprecated beside it. A deprecated twin is still a choice.
2. **Both shapes work through it**, and they leave different rows behind -- one task, or a
   workflow plus its step tasks.
3. **It did not become a bypass.** The chain still validates its graph before writing
   anything (a cycle is refused by the same validator the store uses), and the request
   gate is still on the unified name.
"""

from __future__ import annotations

import asyncio
import json

import pytest


@pytest.fixture(autouse=True)
def _isolated_scheduler(monkeypatch, tmp_path):
    """A scheduler of its own: the tools write to the shared home otherwise."""
    import agent.shared as shared_module
    from agent.security.shell import shell_session_allowlist_clear

    agent_home = tmp_path / ".agent"
    monkeypatch.setattr(shared_module, "AGENT_HOME", agent_home)
    monkeypatch.setattr(shared_module, "DEFAULT_OUTPUT_DIR", agent_home / "output")
    monkeypatch.setattr(shared_module, "SCHEDULER_DIR", agent_home / "tasks")
    monkeypatch.setattr(
        shared_module, "SCHEDULER_DB_FILE", agent_home / "tasks" / "scheduler.db"
    )
    shell_session_allowlist_clear()
    yield


@pytest.fixture()
def tools(tmp_path):
    from agent import BuiltinTools, MemoryPalace, ToolRegistry

    registry = ToolRegistry()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory = MemoryPalace(
        base_dir=tmp_path / "memory", context_dir=tmp_path / "context"
    )
    builtin = BuiltinTools(memory=memory, registry=registry, workspace_root=workspace)
    return builtin, registry


def _call(registry, name: str, args: dict) -> dict:
    return json.loads(asyncio.run(registry.call(name, args)))


def _store():
    from agent.scheduler import open_store

    return open_store()


# ── the surface ─────────────────────────────────────────────────────────────


def test_there_is_one_creation_verb_and_the_two_it_replaced_are_gone(tools):
    """A deprecated twin is still a choice, and a choice is what this removes."""
    _builtin, registry = tools
    names = set(registry.list_tools())

    assert "automation_create" in names
    assert "schedule_create" not in names, "the old task creator is still offered"
    assert "workflow_create" not in names, "the old chain creator is still offered"


def test_the_creation_verb_still_carries_the_request_gate(tools):
    """Unified is not a synonym for ungated: it creates something that outlives a turn."""
    _builtin, registry = tools
    capabilities = registry.tool_capabilities("automation_create")

    assert "requires_request" in capabilities, "the request gate was left behind"
    assert "state_write" in capabilities


# ── one task ────────────────────────────────────────────────────────────────


def test_no_steps_creates_one_task(tools):
    """The single-task shape, unchanged from what `schedule_create` did."""
    _builtin, registry = tools
    result = _call(
        registry,
        "automation_create",
        {
            "name": "每天早上看看日志",
            "action_type": "message",
            "message_text": "早上好",
            "trigger_type": "interval",
            "every": 24,
            "unit": "hours",
            "at": "2026-11-01T00:00:00+08:00",
            "intent": "每天早上看看日志",
        },
    )

    assert result.get("ok") is True, result
    store = _store()
    try:
        assert len(store.list_tasks()) == 1
        assert store.list_workflows() == [], "a single task must not build a chain"
    finally:
        store.close()


# ── a chain ─────────────────────────────────────────────────────────────────


def test_steps_creates_a_chain_and_its_steps(tools):
    """The chain shape, unchanged from what `workflow_create` did."""
    _builtin, registry = tools
    result = _call(
        registry,
        "automation_create",
        {
            "name": "每天整理一下",
            "intent": "每天整理一下",
            "steps": [
                {
                    "key": "collect",
                    "name": "收集",
                    "action_type": "agent_task",
                    "instruction": "收集",
                    "trigger_type": "interval",
                    "every": 24,
                    "unit": "hours",
                    "anchor_at": "2026-11-01T00:00:00+08:00",
                },
                {
                    "key": "report",
                    "name": "汇报",
                    "action_type": "agent_task",
                    "instruction": "汇报",
                    "depends_on": ["collect"],
                },
            ],
        },
    )

    assert result.get("ok") is True, result
    store = _store()
    try:
        (workflow,) = store.list_workflows()
        steps = store.step_tasks(workflow.id)
        assert sorted(steps) == ["collect", "report"]
        # The chain's rows are tasks: that is what makes the two shapes one thing.
        assert len(store.list_tasks()) == 2
    finally:
        store.close()


def test_the_chain_shape_still_validates_its_graph_before_writing(tools):
    """The unified tool must not become a way around the graph's own refusal."""
    _builtin, registry = tools
    result = _call(
        registry,
        "automation_create",
        {
            "name": "转圈的链",
            "intent": "转圈的链",
            "steps": [
                {
                    "key": "a",
                    "name": "A",
                    "action_type": "agent_task",
                    "instruction": "a",
                    "depends_on": ["b"],
                },
                {
                    "key": "b",
                    "name": "B",
                    "action_type": "agent_task",
                    "instruction": "b",
                    "depends_on": ["a"],
                },
            ],
        },
    )

    assert result.get("ok") is False, result
    assert "环" in result.get("error", "") or "cycle" in result.get("error", "").lower()

    store = _store()
    try:
        # Refused before anything was written: a chain row with no tasks behind it is
        # the failure mode the validator exists to make impossible.
        assert store.list_workflows() == []
        assert store.list_tasks() == []
    finally:
        store.close()
