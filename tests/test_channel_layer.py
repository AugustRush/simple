"""Tests for the Channel Layer: OutputSink, CliOutputSink, _fmt_tool_inputs,
_active_sink ContextVar, IncomingMessage, Channel ABC, CliChannel, ChannelRunner.
"""

from __future__ import annotations

import asyncio
import json
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import agent as agent_module
from agent.channels import Channel, ChannelRunner, CliChannel, IncomingMessage
from agent.core import CliOutputSink, OutputSink, SubAgentProgressEvent
from agent import (
    _active_sink,
    _fmt_tool_inputs,
)


# ─────────────────────────────────────────────────────────────────────────────
# _fmt_tool_inputs
# ─────────────────────────────────────────────────────────────────────────────


def test_fmt_tool_inputs_known_tool_returns_primary_key():
    result = _fmt_tool_inputs("bash", {"command": "pytest tests/", "cwd": "/tmp"})
    # "pytest tests/" contains a space → repr form: command='pytest tests/'
    assert "command=" in result
    assert "pytest" in result
    # secondary key should not appear for known tools with one priority key
    assert "cwd" not in result


def test_fmt_tool_inputs_known_tool_web_search():
    result = _fmt_tool_inputs("web_search", {"query": "python asyncio", "n": 5})
    assert "query=" in result
    assert "asyncio" in result


def test_fmt_tool_inputs_unknown_tool_shows_first_two_keys():
    result = _fmt_tool_inputs(
        "my_custom_tool", {"alpha": "a", "beta": "b", "gamma": "c"}
    )
    assert "alpha" in result
    assert "beta" in result
    assert "gamma" not in result


def test_fmt_tool_inputs_empty_inputs_returns_empty_string():
    assert _fmt_tool_inputs("bash", {}) == ""


def test_fmt_tool_inputs_priority_key_missing_falls_back_to_empty():
    # bash priority key is "command"; if absent, no hint
    result = _fmt_tool_inputs("bash", {"cwd": "/tmp"})
    # "cwd" is not in bash's priority list, and bash has only ["command"] configured
    # so the loop produces no parts → empty string
    assert result == ""


def test_fmt_tool_inputs_escapes_rich_markup():
    """LLM-generated inputs containing [bold] must NOT be interpreted as markup.

    rich.markup.escape() converts '[' to '\\[', so the result contains '\\['
    (backslash-bracket).  Rich treats '\\[' as a literal bracket, not markup.
    """
    result = _fmt_tool_inputs("bash", {"command": "echo [red]hello[/red]"})
    # After escaping, every '[' in the hint is preceded by backslash.
    assert "\\[" in result  # backslash-bracket present → markup-safe


def test_fmt_tool_inputs_long_value_truncated():
    long_val = "x" * 200
    result = _fmt_tool_inputs("bash", {"command": long_val})
    # snippet is capped at 80 chars
    assert len(result) < 120  # well under 200


def test_fmt_tool_inputs_newlines_replaced():
    result = _fmt_tool_inputs("bash", {"command": "line1\nline2"})
    assert "\n" not in result
    assert "↵" in result


# ─────────────────────────────────────────────────────────────────────────────
# CliOutputSink
# ─────────────────────────────────────────────────────────────────────────────


class _FakeConsole:
    """Minimal console double that records print() calls."""

    def __init__(self):
        self.lines: list[str] = []

    def print(self, *args, **kwargs):
        self.lines.append(str(args[0]) if args else "")


def _make_sink() -> tuple[CliOutputSink, _FakeConsole]:
    console = _FakeConsole()
    return CliOutputSink(console), console  # type: ignore[arg-type]


def test_cli_output_sink_stream_chunk_accumulated():
    sink, console = _make_sink()
    sink.on_stream_chunk("hello ")
    sink.on_stream_chunk("world")
    assert sink._streamed == ["hello ", "world"]


def test_cli_output_sink_on_turn_complete_streaming_path_no_markdown():
    """When chunks were streamed, on_turn_complete must NOT re-render the text."""
    sink, console = _make_sink()
    sink.on_stream_chunk("some text")
    console.lines.clear()

    sink.on_turn_complete("some text", [])

    # Only a trailing newline should be printed (the "" from CONSOLE.print())
    assert all("some text" not in line for line in console.lines)
    assert sink._streamed == []  # cleared


