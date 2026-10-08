"""The domain buses belong to a composition, not to the import graph.

They were module-level singletons, with a real justification in the code: "the executor is
built in several places (the turn loop, the scheduler, tests) and a per-instance bus would
mean a plugin's gate applied to whichever construction path happened to pass one".  That is
an argument for **one bus per composition** -- and the globals were one per *process*, which
is a different and worse thing.

The cost was measured in this suite: a leaked `PreToolUse: deny` gate from one test won
another test's `emit_blocking`, which is a listener from one composition deciding another
composition's behaviour.

Both halves have to move together.  Making `Domain.attach` resolve a composition bus while
the emitter kept publishing to the global would produce the failure this codebase warns about
most: a listener that is never called, indistinguishable from a plugin that works.
"""

from __future__ import annotations

import pytest

from agent.core.events import bus_name, default_buses
from agent.kernel.plugin import PluginHost
from agent.kernel.service import ServiceRegistry


def _host_with_buses() -> tuple[PluginHost, dict]:
    from agent.kernel.context import Context

    mapping = dict({bus_name(n): b for n, b in default_buses().items()}, cfg={})
    return PluginHost(services=ServiceRegistry.adopt(mapping)), mapping


def _domain(name: str):
    from agent.core.event_domains import DOMAINS

    return DOMAINS[name]


def test_two_compositions_do_not_share_a_tool_bus() -> None:
    """The measured harm: one composition's listener set deciding another's call.

    Each composition gets its **own** listener and its own record.  Sharing one listener
    was the first version of this test, and it made the negative half assert the opposite
    of what it meant: composition B's bus *should* reach composition B's listener, so the
    only thing that proves isolation is that neither listener ever sees the other's event.
    """
    import asyncio

    from agent.core.events import ToolPreExecute

    def subscriber(name: str):
        seen: list = []

        def listener(event) -> None:
            seen.append(event)

        class _Subscriber:
            pass

        _Subscriber.name = name

        def setup(ctx):
            _domain("tool").attach(ctx, listener)

        _Subscriber.setup = staticmethod(setup)
        return _Subscriber(), seen

    first, first_map = _host_with_buses()
    second, second_map = _host_with_buses()
    plugin_a, seen_a = subscriber("subscriber_a")
    plugin_b, seen_b = subscriber("subscriber_b")
    first.load(plugin_a)
    second.load(plugin_b)

    assert first_map[bus_name("tool")] is not second_map[bus_name("tool")], (
        "each composition must have its own bus, or the rest of this proves nothing"
    )

    asyncio.run(first_map[bus_name("tool")].emit(ToolPreExecute("read_file", {})))

    assert len(seen_a) == 1, "the composition's own bus must reach its own listener"
    assert seen_b == [], "and must not reach a listener of a different composition"

    asyncio.run(second_map[bus_name("tool")].emit(ToolPreExecute("read_file", {})))
    assert len(seen_b) == 1
    assert len(seen_a) == 1, "and the traffic does not flow back"


def test_a_composition_without_its_own_bus_falls_back_to_the_process_default() -> None:
    """Control: an embedder that provides nothing keeps the behaviour it had.

    Defaulted rather than required because a bus is only *observable*: an agent built
    directly by a test has nobody to publish to, and demanding one would be ceremony.
    """
    from agent.core.events import TOOL_EVENTS
    from agent.core.event_domains import bus_for

    assert bus_for(_domain("tool"), None) is TOOL_EVENTS


