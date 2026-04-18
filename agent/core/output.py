from __future__ import annotations

import contextvars
from abc import ABC
from typing import Any, Optional

from rich.markdown import Markdown
from rich.markup import escape as _markup_escape

_TOOL_KEY_PRIORITY: dict[str, list[str]] = {
    "bash": ["command"],
    "write_file": ["path"],
    "read_file": ["path"],
    "search": ["query"],
    "web_search": ["query"],
    "grep": ["pattern", "path"],
    "python": ["code"],
}


def _fmt_tool_inputs(name: str, inputs: dict) -> str:
    """Return a terse, single-line hint of the most useful input fields.

    The returned string is safe to embed in Rich markup: bracket characters
    from LLM-generated tool input values are escaped via rich.markup.escape().
    """

    keys = _TOOL_KEY_PRIORITY.get(name, list(inputs.keys())[:2])
    parts = []
    for k in keys:
        v = inputs.get(k)
        if v is not None:
            snippet = str(v)[:80].replace("\n", "↵")
            parts.append(f"{k}={snippet!r}" if " " in snippet else f"{k}={snippet}")
    raw = "  " + "  ".join(parts) if parts else ""
    return _markup_escape(raw)


class OutputSink(ABC):
    """Abstract output contract for one channel session."""

    streaming: bool = True

    def on_stream_chunk(self, chunk: str) -> None:
        """Called for each streamed text token."""

    def on_turn_complete(self, full_text: str, tool_calls: list[str]) -> None:
        """Called once the model turn is fully resolved."""

    def on_tool_start(self, name: str, inputs: dict) -> None:
        """Called immediately before a tool is executed."""

    def on_tool_end(self, name: str, result: str) -> None:
        """Called immediately after a tool returns its result."""

    def on_tool_blocked(self, name: str, reason: str) -> None:
        """Called when a plugin vetoes a tool call before execution."""

    def on_info(self, content: Any) -> None:
        """Display an informational renderable."""

    def on_status(self, text: str, *, level: str = "info") -> None:
        """Display a status message."""

    def on_error(self, error: str) -> None:
        """Display an error message."""

    def on_subagent_event(self, event: "SubAgentProgressEvent") -> None:
        """Display structured multi-agent progress."""

    def sync_stream_cb(self, chunk: str) -> None:
        """Synchronous callback adapter for BaseAgent.send_message."""

        self.on_stream_chunk(chunk)


class CliOutputSink(OutputSink):
    """Rich-console implementation of OutputSink for the CLI channel."""

    def __init__(self, console: Any) -> None:
        self._console = console
        self._streamed: list[str] = []

    def on_stream_chunk(self, chunk: str) -> None:
        self._console.print(chunk, end="", markup=False)
        self._streamed.append(chunk)

    def on_turn_complete(self, full_text: str, tool_calls: list[str]) -> None:
        if not self._streamed and full_text:
            self._console.print(Markdown(full_text))
        self._console.print()
        self._streamed.clear()

    def on_tool_start(self, name: str, inputs: dict) -> None:
        hint = _fmt_tool_inputs(name, inputs)
        self._console.print(f"\n[cyan]→ {name}[/cyan]{hint}")

    def on_tool_end(self, name: str, result: str) -> None:
        self._console.print(
            f"[dim]{result[:200]}{'...' if len(result) > 200 else ''}[/dim]"
        )

    def on_tool_blocked(self, name: str, reason: str) -> None:
        self._console.print(
            f"\n[cyan]→ {name}[/cyan] [yellow](blocked by plugin: {reason})[/yellow]"
        )

    def on_info(self, content: Any) -> None:
        self._console.print(content)

    def on_status(self, text: str, *, level: str = "info") -> None:
        colors = {"info": "dim", "warning": "yellow", "success": "green", "error": "red"}
        self._console.print(f"[{colors.get(level, 'dim')}]{text}[/{colors.get(level, 'dim')}]")

    def on_error(self, error: str) -> None:
        self._console.print(f"[red]{error}[/red]")

    def on_subagent_event(self, event: "SubAgentProgressEvent") -> None:
        msg = event.message or self._format_subagent_event(event)
        if not msg:
            return
        color = "magenta"
        if event.kind == "agent_failed":
            color = "red"
        elif event.kind in ("batch_progress", "batch_finished"):
            color = "dim"
        self._console.print(f"[{color}]{msg}[/{color}]")

    @staticmethod
    def _format_subagent_event(event: "SubAgentProgressEvent") -> str:
        role = event.role or "agent"
        if event.kind == "batch_started":
            return event.message or f"Starting {event.total} sub-agents"
        if event.kind == "batch_progress":
            return (
                event.message
                or f"Sub-agents running: {event.completed}/{event.total} completed"
            )
        if event.kind == "batch_finished":
            return event.message or f"Sub-agents finished: {event.completed}/{event.total}"
        if event.kind == "agent_started":
            return event.message or f"{role} started"
        if event.kind == "agent_finished":
            return event.message or f"{role} finished"
        if event.kind == "agent_failed":
            return event.message or f"{role} failed"
        return event.message


_active_sink: contextvars.ContextVar[Optional[OutputSink]] = contextvars.ContextVar(
    "_active_sink", default=None
)


__all__ = [
    "CliOutputSink",
    "OutputSink",
    "_active_sink",
    "_fmt_tool_inputs",
]
