"""The executor emits events; it does not hold the plugin catalog.

``RegularToolExecutor(self.registry, plugin_catalog=self.plugin_catalog)``
(``agent/core/agent.py:2741-2744``) was a hard dependency from the tool
execution layer onto the plugin machinery, and it was where the two per-tool
hooks fired (``agent/tools/executor.py:413``, ``:537``). That is the smallest
honest test of typed events in this codebase: one class, two call sites.

The seam is a bus the executor publishes to. The catalog subscribes, so the
plugin behaviour is unchanged while the executor stops knowing that plugins
exist.

Two existing semantics are preserved exactly rather than tidied, because each
is load-bearing and neither was documented as intentional:

1. ``on_pre_tool`` **short-circuits on the first block** and discards every
   later listener's result (``agent/plugins/catalog.py:1819-1849``).
2. ``on_post_tool``'s result is computed and then **discarded** at the call
   site (``executor.py:538``); the hook is observational by contract.

Fixing either here would be a behaviour change smuggled in under a refactor.
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path

import pytest

from agent.core.events import EventBus, ToolPostExecute, ToolPreExecute
from agent.tools.executor import RegularToolExecutor
from agent.tools.runtime import ToolRegistry


# ── The event types ──────────────────────────────────────────────────────────


def test_the_pre_event_carries_what_a_gate_needs() -> None:
    event = ToolPreExecute(tool_name="shell", arguments={"command": "rm -rf /"})
    assert event.tool_name == "shell"
    assert event.arguments["command"] == "rm -rf /"


def test_the_post_event_carries_the_result() -> None:
    event = ToolPostExecute(
        tool_name="read_file", arguments={"path": "a"}, result="contents"
    )
    assert event.result == "contents"


def test_events_are_immutable_in_their_identity() -> None:
    """The fields are frozen, but ``arguments`` stays the caller's dict.

    A gate must be able to inspect the exact arguments the tool will receive,
    including mutation by an earlier listener; copying would make a gate act on
    a snapshot nobody executed.
    """
    arguments = {"path": "a"}
    event = ToolPreExecute(tool_name="read_file", arguments=arguments)
    assert event.arguments is arguments
    with pytest.raises(Exception):
        event.tool_name = "other"  # type: ignore[misc]


# ── The bus ──────────────────────────────────────────────────────────────────


def test_a_listener_receives_an_emitted_event() -> None:
    bus = EventBus()
    seen: list = []
    bus.on(ToolPreExecute, seen.append)
    asyncio.run(bus.emit(ToolPreExecute(tool_name="t", arguments={})))
    assert len(seen) == 1
    assert seen[0].tool_name == "t"


def test_listeners_are_isolated_per_event_type() -> None:
    bus = EventBus()
    pre: list = []
    post: list = []
    bus.on(ToolPreExecute, pre.append)
    bus.on(ToolPostExecute, post.append)
    asyncio.run(bus.emit(ToolPreExecute(tool_name="t", arguments={})))
    assert len(pre) == 1
    assert post == []


def test_unsubscribing_stops_delivery() -> None:
    bus = EventBus()
    seen: list = []
    unsubscribe = bus.on(ToolPreExecute, seen.append)
    unsubscribe()
    asyncio.run(bus.emit(ToolPreExecute(tool_name="t", arguments={})))
    assert seen == []


def test_a_failing_listener_does_not_stop_the_others() -> None:
    """One broken plugin must not silently disable every other gate."""
    bus = EventBus()
    seen: list = []

    def boom(_event) -> None:
        raise RuntimeError("listener failed")

    bus.on(ToolPreExecute, boom)
    bus.on(ToolPreExecute, seen.append)
    asyncio.run(bus.emit(ToolPreExecute(tool_name="t", arguments={})))
    assert len(seen) == 1


def test_async_listeners_are_awaited() -> None:
    bus = EventBus()
    seen: list = []

    async def listener(event) -> None:
        seen.append(event.tool_name)

    bus.on(ToolPreExecute, listener)
    asyncio.run(bus.emit(ToolPreExecute(tool_name="async", arguments={})))
    assert seen == ["async"]


def test_delivery_order_is_registration_order() -> None:
    bus = EventBus()
    order: list = []
    bus.on(ToolPreExecute, lambda e: order.append("first"))
    bus.on(ToolPreExecute, lambda e: order.append("second"))
    asyncio.run(bus.emit(ToolPreExecute(tool_name="t", arguments={})))
    assert order == ["first", "second"]


# ── The executor, without a plugin catalog in scope ─────────────────────────


class _StubRegistry(ToolRegistry):
    """A registry whose one tool records that it ran."""

    def __init__(self, calls: list) -> None:
        super().__init__()
        self._calls = calls

    async def call(self, name, inputs):  # type: ignore[override]
        self._calls.append((name, inputs))
        return json.dumps({"ok": True, "echo": inputs})


def _executor(calls: list, bus: EventBus) -> RegularToolExecutor:
    return RegularToolExecutor(_StubRegistry(calls), event_bus=bus)


def test_a_pre_listener_can_block_a_tool() -> None:
    bus = EventBus()
    calls: list = []

    def gate(event):
        return "blocked by policy" if event.tool_name == "shell" else None

    bus.on(ToolPreExecute, gate)
    result = json.loads(
        asyncio.run(_executor(calls, bus).run({"name": "shell", "input": {}}))
    )
    assert result["ok"] is False
    assert result["blocked"] is True
    assert "blocked by policy" in result["reason"]
    assert calls == [], "a blocked tool must not execute"


def test_an_unblocked_tool_still_runs() -> None:
    bus = EventBus()
    calls: list = []
    bus.on(ToolPreExecute, lambda event: None)
    result = json.loads(
        asyncio.run(_executor(calls, bus).run({"name": "read_file", "input": {"p": 1}}))
    )
    assert result["ok"] is True
    assert calls == [("read_file", {"p": 1})]


def test_a_post_listener_sees_the_result() -> None:
    bus = EventBus()
    calls: list = []
    seen: list = []
    bus.on(ToolPostExecute, seen.append)

    asyncio.run(_executor(calls, bus).run({"name": "read_file", "input": {}}))

    assert len(seen) == 1
    assert seen[0].tool_name == "read_file"
    assert json.loads(seen[0].result)["ok"] is True


def test_an_empty_return_does_not_block() -> None:
    """``None`` is "no opinion", which is the only thing a listener defaults to."""
    bus = EventBus()
    calls: list = []
    bus.on(ToolPreExecute, lambda event: None)
    result = json.loads(
        asyncio.run(_executor(calls, bus).run({"name": "t", "input": {}}))
    )
    assert result.get("blocked") is not True


def test_the_first_block_wins_and_later_listeners_do_not_run() -> None:
    """Preserved exactly: the old pre-tool chain short-circuited on block.

    Not tidied into "collect every opinion", because a security gate that a
    later listener can argue with is not a gate.
    """
    bus = EventBus()
    calls: list = []
    second_ran: list = []

    def first(event):
        return "first says no"

    def second(event):
        second_ran.append(True)
        return None

    bus.on(ToolPreExecute, first)
    bus.on(ToolPreExecute, second)
    result = json.loads(
        asyncio.run(_executor(calls, bus).run({"name": "shell", "input": {}}))
    )

    assert result["blocked"] is True
    assert "first says no" in result["reason"], "the blocking listener's reason must survive"
    assert second_ran == [], "a blocked call must not run later listeners"


def test_a_post_listener_return_value_is_ignored_by_the_executor() -> None:
    """Preserved: ``on_post_tool`` is observational by contract.

    ``fire_post_tool`` computes a :class:`HookResult` and ``executor.py:538``
    drops it. The typed version keeps that, so a listener returning something
    is not a behaviour change waiting to happen.
    """
    bus = EventBus()
    calls: list = []
    bus.on(ToolPostExecute, lambda event: "this is deliberately ignored")

    result = asyncio.run(_executor(calls, bus).run({"name": "read_file", "input": {}}))

    assert json.loads(result)["ok"] is True


def test_the_executor_keeps_working_with_no_listeners_at_all() -> None:
    bus = EventBus()
    calls: list = []
    result = json.loads(
        asyncio.run(_executor(calls, bus).run({"name": "t", "input": {}}))
    )
    assert result["ok"] is True
    assert calls == [("t", {})]


# ── The catalog subscribes, so plugin behaviour is unchanged ────────────────


def test_the_catalog_subscribes_to_the_bus_it_is_given(tmp_path) -> None:
    """The wiring that replaces holding the catalog inside the executor."""
    from agent.plugins.catalog import PluginCatalog

    bus = EventBus()
    catalog = PluginCatalog(builtin_dir=tmp_path, event_bus=bus)
    try:
        assert bus.has_listeners(ToolPreExecute)
        assert bus.has_listeners(ToolPostExecute)
    finally:
        catalog.close()


def test_closing_the_catalog_unsubscribes(tmp_path) -> None:
    """Otherwise a reloaded catalog would gate tools twice."""
    from agent.plugins.catalog import PluginCatalog

    bus = EventBus()
    catalog = PluginCatalog(builtin_dir=tmp_path, event_bus=bus)
    catalog.close()
    catalog.close()  # idempotent
    assert not bus.has_listeners(ToolPreExecute)
    assert not bus.has_listeners(ToolPostExecute)


def test_a_plugin_gate_still_blocks_through_the_real_catalog(tmp_path) -> None:
    """End to end over the real objects: plugin -> tool domain -> executor.

    Rewritten from the hook protocol, where the plugin implemented ``on_pre_tool`` and
    the catalog adapted the typed event to it.  The kernel path is the reverse: the
    plugin subscribes to the tool domain itself, and the executor publishes to the bus
    that domain names.  So the executor is given ``TOOL_EVENTS`` here rather than a local
    bus -- a gate that publishes nowhere is the failure this test exists to catch, and a
    local bus would have hidden it by making the subscription unreachable.

    The plugin is unloaded in ``finally`` because ``TOOL_EVENTS`` is module-level and
    shared: a subscription left behind would gate a later test's tools.
    """
    from agent.core.events import TOOL_EVENTS
    from agent.plugins.catalog import PluginCatalog

    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "gate").mkdir()
    (plugin_dir / "gate" / "__init__.py").write_text(
        textwrap.dedent(
            """
            from agent.core.event_domains import TOOL

            def register():
                class Gate:
                    name = "gate"

                    def setup(self, ctx):
                        TOOL.attach(ctx, self.on_pre_tool)

                    def on_pre_tool(self, event):
                        if event.tool_name == "shell":
                            return "no shells"
                        return None
                return Gate()
            """
        ),
        encoding="utf-8",
    )

    catalog = PluginCatalog(builtin_dir=plugin_dir)
    assert catalog.discover_and_load() == ["gate"]

    calls: list = []
    try:
        blocked = json.loads(
            asyncio.run(
                _executor(calls, TOOL_EVENTS).run({"name": "shell", "input": {}})
            )
        )
        allowed = json.loads(
            asyncio.run(
                _executor(calls, TOOL_EVENTS).run({"name": "read_file", "input": {}})
            )
        )
    finally:
        catalog.close()

    assert blocked["blocked"] is True
    assert blocked["reason"] == "no shells"
    assert allowed["ok"] is True
    assert calls == [("read_file", {})]


def test_closing_a_catalog_releases_its_hold_on_the_shared_bus() -> None:
    """A catalog gates tools globally, so a closed one must let go.

    ``PluginCatalog`` subscribes its tool bridge to the **module-level** ``TOOL_EVENTS``,
    which means every catalog in a process is a listener on one bus -- including the ones
    a test creates.  ``close`` unsubscribes, and this asserts it, because the failure it
    prevents is not local: a catalog left open with a denying command hook installs a gate
    that outlives whoever created it, and ``emit_blocking`` stops at the first refusal, so
    the leftover gate wins over the live one.

    That is how this was found: a test's ``PreToolUse: deny`` leaked into an unrelated
    tool call and the assertion failed with the *other* test's reason string.
    """
    from agent.core.events import TOOL_EVENTS
    from agent.plugins.catalog import PluginCatalog

    before = TOOL_EVENTS.has_listeners(ToolPreExecute)
    catalog = PluginCatalog(builtin_dir=Path(__file__).parent / "does-not-exist")
    try:
        assert TOOL_EVENTS.has_listeners(ToolPreExecute), "it listens while it lives"
    finally:
        catalog.close()
    assert TOOL_EVENTS.has_listeners(ToolPreExecute) is before, (
        "closing must restore the bus to what it was"
    )
