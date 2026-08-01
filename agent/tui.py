from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
import shutil
from typing import Any, Awaitable, Callable, Optional

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, DynamicCompleter
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples, to_formatted_text
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Float, FloatContainer, HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.layout.screen import Point

from rich.console import Console


class _OutputPane:
    """File-like target for rich Console that feeds the TUI output window.

    Rich renders every CLI message (markdown streaming, tool lines, tables)
    into this pane as ANSI text.  The fragments are re-parsed lazily and the
    cursor position is pinned to the last line so the window always follows
    the newest output instead of scrolling back to the top.
    """

    def __init__(self, on_write: Optional[Callable[[], None]] = None) -> None:
        self._on_write = on_write
        self._raw = ""
        self._fragments: StyleAndTextTuples = []
        self._dirty = False

    def write(self, text: str) -> int:
        if text:
            self._raw += text
            self._dirty = True
            if self._on_write is not None:
                self._on_write()
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def fragments(self) -> StyleAndTextTuples:
        if self._dirty:
            self._fragments = list(to_formatted_text(ANSI(self._raw)))
            self._dirty = False
        return self._fragments

    def cursor_position(self) -> Point:
        lines = self._raw.split("\n")
        return Point(x=0, y=max(0, len(lines) - 1))


class TuiSession:
    """Full-screen chat layout: scrollable output above a fixed input line.

    The agent output stream (rich Console) renders into the upper pane, which
    auto-scrolls to the newest line, while the input prompt stays docked at
    the bottom of the terminal.  The input buffer keeps the live slash-command
    palette and persistent history from the line-mode CLI.
    """

    def __init__(
        self,
        *,
        history_path: Path,
        completer_factory: Callable[[], Optional[Completer]],
        cancel_callback: Callable[[], None],
        console_width: Optional[int] = None,
    ) -> None:
        self._history_path = history_path
        self._cancel_callback = cancel_callback
        self._input_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self._app_exit_requested = False

        self._pane = _OutputPane(on_write=self._request_redraw)
        width = console_width or max(80, shutil.get_terminal_size().columns)
        self.console = Console(
            file=self._pane,
            force_terminal=True,
            color_system="standard",
            highlight=False,
            width=width,
        )
        self._buffer = Buffer(
            completer=DynamicCompleter(completer_factory),
            complete_while_typing=True,
            history=FileHistory(str(history_path)),
        )
        self._app = Application(
            layout=Layout(container=self._build_layout()),
            key_bindings=self._build_key_bindings(),
            full_screen=True,
            mouse_support=False,
        )

    # ── Layout ────────────────────────────────────────────────────────────

    def _build_layout(self):
        output_window = Window(
            FormattedTextControl(
                self._pane.fragments,
                get_cursor_position=self._pane.cursor_position,
            ),
            wrap_lines=True,
            always_hide_cursor=True,
            get_vertical_scroll=lambda window: 10**9,
        )
        input_window = Window(
            BufferControl(
                buffer=self._buffer,
                input_processors=[BeforeInput("› ")],
            ),
            height=1,
            style="class:input-line",
        )
        hint_window = Window(
            FormattedTextControl(
                "Enter 发送 · 输入 / 实时筛选命令 · ↑/↓ 历史 · "
                "Ctrl+C 取消 · Ctrl+D 退出"
            ),
            height=1,
            style="class:bottom-hint",
        )
        return FloatContainer(
            content=HSplit([output_window, input_window, hint_window]),
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=8),
                ),
            ],
        )

    # ── Key bindings ──────────────────────────────────────────────────────

    def _build_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("enter")
        def _enter(event: Any) -> None:
            buffer = event.app.current_buffer
            if buffer.complete_state is not None and buffer.text.strip() != "/":
                if (
                    buffer.complete_state.complete_index is None
                    and buffer.complete_state.completions
                ):
                    buffer.go_to_completion(0)
                if (
                    buffer.complete_state is not None
                    and buffer.complete_state.current_completion is not None
                ):
                    buffer.apply_completion(buffer.complete_state.current_completion)
                buffer.complete_state = None
            buffer.append_to_history()
            text = buffer.text
            buffer.reset()
            self._submit(text)

        @kb.add("up")
        def _up(event: Any) -> None:
            buffer = event.app.current_buffer
            if buffer.complete_state is not None:
                buffer.complete_previous()
            elif not buffer.text:
                buffer.history_backward(count=event.arg)

        @kb.add("down")
        def _down(event: Any) -> None:
            buffer = event.app.current_buffer
            if buffer.complete_state is not None:
                buffer.complete_next()
            elif not buffer.text:
                buffer.history_forward(count=event.arg)

        @kb.add("c-c")
        @kb.add(Keys.SIGINT)
        def _ctrl_c(event: Any) -> None:
            self._cancel_callback()

        @kb.add("c-d")
        def _ctrl_d(event: Any) -> None:
            buffer = event.app.current_buffer
            if not buffer.text:
                self._submit(None)
                self.request_exit()
            else:
                buffer.delete()

        return kb

    # ── Public API used by the interactive loop ───────────────────────────

    async def run(self, main_coro: Awaitable[None]) -> None:
        """Drive the full-screen app until the main coroutine finishes.

        The app stays alive while the interactive loop runs; once the loop
        ends (normal quit, Ctrl+C/D at idle, or an error), the app is closed
        so the terminal is restored.
        """

        async def _wrapped() -> None:
            try:
                await main_coro
            finally:
                self.request_exit()

        task = asyncio.create_task(_wrapped())
        app_task = asyncio.create_task(self._app.run_async())
        try:
            while get_app_or_none() is not self._app and not app_task.done():
                await asyncio.sleep(0)
            await task
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            app = get_app_or_none()
            if app is self._app and app._is_running:
                app.exit(result=None)
            with suppress(asyncio.CancelledError):
                await app_task

    async def ask_async(self) -> Optional[str]:
        """Return the next submitted input line (None when exiting)."""
        return await self._input_queue.get()

    def _submit(self, text: Optional[str]) -> None:
        self._input_queue.put_nowait(text)

    def request_exit(self) -> None:
        if self._app_exit_requested:
            return
        self._app_exit_requested = True
        self._input_queue.put_nowait(None)

    def _request_redraw(self) -> None:
        app = get_app_or_none()
        if app is not None and app._is_running:
            app.invalidate()
