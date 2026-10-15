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
    TurnCompleted,
    TurnFinished,
    TurnStarted,
)
from agent.core.events import EventBus, LLM_EVENTS, TURN_EVENTS


SUBSCRIBER = '''
    from agent.core.event_domains import LLM, TURN

    def register():
        class Watcher:
            name = "watcher"
            # ``TURN.attach`` / ``LLM.attach`` resolve each domain's bus through
            # ``ctx.get("event_bus.<domain>", None)``, and a composition need not
            # provide one -- the domain table falls back to the process bus -- so
            # both reads are declared ``optional`` rather than gating activation.
            optional = ("event_bus.turn", "event_bus.llm")

            def __init__(self):
                self.turns = []
                self.responses = []
                self.completed = []

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
                # Content events go on their own list: `turns` counts every turn-domain
                # event, and an assertion about content must not pass on an outcome.
                if type(event).__name__ == "TurnCompleted":
                    self.completed.append(event)

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


ONE_TYPE = '''
    from agent.core.event_domains import TurnCompleted

    def register():
        class OneType:
            name = "one-type"
            # ``ctx.on(TurnCompleted, ...)`` routes through ``bus_for_type`` ->
            # ``ctx.get("event_bus.turn", None)``: a read of the turn bus, which a
            # composition may legitimately not provide, hence ``optional``.
            optional = ("event_bus.turn",)

            def __init__(self):
                self.seen = []

            def setup(self, ctx):
                # One event class, the obvious call.  It has to be placed on the bus
                # that owns the type -- which the composition, not the plugin, knows.
                ctx.on(TurnCompleted, self.seen.append)

        return OneType()
