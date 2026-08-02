from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
import re
import shutil
import sys
import threading
import time
from typing import Any, Awaitable, Callable, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, DynamicCompleter
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples, to_formatted_text
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Float, FloatContainer, HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.layout.screen import Point
from prompt_toolkit.mouse_events import MouseButton
from prompt_toolkit.mouse_events import MouseEventType

from rich.console import Console


_TRAILING_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-9;?]*)?$")

# Terminal sequences Rich emits that prompt_toolkit's ANSI parser cannot
# interpret: live redraws (progress bars, spinners) use ``\r`` plus erase-line
# (``CSI K``), cursor show/hide (``CSI ?25 l/h``) would leak as raw digits, and
# OSC 8 hyperlinks (``OSC 8 ; ... ; ST``) would leak as ``8;;file://...`` text.
# Erase-line is already dropped by the parser; strip the rest explicitly so the
# visible path text survives without control-sequence garbage.
_LIVE_REDRAW_SEQUENCES_RE = re.compile(
    r"\x1b\[[0-9]*K|\x1b\[\?25[hl]|\x1b\][^\x1b]*\x1b\\"
)

_PATH_MENU_PROMPT = PromptSession(history=InMemoryHistory())

# Paths shown in output lines: either an explicit ``path=`` value (quoted or
# bare, absolute or relative) or a bare absolute/``~/`` path in prose.
_PATH_EXTRACT_RE = re.compile(
    r"path=(?:'([^']+)'|\"([^\"]+)\"|([^\s'\"，。]+))"
    r"|((?:/|~/)[^\s'\"，。]+)"
)
_PATH_TRAILING_PUNCT = ",.;:!?)]}，。；：！？）】」』"


def _paths_from_line(text: str) -> list[tuple[str, Path]]:
    """Existing local paths mentioned in one output line, in order."""
    found: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for match in _PATH_EXTRACT_RE.finditer(text):
        raw = next((group for group in match.groups() if group), "")
        raw = raw.strip().rstrip(_PATH_TRAILING_PUNCT).strip("\"'")
        if not raw:
            continue
        try:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = Path.cwd() / path
            path = path.resolve(strict=False)
        except (OSError, ValueError):
            continue
        if not path.exists():
            continue
        uri = path.as_uri()
        if uri in seen:
            continue
        seen.add(uri)
        found.append((raw, path))
    return found


