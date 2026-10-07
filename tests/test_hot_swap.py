"""Hot-swapping a real plugin, at the composition level rather than with doubles.

The kernel's own tests use small fakes.  This one uses the plugins the composition actually
mounts, because the property that matters for "replaceable and hot-swappable" is not that
`load(replace=True)` restores a name -- it is that a **running composition** survives
swapping one of its providers: the dependents come back, the replacement's services are
live, and the retired plugin's resources are released rather than leaked.

Both halves are asserted.  Restoring the dependents without closing the old store would be
a leak per swap; closing the store without restoring the dependents is the deletion this
test exists because of.
"""

from __future__ import annotations

import pathlib

import pytest

from agent.kernel.plugin import PluginHost
from agent.kernel.service import ServiceRegistry


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A temporary agent home, so nothing here touches the operator's."""
    from agent import shared

    monkeypatch.setattr(shared, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(shared, "MEMORY_DIR", tmp_path / "memory")
    (tmp_path / "context").mkdir()
    (tmp_path / "memory").mkdir()
    return tmp_path


class _ReplacementMemory:
    """A different implementation of the same services, as a plugin would be."""

    name = "memory"
    inject = ("cfg",)

    def __init__(self) -> None:
        self.store = object()
        self.palace = object()
        self.closed = False

    def setup(self, ctx) -> None:
        ctx.on_unwind(lambda: setattr(self, "closed", True))
        ctx.provide("ltm_store", self.store)
        ctx.provide("memory", self.palace)


def test_a_running_composition_survives_swapping_its_store_provider(home) -> None:
    from agent.memory.plugin import MemoryPlugin
    from agent.memory.session_plugin import SessionPlugin

    mapping = {"cfg": {}, "workspace_root": home}
    host = PluginHost(services=ServiceRegistry.adopt(mapping))
    host.load(MemoryPlugin())
    host.load(SessionPlugin())

    original = mapping["ltm_store"]
    assert "context_manager" in mapping

    closed: list = []
    original_close = original.close
    original.close = lambda: (closed.append(True), original_close())[1]

    replacement = _ReplacementMemory()
    host.load(replacement, replace=True)

    assert "session" in host.loaded(), "the session plugin must come back"
    assert mapping["ltm_store"] is replacement.store, "the replacement's service is live"
    assert "context_manager" in mapping, "and the dependent re-provided its service"
    assert replacement.closed is False, "the replacement is live, not unwound"


def test_the_retired_plugins_resources_are_released(home, monkeypatch) -> None:
    """The other half: a swap must not leak the outgoing plugin's resources.

    Spied on the class, before loading: ``on_unwind`` records the bound method at setup, so
    replacing the attribute afterwards would leave the recorded one untouched.
    """
    from agent.memory.plugin import MemoryPlugin
    from agent.memory.session_plugin import SessionPlugin
    from agent.memory.store import LTMStore

    closed: list = []
    original = LTMStore.close
    monkeypatch.setattr(
        LTMStore, "close", lambda self: (closed.append(True), original(self))[1]
    )

    mapping = {"cfg": {}, "workspace_root": home}
    host = PluginHost(services=ServiceRegistry.adopt(mapping))
    host.load(MemoryPlugin())
    host.load(SessionPlugin())

    host.load(_ReplacementMemory(), replace=True)

    assert closed, "the retired store must be closed by the scope that opened it"


def test_swapping_back_is_just_another_swap(home) -> None:
    """Replacement is not a one-way door, which is what makes it hot-pluggable.

    A second swap must leave the composition in the same shape as the first -- no
    accumulated subscriptions, no stranded names, one provider.
    """
    from agent.memory.plugin import MemoryPlugin
    from agent.memory.session_plugin import SessionPlugin

    mapping = {"cfg": {}, "workspace_root": home}
    host = PluginHost(services=ServiceRegistry.adopt(mapping))
    host.load(MemoryPlugin())
    host.load(SessionPlugin())

    first = _ReplacementMemory()
    host.load(first, replace=True)
    second = _ReplacementMemory()
    host.load(second, replace=True)

    assert host.loaded() == ("memory", "session"), host.loaded()
    assert host.pending() == ()
    assert first.closed is True, "the first replacement is unwound"
    assert second.closed is False
    assert mapping["ltm_store"] is second.store


def test_a_composition_that_provides_nothing_leaves_the_dependents_waiting(home) -> None:
    """The honest failure, at composition level: visible and named."""

    class _Incomplete:
        name = "memory"
        inject = ("cfg",)

        def setup(self, ctx) -> None:
            ctx.provide("memory", object())  # no ltm_store

    from agent.memory.plugin import MemoryPlugin
    from agent.memory.session_plugin import SessionPlugin

    mapping = {"cfg": {}, "workspace_root": home}
    host = PluginHost(services=ServiceRegistry.adopt(mapping))
    host.load(MemoryPlugin())
    host.load(SessionPlugin())

    host.load(_Incomplete(), replace=True)

    assert host.pending() == ("session",)
    assert host.waiting_for() == {"session": ("ltm_store",)}
    assert "context_manager" not in mapping
