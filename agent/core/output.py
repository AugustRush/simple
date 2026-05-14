from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
import json
import re
import time
from abc import ABC
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional

from rich.markdown import Markdown
from rich.markup import escape as _markup_escape
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

_TOOL_KEY_PRIORITY: dict[str, list[str]] = {
    "bash": ["command"],
    "write_file": ["path"],
    "read_file": ["path"],
    "search": ["query"],
    "web_search": ["query"],
    "grep": ["pattern", "path"],
    "python": ["code"],
}


_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(authorization:\s*bearer\s+)[^\s'\"`]+"
    r"|((?:api[_-]?key|access[_-]?token|secret|password|token)\s*[=:]\s*)[^\s'\"`]+"
)


def _redact_sensitive_text(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        prefix = match.group(1) or match.group(2) or ""
        return f"{prefix}[REDACTED]"

    return _SENSITIVE_VALUE_RE.sub(_replace, value)


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
            snippet = _redact_sensitive_text(str(v))[:80].replace("\n", "↵")
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

    def on_tool_progress(self, name: str, progress: Mapping[str, Any]) -> None:
        """Called when a running tool reports progress."""

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

    def on_notification(self, title: str, body: str, *, level: str = "info") -> None:
        """Called for proactive notifications (reminders, summaries) not tied to a user turn.

        Default is a no-op. Channels override this to implement delivery
        (e.g. Feishu sends a message, CLI prints a panel).
        """

    def queue_attachment(self, path: Path) -> None:
        """Queue a file attachment for delivery with the current turn."""

    def sync_stream_cb(self, chunk: str) -> None:
        """Synchronous callback adapter for BaseAgent.send_message."""

        self.on_stream_chunk(chunk)


class CliOutputSink(OutputSink):
    """Rich-console implementation of OutputSink for the CLI channel."""

    def __init__(self, console: Any) -> None:
        self._console = console
        self._streamed: list[str] = []
        self._last_batch_progress_key: tuple[int, int] | None = None
        self._tool_count = 0
        self._tool_start_times: dict[str, float] = {}
        self._progress: Progress | None = None
        self._progress_task: Any = None

    def on_stream_chunk(self, chunk: str) -> None:
        self._console.print(chunk, end="", markup=False)
        self._streamed.append(chunk)

    def on_turn_complete(self, full_text: str, tool_calls: list[str]) -> None:
        if self._progress is not None:
            self._progress.stop()
            self._progress = None
            self._progress_task = None
        if not self._streamed and full_text:
            self._console.print(Markdown(full_text))
        if self._tool_count > 0:
            self._console.print(f"[dim]({self._tool_count} tool call(s) this turn)[/dim]")
        self._console.print()
        self._streamed.clear()
        self._tool_count = 0

    def on_tool_start(self, name: str, inputs: dict) -> None:
        hint = _fmt_tool_inputs(name, inputs)
        self._console.print(f"\n[cyan]→ {name}[/cyan]{hint}")
        self._tool_start_times[name] = time.monotonic()
        self._tool_count += 1

    def on_tool_end(self, name: str, result: str) -> None:
        elapsed = ""
        start = self._tool_start_times.pop(name, None)
        if start is not None:
            elapsed = f" [dim]({time.monotonic() - start:.1f}s)[/dim]"
        try:
            data = json.loads(result)
            ok = data.get("ok", True)
            indicator = "[green]✓[/green]" if ok else "[yellow]✗[/yellow]"
        except Exception:
            indicator = "[dim]·[/dim]"
        self._console.print(
            f"{indicator} [dim]{result[:150]}{'...' if len(result) > 150 else ''}[/dim]{elapsed}"
        )

    def on_tool_blocked(self, name: str, reason: str) -> None:
        self._console.print(
            f"\n[cyan]→ {name}[/cyan] [yellow](blocked by plugin: {reason})[/yellow]"
        )

    def on_tool_progress(self, name: str, progress: Mapping[str, Any]) -> None:
        status = str(progress.get("status") or "running")
        message = str(progress.get("message") or "").strip()
        current = progress.get("current")
        total = progress.get("total")
        suffix = ""
        if current is not None and total:
            try:
                suffix = f" {float(current) / float(total) * 100:.0f}%"
            except Exception:
                suffix = f" {current}/{total}"
        detail = f" - {message}" if message else ""
        self._console.print(f"[dim]↻ {name}: {status}{suffix}{detail}[/dim]")

    def on_notification(self, title: str, body: str, *, level: str = "info") -> None:
        from rich.panel import Panel

        colors = {"info": "cyan", "warning": "yellow", "error": "red"}
        self._console.print(
            Panel(
                Markdown(body) if body else "",
                title=f"[bold {colors.get(level, 'cyan')}]{title}[/bold {colors.get(level, 'cyan')}]",
                border_style=colors.get(level, "cyan"),
            )
        )

    def on_info(self, content: Any) -> None:
        self._console.print(content)

    def on_status(self, text: str, *, level: str = "info") -> None:
        colors = {"info": "dim", "warning": "yellow", "success": "green", "error": "red"}
        self._console.print(f"[{colors.get(level, 'dim')}]{text}[/{colors.get(level, 'dim')}]")

    def on_error(self, error: str) -> None:
        self._console.print(f"[red]{error}[/red]")

    def on_subagent_event(self, event: "SubAgentProgressEvent") -> None:
        if event.kind == "batch_started":
            self._last_batch_progress_key = None
            if event.total > 1:
                self._progress = Progress(
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TextColumn("{task.completed}/{task.total}"),
                    TimeElapsedColumn(),
                    console=self._console,
                )
                self._progress_task = self._progress.add_task(
                    event.message or "Sub-agents", total=event.total
                )
                self._progress.start()
                return
        elif event.kind == "batch_progress":
            key = (event.completed, event.total)
            if self._last_batch_progress_key == key:
                return
            self._last_batch_progress_key = key
            if self._progress and self._progress_task is not None:
                self._progress.update(self._progress_task, completed=event.completed)
                return
        elif event.kind == "batch_finished":
            self._last_batch_progress_key = None
            if self._progress is not None:
                if self._progress_task is not None:
                    self._progress.update(self._progress_task, completed=event.total)
                self._progress.stop()
                self._progress = None
                self._progress_task = None
                return

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


@dataclass(frozen=True)
class RuntimeEvent:
    """Canonical lifecycle fact emitted by runtime services."""

    name: str
    session_id: str = ""
    channel_name: str = ""
    fields: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class EventCollector:
    """Append-only collector for ``RuntimeEvent`` instances, scoped to one turn.

    Set via ``_active_event_collector`` ContextVar by AgentCore at turn start.
    Components emit events into it.  When no collector is active the ContextVar
    returns ``None`` and calls are safe no-ops.
    """

    __slots__ = ("_events",)

    def __init__(self) -> None:
        self._events: list[RuntimeEvent] = []

    def emit(self, name: str, **fields: object) -> None:
        self._events.append(RuntimeEvent(name=name, fields=dict(fields)))

    def drain(self) -> tuple[RuntimeEvent, ...]:
        events = tuple(self._events)
        self._events.clear()
        return events


_active_event_collector: contextvars.ContextVar[EventCollector | None] = (
    contextvars.ContextVar("_active_event_collector", default=None)
)

# Set by BaseAgent.send_message before tool execution: the assistant's
# most recent text response.  Used by RegularToolExecutor to enforce
# the intent-before-action protocol.
_active_assistant_text: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_active_assistant_text", default=""
)


__all__ = [
    "CliOutputSink",
    "EventCollector",
    "OutputSink",
    "RuntimeEvent",
    "_active_event_collector",
    "_active_assistant_text",
    "_active_sink",
    "_fmt_tool_inputs",
]