def test_cli_output_sink_on_turn_complete_non_streaming_renders_full_text():
    """When no chunks were streamed, on_turn_complete must print the full text."""
    sink, console = _make_sink()
    # No on_stream_chunk calls → _streamed is empty

    sink.on_turn_complete("**bold response**", [])

    # CliOutputSink calls self._console.print(Markdown(full_text)) in this path.
    # The _FakeConsole receives whatever object is passed; at least one print() was
    # called with the full text's Markdown wrapper.
    assert len(console.lines) >= 1


def test_cli_output_sink_on_turn_complete_empty_text_no_double_render():
    """on_turn_complete with empty full_text must not crash or render garbage."""
    sink, console = _make_sink()
    sink.on_turn_complete("", [])
    # Just a trailing newline print — no crash
    assert len(console.lines) >= 1


def test_cli_output_sink_on_tool_start():
    sink, console = _make_sink()
    sink.on_tool_start("bash", {"command": "ls"})
    assert any("bash" in line for line in console.lines)


def test_cli_output_sink_on_tool_end():
    sink, console = _make_sink()
    sink.on_tool_end("bash", "file1.py\nfile2.py")
    assert len(console.lines) == 1


def test_cli_output_sink_on_tool_blocked():
    sink, console = _make_sink()
    sink.on_tool_blocked("bash", "policy violation")
    assert any("blocked" in line or "bash" in line for line in console.lines)


def test_cli_output_sink_on_error():
    sink, console = _make_sink()
    sink.on_error("something went wrong")
    assert any("something went wrong" in line for line in console.lines)


def test_cli_output_sink_on_status_levels():
    for level in ("info", "warning", "success", "error"):
        sink, console = _make_sink()
        sink.on_status("test message", level=level)
        assert len(console.lines) == 1


def test_cli_output_sink_sync_stream_cb_delegates():
    sink, console = _make_sink()
    sink.sync_stream_cb("chunk")
    assert sink._streamed == ["chunk"]


def test_cli_output_sink_dedupes_duplicate_batch_progress_events():
    sink, console = _make_sink()
    event = SubAgentProgressEvent(kind="batch_progress", completed=0, total=3)

    sink.on_subagent_event(event)
    sink.on_subagent_event(event)

    matching = [
        line for line in console.lines if "Sub-agents running: 0/3 completed" in line
    ]
    assert len(matching) == 1


# ─────────────────────────────────────────────────────────────────────────────
# _active_sink ContextVar isolation
# ─────────────────────────────────────────────────────────────────────────────


def test_active_sink_default_is_none():
    assert _active_sink.get() is None


def test_active_sink_set_and_reset():
    sink = CliOutputSink(_FakeConsole())  # type: ignore[arg-type]
    token = _active_sink.set(sink)
    try:
        assert _active_sink.get() is sink
    finally:
        _active_sink.reset(token)
    assert _active_sink.get() is None


def test_active_sink_reset_after_exception():
    """Simulates the _interactive_loop finally-block pattern."""
    sink = CliOutputSink(_FakeConsole())  # type: ignore[arg-type]
    token = _active_sink.set(sink)
    try:
        raise ValueError("simulated error")
    except ValueError:
        pass
    finally:
        _active_sink.reset(token)
    assert _active_sink.get() is None


def test_active_sink_nested_turns_are_isolated():
    """Two nested tokens do not bleed into each other."""
    sink1 = CliOutputSink(_FakeConsole())  # type: ignore[arg-type]
    sink2 = CliOutputSink(_FakeConsole())  # type: ignore[arg-type]

    token1 = _active_sink.set(sink1)
    assert _active_sink.get() is sink1

    token2 = _active_sink.set(sink2)
    assert _active_sink.get() is sink2

    _active_sink.reset(token2)
    assert _active_sink.get() is sink1

    _active_sink.reset(token1)
    assert _active_sink.get() is None


# ─────────────────────────────────────────────────────────────────────────────
# IncomingMessage
# ─────────────────────────────────────────────────────────────────────────────


def test_incoming_message_defaults():
    msg = IncomingMessage(text="hello")
    assert msg.text == "hello"
    assert msg.channel_name == "cli"
    assert msg.session_id  # non-empty UUID-like string
    assert msg.metadata == {}


def test_incoming_message_session_ids_are_unique():
    m1 = IncomingMessage(text="a")
    m2 = IncomingMessage(text="b")
    assert m1.session_id != m2.session_id


