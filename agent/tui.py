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
from prompt_toolkit.mouse_events import MouseEventType

from rich.console import Console


class _OutputPane:
    """File-like target for rich Console that feeds the TUI output window.

    Rich renders every CLI message (markdown streaming, tool lines, tables)
    into this pane as ANSI text.  The fragments are re-parsed lazily and the
    cursor position is pinned to the last line so the window always follows
    the newest output instead of scrolling back to the top.
    """

    def __init__(
        self,
        on_write: Optional[Callable[[], None]] = None,
        *,
        view_height: Optional[int] = None,
    ) -> None:
        self._on_write = on_write
        self._raw = ""
        self._fragments: StyleAndTextTuples = []
        self._dirty = False
        self._fixed_view_height = view_height
        # Top line of the visible window; 0 = beginning, max = newest output.
        self._scroll_top = 0
        self._follow_bottom = True
        self._window: Optional[Any] = None

    def attach_window(self, window: Any) -> None:
        self._window = window

    def write(self, text: str) -> int:
        if text:
            self._raw += text
            self._dirty = True
            if self._follow_bottom:
                self._scroll_top = self._max_scroll()
            self._notify()
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
        return Point(
            x=0,
            y=min(self._scroll_top + self._view_height() - 1, self._last_line()),
        )

    def scroll_up(self, lines: int = 3) -> None:
        """Scroll the view backwards into the conversation history."""
        self._follow_bottom = False
        self._scroll_top = max(0, self._scroll_top - lines)
        self._notify()

    def scroll_down(self, lines: int = 3) -> None:
        """Scroll the view towards the newest output."""
        self._scroll_top = min(self._scroll_top + lines, self._max_scroll())
        if self._scroll_top >= self._max_scroll():
            self._follow_bottom = True
        self._notify()

    def scroll_top(self) -> None:
        self._follow_bottom = False
        self._scroll_top = 0
        self._notify()

    def scroll_bottom(self) -> None:
        self._follow_bottom = True
        self._scroll_top = self._max_scroll()
        self._notify()

    def vertical_scroll(self, window: Any) -> int:
        """Window hook: keep the rendered scroll in sync with the pane."""
        info = getattr(window, "render_info", None)
        height = getattr(info, "window_height", None) or self._view_height()
        return min(self._scroll_top, max(0, self._last_line() - height + 1))

    def _last_line(self) -> int:
        return max(0, len(self._raw.split("\n")) - 1)

    def _view_height(self) -> int:
        if self._fixed_view_height is not None:
            return max(1, self._fixed_view_height)
        from prompt_toolkit.application.current import get_app

        try:
            rows = get_app().output.get_size().rows
        except Exception:
            rows = 40
        return max(1, rows - 2)

    def _max_scroll(self) -> int:
        return max(0, self._last_line() - self._view_height() + 1)

    def _notify(self) -> None:
        if self._window is not None:
            self._window.vertical_scroll = self._scroll_top
        if self._on_write is not None:
            self._on_write()


class _ScrollableOutputWindow(Window):
    """Output window that maps the mouse wheel to conversation scrolling."""

    def __init__(self, pane: _OutputPane, *args: Any, **kwargs: Any) -> None:
        self._output_pane = pane
        super().__init__(*args, **kwargs)

    def _mouse_handler(self, mouse_event: Any):
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._output_pane.scroll_up(lines=3)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self._output_pane.scroll_down(lines=3)
            return None
        return super()._mouse_handler(mouse_event)


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
            mouse_support=True,
        )

    # ── Layout ────────────────────────────────────────────────────────────

    def _build_layout(self):
        output_window = _ScrollableOutputWindow(
            self._pane,
            FormattedTextControl(
                self._pane.fragments,
                get_cursor_position=self._pane.cursor_position,
            ),
            wrap_lines=True,
            always_hide_cursor=True,
            get_vertical_scroll=self._pane.vertical_scroll,
        )
        self._pane.attach_window(output_window)
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
                "Enter 发送 · 输入 / 实时筛选命令 · ↑/↓ 或翻页滚动对话 · "
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
            self._pane.scroll_bottom()
            self._submit(text)

        @kb.add("up")
        def _up(event: Any) -> None:
            buffer = event.app.current_buffer
            if buffer.complete_state is not None:
                buffer.complete_previous()
            elif not buffer.text:
                self._pane.scroll_up(lines=3)

        @kb.add("down")
        def _down(event: Any) -> None:
            buffer = event.app.current_buffer
            if buffer.complete_state is not None:
                buffer.complete_next()
            elif not buffer.text:
                self._pane.scroll_down(lines=3)

        @kb.add("pageup")
        def _page_up(event: Any) -> None:
            self._pane.scroll_up(lines=10)

        @kb.add("pagedown")
        def _page_down(event: Any) -> None:
            self._pane.scroll_down(lines=10)

        @kb.add("home")
        def _home(event: Any) -> None:
            self._pane.scroll_top()

        @kb.add("end")
        def _end(event: Any) -> None:
            self._pane.scroll_bottom()

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
