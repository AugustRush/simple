from __future__ import annotations

import asyncio
import json

from agent.core.output import _active_sink
from agent.tools.executor import RegularToolExecutor


class _FakeRegistry:
    def __init__(self):
        self.calls = []

    async def call(self, name, inputs):
        self.calls.append((name, inputs))
        return json.dumps({"ok": True, "name": name})


class _RecordingSink:
    def __init__(self):
        self.events = []

    def on_tool_start(self, name, inputs):
        self.events.append(("start", name, inputs))

    def on_tool_end(self, name, result):
        self.events.append(("end", name, result))

    def on_tool_blocked(self, name, reason):
        self.events.append(("blocked", name, reason))


def test_regular_tool_executor_calls_registry_and_sink():
    registry = _FakeRegistry()
    sink = _RecordingSink()
    token = _active_sink.set(sink)
    try:
        result = asyncio.run(
            RegularToolExecutor(registry).run(
                {"name": "search", "input": {"query": "runtime"}}
            )
        )
    finally:
        _active_sink.reset(token)

    assert registry.calls == [("search", {"query": "runtime"})]
    assert json.loads(result) == {"ok": True, "name": "search"}
    assert sink.events == [
        ("start", "search", {"query": "runtime"}),
        ("end", "search", result),
    ]


def test_regular_tool_executor_honors_plugin_block():
    class _Blocked:
        action = "block"
        message = "nope"

    class _PluginCatalog:
        async def fire_pre_tool(self, event):
            self.pre_event = event
            return _Blocked()

        async def fire_post_tool(self, event):
            raise AssertionError("blocked tools should not fire post hooks")

    registry = _FakeRegistry()
    plugin_catalog = _PluginCatalog()
    sink = _RecordingSink()
    token = _active_sink.set(sink)
    try:
        result = asyncio.run(
            RegularToolExecutor(registry, plugin_catalog=plugin_catalog).run(
                {"name": "shell", "input": {"command": "rm -rf /"}}
            )
        )
    finally:
        _active_sink.reset(token)

    assert registry.calls == []
    assert json.loads(result) == {"ok": False, "blocked": True, "reason": "nope"}
    assert plugin_catalog.pre_event.tool_name == "shell"
    assert sink.events == [("blocked", "shell", "nope")]