# ─────────────────────────────────────────────────────────────────────────────
# CliChannel
# ─────────────────────────────────────────────────────────────────────────────


def test_cli_channel_start_raises_not_implemented():
    """start() is a documented stub; must raise NotImplementedError."""
    ch = CliChannel(MagicMock())

    async def _run():
        await ch.start(lambda msg, sink: True)

    with pytest.raises(NotImplementedError):
        asyncio.run(_run())


def test_cli_channel_stop_is_noop():
    ch = CliChannel(MagicMock())
    asyncio.run(ch.stop())  # must not raise


def test_cli_channel_create_sink_returns_cli_output_sink():
    console = _FakeConsole()
    ch = CliChannel(console)  # type: ignore[arg-type]
    msg = IncomingMessage(text="hi")
    sink = ch.create_sink(msg)
    assert isinstance(sink, CliOutputSink)
    assert sink._console is console


# ─────────────────────────────────────────────────────────────────────────────
# OutputSink base class (no-op defaults)
# ─────────────────────────────────────────────────────────────────────────────


def test_output_sink_base_methods_are_noop():
    """All base-class methods must be callable without errors."""
    sink = OutputSink()
    sink.on_stream_chunk("x")
    sink.on_turn_complete("text", [])
    sink.on_tool_start("bash", {"command": "ls"})
    sink.on_tool_end("bash", "result")
    sink.on_tool_blocked("bash", "reason")
    sink.on_info("info")
    sink.on_status("status")
    sink.on_error("error")
    sink.on_subagent_event(SubAgentProgressEvent(kind="agent_started", role="r"))
    sink.sync_stream_cb("chunk")


class _DummySettableChannel(agent_module.Channel):
    def __init__(self):
        self.output_dir = None
        self.started = False

    async def start(self, handler):
        self.started = True

    async def stop(self):
        return None

    def create_sink(self, msg):
        raise NotImplementedError

    def set_output_dir(self, path):
        self.output_dir = path


def test_channel_runner_sets_output_dir_on_channels_with_setter(tmp_path):
    channel = _DummySettableChannel()
    runner = ChannelRunner(
        channels=[channel],
        components={
            "context_manager": None,
            "plugin_catalog": None,
            "output_dir": tmp_path / "output",
        },
        cfg={},
    )

    asyncio.run(runner._run_channel(channel))

    assert channel.started is True
    assert channel.output_dir == tmp_path / "output"


def test_channel_runner_scopes_context_manager_per_chat():
    class _RecordingStaging:
        def __init__(self, session_id: str):
            self.session_id = session_id
            self.entries: list[tuple[str, str]] = []

        def append(self, role: str, content: str):
            self.entries.append((role, content))

        def count(self) -> int:
            return len(self.entries)

    class _SessionContextManager:
        def __init__(self, session_id: str):
            self.session_id = session_id
            self.staging = _RecordingStaging(session_id)
            self.mark_calls = 0
            self.recorded_turns: list[tuple[str, str, str]] = []

        def mark_activity(self):
            self.mark_calls += 1

        def record_turn(self, *, user_content, assistant_content="", channel="", **_kwargs):
            self.recorded_turns.append((user_content, assistant_content, channel))

        def should_enqueue_consolidation(self):
            return False

        def enqueue_consolidation(self, reason):
            raise AssertionError(f"unexpected enqueue: {reason}")

        def should_compact_messages(self, messages, max_tokens):
            return False

    class _RootContextManager:
        def __init__(self):
            self.staging = _RecordingStaging("root")
            self.spawned: dict[str, _SessionContextManager] = {}
            self.mark_calls = 0

        def spawn_session(self, session_id: str):
            mgr = _SessionContextManager(session_id)
            self.spawned[session_id] = mgr
            return mgr

        # Fallback methods keep the old implementation running long enough for
        # the assertions below to catch the shared-state bug.
        def mark_activity(self):
            self.mark_calls += 1

        def should_enqueue_consolidation(self):
            return False

        def enqueue_consolidation(self, reason):
            raise AssertionError(f"unexpected enqueue on root manager: {reason}")

        def should_compact_messages(self, messages, max_tokens):
            return False

    class _FakeAgent:
        max_tokens = 1024

        async def send_message(self, ctx, user_message, stream_callback=None):
            return agent_module.AgentResult(
                agent_id="agent",
                content=f"reply:{user_message}",
            )

    root_ctx_mgr = _RootContextManager()
    runner = ChannelRunner(
        channels=[],
        components={
            "agent": _FakeAgent(),
            "skill_catalog": object(),
            "plugin_catalog": None,
            "context_manager": root_ctx_mgr,
            "system_prompt": "system",
        },
        cfg={},
    )
    handler = runner._make_message_handler({})

    async def _run():
        await handler(
            IncomingMessage(
                text="hello",
                metadata={"chat_id": "chat-a"},
            ),
            OutputSink(),
        )
        await handler(
            IncomingMessage(
                text="world",
                metadata={"chat_id": "chat-b"},
            ),
            OutputSink(),
        )

    asyncio.run(_run())

    assert set(root_ctx_mgr.spawned) == {"chat-a", "chat-b"}
    assert root_ctx_mgr.staging.entries == []
    assert root_ctx_mgr.spawned["chat-a"].staging.entries == [
        ("user", "hello"),
        ("assistant", "reply:hello"),
    ]
    assert root_ctx_mgr.spawned["chat-b"].staging.entries == [
        ("user", "world"),
        ("assistant", "reply:world"),
    ]
    assert root_ctx_mgr.spawned["chat-a"].recorded_turns == [
        ("hello", "reply:hello", "cli")
    ]
    assert root_ctx_mgr.spawned["chat-b"].recorded_turns == [
        ("world", "reply:world", "cli")
    ]