'''


def test_a_single_type_subscribed_through_the_context_is_delivered(tmp_path: Path) -> None:
    """The call shape a plugin author reaches for first, against the real catalog.

    Not a unit test of ``Context.on``: ``_load`` builds a real ``PluginCatalog``, which is
    the module that wires ``event_bus_lookup``.  So this fails if the wiring is missing or
    points somewhere else -- the whole difference between "the API accepts it" and "the
    listener is called".
    """
    from agent import PluginCatalog

    root = tmp_path / "plugins"
    _write_plugin(root / "one-type", ONE_TYPE)
    catalog = PluginCatalog(builtin_dir=root)
    try:
        loaded = catalog.discover_and_load()
        assert loaded == ["one-type"]
        asyncio.run(TURN_EVENTS.emit(TurnCompleted("a", "hi", "yo")))
        assert len(_plugin(catalog, "one-type").seen) == 1
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
# ``Domain.attach`` / ``ctx.on`` both ask it -- the set form and the single-type form,
# one answer.  The mapping's own tests moved there with it -- a wrong answer here
# produces a listener that is never called, which is indistinguishable from a plugin
# that works.


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


def test_a_plugin_can_read_what_a_turn_was(tmp_path: Path) -> None:
    """The content event, and why ``TurnFinished`` could not serve.

    Measured while trying to migrate ``EvolutionPlugin`` off the hook protocol: it reads
    the user's input against the agent's answer to learn from corrections, and the hook's
    ``TurnEvent`` carries exactly that.  The kernel's turn vocabulary had ``TurnFinished``
    -- ``status`` and ``steps``, an outcome for a log -- and no content event at all, so
    the migration was blocked on **vocabulary** rather than on mechanism.
    """
    catalog, loaded = _load(tmp_path)
    try:
        assert loaded == ["watcher"]
        asyncio.run(
            TURN_EVENTS.emit(
                TurnCompleted(
                    agent_id="a",
                    user_input="no, use the other flag",
                    response="done",
                    tool_calls=("run_tests",),
                )
            )
        )
        watcher = _plugin(catalog, "watcher")
        assert len(watcher.completed) == 1
        assert watcher.completed[0].user_input == "no, use the other flag"
        assert watcher.completed[0].response == "done"
        assert watcher.completed[0].tool_calls == ("run_tests",)
    finally:
        catalog.close()


def test_the_content_event_is_on_the_turn_domain() -> None:
    """A plugin subscribing to the turn domain gets it, which is how a migration does."""
    from agent.core.event_domains import TURN, TurnCompleted

    assert TurnCompleted in TURN.event_types


def test_the_emit_helper_constructs_nothing_without_a_listener() -> None:
    """The guard on the hot path, asserted for this event too.

    ``emit_domain_event`` returns before constructing the event when nobody is
    listening, which is what keeps a turn from paying for a feature nobody mounted.
    """
    from agent.core.turn_context import emit_domain_event

    bus = EventBus()
    asyncio.run(
        emit_domain_event(
            bus, TurnCompleted, agent_id="a", user_input="", response=""
        )
    )
    assert bus.has_listeners(TurnCompleted) is False


def test_the_content_event_is_emitted_before_the_request_is_cleared() -> None:
    """The ordering is the whole correctness of ``user_input``, and it fails silently.

    ``user_input`` is read from the turn's request in the context metadata, and two
    statements later that key is cleared so nothing can claim to be the request of a
    finished turn.  Move the emit after the pop and every turn reports an empty prompt --
    no error, no missing field, just a plugin that never learns anything.  So the order
    is asserted rather than left to the comment beside it.
    """
    import ast as _ast

    source = (Path(__file__).resolve().parents[1] / "agent/core/turn_loop.py").read_text(
        encoding="utf-8"
    )
    tree = _ast.parse(source)

    emit_line = None
    pop_line = None
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            func = node.func
            name = func.id if isinstance(func, _ast.Name) else getattr(func, "attr", "")
            if name == "emit_domain_event" and any(
                isinstance(arg, _ast.Name) and arg.id == "TurnCompleted" for arg in node.args
            ):
                emit_line = node.lineno
            if name == "pop" and node.args:
                first = node.args[0]
                if isinstance(first, _ast.Name) and first.id == "TURN_REQUEST_KEY":
                    pop_line = node.lineno

    assert emit_line is not None, "TurnCompleted must be emitted from the loop"
    assert pop_line is not None, "the turn request must still be cleared"
    assert emit_line < pop_line, (
        f"TurnCompleted is emitted at line {emit_line}, after the turn's request is "
        f"cleared at line {pop_line}: every turn would report an empty prompt"
    )


def test_a_plugin_can_score_a_session_when_it_ends() -> None:
    """The session domain, added because a real plugin needed vocabulary that was missing.

    ``EvolutionPlugin`` scores a session at its end -- it reads the transcript and the
    tools used -- and the domain table covered a *turn*: tools, the loop, the model.  A
    turn finishes many times per session; a session finishes once, and nothing announced
    it.

    Emitted from ``PluginCatalog.fire_session_end``, which is the single place both
    protocols are notified.  The two call sites of that method (``cli`` and the channel
    layer) would each have to remember to emit, and the day one forgot would be a plugin
    that silently stops scoring sessions.
    """
    from agent.core.event_domains import SESSION, SessionFinished
    from agent.core.events import SESSION_EVENTS

    seen: list = []
    unsub = SESSION.subscribe(SESSION_EVENTS, lambda event: seen.append(event))
    try:
        asyncio.run(
            SESSION_EVENTS.emit(
                SessionFinished(
                    agent_id="a",
                    messages=[{"role": "user", "content": "hi"}],
                    tools_used=["run_tests"],
                    session_id="s1",
                    turn_count=3,
                )
            )
        )
    finally:
        unsub()

    assert len(seen) == 1
    assert seen[0].messages == [{"role": "user", "content": "hi"}]
    assert seen[0].tools_used == ["run_tests"]
    assert seen[0].turn_count == 3


def test_the_session_event_is_emitted_from_the_single_dispatch_point() -> None:
    """Asserted at the source, because the alternative is two emitters.

    ``fire_session_end`` is where hooks are dispatched.  Emitting the typed event at its
    callers instead would construct the same fact in two places, which is the shape that
    drifts.

    And emitted on the bus the *subscriber* resolves.  This test used to require
    ``SESSION_EVENTS.emit`` literally -- which pinned the process-wide bus, and so pinned
    the miss: a composition provides its own session bus, ``SESSION.attach`` resolves that
    one, and the event went where nobody was listening.  The receiver is now asserted to be
    ``self._bus_for("session")``, which is one statement further and is the property that
    actually has to hold.
    """
    import ast as _ast

    source = (Path(__file__).resolve().parents[1] / "agent/plugins/catalog.py").read_text(
        encoding="utf-8"
    )
    tree = _ast.parse(source)
    fire = next(
        node
        for node in _ast.walk(tree)
        if isinstance(node, _ast.AsyncFunctionDef) and node.name == "fire_session_end"
    )
    emits = [
        node
        for node in _ast.walk(fire)
        if isinstance(node, _ast.Call) and getattr(node.func, "attr", "") == "emit"
    ]
    assert len(emits) == 1, "fire_session_end must emit exactly one session event"

    receiver = emits[0].func.value
    assert isinstance(receiver, _ast.Call), (
        "the bus must be resolved, not taken from the module: "
        f"got {_ast.dump(receiver)[:80]}"
    )
    assert getattr(receiver.func, "attr", "") == "_bus_for", (
        "the session event must go to the composition's session bus"
    )
    assert [arg.value for arg in receiver.args] == ["session"]
