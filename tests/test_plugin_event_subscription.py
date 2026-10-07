"""A plugin subscribes to an event domain, and unloading unsubscribes it.

The gap this closes: the turn loop and the model-call path now broadcast to
``TURN_EVENTS`` and ``LLM_EVENTS``, and **nothing could subscribe to them**.  The
catalog wired only the two tool event types, so the new domains were a broadcast
with no reachable listener -- the same "a table nothing reads" problem this
refactor keeps finding, one layer up.

Why the event *type* is the channel
-----------------------------------
``ctx.on(TurnStarted, handler)`` rather than ``ctx.on("turn", handler)``.  A domain
name is a string that has to be agreed on in two places and is checked nowhere, so
a typo registers a listener that is never called -- indistinguishable from a plugin
that works.  A type cannot be typo'd into validity.

A whole domain is still reachable, because a domain is a set of types:
``TURN.subscribe(ctx.bus, handler)``.  The kernel does not offer
``ctx.on_domain("turn", ...)`` because it would have to know what a turn is; the
domain table owns that vocabulary (``agent/core/event_domains.py``).

Either way a subscription is an **effect**: the same undo mechanism as
``ctx.provide``, which is what makes unloading a plugin remove everything it did.
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
    from agent.core.event_domains import LLM, TURN

    def register():
        class Watcher:
            name = "watcher"

            def __init__(self):
                self.turns = []
                self.responses = []

            def setup(self, ctx):
                self.ctx = ctx
                # A domain is a set of event types, and the domain table owns that
                # vocabulary -- so subscribing to the whole turn domain is one call
                # to the table with the context's bus, and one undo registered on
                # this plugin's lifetime.
                TURN.attach(ctx, self.on_turn)
                LLM.attach(ctx, self.on_llm)

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
    # The bus is constructor-injected and the host shares it, so ``ctx.bus`` is the
    # bus this test owns.  Monkeypatching a resolver would no longer reach the
    # plugin, which subscribes through its context.
    catalog = PluginCatalog(builtin_dir=root, event_bus=bus)
    loaded = catalog.discover_and_load()
    return catalog, loaded


def _plugin(catalog, name):
    return catalog._plugins[name][0]


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
    """Asserted as a *difference*, because the bus is shared for the session.

    ``"no listeners"`` is not a property a test can assert about a module-level bus
    -- another test's plugin would make it fail for unrelated reasons, and asserting
    it would also stop being true the moment a listener was added elsewhere.  The
    count before and after this plugin's life is the honest question, and it still
    catches a leaked listener (the count would not come back down).

    On ``TURN_EVENTS`` rather than a substitute, because ``TURN.attach`` resolves
    the bus through the domain table: a private bus would test a path production
    does not take.
    """
    before = {
        event_type: len(TURN_EVENTS.listeners(event_type))
        for event_type in (TurnStarted, TurnFinished)
    }
    catalog, _ = _load(tmp_path)
    try:
        assert len(TURN_EVENTS.listeners(TurnStarted)) > before[TurnStarted], (
            "it subscribed to TurnStarted"
        )
        assert len(TURN_EVENTS.listeners(TurnFinished)) > before[TurnFinished], (
            "and to the rest of the domain"
        )

        catalog.unload_plugin("watcher")

        for event_type, count in before.items():
            assert len(TURN_EVENTS.listeners(event_type)) == count, (
                f"{event_type.__name__} still has a listener from the unloaded plugin"
            )
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


# ── The domain table owns "which bus" ────────────────────────────────────────
#
# The catalog used to answer ``resolve_event_bus("turn")``, which meant the mapping
# from a domain to its bus lived in the plugin manager.  It lives in the domain table
# now (``agent/core/event_domains.py``), next to the types it maps, and
# ``Domain.attach`` is the one call a plugin makes.  The mapping's own tests moved
# there with it -- a wrong answer here produces a listener that is never called,
# which is indistinguishable from a plugin that works.


def test_attach_subscribes_on_the_domains_own_bus(tmp_path: Path) -> None:
    """Not on the context's bus.

    ``ctx.bus`` is a single bus for events the composition itself defines; the turn
    and llm domains publish on their own.  Subscribing a turn handler on the
    context's bus would register for events that never arrive there.
    """
    catalog, _ = _load(tmp_path)
    try:
        watcher = _plugin(catalog, "watcher")
        asyncio.run(TURN_EVENTS.emit(TurnStarted("a", True, False)))
        asyncio.run(LLM_EVENTS.emit(LLMResponseParsed("a", "end_turn")))
        assert len(watcher.turns) == 1, "it was subscribed to the turn bus"
        assert len(watcher.responses) == 1, "and to the llm bus"
    finally:
        catalog.close()


def test_attach_returns_the_undo_it_registered(tmp_path: Path) -> None:
    """The undo is both returned and bound to the plugin's lifetime, so a caller can
    reverse a subscription early without losing the unload behaviour."""
    catalog, _ = _load(tmp_path)
    try:
        watcher = _plugin(catalog, "watcher")
        before = len(TURN_EVENTS.listeners(TurnStarted))
        undo = TURN.attach(watcher.ctx, lambda event: None)
        assert len(TURN_EVENTS.listeners(TurnStarted)) == before + 1

        undo()
        assert len(TURN_EVENTS.listeners(TurnStarted)) == before

        # And the plugin's own subscription is still there: reversing this one did
        # not take the plugin's registrations with it.
        asyncio.run(TURN_EVENTS.emit(TurnStarted("a", True, False)))
        assert len(watcher.turns) == 1
    finally:
        catalog.close()
