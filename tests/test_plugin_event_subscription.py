"""A plugin subscribes to an event domain, and unloading unsubscribes it.

The gap this closes: the turn loop and the model-call path now broadcast to
``TURN_EVENTS`` and ``LLM_EVENTS``, and **nothing could subscribe to them**.  The
catalog wired only the two tool event types, so the new domains were a broadcast
with no reachable listener -- the same "a table nothing reads" problem this
refactor keeps finding, one layer up.

Why ``ctx.on(...)`` rather than more method names
-------------------------------------------------
The catalog already supports three conventions for a plugin to attach behaviour:
``setup``/``teardown``, the hook methods (``on_turn_end``, ``on_pre_tool``, ...),
and the ``ctx`` registration surface.  The hook methods are the oldest and the
only ones that require the catalog to know a fixed mapping from domain to method
name -- so extending them to two more domains would mean two more entries in that
mapping and a fourth convention to document.

``ctx.on(domain, handler)`` reuses the registration surface that already exists:
the same undo mechanism as ``ctx.provide`` and ``ctx.add_tool``, which is what
makes unloading a plugin remove every effect it had.  A subscription is an effect.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from agent.core.event_domains import (
    LLM,
    LLMResponseParsed,
    TURN,
    TurnFinished,
    TurnStarted,
)
from agent.core.events import EventBus, LLM_EVENTS, TURN_EVENTS


SUBSCRIBER = '''
    def register():
        class Watcher:
            name = "watcher"

            def __init__(self):
                self.turns = []
                self.responses = []

            def setup(self, ctx):
                self.ctx = ctx
                ctx.on("turn", self.on_turn)
                ctx.on("llm", self.on_llm)

            def on_turn(self, event):
                self.turns.append(event)

            def on_llm(self, event):
                self.responses.append(event)

        return Watcher()
'''


def _write_plugin(plugin_dir: Path, code: str) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "__init__.py").write_text(textwrap.dedent(code), encoding="utf-8")


def _load(tmp_path: Path, *, bus=None):
    from agent import PluginCatalog

    root = tmp_path / "plugins"
    _write_plugin(root / "watcher", SUBSCRIBER)
    catalog = PluginCatalog(builtin_dir=root)
    if bus is not None:
        # Point every domain at a bus this test owns.
        original = catalog.resolve_event_bus
        catalog.resolve_event_bus = lambda domain: bus
    loaded = catalog.discover_and_load()
    return catalog, loaded


def _plugin(catalog, name):
    return catalog._plugins[name][0]


# ── The seam, without a plugin ───────────────────────────────────────────────


def test_the_context_can_subscribe_to_a_domain() -> None:
    """Driven through a real install so the undo path is the production one."""
    from agent.plugins.context import PluginContext, PluginInstall

    class _Cat:
        def resolve_service(self, name, default=None):
            return None

        def resolve_event_bus(self, domain):
            return TURN_EVENTS

    install = PluginInstall("p", None, None, _Cat())
    ctx = PluginContext(install)
    seen: list = []
    ctx.on("turn", lambda event: seen.append(event))

    asyncio.run(TURN_EVENTS.emit(TurnStarted("a", True, False)))
    assert len(seen) == 1


def test_the_handler_reaches_the_right_domain_bus() -> None:
    from agent.plugins.context import PluginContext, PluginInstall

    class _Cat:
        def resolve_event_bus(self, domain):
            return {"turn": TURN_EVENTS, "llm": LLM_EVENTS}.get(domain)

    install = PluginInstall("p", None, None, _Cat())
    ctx = PluginContext(install)
    turns: list = []
    ctx.on("turn", lambda event: turns.append(event))

    asyncio.run(LLM_EVENTS.emit(LLMResponseParsed("a", "end_turn")))
    asyncio.run(TURN_EVENTS.emit(TurnFinished("a", "ok", 1)))
    assert len(turns) == 1, "an llm event must not reach a turn handler"


def test_an_unknown_domain_is_refused() -> None:
    """A typo'd domain would register for nothing and look like a working plugin."""
    from agent.plugins.context import PluginContext, PluginInstall

    class _Cat:
        def resolve_event_bus(self, domain):
            from agent.core.event_domains import domain as resolve

            return {"turn": TURN_EVENTS, "llm": LLM_EVENTS}.get(resolve(domain).name)

    install = PluginInstall("p", None, None, _Cat())
    ctx = PluginContext(install)
    with pytest.raises(KeyError):
        ctx.on("turns", lambda event: None)


def test_subscribing_registers_one_undo() -> None:
    """The undo is what makes a subscription survive being forgotten by the author."""
    from agent.plugins.context import PluginContext, PluginInstall

    class _Cat:
        def resolve_event_bus(self, domain):
            return TURN_EVENTS

    class _Scope:
        def __init__(self):
            self.undos: list = []

        def on_unwind(self, handler):
            self.undos.append(handler)

    scope = _Scope()
    install = PluginInstall("p", None, None, _Cat(), scope=scope)
    PluginContext(install).on("turn", lambda event: None)
    assert len(scope.undos) == 1