def test_channel_runner_wakes_session_memory_worker_on_compaction(monkeypatch):
    worker_instances = []

    class _FakeBackgroundMemoryWorker:
        def __init__(
            self,
            ctx_mgr,
            client,
            model,
            api_format,
            poll_seconds=1.0,
            client_factory=None,
        ):
            self.ctx_mgr = ctx_mgr
            self.started = False
            self.wake_calls = 0
            worker_instances.append(self)

        def start(self):
            self.started = True

        def stop(self):
            return None

        async def wait(self):
            return None

        def wake(self):
            self.wake_calls += 1

    monkeypatch.setattr(agent_module, "BackgroundMemoryWorker", _FakeBackgroundMemoryWorker)

    class _RecordingStaging:
        def __init__(self, session_id: str):
            self.session_id = session_id
            self.entries: list[tuple[str, str]] = []

        def append(self, role: str, content: str):
            self.entries.append((role, content))

        def count(self) -> int:
            return len(self.entries)

    class _SessionContextManager:
        min_messages = 2

        def __init__(self, session_id: str):
            self.staging = _RecordingStaging(session_id)
            self.enqueued: list[str] = []

        def mark_activity(self):
            return None

        def record_turn(self, *, user_content, assistant_content="", channel="", **_kwargs):
            return None

        def should_enqueue_consolidation(self):
            return False

        def enqueue_consolidation(self, reason):
            self.enqueued.append(reason)

        def should_compact_messages(self, messages, max_tokens):
            return True

        def compact_messages(self, messages):
            return [{"role": "user", "content": "compacted"}]

    class _RootContextManager:
        def __init__(self):
            self.staging = _RecordingStaging("root")
            self.spawned: dict[str, _SessionContextManager] = {}

        def spawn_session(self, session_id: str):
            mgr = _SessionContextManager(session_id)
            self.spawned[session_id] = mgr
            return mgr

        def mark_activity(self):
            return None

        def should_enqueue_consolidation(self):
            return False

        def enqueue_consolidation(self, reason):
            raise AssertionError(f"unexpected root enqueue: {reason}")

        def should_compact_messages(self, messages, max_tokens):
            return False

    class _FakeAgent:
        api_format = "openai"
        max_tokens = 1024

        async def send_message(self, ctx, user_message, stream_callback=None):
            ctx.messages = [{"role": "user", "content": "history"}] * 4
            return agent_module.AgentResult(agent_id="agent", content="reply")

    root_ctx_mgr = _RootContextManager()
    runner = ChannelRunner(
        channels=[],
        components={
            "agent": _FakeAgent(),
            "skill_catalog": object(),
            "plugin_catalog": None,
            "context_manager": root_ctx_mgr,
            "system_prompt": "system",
            "client": object(),
            "model": "fake-model",
        },
        cfg={},
    )
    handler = runner._make_message_handler({})

    async def _run():
        await handler(
            IncomingMessage(
                text="trigger compaction",
                metadata={"chat_id": "chat-a"},
            ),
            OutputSink(),
        )

    asyncio.run(_run())

    assert len(worker_instances) == 1
    assert worker_instances[0].started is True
    assert worker_instances[0].wake_calls == 1
    assert root_ctx_mgr.spawned["chat-a"].enqueued == ["compact_triggered"]


