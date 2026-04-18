"""Tests for the Channel Layer: OutputSink, CliOutputSink, _fmt_tool_inputs,
_active_sink ContextVar, IncomingMessage, Channel ABC, CliChannel, ChannelRunner.
"""

from __future__ import annotations

import asyncio
from io import StringIO
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