async def _copy_to_clipboard(path: Path) -> bool:
    """Copy a path string to the system clipboard."""
    if sys.platform == "darwin":
        args = ["pbcopy"]
    elif sys.platform.startswith("win"):
        args = ["clip"]
    else:
        args = ["xclip", "-selection", "clipboard"]
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.communicate(str(path).encode("utf-8"))
        return process.returncode == 0
    except (FileNotFoundError, OSError):
        return False


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
        self._fragments: StyleAndTextTuples = []
        self._pending_escape = ""
        # Number of newlines written; the last line index equals this count.
        self._line_count = 0
        self._fixed_view_height = view_height
        # Top line of the visible window; 0 = beginning, max = newest output.
        self._scroll_top = 0
        self._follow_bottom = True
        self._window: Optional[Any] = None
        self._rows_cache_value = 40
        self._rows_cache_time = 0.0
        # write() and the scroll mutators do read-modify-write on _fragments,
        # _line_count, _pending_escape and _scroll_top.  The background memory
        # worker is a daemon thread that prints to shared.CONSOLE, which is
        # rebound to the TUI console for the session — so these run concurrently
        # with the event loop.  Rich's own lock only serializes whole print()
        # calls, which is incidental protection this class does not control.
        self._lock = threading.RLock()
        # Last bottom position the renderer reported, in logical lines.  Wrap
        # information only exists at render time, so the pane has to remember
        # it for scroll operations that happen between renders.
        self._rendered_bottom = 0

    def attach_window(self, window: Any) -> None:
        self._window = window

    def write(self, text: str) -> int:
        if not text:
            return 0
        with self._lock:
            return self._write_locked(text)

    def _write_locked(self, text: str) -> int:
        # A newline can never continue an escape sequence.  If a partial
        # escape is still buffered, the stream abandoned it; drop it so the
        # ANSI parser does not swallow the newline as an unsupported CSI
        # character (which would desynchronize the line counter from the
        # rendered content).
        if self._pending_escape and text.startswith("\n"):
            self._pending_escape = ""
        # Parse incrementally: re-parsing the whole conversation on every
        # write is O(total output) per render and makes the UI unusable once
        # the session grows.  A trailing partial escape sequence is held and
        # completed by the next chunk (rich writes complete lines, so this is
        # only needed for streamed plain-text tails).
        chunk = self._pending_escape + text
        self._pending_escape = ""
        tail = _TRAILING_ESCAPE_RE.search(chunk)
        if tail is not None:
            self._pending_escape = tail.group(0)
            chunk = chunk[: tail.start()]
        if chunk:
            # Rich live displays redraw the current line with "\r" plus an
            # erase-line sequence.  Emulate the terminal instead of appending
            # every frame: "\r\n" is an ordinary newline, and a segment after a
            # bare "\r" replaces the current logical line.
            chunk = chunk.replace("\r\n", "\n")
            chunk = _LIVE_REDRAW_SEQUENCES_RE.sub("", chunk)
            for index, segment in enumerate(chunk.split("\r")):
                if not segment:
                    continue
                if index > 0:
                    self._replace_current_line()
                parsed = list(to_formatted_text(ANSI(segment)))
                self._fragments.extend(parsed)
                # Line accounting comes from what the parser actually emitted,
                # never from the raw input: the cursor must always point at a
                # real rendered line, even if the parser drops characters from
                # a malformed escape stream.
                self._line_count += sum(
                    item[1].count("\n") for item in parsed if len(item) >= 2
                )
        if self._follow_bottom:
            self._scroll_top = self._max_scroll()
        self._notify()
        return len(text)

    def _replace_current_line(self) -> None:
        """Drop fragments belonging to the unterminated current line.

        Called after a carriage return: the incoming segment redraws the line
        from column 0, so everything after the last newline is discarded.  The
        truncated content contains no newline, so ``_line_count`` stays valid.
        """
        while self._fragments:
            frag = self._fragments[-1]
            if len(frag) >= 2 and "\n" in frag[1]:
                cut = frag[1].rfind("\n")
                if cut >= 0:
                    self._fragments[-1] = (frag[0], frag[1][: cut + 1]) + frag[2:]
                return
            self._fragments.pop()

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def fragments(self) -> StyleAndTextTuples:
        return self._fragments

    def cursor_position(self) -> Point:
        return Point(
            x=0,
            y=min(self._scroll_top + self._view_height() - 1, self._last_line()),
        )

    def scroll_up(self, lines: int = 3) -> None:
        """Scroll the view backwards into the conversation history."""
        with self._lock:
            self._follow_bottom = False
            self._scroll_top = max(0, self._scroll_top - lines)
        self._notify()

    def scroll_down(self, lines: int = 3) -> None:
        """Scroll the view towards the newest output."""
        with self._lock:
            bottom = self._known_bottom()
            self._scroll_top = min(self._scroll_top + lines, bottom)
            if self._scroll_top >= bottom:
                self._follow_bottom = True
        self._notify()

    def scroll_top(self) -> None:
        with self._lock:
            self._follow_bottom = False
            self._scroll_top = 0
        self._notify()

    def scroll_bottom(self) -> None:
        with self._lock:
            self._follow_bottom = True
            self._scroll_top = self._known_bottom()
        self._notify()

    def _known_bottom(self) -> int:
        """Best available bottom position, in logical lines.

        Prefers what the renderer last reported (which accounts for wrapping)
        and falls back to logical accounting before the first render.
        """
        return max(self._rendered_bottom, self._max_scroll())

    def vertical_scroll(self, window: Any) -> int:
        """Window hook: keep the rendered scroll in sync with the pane.

        ``vertical_scroll`` is a *logical* line index, but the window renders
        with ``wrap_lines=True``, so one logical line can occupy several display
        rows.  Clamping with the logical line count therefore pins the view too
        high whenever output is wider than the terminal — the newest lines end up
        below the viewport even while following the bottom.  The bound is
        computed from real wrapped heights when the renderer can supply them.
        """
        info = getattr(window, "render_info", None)
        height = getattr(info, "window_height", None) or self._view_height()
        bound = self._max_logical_scroll(info, height)
        # _scroll_top is maintained during write() from logical line counts,
        # before any renderer exists.  While following the bottom the renderer
        # is the authority on where the bottom actually is, so use its bound
        # directly — and remember it, so a subsequent scroll_up() starts from
        # the real bottom instead of the stale logical value.
        with self._lock:
            self._rendered_bottom = bound
            if self._follow_bottom:
                self._scroll_top = bound
                return bound
            return min(self._scroll_top, bound)

    def _max_logical_scroll(self, info: Any, height: int) -> int:
        """Largest logical top line whose wrapped tail still fills the window."""
        last_line = self._last_line()
        get_height_for_line = getattr(info, "get_height_for_line", None)
        if not callable(get_height_for_line):
            return max(0, last_line - height + 1)
        rows = 0
        line = last_line
        # Walk backwards until the accumulated display rows fill the viewport.
        # Bounded by `height` iterations: every line costs at least one row.
        while line >= 0:
            try:
                rows += max(1, int(get_height_for_line(line)))
            except Exception:
                return max(0, last_line - height + 1)
            if rows >= height:
                return line if rows == height else min(line + 1, last_line)
            line -= 1
        return 0

    def _last_line(self) -> int:
        return self._line_count

    def logical_line_at(self, screen_y: int) -> int:
        """Logical line index visible at a screen row inside the window."""
        info = getattr(self._window, "render_info", None)
        get_height = getattr(info, "get_height_for_line", None)
        line = max(0, self._scroll_top)
        remaining = max(0, int(screen_y))
        while remaining > 0:
            try:
                height = (
                    max(1, int(get_height(line))) if callable(get_height) else 1
                )
            except Exception:
                height = 1
            if remaining < height:
                break
            remaining -= height
            line += 1
        return min(line, self._line_count)

    def line_text(self, line_index: int) -> str:
        """Plain text of one logical output line."""
        joined = "".join(
            item[1] for item in self._fragments if len(item) >= 2
        )
        lines = joined.split("\n")
        if 0 <= line_index < len(lines):
            return lines[line_index]
        return ""

    def _view_height(self) -> int:
        if self._fixed_view_height is not None:
            return max(1, self._fixed_view_height)
        now = time.monotonic()
        if now - self._rows_cache_time >= 1.0:
            app = get_app_or_none()
            if app is None:
                rows = 40
            else:
                try:
                    rows = app.output.get_size().rows
                except Exception:
                    rows = 40
            self._rows_cache_value = rows
            self._rows_cache_time = now
        return max(1, self._rows_cache_value - 2)

    def _max_scroll(self) -> int:
        return max(0, self._last_line() - self._view_height() + 1)

    def _notify(self) -> None:
        if self._window is not None:
            self._window.vertical_scroll = self._scroll_top
        if self._on_write is not None:
            self._on_write()


