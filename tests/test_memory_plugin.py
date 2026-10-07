"""Memory as a plugin: the composition stops assembling it above the catalog.

The chain that blocked this was an ordering one, not a size one:

    LTMStore -> EvolutionEngine -> catalog
    LTMStore -> BuiltinTools    -> catalog

Both consumers sat above the plugin catalog, so a plugin mounted by the catalog would
have provided ``memory`` after its users had already been handed the object.  Both moved
first -- the engine into its own plugin, the tools block to after the mount -- and this
is what those moves were for.
"""

from __future__ import annotations

import pathlib

import pytest

from agent.kernel.plugin import PluginHost
from agent.kernel.service import ServiceRegistry
from agent.memory.plugin import MemoryPlugin


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    """Point the store and palace at a temporary home.

    A plugin that reads ``shared`` for its directories is reading process state -- that
    is deliberate, because a session name rewrites them before a composition is built --
    and a test that did not redirect them would write into the operator's real home.
    """
    from agent import shared

    monkeypatch.setattr(shared, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(shared, "MEMORY_DIR", tmp_path / "memory")
    (tmp_path / "context").mkdir()
    (tmp_path / "memory").mkdir()
    return tmp_path


def _host(mapping: dict) -> PluginHost:
    return PluginHost(services=ServiceRegistry.adopt(mapping))


def test_it_provides_the_store_and_the_palace(paths) -> None:
    mapping = {"cfg": {}}
    host = _host(mapping)
    host.load(MemoryPlugin())

    assert host.pending() == ()
    assert type(mapping["ltm_store"]).__name__ == "LTMStore"
    assert type(mapping["memory"]).__name__ == "MemoryPalace"


def test_a_composition_without_it_has_no_memory_at_all(paths) -> None:
    """Absence is a state, which is what mounting buys over building inline."""
    mapping = {"cfg": {}}
    assert "memory" not in mapping
    assert "ltm_store" not in mapping


def test_it_declares_the_config_it_reads(paths) -> None:
    assert MemoryPlugin.inject == ("cfg",)
    assert MemoryPlugin.name == "memory"


def test_unloading_releases_both_and_closes_the_store(paths, monkeypatch) -> None:
    """The plugin opened the store, so the plugin closes it.

    Same rule as the scheduler plugin, and same reason: an earlier attempt gave the
    store's lifetime to a *service* that had merely been handed it, and the tests
    overturned it -- a caller may legitimately read a store after the service stops.

    Two details this test had to learn.  The spy is installed on the **class** before
    loading, because ``on_unwind`` records the bound method at setup and replacing the
    attribute afterwards would leave the recorded one untouched.  And it spies rather
    than using the store afterwards, because ``close`` is a *release* and not a poison:
    ``LTMStore.close`` drops its thread-local connections and the next call reopens one.
    """
    from agent.memory.store import LTMStore

    closed: list = []
    original = LTMStore.close

    def spy(self):
        closed.append(True)
        return original(self)

    monkeypatch.setattr(LTMStore, "close", spy)

    mapping = {"cfg": {}}
    host = _host(mapping)
    host.load(MemoryPlugin())

    host.unload("memory")

    assert "memory" not in mapping and "ltm_store" not in mapping
    assert closed == [True], "the scope that opened the store must close it"


def test_it_seeds_the_assistant_identity_it_was_configured_with(paths) -> None:
    """A composition writing down what the config declares about the assistant.

    Not memory internals -- which is why it lives in the plugin rather than in the
    store's module.
    """
    mapping = {"cfg": {"assistant_identity": {"name": "Ada", "role": "Helper"}}}
    host = _host(mapping)
    host.load(MemoryPlugin())

    store = mapping["ltm_store"]
    values = {fact.value for fact in store.read_fact_assertions()}
    assert {"Ada", "Helper"} <= values


def test_an_absent_identity_seeds_nothing(paths) -> None:
    """Control: the seeding is conditional on the config, not unconditional."""
    mapping = {"cfg": {}}
    host = _host(mapping)
    host.load(MemoryPlugin())
    assert mapping["ltm_store"].read_fact_assertions() == []
