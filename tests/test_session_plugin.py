"""The session log as a plugin: ``context_manager`` becomes a service like the others."""

from __future__ import annotations

import pytest

from agent.kernel.plugin import PluginHost
from agent.kernel.service import ServiceRegistry
from agent.memory.session_plugin import SessionPlugin


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """A host with what the plugin declares, and a temporary home for its store."""
    from agent import shared
    from agent.memory.plugin import MemoryPlugin

    monkeypatch.setattr(shared, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(shared, "MEMORY_DIR", tmp_path / "memory")
    (tmp_path / "context").mkdir()
    (tmp_path / "memory").mkdir()

    mapping = {"cfg": {}, "workspace_root": tmp_path}
    host = PluginHost(services=ServiceRegistry.adopt(mapping))
    host.load(MemoryPlugin())
    return host, mapping


def test_it_provides_the_context_manager(wired) -> None:
    host, mapping = wired
    host.load(SessionPlugin())

    assert host.pending() == ()
    assert type(mapping["context_manager"]).__name__ == "ContextManager"


def test_it_waits_for_the_store_it_reads(wired) -> None:
    """Loaded first, it stays pending rather than building against nothing.

    ``ltm_store`` comes from ``MemoryPlugin``; this is the dependency that made the two a
    pair, and the reason the composition mounts memory first.
    """
    host, mapping = wired
    host.unload("memory")

    host.load(SessionPlugin())

    assert host.pending() == ("session",)
    assert "context_manager" not in mapping


def test_a_composition_without_it_has_no_context_manager(wired) -> None:
    _, mapping = wired
    assert "context_manager" not in mapping


def test_it_declares_what_it_takes(wired) -> None:
    assert set(SessionPlugin.inject) == {"ltm_store", "workspace_root", "cfg"}
    assert SessionPlugin.name == "session"


def test_the_memory_scope_follows_the_workspace(tmp_path) -> None:
    """Two workspaces get two scopes, and the scope is not the path.

    The digest is what travels into stored facts; a raw path would carry a user name and
    a directory layout with it.
    """
    from agent.pathing import project_memory_scope

    first = project_memory_scope(tmp_path)
    second = project_memory_scope(tmp_path / "other")
    assert first != second
    assert first.startswith("project:")
    assert str(tmp_path) not in first
