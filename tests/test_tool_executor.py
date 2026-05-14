from __future__ import annotations

import asyncio
import json

from agent.core.output import EventCollector, _active_event_collector, _active_sink
from agent.tools.executor import RegularToolExecutor, report_tool_progress
from agent.tools.runtime import ToolRegistry


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

    def on_tool_progress(self, name, progress):
        self.events.append(("progress", name, progress))


def test_regular_tool_executor_calls_registry_and_sink():
    registry = _FakeRegistry()
    sink = _RecordingSink()
    collector = EventCollector()
    token = _active_sink.set(sink)
    event_token = _active_event_collector.set(collector)
    try:
        result = asyncio.run(
            RegularToolExecutor(registry).run(
                {"name": "search", "input": {"query": "runtime"}}
            )
        )
    finally:
        _active_event_collector.reset(event_token)
        _active_sink.reset(token)

    assert registry.calls == [("search", {"query": "runtime"})]
    assert json.loads(result) == {"ok": True, "name": "search"}
    assert sink.events == [
        ("start", "search", {"query": "runtime"}),
        ("end", "search", result),
    ]
    events = collector.drain()
    assert [event.name for event in events] == ["tool_started", "tool_completed"]
    assert events[0].fields["operation_id"] == events[1].fields["operation_id"]
    assert events[0].fields["timeout_seconds"] == 1800


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


def test_regular_tool_executor_requires_structured_shell_intent():
    registry = ToolRegistry()
    called = []

    async def shell(**kwargs):
        called.append(kwargs)
        return {"ok": True}

    registry.register(
        "shell",
        "Shell",
        {"type": "object", "properties": {}, "required": []},
        shell,
        source="builtin",
    )

    result = asyncio.run(
        RegularToolExecutor(registry).run(
            {"name": "shell", "input": {"command": "echo ok"}}
        )
    )
    payload = json.loads(result)

    assert payload["ok"] is False
    assert payload["intent_required"] is True
    assert "Shell intent required" in payload["error"]
    assert called == []


def test_regular_tool_executor_accepts_structured_shell_intent_without_prose():
    registry = ToolRegistry()
    called = []

    async def shell(**kwargs):
        called.append(kwargs)
        return {"ok": True}

    registry.register(
        "shell",
        "Shell",
        {"type": "object", "properties": {}, "required": []},
        shell,
        source="builtin",
    )

    result = asyncio.run(
        RegularToolExecutor(registry).run(
            {
                "name": "shell",
                "input": {
                    "command": "git status --short",
                    "intent": "检查当前仓库是否还有未提交变更。",
                },
            }
        )
    )

    assert json.loads(result) == {"ok": True}
    assert called == [
        {
            "command": "git status --short",
            "intent": "检查当前仓库是否还有未提交变更。",
        }
    ]


def test_regular_tool_executor_rejects_vague_shell_intent():
    registry = ToolRegistry()
    called = []

    async def shell(**kwargs):
        called.append(kwargs)
        return {"ok": True}

    registry.register(
        "shell",
        "Shell",
        {"type": "object", "properties": {}, "required": []},
        shell,
        source="builtin",
    )

    result = asyncio.run(
        RegularToolExecutor(registry).run(
            {"name": "shell", "input": {"command": "echo ok", "intent": "执行命令"}}
        )
    )
    payload = json.loads(result)

    assert payload["ok"] is False
    assert payload["intent_required"] is True
    assert "too vague" in payload["error"]
    assert called == []


def test_regular_tool_executor_emits_running_progress_for_slow_tool():
    class _SlowRegistry:
        async def call(self, name, inputs):
            await asyncio.sleep(0.03)
            return json.dumps({"ok": True})

    async def run():
        collector = EventCollector()
        token = _active_event_collector.set(collector)
        original_interval = RegularToolExecutor._HEARTBEAT_INTERVAL_SECONDS
        RegularToolExecutor._HEARTBEAT_INTERVAL_SECONDS = 0.01
        try:
            result = await RegularToolExecutor(
                _SlowRegistry(),
                timeout_seconds=1,
                stale_timeout_seconds=0.5,
            ).run({"name": "download", "input": {}})
        finally:
            RegularToolExecutor._HEARTBEAT_INTERVAL_SECONDS = original_interval
            _active_event_collector.reset(token)
        return result, collector.drain()

    result, events = asyncio.run(run())

    assert json.loads(result) == {"ok": True}
    names = [event.name for event in events]
    assert names[0] == "tool_started"
    assert "tool_progress" in names
    assert names[-1] == "tool_completed"
    operation_ids = {event.fields["operation_id"] for event in events}
    assert len(operation_ids) == 1


