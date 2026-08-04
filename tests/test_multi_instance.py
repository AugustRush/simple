"""Multi-instance (``--name``) isolation regression tests.

``gateway --name prod`` must switch every home-derived path — including ones
captured at import time — so a named instance never writes into the default
``~/.agent`` (or another instance's home).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import agent as agent_module
from agent import shared
from agent.memory.system import (
    ConsolidationEngine,
    ContextManager,
    LocalRetriever,
    LTMStore,
    MemoryPalace,
    StagingBuffer,
)

# Globals rewritten by ``_set_agent_home``; the fixture restores every one.
_SHARED_PATHS = (
    "AGENT_HOME",
    "MEMORY_DIR",
    "SKILLS_DIR",
    "TOOLS_DIR",
    "PACKAGE_ROOT",
    "BUILTIN_SKILLS_DIR",
    "PROMPTS_DIR",
    "RL_DIR",
    "SCHEDULER_DIR",
    "SCHEDULER_DB_FILE",
    "CONFIG_FILE",
    "INDEX_FILE",
    "SESSIONS_FILE",
    "DEFAULT_OUTPUT_DIR",
    "PLUGINS_DIR",
    "USER_PLUGINS_DIR",
    "CONTEXT_DIR",
    "STAGING_DIR",
    "PALACE_DB_FILE",
)


@pytest.fixture
def switched_home(tmp_path):
    """Switch to a per-test agent home, restoring all globals afterwards."""
    saved = {name: getattr(shared, name) for name in _SHARED_PATHS}
    saved_tasks_dir = agent_module.TASKS_DIR
    saved_env = os.environ.get("SIMPLE_AGENT_HOME")
    home = tmp_path / ".agent-prod"

    agent_module._set_agent_home(home)
    try:
        yield home
    finally:
        for name, value in saved.items():
            setattr(shared, name, value)
        agent_module.TASKS_DIR = saved_tasks_dir
        if saved_env is None:
            os.environ.pop("SIMPLE_AGENT_HOME", None)
        else:
            os.environ["SIMPLE_AGENT_HOME"] = saved_env


def test_set_agent_home_rewrites_shared_paths_and_tasks_dir(switched_home):
    assert shared.AGENT_HOME == switched_home
    assert agent_module.TASKS_DIR == switched_home / "tasks"
    assert shared.CONFIG_FILE == switched_home / "config.json"
    assert shared.CONTEXT_DIR == switched_home / "context"
    assert shared.PALACE_DB_FILE == switched_home / "context" / "palace.db"
    assert shared.SCHEDULER_DB_FILE == switched_home / "tasks" / "scheduler.db"
    assert shared.SKILLS_DIR == switched_home / "skills"
    assert os.environ["SIMPLE_AGENT_HOME"] == str(switched_home)


def test_staging_buffer_follows_switched_home(switched_home):
    buffer = StagingBuffer()
    try:
        assert buffer.context_dir == switched_home / "context"
        assert buffer.path.parent == switched_home / "context" / "_staging"
    finally:
        buffer.close()


def test_ltm_store_follows_switched_home(switched_home):
    store = LTMStore()
    try:
        assert store.dir == switched_home / "context"
        assert store.memory_dir == switched_home / "memory"
    finally:
        store.close()


def test_memory_palace_follows_switched_home(switched_home):
    palace = MemoryPalace()
    try:
        assert palace.base_dir == switched_home / "memory"
        assert palace.store.dir == switched_home / "context"
    finally:
        palace.store.close()


def test_context_manager_staging_follows_switched_home(switched_home):
    """The gateway builds ContextManager without an explicit staging buffer, so
    the default must resolve against the current (switched) home."""
    store = LTMStore()
    try:
        manager = ContextManager(
            store=store,
            retriever=LocalRetriever(),
            consolidation=ConsolidationEngine(store=store),
        )
        try:
            assert manager.staging.context_dir == switched_home / "context"
        finally:
            manager.staging.close()
    finally:
        store.close()


def test_gateway_name_wires_agent_home(tmp_path, switched_home, monkeypatch):
    """End-to-end: ``gateway --name prod`` must land on ~/.agent-prod."""
    import agent.cli as cli_module

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(cli_module.agent_module, "load_config", lambda: ({}, False))

    async def fake_build_components_async(cfg):
        return {}

    async def fake_close_components(components):
        return None

    monkeypatch.setattr(
        cli_module.agent_module, "_build_components_async", fake_build_components_async
    )
    monkeypatch.setattr(
        cli_module.agent_module, "_close_components", fake_close_components
    )
    monkeypatch.setattr(cli_module, "_build_gateway_channels", lambda cfg: [])

    cli_module.gateway(name="prod")

    assert shared.AGENT_HOME == tmp_path / ".agent-prod"
    assert agent_module.TASKS_DIR == tmp_path / ".agent-prod" / "tasks"
    assert (tmp_path / ".agent-prod" / ".owner.lock").exists()
