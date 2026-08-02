from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
import functools
import json
import logging
import re
import sys
import time
from abc import ABC
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional

from rich.markdown import Markdown
from rich.markup import escape as _markup_escape
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.text import Text

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.application.run_in_terminal import in_terminal
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.validation import Validator

logger = logging.getLogger(__name__)

_APPROVAL_PROMPT = PromptSession(history=InMemoryHistory())
# Serializes concurrent consent flows.  The tool loop runs medium-risk
# shell calls in parallel (asyncio.gather); the shell tool acquires this
# lock around its allowlist re-check + sink prompt + token redemption so
# two prompt_toolkit sessions never share one terminal and an identical
# command approved by the first parallel call is not asked about twice.
_APPROVAL_LOCK = asyncio.Lock()

_APPROVAL_ACCEPT_ANSWERS = frozenset({"1", "y", "yes", "同意", "批准"})
_APPROVAL_DECLINE_ANSWERS = frozenset({"2", "n", "no", "拒绝"})
_APPROVAL_VALIDATOR = Validator.from_callable(
    lambda text: text.strip().casefold() in (
        _APPROVAL_ACCEPT_ANSWERS | _APPROVAL_DECLINE_ANSWERS
    ),
    error_message="请输入 1（批准执行）或 2（拒绝）",
    move_cursor_to_end=True,
)


def _approval_choice_accepted(answer: str) -> bool:
    """Map an approval-menu answer to consent (True) or decline (False)."""
    return str(answer or "").strip().casefold() in _APPROVAL_ACCEPT_ANSWERS


def _consent_pending() -> bool:
    """True while a shell consent prompt is open (deadline guard)."""
    return _APPROVAL_LOCK.locked()


def _deferred_during_consent(fn):
    """Buffer one sink render while an approval menu is on screen.

    Parallel tool calls keep emitting tool results, heartbeats, and progress
    while the human is deciding; prompt_toolkit and rich must never write to
    the terminal at the same time, or the menu becomes unreadable.  The
    buffered renders flush as soon as the consent prompt closes.
    """

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        if self._approval_active:
            self._approval_buffers.append(
                lambda: fn(self, *args, **kwargs)
            )
            return
        return fn(self, *args, **kwargs)

    return wrapper

# Inline markdown tokens styled per-line while streaming (code, bold,
# italic, links).  Order matters: ** before * so bold consumes both stars.
_INLINE_TOKEN_RE = re.compile(
    r"(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|\[[^\]]*\]\([^)]*\))"
)


def _append_inline_styles(text: Text, value: str) -> None:
    """Append ``value`` to ``text`` with per-token inline styling."""
    position = 0
    for match in _INLINE_TOKEN_RE.finditer(value):
        if match.start() > position:
            text.append(value[position : match.start()])
        token = match.group(1)
        if token.startswith("`") and len(token) >= 2:
            text.append(token[1:-1], style="cyan")
        elif token.startswith("**") and len(token) >= 4:
            text.append(token[2:-2], style="bold")
        elif token.startswith("["):
            link = re.match(r"\[([^\]]*)\]\(([^)]*)\)", token)
            label = link.group(1) if link else token
            text.append(label, style="underline cyan")
        else:
            text.append(token[1:-1], style="italic")
        position = match.end()
    if position < len(value):
        text.append(value[position:])


def _render_markdown_line(line: str) -> Text:
    """Render one complete markdown line as a styled rich Text."""
    stripped = line.strip()
    if not stripped:
        return Text()
    heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
    if heading:
        level = len(heading.group(1))
        text = Text()
        text.append("#" * level + " ", style="bold dim")
        _append_inline_styles(text, heading.group(2))
        text.stylize("bold" if level <= 2 else "bold dim")
        return text
    if re.fullmatch(r"([-*_])\1{2,}", stripped):
        return Text("─" * min(60, max(10, len(stripped))), style="dim")
    if stripped.startswith(">"):
        text = Text()
        text.append(line, style="dim italic")
        return text
    bullet = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", line)
    if bullet:
        text = Text()
        text.append(bullet.group(1) + bullet.group(2) + " ", style="dim")
        _append_inline_styles(text, bullet.group(3))
        return text
    text = Text()
    _append_inline_styles(text, line)
    return text