def test_the_agent_takes_the_buses_it_is_given() -> None:
    """The emitting half, which is why both had to move together.

    ``BaseAgent`` hardcoded ``TURN_EVENTS`` and ``LLM_EVENTS`` in ``__init__`` and passed
    ``TOOL_EVENTS`` at its call site, so resolving only the subscribing half would have
    subscribed to a bus nothing publishes to.
    """
    import ast
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1] / "agent/core/agent.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    agent_cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BaseAgent"
    )
    init = next(
        node
        for node in agent_cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    params = {a.arg for a in init.args.args} | {a.arg for a in init.args.kwonlyargs}
    for name in ("event_bus", "llm_event_bus", "tool_event_bus"):
        assert name in params, f"BaseAgent must accept {name}"

    # And no hardcoded singleton is still passed at a call site inside the class.
    hardcoded = [
        node
        for node in ast.walk(agent_cls)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "event_bus"
        and isinstance(kw.value, ast.Name)
        and kw.value.id in {"TOOL_EVENTS", "TURN_EVENTS", "LLM_EVENTS", "SESSION_EVENTS"}
    ]
    assert not hardcoded, "the buses must come from the instance, not from the module"


def _hooked_plugin_dir(tmp_path, reason: str = "not on my watch"):
    """A plugin whose ``hooks.json`` denies every ``Bash`` (our ``shell``) call."""
    import json

    plugin_dir = tmp_path / "hooked"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "hooked"}', encoding="utf-8"
    )
    (plugin_dir / "hooks").mkdir()
    denial = json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )
    (plugin_dir / "hooks" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"printf '%s' {json.dumps(denial)}",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_the_command_hook_bridge_listens_on_the_composition_bus(tmp_path) -> None:
    """The third half, and the one that was silently left on the global.

    Two halves were moved together last round -- ``Domain.attach`` resolving a
    composition bus, and the agent publishing to the one it was given -- and a third
    listener was missed: the catalog's ``hooks.json`` bridge, subscribed at construction
    from a default of ``TOOL_EVENTS``.  ``bootstrap`` constructs the catalog **without**
    an ``event_bus`` and *with* the component mapping that already contains the
    composition's buses, so in production the bridge listened on the process bus while
    the executor published on the composition's.

    That is the failure this file is about, one listener further out: a gate that is
    registered, reported as loaded, and never called.  Every existing test of the bridge
    calls ``catalog.fire_pre_tool`` directly, so none of them could see it.
    """
    import asyncio

    from agent import PluginCatalog
    from agent.core.events import ToolPreExecute, bus_name, default_buses

    buses = default_buses()
    catalog = PluginCatalog(
        builtin_dir=_hooked_plugin_dir(tmp_path),
        components={bus_name(name): bus for name, bus in buses.items()},
    )
    try:
        catalog.discover_and_load()

        # The executor in a composition publishes here, so this is where the bridge has
        # to be.  A hook that never fires returns None, which reads as "no opinion".
        reason = asyncio.run(
            buses["tool"].emit_blocking(ToolPreExecute("shell", {"command": "ls"}))
        )
        assert reason == "not on my watch", (
            "the command-hook bridge must listen on the bus the composition provides; "
            f"got {reason!r} (a `None` here is a gate that never fired)"
        )
    finally:
        catalog.close()


def test_a_catalog_without_a_composition_still_bridges_on_the_process_bus(tmp_path) -> None:
    """Control: the embedder that provides no buses keeps the behaviour it had.

    Resolving from the composition must not turn "no composition" into "no bridge" --
    that would replace a silent miss with a loud one in a different place.
    """
    import asyncio

    from agent import PluginCatalog
    from agent.core.events import TOOL_EVENTS, ToolPreExecute

    catalog = PluginCatalog(builtin_dir=_hooked_plugin_dir(tmp_path))
    try:
        catalog.discover_and_load()
        reason = asyncio.run(
            TOOL_EVENTS.emit_blocking(ToolPreExecute("shell", {"command": "ls"}))
        )
        assert reason == "not on my watch"
    finally:
        catalog.close()


def test_session_finished_reaches_a_plugin_subscribed_through_the_domain(tmp_path) -> None:
    """The same miss, on the session domain, asserted end to end.

    ``fire_session_end`` is documented as "the only place the typed event is published",
    and it published on the process bus while ``SESSION.attach`` -- which the one real
    subscriber uses -- resolves the composition's.  So the event was constructed,
    emitted and heard by nobody, and what it carries is the transcript a plugin scores.
    No test covered it because the two existing callers of ``fire_session_end`` are a CLI
    loop and a channel.
    """
    import asyncio

    from agent import PluginCatalog
    from agent.core.event_domains import SESSION
    from agent.core.events import bus_name, default_buses
    from agent.plugins.catalog import SessionEvent

    class _Scorer:
        name = "scorer"

        def __init__(self) -> None:
            self.seen: list = []

        def setup(self, ctx):
            SESSION.attach(ctx, self.seen.append)

    buses = default_buses()
    catalog = PluginCatalog(
        builtin_dir=tmp_path,
        components={bus_name(name): bus for name, bus in buses.items()},
    )
    scorer = _Scorer()
    try:
        catalog._host.load(scorer)
        asyncio.run(
            catalog.fire_session_end(
                SessionEvent(
                    messages=[{"role": "user", "content": "hi"}],
                    tools_used=["shell"],
                    session_id="s-1",
                    turn_count=2,
                )
            )
        )
        assert len(scorer.seen) == 1, "the session's end must reach a domain subscriber"
        assert scorer.seen[0].session_id == "s-1"
        assert scorer.seen[0].turn_count == 2
    finally:
        catalog.close()