# ── Through a loaded plugin ──────────────────────────────────────────────────


def test_a_plugin_receives_turn_events(tmp_path: Path) -> None:
    catalog, loaded = _load(tmp_path)
    try:
        assert loaded == ["watcher"]
        asyncio.run(TURN_EVENTS.emit(TurnStarted("a", True, False)))
        assert len(_plugin(catalog, "watcher").turns) == 1
    finally:
        catalog.close()


def test_a_plugin_receives_llm_events(tmp_path: Path) -> None:
    catalog, _ = _load(tmp_path)
    try:
        asyncio.run(LLM_EVENTS.emit(LLMResponseParsed("a", "end_turn")))
        assert len(_plugin(catalog, "watcher").responses) == 1
    finally:
        catalog.close()


def test_a_plugin_subscribes_to_both_domains(tmp_path: Path) -> None:
    catalog, _ = _load(tmp_path)
    try:
        watcher = _plugin(catalog, "watcher")
        asyncio.run(TURN_EVENTS.emit(TurnFinished("a", "ok", 1)))
        asyncio.run(LLM_EVENTS.emit(LLMResponseParsed("a", "end_turn")))
        assert len(watcher.turns) == 1
        assert len(watcher.responses) == 1
    finally:
        catalog.close()


# ── Unloading removes the subscription ───────────────────────────────────────


def test_unloading_a_plugin_stops_its_subscriptions(tmp_path: Path) -> None:
    """A listener left on a bus after its plugin is gone is called for ever.

    It would then raise into the isolation handler on every turn, printing a line
    about a plugin the user already removed.
    """
    catalog, _ = _load(tmp_path)
    try:
        watcher = _plugin(catalog, "watcher")
        catalog.unload_plugin("watcher")
        asyncio.run(TURN_EVENTS.emit(TurnStarted("a", True, False)))
        asyncio.run(LLM_EVENTS.emit(LLMResponseParsed("a", "end_turn")))
        assert watcher.turns == []
        assert watcher.responses == []
    finally:
        catalog.close()


def test_unload_removes_the_listener_from_the_bus(tmp_path: Path) -> None:
    """Asserted on a bus this test owns.

    The module-level buses are shared for the whole session, so "no listeners" is
    not a property a test can assert about them -- an earlier test's plugin would
    make it fail for reasons unrelated to this one.  A private bus is substituted
    for the domain, so the assertion is about the unsubscribe and nothing else.
    """
    owned = EventBus()
    catalog, _ = _load(tmp_path, bus=owned)
    try:
        assert owned.has_listeners(TurnStarted), "it subscribed"
        assert owned.has_listeners(TurnFinished), "to the whole domain"
        catalog.unload_plugin("watcher")
        assert not owned.has_listeners(TurnStarted)
        assert not owned.has_listeners(TurnFinished), "every type in the domain"
    finally:
        catalog.close()


def test_a_reload_does_not_double_subscribe(tmp_path: Path) -> None:
    """Reload unloads then loads; a leaked listener would double every event."""
    catalog, _ = _load(tmp_path)
    try:
        watcher = _plugin(catalog, "watcher")
        catalog.unload_plugin("watcher")
        catalog.discover_and_load()
        asyncio.run(TURN_EVENTS.emit(TurnStarted("a", True, False)))
        fresh = _plugin(catalog, "watcher")
        assert fresh is not watcher, "a new plugin object"
        assert len(fresh.turns) == 1, "delivered exactly once"
    finally:
        catalog.close()


# ── The catalog resolves buses from the domain table ─────────────────────────


def test_the_catalog_resolves_a_bus_by_domain_name() -> None:
    from agent import PluginCatalog

    catalog = PluginCatalog(builtin_dir=Path("/tmp/unused"))
    try:
        assert catalog.resolve_event_bus("turn") is TURN_EVENTS
        assert catalog.resolve_event_bus("llm") is LLM_EVENTS
    finally:
        catalog.close()


def test_resolving_an_unknown_domain_raises() -> None:
    from agent import PluginCatalog

    catalog = PluginCatalog(builtin_dir=Path("/tmp/unused"))
    try:
        with pytest.raises(KeyError):
            catalog.resolve_event_bus("nope")
    finally:
        catalog.close()


def test_the_tool_domain_resolves_to_the_tool_bus() -> None:
    from agent import PluginCatalog
    from agent.core.events import TOOL_EVENTS

    catalog = PluginCatalog(builtin_dir=Path("/tmp/unused"))
    try:
        assert catalog.resolve_event_bus("tool") is TOOL_EVENTS
    finally:
        catalog.close()