class _StreamMarkdown:
    """Line-buffered streaming markdown renderer.

    Completed lines are printed once, styled, so terminal scrollback is
    preserved and nothing is re-rendered.  Fenced code blocks are buffered
    until the closing fence so they can be syntax-highlighted as a unit; a
    truncated fence is flushed as plain lines at turn end.
    """

    def __init__(self, console: Any) -> None:
        self._console = console
        self._partial = ""
        self._in_fence = False
        self._fence_lang = ""
        self._fence_lines: list[str] = []

    def feed(self, text: str) -> None:
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            self._line(line)

    def _line(self, line: str) -> None:
        stripped = line.strip()
        if self._in_fence:
            if stripped == "```":
                self._emit_fence()
                self._in_fence = False
            else:
                self._fence_lines.append(line)
            return
        if stripped.startswith("```"):
            self._in_fence = True
            self._fence_lang = stripped[3:].strip()
            self._fence_lines = []
            return
        self._console.print(_render_markdown_line(line))

    def _emit_fence(self) -> None:
        code = "\n".join(self._fence_lines)
        lang = self._fence_lang or "text"
        try:
            from rich.syntax import Syntax

            self._console.print(
                Syntax(
                    code,
                    lexer=lang,
                    background_color="default",
                    word_wrap=True,
                )
            )
        except Exception:
            for fence_line in self._fence_lines:
                self._console.print(fence_line)
        self._fence_lines = []

    def flush(self) -> None:
        """Flush a truncated fence and any unterminated partial line."""
        if self._in_fence:
            for fence_line in self._fence_lines:
                self._console.print(fence_line)
            self._fence_lines = []
            self._in_fence = False
        if self._partial:
            self._console.print(_render_markdown_line(self._partial))
            self._partial = ""

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