def test_channel_runner_fires_session_end_per_chat_session():
    class _FakeChannel(Channel):
        async def start(self, handler):
            await handler(
                IncomingMessage(text="hello", metadata={"chat_id": "chat-a"}),
                OutputSink(),
            )
            await handler(
                IncomingMessage(text="world", metadata={"chat_id": "chat-b"}),
                OutputSink(),
            )

        async def stop(self):
            return None

        def create_sink(self, msg):
            return OutputSink()

    class _FakeAgent:
        max_tokens = 1024

        async def send_message(self, ctx, user_message, stream_callback=None):
            ctx.messages.append({"role": "assistant", "content": f"reply:{user_message}"})
            return agent_module.AgentResult(
                agent_id=ctx.agent_id,
                content=f"reply:{user_message}",
            )

    class _PluginCatalog:
        def __init__(self):
            self.session_events = []
            self.turn_events = []

        def fire_session_start(self, components):
            return None

        async def fire_turn_end(self, event):
            self.turn_events.append(event)
            return []

        async def fire_session_end(self, event):
            self.session_events.append(event)

    plugin_catalog = _PluginCatalog()
    channel = _FakeChannel()
    runner = ChannelRunner(
        channels=[channel],
        components={
            "agent": _FakeAgent(),
            "skill_catalog": object(),
            "plugin_catalog": plugin_catalog,
            "context_manager": None,
            "system_prompt": "system",
        },
        cfg={},
    )

    asyncio.run(runner._run_channel(channel))

    assert [(event.session_id, event.turn_count) for event in plugin_catalog.session_events] == [
        ("chat-a", 1),
        ("chat-b", 1),
    ]
    assert [event.tools_used for event in plugin_catalog.session_events] == [[], []]
    assert [event.messages for event in plugin_catalog.session_events] == [
        [
            {"role": "assistant", "content": "reply:hello"},
        ],
        [
            {"role": "assistant", "content": "reply:world"},
        ],
    ]


def test_channel_runner_exposes_feishu_delivery_target_to_scheduler_tools(
    monkeypatch, tmp_path
):
    import agent.tools.runtime as runtime_module
    from agent.scheduler import SchedulerStore

    monkeypatch.setattr(agent_module.shared, "SCHEDULER_DIR", tmp_path / "tasks")
    monkeypatch.setattr(
        agent_module.shared, "SCHEDULER_DB_FILE", tmp_path / "tasks" / "scheduler.db"
    )

    registry = agent_module.ToolRegistry()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory = agent_module.MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
    )
    agent_module.BuiltinTools(memory=memory, registry=registry, workspace_root=workspace)
    observed: dict[str, str] = {}

    class _FakeAgent:
        max_tokens = 1024

        async def send_message(self, ctx, user_message, stream_callback=None):
            result = await registry.call(
                "schedule_create",
                {
                    "name": "reminder",
                    "trigger_type": "once",
                    "prompt": "测试一下",
                    "at": "2026-04-20T10:00:00+08:00",
                    "timezone_name": "Asia/Shanghai",
                },
            )
            observed["payload"] = result
            return agent_module.AgentResult(agent_id="agent", content="scheduled")

    runner = ChannelRunner(
        channels=[],
        components={
            "agent": _FakeAgent(),
            "registry": registry,
            "skill_catalog": object(),
            "plugin_catalog": None,
            "context_manager": None,
            "system_prompt": "system",
        },
        cfg={},
    )
    handler = runner._make_message_handler({})

    async def _run():
        await handler(
            IncomingMessage(
                text="两分钟后提醒我",
                channel_name="feishu",
                metadata={"chat_id": "oc_test_chat", "chat_type": "group"},
            ),
            OutputSink(),
        )

    asyncio.run(_run())

    payload = json.loads(observed["payload"])
    store = SchedulerStore(db_path=Path(payload["task"]["db_path"]))
    try:
        task = store.get_task(payload["task"]["id"])
    finally:
        store.close()

    assert task is not None
    assert task.delivery_mode == "channel"
    assert task.delivery_target.payload["chat_id"] == "oc_test_chat"
