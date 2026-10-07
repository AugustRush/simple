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


# ── hot reload: changed code on disk replaces the live plugin ────────────────


def _write_hotplug(root: pathlib.Path, value: str) -> None:
    """A plugin whose whole behaviour is one provided value, so a swap is visible."""
    import json

    plugin = root / "hotplug"
    plugin.mkdir(exist_ok=True)
    (plugin / "plugin.json").write_text(json.dumps({"name": "hotplug"}), encoding="utf-8")
    (plugin / "__init__.py").write_text(
        "def register():\n"
        "    class P:\n"
        "        name = 'hotplug'\n"
        "        inject = ('vault',)\n"
        "        def setup(self, ctx):\n"
        f"            ctx.provide('greeting', {value!r})\n"
        "    return P()\n",
        encoding="utf-8",
    )


def test_reloading_picks_up_a_same_length_edit(tmp_path) -> None:
    """The hard case, and the one that was silently broken.

    Python validates a ``.pyc`` against the source's **mtime and size**.  A plugin edited
    from ``'v1'`` to ``'v2'`` -- same size, same clock tick, which is what a script or a
    test does -- therefore kept its **old bytecode**: the reload re-imported, re-ran setup,
    re-provided the service, and provided the old value.  Nothing errored.

    Measured: ``'v1' -> 'v2'`` gave ``v1``; ``'v1' -> 'v2-longer'`` gave the new value.  The
    difference was not the reload, it was the bytecode cache, so this test uses the case that
    used to fail rather than the one that happened to work.
    """
    import asyncio

    from agent import PluginCatalog

    _write_hotplug(tmp_path, "v1")
    mapping: dict = {"vault": object()}
    catalog = PluginCatalog(builtin_dir=tmp_path, components=mapping)
    catalog.discover_and_load()
    assert mapping["greeting"] == "v1"

    _write_hotplug(tmp_path, "v2")
    asyncio.run(catalog.reload(mapping))

    assert mapping["greeting"] == "v2", "the changed code must be what is live"
    assert len(catalog._installs) == 1, (
        f"exactly one install must remain, found {sorted(catalog._installs)}"
    )


def test_a_plugin_is_not_stacked_by_a_reload(tmp_path) -> None:
    """Control: the reload replaces, it does not add a second copy of the plugin."""
    import asyncio

    from agent import PluginCatalog

    _write_hotplug(tmp_path, "v1")
    mapping: dict = {"vault": object()}
    catalog = PluginCatalog(builtin_dir=tmp_path, components=mapping)
    catalog.discover_and_load()
    first = catalog._installs["hotplug"].plugin

    _write_hotplug(tmp_path, "v2-longer")
    asyncio.run(catalog.reload(mapping))

    second = catalog._installs["hotplug"].plugin
    assert first is not second, "a reload builds a new object"
    assert len(catalog._installs) == 1


def test_a_reloaded_plugin_that_stops_providing_removes_its_service(tmp_path) -> None:
    """The other half of a swap: what the old version registered must be gone.

    Without this, a reload that changed what a plugin provides would leave the previous
    version's services behind, and "which implementation is live" would depend on how many
    times the file had been saved.
    """
    import asyncio

    from agent import PluginCatalog

    _write_hotplug(tmp_path, "v1")
    mapping: dict = {"vault": object()}
    catalog = PluginCatalog(builtin_dir=tmp_path, components=mapping)
    catalog.discover_and_load()
    assert mapping["greeting"] == "v1"

    plugin = tmp_path / "hotplug"
    (plugin / "__init__.py").write_text(
        "def register():\n"
        "    class P:\n"
        "        name = 'hotplug'\n"
        "        inject = ('vault',)\n"
        "        def setup(self, ctx):\n"
        "            ctx.provide('farewell', 'bye')\n"
        "    return P()\n",
        encoding="utf-8",
    )
    asyncio.run(catalog.reload(mapping))

    assert mapping.get("farewell") == "bye"
    assert "greeting" not in mapping, (
        "the service the previous version provided must not survive the reload"
    )