class _ScrollableOutputWindow(Window):
    """Output window that maps the mouse wheel to conversation scrolling."""

    def __init__(
        self,
        pane: _OutputPane,
        *args: Any,
        context_menu: Optional[Callable[[int, str, Point], None]] = None,
        **kwargs: Any,
    ) -> None:
        self._output_pane = pane
        self._context_menu = context_menu
        super().__init__(*args, **kwargs)

    def _mouse_handler(self, mouse_event: Any):
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._output_pane.scroll_up(lines=3)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self._output_pane.scroll_down(lines=3)
            return None
        if (
            mouse_event.event_type == MouseEventType.MOUSE_DOWN
            and mouse_event.button == MouseButton.RIGHT
            and self._context_menu is not None
        ):
            line_index = self._output_pane.logical_line_at(mouse_event.position.y)
            self._context_menu(
                line_index,
                self._output_pane.line_text(line_index),
                mouse_event.position,
            )
            return True
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
        busy: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._history_path = history_path
        self._cancel_callback = cancel_callback
        self._busy = busy
        self._input_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self._app_exit_requested = False
        self._path_menu_open = False

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
            min_redraw_interval=0.03,
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
            context_menu=self._handle_output_context_menu,
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

    # ── Right-click path menu ──────────────────────────────────────────────

    def _handle_output_context_menu(
        self, line_index: int, text: str, position: Point
    ) -> None:
        paths = _paths_from_line(text)
        if not paths:
            self.console.print(
                "[dim]该行未检测到可打开的本地路径（右键菜单）[/dim]"
            )
            return
        if self._path_menu_open:
            return
        app = get_app_or_none()
        if app is None:
            return
        self._path_menu_open = True
        app.create_background_task(self._run_path_menu(paths))

    async def _run_path_menu(self, paths: list[tuple[str, Path]]) -> None:
        """Show a right-click menu: open, reveal in Finder, or copy."""
        from agent.commands.builtin import _launch_path
        from prompt_toolkit.application.run_in_terminal import in_terminal

        status = ""
        try:
            async with in_terminal():
                selected = paths[0]
                if len(paths) > 1:
                    print("检测到多个路径：")
                    for index, (display, _path) in enumerate(paths, start=1):
                        print(f"  {index}) {display}")
                    answer = await _PATH_MENU_PROMPT.prompt_async(
                        "选择路径（回车=1）› "
                    )
                    choice = int(answer.strip() or "1")
                    if not 1 <= choice <= len(paths):
                        return
                    selected = paths[choice - 1]

                display, path = selected
                print(f"路径：{display}")
                print(
                    "  1) 打开\n"
                    "  2) 在访达中显示\n"
                    "  3) 复制路径\n"
                    "  0) 取消"
                )
                answer = await _PATH_MENU_PROMPT.prompt_async("选择操作 › ")
                action = str(answer or "").strip().casefold()
                if action in ("1", "open", "打开"):
                    result = await _launch_path(path, reveal=False)
                    status = result.response_text or ""
                elif action in ("2", "reveal", "finder", "在访达中显示"):
                    result = await _launch_path(path, reveal=True)
                    status = result.response_text or ""
                elif action in ("3", "copy", "复制"):
                    copied = await _copy_to_clipboard(path)
                    status = (
                        f"已复制到剪贴板：{display}"
                        if copied
                        else "复制失败：缺少系统剪贴板命令。"
                    )
                else:
                    return
        except (KeyboardInterrupt, EOFError, ValueError):
            return
        finally:
            self._path_menu_open = False

        if status:
            self.console.print(f"[dim]{status}[/dim]")

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
        if (
            text
            and self._busy is not None
            and self._busy()
        ):
            # The loop only reads input between turns; make queued input
            # visible so Enter during a running turn does not look dead.
            self.console.print(
                f"⏎ 已排队（当前任务结束后发送）：{text}",
                markup=False,
                style="dim",
            )
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