def test_regular_tool_executor_exposes_explicit_progress_reporter_to_tools():
    class _ProgressRegistry:
        async def call(self, name, inputs):
            report_tool_progress(
                status="downloading",
                message="chunk 1",
                current=1,
                total=3,
                bytes_done=1024,
            )
            return json.dumps({"ok": True})

    async def run():
        collector = EventCollector()
        token = _active_event_collector.set(collector)
        try:
            result = await RegularToolExecutor(
                _ProgressRegistry(),
                timeout_seconds=1,
            ).run({"name": "download", "input": {}})
        finally:
            _active_event_collector.reset(token)
        return result, collector.drain()

    result, events = asyncio.run(run())

    assert json.loads(result) == {"ok": True}
    progress = [event for event in events if event.name == "tool_progress"]
    assert len(progress) == 1
    assert progress[0].fields["status"] == "downloading"
    assert progress[0].fields["message"] == "chunk 1"
    assert progress[0].fields["current"] == 1
    assert progress[0].fields["total"] == 3
    assert progress[0].fields["bytes_done"] == 1024
    assert progress[0].fields["operation_id"] == events[0].fields["operation_id"]


def test_regular_tool_executor_forwards_progress_to_active_sink():
    class _ProgressRegistry:
        async def call(self, name, inputs):
            report_tool_progress(status="downloading", current=1, total=2)
            return json.dumps({"ok": True})

    async def run():
        sink = _RecordingSink()
        token = _active_sink.set(sink)
        try:
            result = await RegularToolExecutor(
                _ProgressRegistry(),
                timeout_seconds=1,
            ).run({"name": "download", "input": {}})
        finally:
            _active_sink.reset(token)
        return result, sink.events

    result, events = asyncio.run(run())

    assert json.loads(result) == {"ok": True}
    progress_events = [event for event in events if event[0] == "progress"]
    assert len(progress_events) == 1
    assert progress_events[0][1] == "download"
    assert progress_events[0][2]["status"] == "downloading"
    assert progress_events[0][2]["current"] == 1
    assert progress_events[0][2]["operation_id"].startswith("tool_")


def test_regular_tool_executor_extends_timeout_when_tool_reports_progress():
    class _ProgressThenCompleteRegistry:
        async def call(self, name, inputs):
            await asyncio.sleep(0.015)
            report_tool_progress(status="downloading", current=1, total=2)
            await asyncio.sleep(0.015)
            return json.dumps({"ok": True, "done": True})

    async def run():
        collector = EventCollector()
        token = _active_event_collector.set(collector)
        try:
            result = await RegularToolExecutor(
                _ProgressThenCompleteRegistry(),
                timeout_seconds=0.02,
                stale_timeout_seconds=0.03,
            ).run({"name": "download", "input": {}})
        finally:
            _active_event_collector.reset(token)
        return result, collector.drain()

    result, events = asyncio.run(run())

    assert json.loads(result) == {"ok": True, "done": True}
    names = [event.name for event in events]
    assert "tool_timed_out" not in names
    assert "tool_completed" in names
    assert any(
        event.name == "tool_progress"
        and event.fields.get("status") == "timeout_extended"
        for event in events
    )


def test_regular_tool_executor_timeout_includes_operation_metadata():
    class _NeverRegistry:
        async def call(self, name, inputs):
            await asyncio.sleep(1)
            return json.dumps({"ok": True})

    async def run():
        collector = EventCollector()
        token = _active_event_collector.set(collector)
        try:
            result = await RegularToolExecutor(
                _NeverRegistry(),
                timeout_seconds=0.01,
                stale_timeout_seconds=0.01,
            ).run({"name": "download", "input": {}})
        finally:
            _active_event_collector.reset(token)
        return result, collector.drain()

    result, events = asyncio.run(run())

    assert json.loads(result)["ok"] is False
    assert [event.name for event in events] == [
        "tool_started",
        "tool_timed_out",
        "tool_failed",
    ]
    assert events[1].fields["operation_id"] == events[2].fields["operation_id"]
    assert events[1].fields["timeout_seconds"] == 0.01