def _clip_single_line(value: Any, limit: int = 120) -> str:
    text = _redact_sensitive_text(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _summarize_tool_result(result: str) -> tuple[bool | None, str]:
    """Return a safe, compact CLI summary instead of dumping tool JSON."""
    try:
        payload = json.loads(result)
    except Exception:
        return None, _clip_single_line(result)
    if not isinstance(payload, dict):
        return None, _clip_single_line(result)

    ok = bool(payload.get("ok", True))
    if not ok:
        return False, _clip_single_line(payload.get("error") or "Tool failed")

    summary = payload.get("summary_text")
    if summary:
        return True, _clip_single_line(summary)

    parts: list[str] = []
    for key, label in (
        ("path", "path"),
        ("count", "items"),
        ("bytes_written", "bytes"),
        ("exit_code", "exit"),
    ):
        value = payload.get(key)
        if value is not None and value != "":
            if key == "path":
                # Keep the whole path: it is the payload of the line, and a
                # clipped tail would render as a broken clickable link.
                clean = _redact_sensitive_text(str(value)).replace("\n", "↵")
                parts.append(f"{label}={clean}")
            else:
                parts.append(f"{label}={_clip_single_line(value, 72)}")
    if parts:
        return True, " · ".join(parts)

    output = payload.get("output")
    if output:
        return True, _clip_single_line(output)
    return True, "Completed"


_SUMMARY_PATH_RE = re.compile(
    r"path=(?:'(?P<sq>[^']*)'|\"(?P<dq>[^\"]*)\"|(?P<bare>[^ ·\n]+))"
)


def _path_uri(value: str) -> str | None:
    """Best-effort ``file://`` URI for a displayed local path.

    Returns None when the value cannot be treated as a filesystem path, in
    which case the caller should render it as plain text.
    """
    candidate = str(value).strip().rstrip("…")
    if not candidate:
        return None
    try:
        path = Path(candidate).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve(strict=False).as_uri()
    except (OSError, ValueError):
        return None


def _render_path_links(text: str, *, style: str = "") -> Text:
    """Render summary text, turning ``path=...`` spans into clickable links.

    Rich emits OSC 8 hyperlinks for ``link`` styles, so terminals that support
    them (iTerm2, VS Code, Warp, WezTerm, kitty) open the file on click.  The
    TUI pane strips the OSC sequences and keeps the underlined path text.
    """
    out = Text(style=style)
    position = 0
    for match in _SUMMARY_PATH_RE.finditer(text):
        out.append(text[position : match.start()])
        value = match.group("sq") or match.group("dq") or match.group("bare")
        uri = _path_uri(value)
        if uri is None:
            out.append(match.group(0))
        else:
            out.append(
                match.group(0),
                style=f"{style} underline link {uri}",
            )
        position = match.end()
    out.append(text[position:])
    return out


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

    async def on_tool_confirmation(
        self,
        name: str,
        *,
        command: str,
        risk_level: str,
        reason: str,
        confirmation_token: str,
        scope: Any,
    ) -> bool:
        """Ask the human whether a medium-risk tool action may proceed.

        Returns True only after the human approved in their own medium.
        The default returns False because there is no interactive human at
        this sink; callers must then fall back to the structured
        confirmation-required result instead of assuming consent.
        """
        return False

    @property
    def interactive_confirmation(self) -> bool:
        """True when this sink can show an interactive approval menu.

        Non-interactive sinks (gateway/Feishu, pipes) leave a structured
        confirmation-required result plus a pending record so the human can
        approve with a chat reply instead.
        """
        return False

    def on_info(self, content: Any) -> None:
        """Display an informational renderable."""

    def on_status(self, text: str, *, level: str = "info") -> None:
        """Display a status message."""

    def on_error(self, error: str) -> None:
        """Display an error message."""

    def on_subagent_event(self, event: "SubAgentProgressEvent") -> None:
        """Display structured multi-agent progress."""

    def on_heartbeat(
        self,
        *,
        elapsed_seconds: float,
        current_op: str,
        op_detail: str = "",
        pending_messages: int = 0,
    ) -> None:
        """Periodic 'agent is alive' tick during long-running operations.

        Fired every few seconds by the per-turn heartbeat coroutine while
        any LLM call, tool, or sub-agent batch is in flight.  Sinks that
        support live UI (Feishu cards, future TUI) override this to keep
        the user informed that the agent is working — not stuck.

        Default is a no-op so non-live sinks (file logs, CLI prints)
        ignore the tick.

        Args:
            elapsed_seconds: time since the current op started
            current_op: short label like "shell", "LLM", "sub-agent batch"
            op_detail: secondary info ("git clone https://...", "3/5 done")
            pending_messages: count of un-drained mailbox entries (0 = empty)
        """

    def on_notification(self, title: str, body: str, *, level: str = "info") -> None:
        """Called for proactive notifications (reminders, summaries) not tied to a user turn.

        Default is a no-op. Channels override this to implement delivery
        (e.g. Feishu sends a message, CLI prints a panel).
        """

    def queue_attachment(self, path: Path) -> object | None:
        """Queue an attachment and optionally return an opaque cleanup receipt."""

    async def flush_attachments(self) -> None:
        """Consume attachments queued so far before returning."""

    def defer_temporary_attachment_cleanup(self, receipt: object) -> bool:
        """Return true only after irrevocably assuming responsibility for cleanup.

        Implementations must return false, not raise, when ownership cannot be
        transferred.
        """

        return False

    def sync_stream_cb(self, chunk: str) -> None:
        """Synchronous callback adapter for BaseAgent.send_message."""

        self.on_stream_chunk(chunk)


class CliOutputSink(OutputSink):
    """Rich-console implementation of OutputSink for the CLI channel."""

    def __init__(
        self,
        console: Any,
        *,
        live_status: bool = True,
        can_prompt: bool | None = None,
    ) -> None:
        self._console = console
        self._live_status = live_status
        # Whether a human can be prompted for consent.  This is NOT the same
        # capability as rendering a live spinner: the full-screen TUI cannot
        # host a spinner but prompts fine (via prompt_toolkit's in_terminal).
        # Deriving consent from the spinner flag silently disables the approval
        # menu on the default interactive path.
        self._can_prompt = live_status if can_prompt is None else bool(can_prompt)
        self._streamed: list[str] = []
        self._last_batch_progress_key: tuple[int, int] | None = None
        self._tool_count = 0
        self._tool_start_times: dict[str, float] = {}
        self._approval_active = False
        self._approval_buffers: list[Callable[[], None]] = []
        self._progress: Progress | None = None
        self._progress_task: Any = None
        self._tool_progress: Progress | None = None
        self._tool_progress_task: Any = None
        self._stream_md: _StreamMarkdown | None = None
        self._last_heartbeat_at = 0.0
        self._last_heartbeat_op = ""
        self._last_tool_progress = ""
        self._activity: Any = None
        self._line_open = False
        self._turn_started_at = time.monotonic()

    def begin_turn(self) -> None:
        """Open a visually distinct response region for an interactive turn."""
        self._turn_started_at = time.monotonic()
        self._console.print("[bold cyan]Agent[/bold cyan]")
        self._set_activity("Preparing response…")

    def _supports_live_status(self) -> bool:
        # ``is_terminal`` alone is not enough: the full-screen TUI console is
        # force-marked as a terminal so Rich emits ANSI, but its backing file is
        # the output pane, not a TTY.  A live render there (Rich Progress /
        # Status) writes ``\r``-based redraws that the pane accumulates as
        # garbage instead of updating a line, so live status requires a real
        # TTY file behind the console.
        console_file = getattr(self._console, "file", None)
        file_isatty = bool(
            console_file is not None
            and callable(getattr(console_file, "isatty", None))
            and console_file.isatty()
        )
        return bool(
            self._live_status
            and getattr(self._console, "is_terminal", False)
            and file_isatty
            and callable(getattr(self._console, "status", None))
        )

    def _supports_stream_markdown(self) -> bool:
        return bool(
            getattr(self._console, "is_terminal", False)
            and callable(getattr(self._console, "print", None))
        )

    def _supports_consent_prompt(self) -> bool:
        return bool(
            self._can_prompt
            and getattr(self._console, "is_terminal", False)
            and sys.stdin.isatty()
        )

    @property
    def interactive_confirmation(self) -> bool:
        return self._supports_consent_prompt()

    def _set_activity(self, text: str) -> None:
        if not self._supports_live_status():
            return
        label = f"[dim]{_markup_escape(_clip_single_line(text, 160))}[/dim]"
        if self._activity is None:
            self._activity = self._console.status(label, spinner="dots")
            self._activity.start()
            return
        self._activity.update(label)

    def _stop_activity(self) -> None:
        if self._activity is None:
            return
        self._activity.stop()
        self._activity = None

    def _finish_open_line(self) -> None:
        if self._line_open:
            self._console.print()
            self._line_open = False

    @_deferred_during_consent
    def on_stream_chunk(self, chunk: str) -> None:
        self._stop_activity()
        self._streamed.append(chunk)
        if self._supports_stream_markdown():
            if self._stream_md is None:
                self._stream_md = _StreamMarkdown(self._console)
            self._stream_md.feed(chunk)
            return
        self._console.print(chunk, end="", markup=False)
        self._line_open = bool(chunk) and not chunk.endswith("\n")

    def on_turn_complete(self, full_text: str, tool_calls: list[str]) -> None:
        self._stop_activity()
        if self._progress is not None:
            self._progress.stop()
            self._progress = None
            self._progress_task = None
        if self._tool_progress is not None:
            self._tool_progress.stop()
            self._tool_progress = None
            self._tool_progress_task = None
        self._finish_open_line()
        if self._stream_md is not None:
            self._stream_md.flush()
            self._stream_md = None
        if not self._streamed and full_text:
            self._console.print(Markdown(full_text))
        elapsed = max(0.0, time.monotonic() - self._turn_started_at)
        if self._tool_count or elapsed >= 2.0:
            details = []
            if self._tool_count:
                suffix = "" if self._tool_count == 1 else "s"
                details.append(f"{self._tool_count} tool{suffix}")
            if elapsed >= 2.0:
                details.append(f"{elapsed:.1f}s")
            self._console.print(f"[dim]Done · {' · '.join(details)}[/dim]")
        self._console.print()
        self._streamed.clear()
        self._tool_count = 0

    @_deferred_during_consent
    def on_tool_start(self, name: str, inputs: dict) -> None:
        self._stop_activity()
        self._finish_open_line()
        hint = _fmt_tool_inputs(name, inputs)
        self._console.print(f"[cyan]›[/cyan] [bold]{_markup_escape(name)}[/bold]{hint}")
        self._tool_start_times[name] = time.monotonic()
        self._tool_count += 1
        self._set_activity(f"Running {name}…")

    @_deferred_during_consent
    def on_tool_end(self, name: str, result: str) -> None:
        self._stop_activity()
        if self._tool_progress is not None:
            self._tool_progress.stop()
            self._tool_progress = None
            self._tool_progress_task = None
        elapsed = ""
        start = self._tool_start_times.pop(name, None)
        if start is not None:
            elapsed = f" [dim]({time.monotonic() - start:.1f}s)[/dim]"
        ok, summary = _summarize_tool_result(result)
        line = Text()
        if ok is True:
            line.append("✓", style="green")
        elif ok is False:
            line.append("✗", style="red")
        else:
            line.append("·", style="dim")
        line.append(" ")
        line.append_text(_render_path_links(summary, style="dim"))
        if elapsed:
            line.append_text(Text.from_markup(elapsed))
        self._console.print(line)
        self._set_activity("Processing results…")

    @_deferred_during_consent
    def on_tool_blocked(self, name: str, reason: str) -> None:
        self._stop_activity()
        self._finish_open_line()
        self._console.print(
            f"[yellow]![/yellow] [bold]{_markup_escape(name)}[/bold] "
            f"[yellow]{_markup_escape(_clip_single_line(reason))}[/yellow]"
        )
        self._set_activity("Processing results…")

    async def on_tool_confirmation(
        self,
        name: str,
        *,
        command: str,
        risk_level: str,
        reason: str,
        confirmation_token: str,
        scope: Any,
    ) -> bool:
        """Ask the human at the terminal whether a medium-risk action may run.

        Only an interactive terminal qualifies; every other sink declines,
        leaving the structured confirmation-required result for the caller.
        The human's answer here is the only path into the allowlist — the
        model can never fabricate it (the token is validated separately by
        shell.py).
        """
        if not self._supports_consent_prompt():
            return False
        self._approval_active = True
        try:
            self._stop_activity()
            self._finish_open_line()
            # The command is shown in full, wrapped rather than clipped: the
            # approval allowlists the entire string, so eliding its tail would
            # let a long benign prefix hide the part that actually matters.
            self._console.print(
                f"[yellow]需要批准的工具操作：[/yellow]\n"
                f"[bold]{_markup_escape(str(command))}[/bold]\n"
                f"[dim]风险等级: {_markup_escape(str(risk_level))} · "
                f"{_markup_escape(_clip_single_line(str(reason), 160))}[/dim]",
                soft_wrap=False,
            )
            self._console.print(
                "  [bold cyan]1)[/bold cyan] 批准执行\n"
                "  [bold cyan]2)[/bold cyan] 拒绝\n"
                "[dim]输入 1/2 后按 Enter（也可输入 y/n 或 同意/拒绝）；Ctrl+C 取消[/dim]"
            )

            async def _prompt() -> str:
                return await _APPROVAL_PROMPT.prompt_async(
                    "请选择 1/2 › ",
                    validator=_APPROVAL_VALIDATOR,
                )

            try:
                if get_app_or_none() is not None:
                    async with in_terminal():
                        answer = await _prompt()
                else:
                    answer = await _prompt()
            except (KeyboardInterrupt, EOFError):
                return False
            return _approval_choice_accepted(answer)
        finally:
            self._approval_active = False
            self._flush_consent_buffer()

    def _flush_consent_buffer(self) -> None:
        pending = list(self._approval_buffers)
        self._approval_buffers.clear()
        for render in pending:
            try:
                render()
            except Exception:
                logger.exception("deferred consent-render failed")

    @_deferred_during_consent
    def on_tool_progress(self, name: str, progress: Mapping[str, Any]) -> None:
        status = str(progress.get("status") or "running")
        message = str(progress.get("message") or "").strip()
        current = progress.get("current")
        total = progress.get("total")
        if self._supports_live_status() and isinstance(total, (int, float)):
            if self._tool_progress is None:
                self._tool_progress = Progress(
                    BarColumn(bar_width=None),
                    TextColumn("{task.description}"),
                    TimeElapsedColumn(),
                    console=self._console,
                    transient=True,
                )
                self._tool_progress_task = self._tool_progress.add_task(
                    name,
                    total=float(total),
                )
                self._tool_progress.start()
            completed = float(current) if isinstance(current, (int, float)) else 0.0
            self._tool_progress.update(
                self._tool_progress_task,
                completed=completed,
                description=f"{name}: {message}" if message else name,
            )
            return
        suffix = ""
        if current is not None and total:
            try:
                suffix = f" {float(current) / float(total) * 100:.0f}%"
            except Exception:
                suffix = f" {current}/{total}"
        detail = f" - {message}" if message else ""
        text = f"{name}: {status}{suffix}{detail}"
        if text == self._last_tool_progress:
            return
        self._last_tool_progress = text
        if self._supports_live_status():
            self._set_activity(text)
            return
        self._console.print(f"[dim]↻ {_markup_escape(_clip_single_line(text, 200))}[/dim]")

    @_deferred_during_consent
    def on_notification(self, title: str, body: str, *, level: str = "info") -> None:
        from rich.panel import Panel

        self._stop_activity()
        self._finish_open_line()
        colors = {"info": "cyan", "warning": "yellow", "error": "red"}
        self._console.print(
            Panel(
                Markdown(body) if body else "",
                title=f"[bold {colors.get(level, 'cyan')}]{title}[/bold {colors.get(level, 'cyan')}]",
                border_style=colors.get(level, "cyan"),
            )
        )

    @_deferred_during_consent
    def on_info(self, content: Any) -> None:
        self._stop_activity()
        self._finish_open_line()
        self._console.print(content)

    @_deferred_during_consent
    def on_status(self, text: str, *, level: str = "info") -> None:
        self._stop_activity()
        self._finish_open_line()
        colors = {"info": "dim", "warning": "yellow", "success": "green", "error": "red"}
        color = colors.get(level, "dim")
        clean = _markup_escape(_redact_sensitive_text(str(text or "")))
        self._console.print(
            f"[{color}]{clean}[/{color}]"
        )

    @_deferred_during_consent
    def on_heartbeat(
        self,
        *,
        elapsed_seconds: float,
        current_op: str,
        op_detail: str = "",
        pending_messages: int = 0,
    ) -> None:
        if self._progress is not None:
            return
        now = time.monotonic()
        op = str(current_op or "working")
        if op == self._last_heartbeat_op and now - self._last_heartbeat_at < 10.0:
            return
        self._last_heartbeat_at = now
        self._last_heartbeat_op = op
        labels = {
            "LLM": "模型正在生成",
            "tools": "工具正在执行",
            "starting": "正在准备",
        }
        label = labels.get(op, op)
        detail = f" · {op_detail}" if op_detail else ""
        pending = f" · {pending_messages} 条新消息待处理" if pending_messages else ""
        activity_text = f"{label} ({elapsed_seconds:.0f}s){detail}{pending}"
        if self._supports_live_status():
            self._set_activity(activity_text)
            return
        self._console.print(
            f"[dim]… {_markup_escape(activity_text)}[/dim]"
        )

    @_deferred_during_consent
    def on_error(self, error: str) -> None:
        self._stop_activity()
        self._finish_open_line()
        clean = _markup_escape(_redact_sensitive_text(str(error or "Unknown error")))
        self._console.print(f"[red]Error[/red] [dim]{clean}[/dim]")

    @_deferred_during_consent
    def on_subagent_event(self, event: "SubAgentProgressEvent") -> None:
        if event.kind == "batch_started":
            self._stop_activity()
            self._last_batch_progress_key = None
            if event.total > 1 and self._supports_live_status():
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
        self._console.print(f"[{color}]{_markup_escape(str(msg))}[/{color}]")

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
