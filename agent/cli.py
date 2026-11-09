from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import inspect
import json
import logging
import os
from pathlib import Path
import signal
import sys
from typing import Any, Callable, Optional, Sequence
import webbrowser

import typer
from rich.markdown import Markdown
from rich.markup import escape as markup_escape
from rich.panel import Panel
from rich.table import Table

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.application.run_in_terminal import in_terminal
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters.app import has_completions
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

import agent as agent_module
from agent import shared
from agent import logs as logs_module
from agent.core.output import CliOutputSink
from agent.commands import CommandCoordinator, CommandRouter, register_builtin_commands
from agent.commands.composition import ensure_command_router
from agent.kernel.service import require_service, resolve
from agent.bundles import loader_for_name
from agent.runtime import AgentCore, RuntimeComponents, RuntimeSessionState, TurnInput
from agent.runtime.lock import AgentHomeBusyError, acquire_agent_home_lock
from agent.scheduler.models import (
    DEFAULT_SIGNAL_MAX_DEPTH,
    LOCAL_TIMEZONE,
    acceptance_for_run,
    produces_for_run,
    resolve_product_path,
)
from agent.scheduler.profiles import (
    apply_profile_to_config,
    describe_profile_for_prompt,
    resolve_permission_profile,
)
from agent.scheduler.unattended import UnattendedAudit, UnattendedOutputSink
from agent.shared import CancelToken
from agent.tools.runtime import RunSelfReport, _active_run_self_report
from agent.tui import TuiSession
from agent.scheduler.settings import SchedulerSettings
from agent.scheduler.build import (
    build_scheduler_service as _build_scheduler_service,
    describe_run_contract as _describe_run_contract,
    describe_upstream_results as _describe_upstream_results,
)
from agent.runtime.contracts import agent_core_for_components as _agent_core_for_components
from agent.scheduler.store import scheduler_store as _scheduler_store
from agent.scheduler.plugin import SchedulerPlugin

AgentContext = agent_module.AgentContext
BaseAgent = agent_module.BaseAgent
ChannelRunner = agent_module.ChannelRunner
CliOutputSink = CliOutputSink
CONSOLE = shared.CONSOLE
ContextManager = agent_module.ContextManager
EvolutionEngine = agent_module.EvolutionEngine
ExecutionResult = agent_module.ExecutionResult
DeliveryTarget = agent_module.DeliveryTarget
MemoryPalace = agent_module.MemoryPalace
NewScheduledTask = agent_module.NewScheduledTask
PluginCatalog = agent_module.PluginCatalog
SchedulerDelivery = agent_module.SchedulerDelivery
SchedulerService = agent_module.SchedulerService
SchedulerStore = agent_module.SchedulerStore
SessionEvent = agent_module.SessionEvent
SkillCatalog = agent_module.SkillCatalog
StagingBuffer = agent_module.StagingBuffer
TriggerSpec = agent_module.TriggerSpec
enqueue_orphan_staging_recovery = agent_module.enqueue_orphan_staging_recovery

app = typer.Typer(
    name="agent",
    help="Personal AI Agent with Memory Palace, Multi-Agent Orchestration, and Self-Evolution",
    add_completion=False,
)
memory_app = typer.Typer(help="Memory palace commands")
app.add_typer(memory_app, name="memory")
schedule_app = typer.Typer(help="Scheduled task commands")
app.add_typer(schedule_app, name="schedule")


_cli_prompt_session: Optional[PromptSession] = None
_ACTIVE_TUI: Optional[TuiSession] = None


def _accept_completion_or_submit(event: Any) -> None:
    """Enter accepts the highlighted completion when a menu is open."""
    buffer = event.current_buffer
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
    buffer.validate_and_handle()


_CLI_KEY_BINDINGS = KeyBindings()
_CLI_KEY_BINDINGS.add("enter", filter=has_completions)(
    _accept_completion_or_submit
)


def _cli_history_path() -> Path:
    """Persistent input history shared across interactive sessions."""
    history = shared.AGENT_HOME / "cli_history"
    history.parent.mkdir(parents=True, exist_ok=True)
    return history


def _cli_prompt() -> PromptSession:
    """Build the interactive input session with persistent arrow-key history."""
    global _cli_prompt_session
    if _cli_prompt_session is None:
        _cli_prompt_session = PromptSession(
            history=FileHistory(_cli_history_path()),
            key_bindings=_CLI_KEY_BINDINGS,
        )
    return _cli_prompt_session


async def _ask_user_input() -> str:
    """Read one user turn with history, paste safety, and cancel/EOF handling."""
    tui = _ACTIVE_TUI
    if tui is not None:
        text = await tui.ask_async()
        if text is None:
            raise EOFError
        return text
    return await _cli_prompt().prompt_async(
        HTML("\n<ansigreen>› </ansigreen>"),
        bottom_toolbar=HTML(
            "<i>Enter 发送 · ↑/↓ 历史 · 输入 / 实时筛选命令 · "
            "Ctrl+C 取消 · Ctrl+D 退出</i>"
        ),
        completer=_cli_command_completer(),
        complete_while_typing=True,
    )


async def _ask_tui_confirmation_input() -> str:
    """Read consent from the active TUI without queuing a new user turn."""
    if _ACTIVE_TUI is None:
        return await _ask_user_input()
    answer = await _ACTIVE_TUI.ask_async(during_turn=True)
    if answer is None:
        raise EOFError
    return answer


# ── Interactive command selection menu ────────────────────────────────
# Typing "/" at the prompt opens a numbered menu of the slash commands
# available in the CLI channel.  Commands with fixed argument choices
# (/permissions, /auto-approve) show a second menu for the argument, so
# the user never has to remember command syntax.

_cli_router: Optional[CommandRouter] = None


def _set_cli_router(router: Optional[CommandRouter]) -> None:
    """Point CLI input helpers at the live command router."""
    global _cli_router
    _cli_router = router


def _cli_command_completer() -> Optional[Completer]:
    """Live command palette for slash commands in the main input line."""
    router = _cli_router
    if router is None:
        return None
    descriptors = router.visible_descriptors("cli")
    if not descriptors:
        return None
    return _SlashCommandCompleter(descriptors)


class _SlashCommandCompleter(Completer):
    """Live command palette: typing "/p" narrows to matching commands.

    A bare "/" offers every command; each extra character narrows the list.
    Prefix matches win (so "/p" suggests /permissions and /plugins); when no
    command starts with the query, matches inside the name as a fallback.
    The palette only completes a single "/word" token, so paths like
    "/Users/..." never trigger it.
    """

    def __init__(self, descriptors: Sequence[Any]) -> None:
        self._descriptors = list(descriptors)

    def get_completions(self, document: Any, complete_event: Any):
        word = document.get_word_before_cursor(WORD=True)
        if not word.startswith("/") or "/" in word[1:]:
            return
        query = word[1:].casefold()
        if not query:
            candidates = self._descriptors
        else:
            prefix_matches = [
                descriptor
                for descriptor in self._descriptors
                if descriptor.name.startswith(query)
            ]
            candidates = prefix_matches or [
                descriptor
                for descriptor in self._descriptors
                if query in descriptor.name
            ]
        for descriptor in candidates:
            usage = descriptor.usage or f"/{descriptor.name}"
            meta = usage + (
                f" · {descriptor.description}" if descriptor.description else ""
            )
            yield Completion(
                text=f"/{descriptor.name}",
                start_position=-len(word),
                display=f"/{descriptor.name}",
                display_meta=meta,
            )


_COMMAND_OPTIONS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "auto-approve": (
        ("on", "/auto-approve on", "本会话高危命令自动放行（操作符仍需确认）"),
        ("off", "/auto-approve off", "恢复高风险确认"),
        ("status", "/auto-approve status", "查看当前会话状态"),
    ),
    "trust": (
        ("", "/trust", "查看本会话与本项目已记住的同意"),
        ("revoke all", "/trust revoke all", "撤销以上全部，之后重新询问"),
    ),
    "permissions": (
        ("", "/permissions", "查看当前权限等级与沙箱模式"),
        ("ask", "/permissions ask", "低/中风险自动执行；高风险需确认"),
        ("medium", "/permissions medium", "高危命令/破坏性选项自动放行；操作符仍确认"),
        ("high", "/permissions high", "全部自动放行（保留黑名单与结构拦截）"),
        ("full", "/permissions full", "全部自动放行；配合沙箱 none 可无沙箱"),
        (
            "sandbox restricted",
            "/permissions sandbox restricted",
            "受限读取 + 输出目录写入",
        ),
        (
            "sandbox read_all",
            "/permissions sandbox read_all",
            "全盘读取 + 输出目录写入",
        ),
        ("sandbox none", "/permissions sandbox none", "无 OS 沙箱（需 full 等级）"),
        ("default ask", "/permissions default ask", "持久化默认等级 ask"),
        ("default medium", "/permissions default medium", "持久化默认等级 medium"),
        ("default high", "/permissions default high", "持久化默认等级 high"),
        ("default full", "/permissions default full", "持久化默认等级 full"),
        (
            "default sandbox restricted",
            "/permissions default sandbox restricted",
            "持久化默认沙箱 restricted",
        ),
        (
            "default sandbox read_all",
            "/permissions default sandbox read_all",
            "持久化默认沙箱 read_all",
        ),
        (
            "default sandbox none",
            "/permissions default sandbox none",
            "持久化默认沙箱 none",
        ),
    ),
}


class _MenuState:
    """Filterable selection state shared by the menu toolbar and key bindings."""

    def __init__(self, items: Sequence[tuple[str, str, str]]) -> None:
        self.items = list(items)
        self.selected = 0

    def filtered(self, query: str) -> list[tuple[str, str, str]]:
        """Live-filter items by the text currently being typed."""
        lowered = query.strip().lstrip("/").casefold()
        if not lowered:
            return list(self.items)
        if lowered.isdigit():
            index = int(lowered)
            if 1 <= index <= len(self.items):
                return [self.items[index - 1]]
            return []
        return [
            item
            for item in self.items
            if lowered in item[0].casefold()
            or lowered in item[1].casefold()
            or lowered in item[2].casefold()
        ]

    def pick(self, query: str) -> Optional[str]:
        """Return the highlighted item's value for the current query."""
        filtered = self.filtered(query)
        if not filtered:
            return None
        return filtered[min(self.selected, len(filtered) - 1)][0]


def _menu_key_bindings(state: "_MenuState") -> KeyBindings:
    """Arrow-key navigation plus Enter/Esc confirmation for the menu."""
    kb = KeyBindings()

    @kb.add("up")
    def _up(event: Any) -> None:
        filtered = state.filtered(event.app.current_buffer.text)
        if filtered:
            state.selected = max(
                0, min(state.selected, len(filtered) - 1) - 1
            )

    @kb.add("down")
    def _down(event: Any) -> None:
        filtered = state.filtered(event.app.current_buffer.text)
        if filtered:
            state.selected = min(
                len(filtered) - 1, max(0, state.selected) + 1
            )

    @kb.add("enter")
    def _enter(event: Any) -> None:
        picked = state.pick(event.app.current_buffer.text)
        if picked is not None:
            event.app.exit(result=picked)

    @kb.add("escape")
    def _cancel(event: Any) -> None:
        event.app.exit(result=None)

    return kb


def _menu_toolbar(state: "_MenuState", title: str):
    """Bottom-toolbar renderer that narrows the list on every keystroke."""
    from prompt_toolkit.application.current import get_app

    def render() -> list[tuple[str, str]]:
        query = get_app().current_buffer.text
        filtered = state.filtered(query)
        fragments: list[tuple[str, str]] = [
            ("bold", title),
            (
                "dim",
                "  输入即筛选 · ↑/↓ 选择 · Enter 确认 · Esc 取消",
            ),
            ("", "\n"),
        ]
        if not filtered:
            fragments.append(("red", "没有匹配项"))
            return fragments
        selected = min(state.selected, len(filtered) - 1)
        for index, (_value, label, hint) in enumerate(filtered[:8]):
            marker = "›" if index == selected else " "
            style = "bold cyan" if index == selected else ""
            fragments.append((style, f"{marker} {index + 1}. {label}"))
            if hint:
                fragments.append(("dim", f"  {hint}"))
            fragments.append(("", "\n"))
        if len(filtered) > 8:
            fragments.append(
                ("dim", f"… 共 {len(filtered)} 项，输入更多字符缩小范围")
            )
        return fragments

    return render


async def _menu_prompt_async(
    state: "_MenuState", title: str
) -> Optional[str]:
    """Run the live-filtering menu and return the picked value (or None)."""
    session = PromptSession(key_bindings=_menu_key_bindings(state))

    async def _prompt() -> str:
        return await session.prompt_async(
            "请选择 › ",
            bottom_toolbar=_menu_toolbar(state, title),
        )

    if get_app_or_none() is not None:
        async with in_terminal():
            return await _prompt()
    return await _prompt()


async def _select_from_menu(
    title: str,
    items: Sequence[tuple[str, str, str]],
) -> Optional[str]:
    """Live-filtering selection menu returning the chosen value."""
    state = _MenuState(items)
    try:
        return await _menu_prompt_async(state, title)
    except (KeyboardInterrupt, EOFError):
        return None


async def _command_menu(router: CommandRouter) -> Optional[str]:
    """Select a slash command (and its argument) from an interactive menu."""
    descriptors = router.visible_descriptors("cli")
    if not descriptors:
        return None
    items = [
        (
            descriptor.name,
            f"/{descriptor.name}",
            (descriptor.usage or f"/{descriptor.name}")
            + (f" · {descriptor.description}" if descriptor.description else ""),
        )
        for descriptor in descriptors
    ]
    command = await _select_from_menu("可用命令", items)
    if command is None:
        return None
    options = _COMMAND_OPTIONS.get(command)
    if not options:
        return f"/{command}"
    option = await _select_from_menu(f"选择 /{command} 参数", list(options))
    if option is None:
        return None
    return f"/{command} {option}".strip()

_INTERACTION_LOGGER_NAMES = (
    "agent.channels.base",
    "agent.core.agent",
    "agent.channels.feishu",
)


async def _build_quiet_cli_components(cfg: dict) -> dict:
    """Build interactive components without duplicating the session header."""
    builder = agent_module._build_components_async
    if "announce" in inspect.signature(builder).parameters:
        return await builder(cfg, announce=False)
    return await builder(cfg)


class _SinkLogHandler(logging.Handler):
    """Route log records through the CLI console instead of raw stderr.

    The interactive CLI never called ``_configure_runtime_logging`` (only the
    gateway does), so the root logger had no handlers and ``logging.lastResort``
    wrote full tracebacks straight to stderr.  Two consequences: while the TUI
    owns the screen those writes bypass the output pane and corrupt the
    full-screen layout, and the text skips the sink's redaction pass entirely.

    Redaction is best-effort — it matches ``key=value`` shapes, not a secret
    mentioned in prose — so the guarantee here is about output *ownership*: one
    channel owns the screen, and log records travel through it.
    """

    def __init__(self, console: Any) -> None:
        super().__init__(level=logging.WARNING)
        self._console = console

    def emit(self, record: logging.LogRecord) -> None:
        from agent.core.output import _redact_sensitive_text

        try:
            text = _redact_sensitive_text(self.format(record))
            self._console.print(
                f"[dim]{markup_escape(text)}[/dim]",
                soft_wrap=True,
            )
        except Exception:  # pragma: no cover - never let logging kill a turn
            self.handleError(record)


def _configure_runtime_logging() -> None:
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    for logger_name in _INTERACTION_LOGGER_NAMES:
        logging.getLogger(logger_name).setLevel(logging.INFO)

def _missing_feishu_dependency_hint() -> str:
    exe = Path(sys.executable).as_posix()
    if "/.local/share/uv/tools/" in exe:
        return (
            "lark-oapi not installed in the uv tool environment.\n"
            "If you're running from this repo, use:\n"
            "  uv run simple gateway\n"
            "after:\n"
            "  uv sync --extra feishu\n"
            "Or reinstall the tool from this repo with:\n"
            "  uv tool install --reinstall --editable . --with lark-oapi"
        )
    return (
        "lark-oapi not installed in the current Python environment.\n"
        "If you're in this repo, run:\n"
        "  uv sync --extra feishu\n"
        "and start with:\n"
        "  uv run simple gateway"
    )


def _scheduler_delivery_target(
    delivery_mode: str,
    *,
    chat_id: Optional[str] = None,
    chat_type: str = "p2p",
):
    if delivery_mode == "standalone":
        return DeliveryTarget.standalone()
    if delivery_mode == "channel":
        if not chat_id:
            raise typer.BadParameter(
                "--chat-id is required when delivery-mode=channel"
            )
        return DeliveryTarget.channel(
            target_type="feishu_chat",
            chat_id=chat_id,
            chat_type=chat_type,
        )
    raise typer.BadParameter(f"Unsupported delivery mode: {delivery_mode}")


def _scheduler_print_task_table(tasks: list) -> None:
    table = Table(title="Scheduled Tasks")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Kind")
    table.add_column("Enabled")
    table.add_column("Delivery")
    table.add_column("Next Run")
    for task in tasks:
        table.add_row(
            task.id,
            task.name,
            task.kind,
            "yes" if task.enabled else "no",
            task.delivery_mode,
            task.next_run_at.isoformat() if task.next_run_at else "—",
        )
    shared.CONSOLE.print(table)


# ── CLI SIGINT handler (Ctrl+C → cancel turn, not exit process) ──────────
# Module-level state so the signal handler can reach the current token.
_current_cancel_token: Optional[CancelToken] = None
_sigint_count = 0


def _cli_sigint_handler(signum: int, frame: Any) -> None:
    """Convert SIGINT to CancelToken.cancel() when a turn is running.

    First  Ctrl+C: graceful cancel (SIGTERM to subprocesses, cooperative check).
    Second Ctrl+C: force cancel (SIGKILL, abort LLM HTTP request).
    Third  Ctrl+C: restore default handler and exit.
    """
    global _sigint_count
    _sigint_count += 1
    token = _current_cancel_token
    if token is None:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGINT)
        return
    if _sigint_count == 1:
        token.cancel()
        shared.CONSOLE.print(
            "\n[yellow](Ctrl+C) 正在取消当前任务… (再按一次 Ctrl+C 强制取消)[/yellow]"
        )
    elif _sigint_count == 2:
        token.cancel("force")
        shared.CONSOLE.print(
            "\n[red](Ctrl+C) 已强制取消 (SIGKILL + 中断 LLM 请求)[/red]"
        )
    else:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGINT)


def _build_cli_context_manager(components: dict) -> Optional[ContextManager]:
    """Return the stable memory session shared by successive CLI processes."""
    base_ctx_mgr: Optional[ContextManager] = components.get("context_manager")
    if base_ctx_mgr is None:
        return base_ctx_mgr
    if getattr(base_ctx_mgr, "session_id", None) == "cli":
        return base_ctx_mgr
    spawn_session = getattr(base_ctx_mgr, "spawn_session", None)
    return spawn_session("cli") if callable(spawn_session) else base_ctx_mgr


def _cli_session_header(components: dict, cfg: dict) -> Panel:
    """Build a compact startup identity without consuming terminal width."""
    agent = components["agent"]
    model = markup_escape(str(getattr(agent, "model", "") or "default"))
    provider = markup_escape(str(cfg.get("active_provider") or "default"))
    api_format = markup_escape(str(getattr(agent, "api_format", "") or "unknown"))
    return Panel.fit(
        f"[bold cyan]simple[/bold cyan] [dim]interactive[/dim]\n"
        f"[white]{model}[/white] [dim]· {provider}/{api_format} · cli[/dim]",
        border_style="bright_black",
        padding=(0, 1),
    )


def _tui_enabled() -> bool:
    """Full-screen TUI mode only makes sense on a real interactive terminal."""
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _owns_process_sigint(*, tui_active: bool) -> bool:
    """Whether this mode should install the process-level SIGINT handler.

    Ctrl+C must have exactly one owner.  In TUI mode prompt_toolkit installs its
    own SIGINT handler routed to ``_tui_cancel_callback``; installing the
    process-level handler as well makes both fire for a single keypress, so
    ``_sigint_count`` reaches 2 immediately and the first Ctrl+C escalates
    straight to force-cancel.  There, the key binding is the sole owner.
    """
    return not tui_active


def _tui_cancel_callback() -> None:
    """TUI Ctrl+C: cancel the running turn, or exit when idle."""
    global _sigint_count
    tui = _ACTIVE_TUI
    if tui is None:
        return
    token = _current_cancel_token
    if token is None:
        tui.request_exit()
        return
    _sigint_count += 1
    if _sigint_count == 1:
        token.cancel()
        shared.CONSOLE.print(
            "\n[yellow](Ctrl+C) 正在取消当前任务… (再按一次 Ctrl+C 强制取消)[/yellow]"
        )
    elif _sigint_count == 2:
        token.cancel("force")
        shared.CONSOLE.print(
            "\n[red](Ctrl+C) 已强制取消 (SIGKILL + 中断 LLM 请求)[/red]"
        )
    else:
        tui.request_exit()


async def _run_tui_session(components: dict, cfg: dict) -> None:
    """Run the interactive loop inside a full-screen TUI session."""
    global _ACTIVE_TUI

    tui = TuiSession(
        history_path=_cli_history_path(),
        completer_factory=_cli_command_completer,
        cancel_callback=_tui_cancel_callback,
        busy=lambda: _current_cancel_token is not None,
    )
    _ACTIVE_TUI = tui
    try:
        await tui.run(_interactive_loop_coro(components, cfg, tui))
    finally:
        _ACTIVE_TUI = None


async def _interactive_loop(components: dict, cfg: dict):
    """Main interactive chat loop (line mode or full-screen TUI)."""
    if _tui_enabled():
        await _run_tui_session(components, cfg)
    else:
        await _interactive_loop_coro(components, cfg, None)


async def _interactive_loop_coro(
    components: dict, cfg: dict, tui: Optional[TuiSession]
):
    """Main interactive chat loop body shared by line and TUI modes."""
    console = tui.console if tui is not None else shared.CONSOLE
    original_console = shared.CONSOLE
    if tui is not None:
        shared.CONSOLE = console
    try:
        if tui is None:
            # Background work (notably MCP connection completion) can print
            # while prompt_async owns the input line.  prompt_toolkit's proxy
            # renders such output above the prompt and restores the user's
            # partially typed text instead of letting both share one row.
            with patch_stdout(raw=True):
                await _interactive_loop_body(
                    components,
                    cfg,
                    console,
                    live_status=True,
                    echo_input=False,
                    tui_active=False,
                )
        else:
            await _interactive_loop_body(
                components,
                cfg,
                console,
                live_status=False,
                echo_input=True,
                tui_active=True,
                status_callback=tui.set_status,
            )
    finally:
        shared.CONSOLE = original_console


def _command_coordinator(
    components: dict,
    cfg: dict,
    *,
    cancel_token_factory: Optional[Callable[[], Any]] = None,
) -> CommandCoordinator:
    """Build the pipeline one line of user input goes through.

    Both the interactive loop and ``simple chat`` use this, because a slash
    command has to mean the same thing wherever it was typed.  ``chat`` used to
    hand its text straight to the agent core, so ``/plugin:command`` reached the
    model as prose: the model saw the command's name in its prompt, could not
    read the body, and improvised an "execution" nobody had run.
    """
    coordinator_factory = components.get("command_coordinator_factory")
    if callable(coordinator_factory):
        return coordinator_factory(cancel_token_factory=cancel_token_factory)
    router = components.get("command_router")
    if router is None:
        router = ensure_command_router(components)
    return CommandCoordinator(
        _agent_core_for_components(components),
        router,
        components=components,
        config=cfg,
        cancel_token_factory=cancel_token_factory,
    )


async def _run_cli_turn(
    components: dict,
    cfg: dict,
    text: str,
    sink: Any,
) -> None:
    """Run one user line through the coordinator, once."""
    coordinator = _command_coordinator(components, cfg)
    state = RuntimeSessionState(
        ctx=AgentContext(system_prompt=components["system_prompt"])
    )
    await coordinator.handle(
        TurnInput.from_text(text, channel_name="cli"),
        state,
        sink,
    )


def _fallback_plugin_catalog(cfg: dict, components: dict) -> PluginCatalog:
    """The catalog the interactive loop builds when the composition did not register one.

    Extracted so this branch is testable at all: it is the one place the CLI hands the
    *user's* plugin directory to a catalog, and a catalog without change detection loads
    whatever it finds there -- see the test that names the fallback.
    """
    plugin_catalog = PluginCatalog(
        builtin_dir=shared.PLUGINS_DIR,
        user_dir=shared.USER_PLUGINS_DIR,
        plugin_config=cfg.get("plugins", {}),
        # Change detection, because this catalog is handed the *user's* directory.  bootstrap
        # turns it on for the composition and notes that it is "off elsewhere (a bare catalog
        # loads what it is handed)" -- true of a catalog handed only the runtime's own plugins,
        # and not true here: a bare catalog pointed at ``~/.agent/plugins`` runs whatever is in
        # it, which is the one thing the gate exists to notice.  Turning it on costs nothing
        # when the directory is empty, and asks the same questions the composition asks.
        detect_plugin_changes=True,
        # The composition the plugins run in.  Without it the catalog's host has an
        # empty registry, so a plugin that declares what it needs stays *pending* --
        # which is what the hook protocol's `on_session_start(components)` was a
        # workaround for: it handed the mapping to a plugin the host could not
        # resolve it for.  bootstrap has always passed its mapping here; the
        # interactive loop did not, because nothing had asked until now.
        components=components,
        turn_hook_timeout_seconds=cfg.get("orchestration", {}).get(
            "turn_hook_timeout_seconds",
            shared.DEFAULT_TURN_HOOK_TIMEOUT_SECONDS,
        ),
    )
    plugin_catalog.discover_and_load()
    return plugin_catalog


async def _interactive_loop_body(
    components: dict,
    cfg: dict,
    console,
    *,
    live_status: bool = True,
    echo_input: bool = False,
    tui_active: bool = False,
    status_callback: Optional[Callable[[str], None]] = None,
):
    """Main interactive chat loop."""
    global _current_cancel_token
    # Computed in a container view of the same mapping, so these are service
    # *lookups* rather than dict pokes.  See agent/runtime/services.py:
    # the difference is what makes a name replaceable.
    agent: BaseAgent = require_service(components, "agent")
    plugin_catalog: PluginCatalog = resolve(components, "plugin_catalog")  # type: ignore[assignment]
    if plugin_catalog is None:
        plugin_catalog = _fallback_plugin_catalog(cfg, components)
        components["plugin_catalog"] = plugin_catalog
    system_prompt = components["system_prompt"]
    ctx_mgr = _build_cli_context_manager(components)
    skill_catalog: SkillCatalog = require_service(components, "skill_catalog")

    ctx = AgentContext(system_prompt=system_prompt)
    consolidation_cfg = (cfg.get("context") or {}).get("consolidation", {}) or {}
    # Incomplete-task guidance is persisted by the context manager. Completed
    # requests are not duplicated into the system prompt.
    # Consolidation may name any configured group's model, so the client it
    # runs on is looked up *by that model* — see BaseAgent.endpoint_for.  The
    # lookup runs only when there is a worker to spend it on, so a caller that
    # brings no context manager needs no agent that can resolve models.
    memory_worker = None
    memory_model = ""
    memory_endpoint = None
    if ctx_mgr:
        memory_model = agent.consolidation_model(cfg)
        memory_endpoint = agent.endpoint_for(memory_model)
        memory_worker = agent_module.BackgroundMemoryWorker(
            ctx_mgr,
            memory_endpoint,
            memory_model,
        )
        memory_worker.start()
    state = RuntimeSessionState(
        ctx=ctx,
        context_manager=ctx_mgr,
        memory_worker=memory_worker,
        cancel_token=CancelToken(),
    )

    def _new_cli_cancel_token() -> CancelToken:
        global _current_cancel_token, _sigint_count
        token = CancelToken()
        _current_cancel_token = token
        _sigint_count = 0
        return token

    router = components.get("command_router")
    coordinator = _command_coordinator(
        components,
        cfg,
        cancel_token_factory=_new_cli_cancel_token,
    )
    if router is None:
        router = ensure_command_router(components)
    _set_cli_router(router)

    # Queue staged turns left behind by sessions that died before consolidating
    # them, for background recovery.  Doing this synchronously would block
    # startup on a network model call before the user even sees the prompt.
    if ctx_mgr:
        recovered = ctx_mgr.recover_orphan_staging()
        if recovered:
            shared.CONSOLE.print(
                f"[dim]Recovering {recovered} interrupted session(s)…[/dim]"
            )
            if memory_worker:
                memory_worker.wake()

    shared.CONSOLE.print(_cli_session_header(components, cfg))

    old_sigint = (
        signal.signal(signal.SIGINT, _cli_sigint_handler)
        if _owns_process_sigint(tui_active=tui_active)
        else None
    )
    # Give log records an owner for the life of the session, so nothing reaches
    # raw stderr behind the console's back.
    _root_logger = logging.getLogger()
    _log_handler: Optional[logging.Handler] = None
    _previous_last_resort = logging.lastResort
    if not _root_logger.handlers:
        _log_handler = _SinkLogHandler(console)
        _root_logger.addHandler(_log_handler)
        logging.lastResort = None  # type: ignore[assignment]
    try:
        while True:
            try:
                # prompt_toolkit's async prompt keeps the event loop alive, so
                # future multi-channel concurrency (Telegram, Feishu, …) can
                # run in the same loop while the human types.
                user_input = await _ask_user_input()
                if user_input.strip() == "/" and router is not None:
                    menu_input = await _command_menu(router)
                    if menu_input is None:
                        continue
                    user_input = menu_input
                    if not echo_input:
                        shared.CONSOLE.print(f"[dim]› {menu_input}[/dim]")
            except (EOFError, KeyboardInterrupt):
                break

            if not user_input.strip():
                continue
            if echo_input:
                console.print(f"[green]›[/green] {markup_escape(user_input)}")

            # The TUI cannot host a live spinner but can prompt for consent and
            # owns an in-place status row, so the three capabilities are
            # declared separately.
            _turn_sink = CliOutputSink(
                console,
                live_status=live_status,
                can_prompt=True,
                confirmation_prompt=(
                    _ask_tui_confirmation_input if tui_active else None
                ),
                status_callback=status_callback,
            )
            try:
                ctx.metadata["skill_catalog"] = skill_catalog
                if not user_input.lstrip().startswith("/"):
                    _turn_sink.begin_turn()
                action = await coordinator.handle(
                    TurnInput.from_text(
                        user_input,
                        session_id="cli",
                        channel_name="cli",
                    ),
                    state,
                    _turn_sink,
                )
                if action == "exit_cli":
                    break
            except Exception as e:
                _turn_sink.on_error(str(e))
            finally:
                _current_cancel_token = None
                # A cancelled turn can unwind without ever reaching
                # on_turn_complete, and a status row left spinning would claim
                # the agent is still working.
                if status_callback is not None:
                    status_callback("")

    finally:
        if _log_handler is not None:
            _root_logger.removeHandler(_log_handler)
            logging.lastResort = _previous_last_resort  # type: ignore[assignment]
        if old_sigint is not None:
            signal.signal(signal.SIGINT, old_sigint)

        if memory_worker:
            memory_worker.stop()
            await memory_worker.wait()

        # Session-end consolidation runs inside the finally block so it is
        # protected against KeyboardInterrupt during the input loop.  A single
        # ^C is caught by the inner except and causes a normal break; the
        # finally block then runs this code before the process exits.
        # (A ^C^C that arrives *here* can still abort — that is user intent.)
        flush_on_end = bool(consolidation_cfg.get("flush_on_session_end", False))
        if flush_on_end and ctx_mgr and ctx_mgr.should_session_end_sleep():
            shared.CONSOLE.print("[dim]Saving session context…[/dim]")
            try:
                flush_timeout = max(
                    1.0,
                    float(
                        cfg.get("memory", {}).get(
                            "session_end_flush_timeout_seconds",
                            shared.DEFAULT_SESSION_END_FLUSH_TIMEOUT_SECONDS,
                        )
                    ),
                )
                async with asyncio.timeout(flush_timeout):
                    ctx_mgr.enqueue_consolidation("session_end")
                    while ctx_mgr.pending_jobs():
                        processed = await ctx_mgr.process_one_job(
                            memory_endpoint,
                            memory_model,
                        )
                        if not processed:
                            break
                ctx.messages = ctx_mgr.compact_messages(
                    ctx.messages,
                    input_token_budget=agent.context_window - agent.max_tokens,
                )
            except Exception as e:
                shared.CONSOLE.print(f"[dim]Session-end consolidation error: {e}[/dim]")

        # P0-1: session-end plugin notifications INSIDE finally so they fire
        # even when KeyboardInterrupt breaks the input loop.
        if len(ctx.messages) >= 2:
            try:
                await plugin_catalog.fire_session_end(
                    SessionEvent(
                        messages=ctx.messages,
                        tools_used=state.tools_used,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        turn_count=state.turn_count,
                    )
                )
            except Exception as exc:
                shared.CONSOLE.print(f"[dim]Plugin session_end error: {exc}[/dim]")
        if ctx_mgr is not None:
            close_manager = getattr(ctx_mgr, "close", None)
            if callable(close_manager):
                close_manager()

    shared.CONSOLE.print("\n[dim]Session closed.[/dim]")


def _acquire_home_lock_or_exit(mode: str):
    """Enforce one live owner per agent home before touching any of its state.

    Acquired before ``load_config`` so first-run setup — which writes
    ``config.json`` — is covered too.
    """
    try:
        return acquire_agent_home_lock(mode)
    except AgentHomeBusyError as exc:
        shared.CONSOLE.print(f"[red]{markup_escape(str(exc))}[/red]")
        raise typer.Exit(1)


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    name: Optional[str] = typer.Option(
        None, "--name", help="Session name (default: ~/.agent)"
    ),
):
    """Enter interactive chat when no subcommand is given."""
    if isinstance(name, str):
        agent_module._set_agent_home(shared.session_home(name))
    if ctx.invoked_subcommand is None:
        home_lock = _acquire_home_lock_or_exit("cli")
        try:
            cfg, first_run = agent_module.load_config()
            if first_run:
                if not agent_module._first_run_setup():
                    raise typer.Exit(0)
                # Reload after potential edits
                cfg, _ = agent_module.load_config()

            async def _run():
                try:
                    components = await _build_quiet_cli_components(cfg)
                except RuntimeError as exc:
                    shared.CONSOLE.print(f"[red]Error: {exc}[/red]")
                    raise typer.Exit(1)
                try:
                    await _interactive_loop(components, cfg)
                finally:
                    await agent_module._close_components(components)

            asyncio.run(_run())
        finally:
            home_lock.release()


def _gateway_workspace_config(cfg: dict) -> dict:
    """The config the gateway runs with, carrying a workspace it can defend.

    The gateway is not a process somebody started *inside* a project: its cwd is
    wherever the shell happened to be, and every conversation that has not
    chosen a folder would inherit that -- reading it, and (once a folder is
    granted in the UI) writing it, with nothing on screen explaining why.  A
    server hands out the agent's own workspace instead.

    An explicit ``workspace_root`` in the config still wins, and a folder chosen
    in the UI is per session.  The directory is created only when *we* are the
    ones choosing it: a configured path that does not exist is somebody's typo,
    and creating it silently would hide that.
    """
    if str(cfg.get("workspace_root") or "").strip():
        return cfg
    workspace = shared.DEFAULT_WORKSPACE_DIR
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        shared.CONSOLE.print(
            f"[yellow]Could not create the default workspace {workspace}: {exc}.\n"
            "Set workspace_root in your config, or pick a folder per session.[/yellow]"
        )
        return cfg
    return {**cfg, "workspace_root": str(workspace)}


def _gateway_channel_guidance(cfg: dict) -> str:
    """What to tell somebody whose gateway started no channels.

    Two different situations used to get one message, and that message named
    only Feishu -- the option that needs an app id, a secret and an external
    service -- while the local web channel needs nothing and can serve the
    bundled UI immediately.  A person who enables ``channels.web`` and still
    gets "add channels.feishu.enabled=true" is being sent down the harder path.

    So: say which situation this is, and name the local option first.
    """
    sections = cfg.get("channels") or {}
    enabled = sorted(
        str(name)
        for name, section in sections.items()
        if isinstance(section, dict) and section.get("enabled")
    )
    if enabled:
        return (
            f"[yellow]Channel(s) enabled but not started: {', '.join(enabled)}. "
            "The channel itself reported why above.[/yellow]"
        )
    return (
        "[yellow]No channels are enabled, so the gateway has nothing to run.\n"
        'Add one to the "channels" section of your config, for example:\n'
        '  "channels": {"web": {"enabled": true}}     '
        "# local web UI on http://127.0.0.1:8787\n"
        '  "channels": {"feishu": {"enabled": true}}  '
        "# needs a Feishu app id/secret[/yellow]"
    )


def _gateway_web_channel(channels: Sequence[Any]) -> Any:
    """The mounted web channel, or None when this gateway has none.

    Asked of the channel rather than rebuilt from the config here: the bind
    address and the address a browser can use are not the same string (a
    wildcard bind is not a URL), and that distinction belongs next to the
    server that knows it.
    """
    for channel in channels:
        url = getattr(channel, "public_url", "")
        if isinstance(url, str) and url:
            return channel
    return None


def _should_open_browser(*, no_open: bool, interactive: bool) -> bool:
    """Whether this gateway may put a window on the reader's screen.

    Three gates. ``--no-open`` and ``SIMPLE_NO_BROWSER`` are the operator saying
    no; a non-interactive stdout is the *absence* of an operator -- a service, a
    CI job, or an agent's background command.  Opening a browser there is not a
    convenience, it is a window nobody asked for and nobody is in front of.
    """
    if no_open:
        return False
    if os.environ.get("SIMPLE_NO_BROWSER", "").strip():
        return False
    return interactive


def _stdout_is_interactive() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


async def _open_browser_when_ready(
    url: str,
    ready: Callable[[], Any],
    *,
    timeout: float = 10.0,
) -> None:
    """Open the browser once the server says it is listening.

    ``ready`` is the channel's own answer, not a socket probe: a port can be
    held by something else entirely, and opening then would point the browser
    at a stranger while this gateway's startup had failed.  A timeout or a
    refusal simply means no window -- the URL was printed either way, and a
    server that is still running is what the reader actually wanted.

    A failure to open is not a gateway failure: a headless session, a browser
    that is not installed, or a display that is not there all leave the server
    running.
    """
    try:
        listening = await asyncio.wait_for(ready(), timeout=max(0.0, float(timeout)))
    except Exception:
        return
    if not listening:
        return
    with contextlib.suppress(Exception):
        webbrowser.open(url)


@app.command()
def gateway(
    name: Optional[str] = typer.Option(
        None, "--name", help="Session name (default: ~/.agent)"
    ),
    bundle: Optional[str] = typer.Option(
        None,
        "--bundle",
        help=(
            "Named composition for this process's scheduled runs, e.g. "
            "'unattended' (see agent/bundles.py). Applied to each run's own copy "
            "of the config, so the file on disk is not changed."
        ),
    ),
    no_open: bool = typer.Option(
        False,
        "--no-open",
        help=(
            "Do not open the web UI in a browser on startup. Opening is skipped "
            "anyway when stdout is not a terminal or SIMPLE_NO_BROWSER is set."
        ),
    ),
):
    """Start all configured external channels (Feishu, etc.).

    Reads channel configuration from the agent home directory.
    Runs until interrupted (Ctrl-C) or all channels disconnect.

    Use --name to run a named session::

        simple gateway --name prod    # data -> ~/.agent-prod/, config shared from ~/.agent
        simple gateway --name dev     # data -> ~/.agent-dev/
        simple gateway                # data -> ~/.agent/

    Use --bundle to run under a named composition (see ``agent/bundles.py``).
    It is applied to the copy each scheduled run builds, so the configuration
    file keeps the operator's settings and only this process's runs differ.
    """
    if isinstance(name, str):
        agent_module._set_agent_home(shared.session_home(name))
    # After _set_agent_home so each named session locks its own home.
    home_lock = _acquire_home_lock_or_exit("gateway")
    cfg, first_run = agent_module.load_config()
    _configure_runtime_logging()
    # A gateway runs for days; a terminal is not a durable place to keep its trail.
    log_path = logs_module.attach_file_log("gateway")
    if first_run:
        if not agent_module._first_run_setup():
            raise typer.Exit(0)
        cfg, _ = agent_module.load_config()

    # Chosen here rather than inherited from the shell: see the helper.
    gateway_cfg = _gateway_workspace_config(cfg)

    async def _run():
        try:
            components = await agent_module._build_components_async(gateway_cfg)
        except RuntimeError as exc:
            shared.CONSOLE.print(f"[red]Error: {exc}[/red]")
            raise typer.Exit(1)
        # One resolution, shared with the `scheduler` command.  The two used to read
        # the same four keys with the same four defaults independently -- see
        # agent/scheduler/settings.py for why that is the defect.
        scheduler_settings = SchedulerSettings.resolve(cfg)
        scheduler_poll = scheduler_settings.poll_seconds
        scheduler_lease = scheduler_settings.lease_seconds
        scheduler_max_concurrent = scheduler_settings.max_concurrent_runs
        scheduler_signal_depth = scheduler_settings.signal_max_depth
        scheduler_task: Optional[asyncio.Task] = None
        browser_task: Optional[asyncio.Task] = None
        scheduler_store = None
        patrol_service = None
        try:
            # From the composition, not from a tree of its own: the channel plugins
            # are mounted with everything else now, so a profile can disable or
            # replace one and a plugin can inject `channels`.
            channels = list(require_service(components, "channels").all())
            if not channels:
                shared.CONSOLE.print(_gateway_channel_guidance(gateway_cfg))
                return
            shared.CONSOLE.print(
                f"[dim]Gateway starting {len(channels)} channel(s). "
                "Press Ctrl-C to stop.[/dim]"
            )
            # The UI is the thing most people started this for, so open it --
            # once the port actually accepts, not before (see the helper).  The
            # address is printed either way: a browser that did not open (no
            # display, another machine, --no-open) must still leave the reader
            # something to click.
            web_channel = _gateway_web_channel(channels)
            if web_channel is not None:
                web_url = str(web_channel.public_url)
                shared.CONSOLE.print(f"[dim]Web UI: {web_url}[/dim]")
                shared.CONSOLE.print(
                    f"[dim]Log: {logs_module.describe(log_path)}[/dim]"
                )
                if _should_open_browser(
                    no_open=no_open,
                    interactive=_stdout_is_interactive(),
                ):
                    browser_task = asyncio.create_task(
                        _open_browser_when_ready(web_url, web_channel.wait_ready)
                    )
            # The scheduler is mounted, not built here.  The two things it cannot read
            # from the composition are the two this command resolves -- the run
            # parameters (config merged with argv) and the bundle's loader -- so they go
            # in as data before the plugin is mounted.
            #
            # A run is a fresh execution, so a composition belongs here rather than in
            # the file: the operator's settings stay on disk and only this process's runs
            # differ.  ``loader_for_name`` resolves the name now, so a typo fails at
            # startup instead of each run quietly proceeding uncomposed.
            components["scheduler_settings"] = scheduler_settings
            components["scheduler_config_loader"] = loader_for_name(bundle)
            catalog = components.get("plugin_catalog")
            catalog.mount(SchedulerPlugin())
            service = require_service(components, "scheduler")
            scheduler_task = asyncio.create_task(service.run_forever())
            # The patrol runs here because its input is *silence*: it looks
            # for sessions nobody is in. A patrol that only runs when a command is
            # typed finds nothing, because typing is the opposite of silence.
            # Its effect on the world is rows in `autonomy/proposals.db`, plus the
            # one-off scheduler tasks it creates for reversible candidates.
            patrol_service = _start_patrol(gateway_cfg, components)
            runner = ChannelRunner(channels, components, gateway_cfg)
            await runner.run()
        finally:
            if patrol_service is not None:
                await patrol_service.stop()
            if browser_task is not None and not browser_task.done():
                browser_task.cancel()
                await asyncio.gather(browser_task, return_exceptions=True)
            if scheduler_task is not None:
                scheduler_task.cancel()
                await asyncio.gather(scheduler_task, return_exceptions=True)
            if 'service' in locals() and hasattr(service, "shutdown"):
                await service.shutdown()
            if scheduler_store is not None:
                scheduler_store.close()
            await agent_module._close_components(components)

    try:
        asyncio.run(_run())
    finally:
        home_lock.release()



@app.command()
def chat(question: str = typer.Argument(..., help="Question or task for the agent")):
    """Single-turn chat with the agent."""
    cfg, first_run = agent_module.load_config()
    if first_run:
        if not agent_module._first_run_setup():
            raise typer.Exit(0)
        cfg, _ = agent_module.load_config()

    async def _run():
        components = await _build_quiet_cli_components(cfg)
        # This command runs exactly one turn and then tears the composition down, so an MCP
        # connection that is still in flight when the turn starts is cancelled before the
        # model can be offered its tools -- see `await_mcp_ready`.  Wait for it first, and
        # build the context afterwards: connecting refreshes the system prompt, and waiting
        # must not be the only part of the composition the turn misses.
        timeout = cfg.get("mcp_connect_timeout_seconds")
        await agent_module.await_mcp_ready(
            components,
            timeout=float(timeout) if timeout is not None else None,
        )
        sink = CliOutputSink(shared.CONSOLE)
        sink.begin_turn()
        try:
            # Through the command pipeline, exactly like one line typed at the
            # interactive prompt: a slash command has one meaning, not one per
            # door it came through.
            await _run_cli_turn(components, cfg, question, sink)
        finally:
            await agent_module._close_components(components)

    asyncio.run(_run())


@app.command()
def evolve(
    rewrite: bool = typer.Option(
        False, "--rewrite", help="Rewrite system prompt from session history"
    ),
    apply_best: bool = typer.Option(
        False, "--apply-best", help="Apply best-scoring prompt"
    ),
    stats: bool = typer.Option(False, "--stats", help="Show RL statistics"),
):
    """Self-evolution: analyze history and optimize the agent."""
    cfg, _ = agent_module.load_config()

    async def _run():
        components = await agent_module._build_components_async(cfg)
        evolution: Optional[EvolutionEngine] = components["evolution"]
        if evolution is None:
            shared.CONSOLE.print(
                "[yellow]Evolution is disabled (set evolution.enabled=true in config to enable).[/yellow]"
            )
            await agent_module._close_components(components)
            return
        try:
            if stats:
                s = evolution.get_stats()
                table = Table(title="RL Statistics")
                table.add_column("Metric")
                table.add_column("Value")
                for k, v in s.items():
                    table.add_row(k, str(v))
                shared.CONSOLE.print(table)
            elif apply_best:
                prompt = evolution.apply_best_prompt()
                shared.CONSOLE.print("[green]Applied best prompt.[/green]")
                shared.CONSOLE.print(f"[dim]{prompt[:200]}...[/dim]")
            else:
                shared.CONSOLE.print("[yellow]Rewriting system prompt...[/yellow]")
                new_prompt = await evolution.rewrite_system_prompt()
                shared.CONSOLE.print("[green]Done. New prompt:[/green]")
                shared.CONSOLE.print(Markdown(new_prompt[:500]))
        finally:
            await agent_module._close_components(components)

    asyncio.run(_run())


@app.command()
def config(
    action: str = typer.Argument(..., help="Action: list | models | get"),
    key: Optional[str] = typer.Argument(
        None, help="Config key (dot-notation supported, e.g. providers.qwen.base_url)"
    ),
):
    """View agent configuration (read-only).

    Examples:
      config list                              # show current config
      config models                            # list configured providers
      config get providers.qwen.default_model  # read a specific key
    """
    cfg, _ = agent_module.load_config()

    if action == "list":
        shared.CONSOLE.print(
            Markdown(f"```json\n{json.dumps(cfg, indent=2, ensure_ascii=False)}\n```")
        )

    elif action == "models":
        providers = agent_module.ModelClientFactory.list_providers(cfg)
        table = Table(title="Configured Providers")
        table.add_column("Name")
        table.add_column("Format")
        table.add_column("Default Model")
        table.add_column("Base URL")
        table.add_column("Active")
        for p in providers:
            mark = "[bold green]✓[/bold green]" if p["active"] else ""
            table.add_row(p["name"], p["format"], p["model"], p["base_url"], mark)
        shared.CONSOLE.print(table)

    elif action == "get":
        if not key:
            shared.CONSOLE.print("[red]Key required for 'get'[/red]")
            raise typer.Exit(1)
        parts = key.split(".")
        cur: Any = cfg
        for p in parts:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(p)
        if cur is None:
            shared.CONSOLE.print(f"[yellow]Key '{key}' not found[/yellow]")
        else:
            shared.CONSOLE.print(f"{key} = {cur}")

    else:
        shared.CONSOLE.print(f"[red]Unknown action '{action}'. Use: list | models | get[/red]")
        raise typer.Exit(1)


# ── Scheduler commands ───────────────────────────────────────────────────────


@schedule_app.command("once")
def schedule_once(
    name: str = typer.Argument(..., help="Task name"),
    at: str = typer.Option(..., "--at", help="ISO datetime with timezone"),
    timezone_name: str = typer.Option(
        LOCAL_TIMEZONE, "--timezone", help="IANA timezone name; default is this machine's zone"
    ),
    prompt: str = typer.Option(..., "--prompt", help="Prompt to run"),
    delivery_mode: str = typer.Option("standalone", "--delivery-mode"),
    chat_id: Optional[str] = typer.Option(None, "--chat-id"),
    chat_type: str = typer.Option("p2p", "--chat-type"),
    model: Optional[str] = typer.Option(None, "--model"),
):
    store = _scheduler_store()
    try:
        task = store.create_task(
            NewScheduledTask(
                name=name,
                kind="agent_prompt",
                trigger=TriggerSpec.once(at, timezone_name),
                payload={"prompt": prompt},
                delivery_mode=delivery_mode,
                delivery_target=_scheduler_delivery_target(
                    delivery_mode, chat_id=chat_id, chat_type=chat_type
                ),
                model_override=model,
            )
        )
        shared.CONSOLE.print(
            f"[green]Created scheduled task[/green] {task.name} ({task.id})"
        )
    finally:
        store.close()


@schedule_app.command("interval")
def schedule_interval(
    name: str = typer.Argument(..., help="Task name"),
    every: int = typer.Option(..., "--every", min=1),
    unit: str = typer.Option(..., "--unit", help="minutes|hours|days|weeks"),
    anchor_at: str = typer.Option(..., "--anchor-at", help="ISO datetime with timezone"),
    timezone_name: str = typer.Option(
        LOCAL_TIMEZONE, "--timezone", help="IANA timezone name; default is this machine's zone"
    ),
    prompt: str = typer.Option(..., "--prompt", help="Prompt to run"),
    delivery_mode: str = typer.Option("standalone", "--delivery-mode"),
    chat_id: Optional[str] = typer.Option(None, "--chat-id"),
    chat_type: str = typer.Option("p2p", "--chat-type"),
    model: Optional[str] = typer.Option(None, "--model"),
):
    store = _scheduler_store()
    try:
        task = store.create_task(
            NewScheduledTask(
                name=name,
                kind="agent_prompt",
                trigger=TriggerSpec.interval(every, unit, anchor_at, timezone_name),
                payload={"prompt": prompt},
                delivery_mode=delivery_mode,
                delivery_target=_scheduler_delivery_target(
                    delivery_mode, chat_id=chat_id, chat_type=chat_type
                ),
                model_override=model,
            )
        )
        shared.CONSOLE.print(
            f"[green]Created scheduled task[/green] {task.name} ({task.id})"
        )
    finally:
        store.close()


@schedule_app.command("daily")
def schedule_daily(
    name: str = typer.Argument(..., help="Task name"),
    time_of_day: str = typer.Option(..., "--time", help="HH:MM local wall clock"),
    timezone_name: str = typer.Option(
        LOCAL_TIMEZONE, "--timezone", help="IANA timezone name; default is this machine's zone"
    ),
    prompt: str = typer.Option(..., "--prompt", help="Prompt to run"),
    delivery_mode: str = typer.Option("standalone", "--delivery-mode"),
    chat_id: Optional[str] = typer.Option(None, "--chat-id"),
    chat_type: str = typer.Option("p2p", "--chat-type"),
    model: Optional[str] = typer.Option(None, "--model"),
):
    store = _scheduler_store()
    try:
        task = store.create_task(
            NewScheduledTask(
                name=name,
                kind="agent_prompt",
                trigger=TriggerSpec.daily(time_of_day, timezone_name),
                payload={"prompt": prompt},
                delivery_mode=delivery_mode,
                delivery_target=_scheduler_delivery_target(
                    delivery_mode, chat_id=chat_id, chat_type=chat_type
                ),
                model_override=model,
            )
        )
        shared.CONSOLE.print(
            f"[green]Created scheduled task[/green] {task.name} ({task.id})"
        )
    finally:
        store.close()


@schedule_app.command("weekly")
def schedule_weekly(
    name: str = typer.Argument(..., help="Task name"),
    day_of_week: str = typer.Option(..., "--day", help="mon|tue|..."),
    time_of_day: str = typer.Option(..., "--time", help="HH:MM local wall clock"),
    timezone_name: str = typer.Option(
        LOCAL_TIMEZONE, "--timezone", help="IANA timezone name; default is this machine's zone"
    ),
    prompt: str = typer.Option(..., "--prompt", help="Prompt to run"),
    delivery_mode: str = typer.Option("standalone", "--delivery-mode"),
    chat_id: Optional[str] = typer.Option(None, "--chat-id"),
    chat_type: str = typer.Option("p2p", "--chat-type"),
    model: Optional[str] = typer.Option(None, "--model"),
):
    store = _scheduler_store()
    try:
        task = store.create_task(
            NewScheduledTask(
                name=name,
                kind="agent_prompt",
                trigger=TriggerSpec.weekly(day_of_week, time_of_day, timezone_name),
                payload={"prompt": prompt},
                delivery_mode=delivery_mode,
                delivery_target=_scheduler_delivery_target(
                    delivery_mode, chat_id=chat_id, chat_type=chat_type
                ),
                model_override=model,
            )
        )
        shared.CONSOLE.print(
            f"[green]Created scheduled task[/green] {task.name} ({task.id})"
        )
    finally:
        store.close()


@schedule_app.command("list")
def schedule_list():
    store = _scheduler_store()
    try:
        tasks = store.list_tasks()
    finally:
        store.close()
    if not tasks:
        shared.CONSOLE.print("[yellow]No scheduled tasks.[/yellow]")
        return
    _scheduler_print_task_table(tasks)


@schedule_app.command("show")
def schedule_show(task_id: str = typer.Argument(..., help="Task id")):
    store = _scheduler_store()
    try:
        task = store.get_task(task_id)
        runs = store.list_runs(task_id)
    finally:
        store.close()
    if task is None:
        shared.CONSOLE.print(f"[red]Task not found:[/red] {task_id}")
        raise typer.Exit(1)
    payload = {
        "id": task.id,
        "name": task.name,
        "kind": task.kind,
        "enabled": task.enabled,
        "delivery_mode": task.delivery_mode,
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "payload": task.payload,
        "runs": [
            {
                "id": run.id,
                "status": run.status,
                "scheduled_for": run.scheduled_for.isoformat(),
                "summary": run.summary,
            }
            for run in runs
        ],
    }
    shared.CONSOLE.print(
        Markdown(f"```json\n{json.dumps(payload, indent=2, ensure_ascii=False)}\n```")
    )


@schedule_app.command("pause")
def schedule_pause(task_id: str = typer.Argument(..., help="Task id")):
    store = _scheduler_store()
    try:
        store.set_enabled(task_id, False)
    except ValueError as exc:
        shared.CONSOLE.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    finally:
        store.close()
    shared.CONSOLE.print(f"[green]Paused[/green] {task_id}")


@schedule_app.command("resume")
def schedule_resume(task_id: str = typer.Argument(..., help="Task id")):
    store = _scheduler_store()
    try:
        store.set_enabled(task_id, True)
    except ValueError as exc:
        shared.CONSOLE.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    finally:
        store.close()
    shared.CONSOLE.print(f"[green]Resumed[/green] {task_id}")


@schedule_app.command("delete")
def schedule_delete(task_id: str = typer.Argument(..., help="Task id")):
    store = _scheduler_store()
    try:
        store.delete_task(task_id)
    except ValueError as exc:
        shared.CONSOLE.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    finally:
        store.close()
    shared.CONSOLE.print(f"[green]Deleted[/green] {task_id}")


def _scheduler_services(components: dict) -> dict:
    """The four services the scheduler's builder reads, pulled out of a mapping.

    The builder used to take the mapping itself, which meant a plugin had to be handed
    the whole composition.  It reads four names -- see
    :func:`agent.scheduler.build.build_scheduler_service` -- and this is the one place
    that knows which, so the two call sites cannot disagree about it.

    ``memory`` is required by the builder and is passed through as ``None`` when absent,
    so the builder's own error names the missing service instead of this raising a
    ``KeyError`` at a call site that has nothing to do with memory.
    """
    return {
        "memory": components.get("memory"),
        "output_dir": components.get("output_dir"),
        "workspace_root": components.get("workspace_root"),
        "registry": components.get("registry"),
    }


#: How far back a repeated failure counts. A week is long enough for a weekly
#: task to fail twice and short enough that last month's outage is not evidence.
_REPEAT_FAILURE_WINDOW = timedelta(days=7)


def _task_failure_history(store: Any, task_id: str) -> list[dict[str, str]]:
    """This task's failures inside the window, classified.

    Read from the runs rather than the attention count: attention says *how
    many* failures nobody has read, and the repeated-failure rule needs the
    reasons and their times. Returns newest-last, with the label from
    `agent.scheduler.diagnostics` -- the same classification the board shows, so
    two surfaces cannot describe one failure differently.
    """
    from agent.scheduler.diagnostics import describe_failure

    try:
        runs = store.list_runs(task_id)
    except Exception:
        return []
    horizon = datetime.now(timezone.utc) - _REPEAT_FAILURE_WINDOW
    history: list[dict[str, str]] = []
    for run in runs:
        if str(getattr(run, "status", "") or "") not in {"failed", "interrupted"}:
            continue
        moment = getattr(run, "finished_at", None) or getattr(run, "started_at", None)
        if moment is None or moment < horizon:
            continue
        reason = describe_failure(getattr(run, "error", ""))
        history.append(
            {
                "at": moment.astimezone(timezone.utc).isoformat(),
                "label": reason.label if reason is not None else "",
                "task_id": str(task_id),
            }
        )
    return history


def _autonomy_session_rows(
    limit: int = 50, *, live_sessions: Any = None
) -> list[dict[str, object]]:
    """What the patrol may look at, per source -- from the stores that hold it.

    Four facts, and the reason each is where it is:

    * **which sessions exist, and when they were last active** -- the web session
      index (the same file the page reads);
    * **an unfinished task** -- ``session_working_state``, which lives on the *memory*
      store, not the scheduler's: it is a projection of the conversation, not of a
      schedule;
    * **a queue that has not moved** -- the live runtime state, which only exists
      while the process serving it is running. A session nobody is serving has no
      queue to be stuck in, so its absence is the honest answer rather than zero;
    * **a failed run nobody has read** -- the scheduler's attention snapshot. It
      is a fact about a *task*, and a task belongs to a session only when its row
      names one. A task without a session id (every task the patrol itself
      executes is created that way) gets a row of its own keyed ``task:<id>``;
      without that row rule 1 can never fire for it, and its loss is invisible.

    A missing fact means the matching rule cannot fire. What must not happen is
    what happened before this was written: the rules were given *no* facts at all,
    so "0 proposals" meant "the funnel is blind", and it read exactly like "there
    is nothing to do".
    """
    from agent import shared
    from agent.autonomy import is_own_errand, live_queue_facts, unowned_task_rows
    from agent.session_service import _WebSessionRegistry

    def _stamp(value: Any) -> str:
        return value.astimezone(timezone.utc).isoformat() if value is not None else ""

    queue_by_session = live_queue_facts(live_sessions)
    rows: list[dict[str, object]] = []
    try:
        registry = _WebSessionRegistry(shared.AGENT_HOME / "web" / "sessions" / "index.db")
        entries = list(registry.list(limit=limit))
    except Exception:
        # No session index is not "no failures": the scheduler may still hold
        # runs nobody has read, and those get their own rows below.
        entries = []

    # The scheduler's attention snapshot, read once: which tasks have failures
    # nobody has acknowledged, and when each task last did anything.
    attention_counts: dict[str, int] = {}
    session_task_ids: dict[str, set[str]] = {}
    task_facts: list[dict[str, object]] = []
    latest_run_at: dict[str, str] = {}
    try:
        from agent.scheduler import open_store as open_scheduler_store

        scheduler_store = open_scheduler_store()
        try:
            snapshot = scheduler_store.attention_snapshot()
            attention_counts = {
                str(key): int(value)
                for key, value in (snapshot.get("counts") or {}).items()
            }
            for task in scheduler_store.list_tasks():
                # The agent's own errands are skipped on purpose.
                #
                # An autonomous proposal executes by creating a one-off task (see
                # `execute.py`), and that task carries `origin: autonomy`. Reading
                # its failure back as a new candidate closes a loop with no exit:
                # failure -> attention -> "read the failed run" -> another
                # autonomous task -> another failure. Every round has a fresh task
                # id and a fresh action sentence, so the dedupe key never matches
                # and each round leaves another task behind.
                # The failure is not hidden by this: it lands on the proposal's
                # `outcome`, which is what 自主 renders next to the proposal.
                if is_own_errand(getattr(task, "payload", None)):
                    continue
                # The *session*, not the words. `request_quote` holds what the user
                # typed ("每天早上九点提醒我写日报"); grepping it for a session id
                # never matched anything, which is why the funnel was blind even
                # after the other facts were wired.
                owner = str(getattr(task, "session_id", "") or "").strip()
                if owner:
                    session_task_ids.setdefault(owner, set()).add(task.id)
                task_facts.append(
                    {
                        "task_id": task.id,
                        "name": str(getattr(task, "name", "") or ""),
                        "last_run_at": _stamp(getattr(task, "last_run_at", None)),
                        "created_at": _stamp(getattr(task, "created_at", None)),
                        # The reasons, not just the count. "This task keeps
                        # failing" is only actionable with *why* -- two failures
                        # from a dropped connection and two from a broken
                        # credential need different answers, and the classified
                        # label is what tells them apart.
                        "failures": _task_failure_history(scheduler_store, task.id),
                        # What the task is set to *now*, so a proposal can name
                        # an exact change instead of "make it longer".
                        "timeout_seconds": int(
                            getattr(task, "timeout_seconds", 0) or 0
                        ),
                        "retry_policy": dict(
                            getattr(task, "retry_policy", {}) or {}
                        ),
                    }
                )
            # The row's ``last_activity`` has to be real: the warm/stale gates
            # drop rows by it. A failure is activity at the run's time, not at
            # the task's creation.
            for task_id in attention_counts:
                try:
                    latest = scheduler_store.latest_run(task_id)
                except Exception:
                    continue
                if latest is None:
                    continue
                moment = getattr(latest, "finished_at", None) or getattr(
                    latest, "scheduled_for", None
                )
                if moment is not None:
                    latest_run_at[task_id] = _stamp(moment)
        finally:
            scheduler_store.close()
    except Exception:
        attention_counts = {}
        session_task_ids = {}
        task_facts = []
        latest_run_at = {}

    # The memory store is where the working state lives; opened once for the
    # whole pass rather than per session, because it is one database.
    memory_store = None
    try:
        from agent.memory.store import LTMStore

        memory_store = LTMStore(shared.AGENT_HOME / "context")
    except Exception:
        memory_store = None

    placed_task_ids: set[str] = set()
    try:
        for item in entries:
            session_id = str(item.get("session_id") or "")
            if not session_id:
                continue
            row: dict[str, object] = {
                "session_id": session_id,
                "title": str(item.get("title") or ""),
                "last_activity": str(item.get("last_activity") or ""),
                "status": str(item.get("status") or ""),
                "turn_count": int(item.get("turn_count") or 0),
            }
            # Tasks this session owns, with their unacknowledged failure counts.
            # The attention snapshot is keyed by task id, so the join is here:
            # "which tasks belong to this session" is a fact only the scheduler's
            # task rows carry (`request_quote` / the session that created them).
            try:
                if attention_counts:
                    history = {
                        str(fact["task_id"]): fact.get("failures") or []
                        for fact in task_facts
                    }
                    names = {
                        str(fact["task_id"]): str(fact.get("name") or "")
                        for fact in task_facts
                    }
                    settings = {
                        str(fact["task_id"]): {
                            "timeout_seconds": fact.get("timeout_seconds") or 0,
                            "retry_policy": fact.get("retry_policy") or {},
                        }
                        for fact in task_facts
                    }
                    row["tasks"] = [
                        {
                            "task_id": task_id,
                            "name": names.get(task_id, ""),
                            "attention": count,
                            "failures": history.get(task_id, []),
                            "timeout_seconds": settings.get(task_id, {}).get("timeout_seconds", 0),
                            "retry_policy": settings.get(task_id, {}).get("retry_policy") or {},
                        }
                        for task_id, count in attention_counts.items()
                        if task_id in session_task_ids.get(session_id, ())
                    ]
                    placed_task_ids.update(
                        str(task["task_id"]) for task in row["tasks"]
                    )
            except Exception:
                pass
            # A queue only exists while something is serving it. The gateway
            # hands its live states in; a CLI pass has none, which is the honest
            # answer for a process that is not serving any session.
            if live_sessions is not None:
                queue = queue_by_session.get(session_id)
                if queue:
                    row["queue"] = queue
            if memory_store is not None:
                try:
                    snapshot = memory_store.load_session_working_state(session_id)
                    raw = getattr(snapshot, "state", None) if snapshot is not None else None
                    if isinstance(raw, dict) and raw:
                        # `task` when the record uses the newer collection shape,
                        # the record itself when it is the older single-task one.
                        row["task"] = raw.get("task") or raw
                except Exception:
                    pass
            rows.append(row)
    finally:
        if memory_store is not None:
            try:
                memory_store.close()
            except Exception:
                pass
    # A task with an unread failure but no session row would otherwise never be
    # seen by rule 1, which fires per row.  Only tasks that are *not* already
    # shown under a session are added, so one failure cannot become two proposals
    # under two sources (which would also walk around the dedupe).
    rows.extend(
        unowned_task_rows(
            task_facts,
            attention_counts=attention_counts,
            placed_task_ids=placed_task_ids,
            latest_run_at=latest_run_at,
        )
    )
    return rows


def _start_patrol(config: dict, components: dict | None = None) -> Any:
    """Start the patrol in this process, or return None when it is off.

    Its session list comes from the same index the page reads (see
    ``_autonomy_session_rows``), so "which sessions exist" has one answer in this
    process. ``components`` is optional and is used for one thing: the live
    sessions a queue only exists inside. A patrol that cannot start is a feature
    that is off -- nothing here raises into the gateway's startup.
    """
    try:
        from agent.autonomy.patrol import PatrolSettings, open_patrol_service

        settings = PatrolSettings.from_config(config)
        if not settings.enabled:
            shared.CONSOLE.print("[dim]Autonomy patrol: off (autonomy.enabled=false)[/dim]")
            return None

        def _live_sessions() -> Any:
            """The runner's live sessions, if this process has a runner."""
            if components is None:
                return None
            try:
                from agent.runtime.services import resolve as _resolve

                registry = _resolve(components, "registry")
                return registry.get_context("live_sessions") if registry is not None else None
            except Exception:
                return None

        service = open_patrol_service(
            settings=settings,
            sessions_provider=lambda: _autonomy_session_rows(
                limit=50, live_sessions=_live_sessions()
            ),
        )
        if not service.start():
            return None
        shared.CONSOLE.print(
            f"[dim]Autonomy patrol: every {settings.interval_minutes} min, "
            "acting on reversible proposals[/dim]"
        )
        return service
    except Exception as exc:  # noqa: BLE001 - never fail a gateway over this
        shared.CONSOLE.print(f"[yellow]Autonomy patrol did not start: {exc}[/yellow]")
        return None


@app.command()
def autonomy(
    action: str = typer.Argument(
        "review",
        help=(
            "review (list proposals) | tick (run one pass) | decide | "
            "stats | act (execute one approved reversible proposal)"
        ),
    ),
    proposal_id: Optional[str] = typer.Argument(
        None, help="The proposal to answer, for `decide`"
    ),
    state: str = typer.Option(
        "declined", "--state", help="accepted | declined | acted (for `decide`)"
    ),
    name: Optional[str] = typer.Option(
        None, "--name", help="Session name (default: ~/.agent)"
    ),
):
    """The patrol: what the agent noticed, what it did about it, and what you said.

    `tick` runs one pass through the same funnel the gateway's scheduled pass uses
    -- including acting on what admission allowed -- so the numbers and the
    behaviour are the same ones a person would see in production.  `review` is
    what a person reads, `decide` is how a proposal stops being live (including
    "no", which is the answer worth keeping), and `act` is the road left for a
    person who wants to run a reversible proposal by hand.
    """
    if isinstance(name, str):
        agent_module._set_agent_home(shared.session_home(name))
    from agent.autonomy import PatrolOptions, patrol_tick
    from agent.autonomy.rules import rule_candidates
    from agent.autonomy.store import open_proposal_store

    store = open_proposal_store()
    try:
        if action == "tick":
            # The sessions, read from the index the web channel writes.
            #
            # Not through `SessionService`: its registry is only built when the
            # composition hands it a `store_factory`, and this command has no
            # composition. The index file is the same truth the page reads, so
            # reading it directly is the smaller dependency rather than a
            # different answer.
            sessions = _autonomy_session_rows(limit=50)
            # Through the service, not `patrol_tick`, so a manual pass does exactly
            # what a scheduled one does -- including acting on what admission
            # allowed. Two paths that run "the same" funnel and behave differently
            # is how a person ends up debugging behaviour the gateway never has.
            from agent.autonomy.patrol import PatrolService

            service = PatrolService(
                store=store,
                sessions_provider=lambda: sessions,
            )
            result = asyncio.run(service.tick())
            shared.CONSOLE.print(
                f"[dim]tick: examined {result['examined']}, "
                f"eligible {result['eligible']}, warm {result['skipped_warm']}, "
                f"stale {result['skipped_stale']}, proposed {result['proposed']}, "
                f"duplicates {result['duplicates']}, "
                f"[bold]did {result.get('executed', 0)}[/bold][/dim]"
            )
            for row in result["proposals"]:
                verdict = (row.get("context_json") or {}).get("admission") or {}
                shared.CONSOLE.print(
                    f"[cyan]{row['reversibility']}[/cyan] "
                    f"[bold]{verdict.get('answer', '?')}[/bold] {row['action']}"
                    f"\n  [dim]because {row['reason']}[/dim]"
                    f"\n  [dim]→ {verdict.get('reason', '')}[/dim]"
                )
            return
        if action == "stats":
            # The funnel's numbers, which are the calibration: a
            # funnel that proposes nothing because it examines nothing looks
            # exactly like one that proposes nothing because there is nothing to do.
            from datetime import datetime, timedelta, timezone

            # A week by default: long enough to have passes in it, short enough
            # that the numbers are about the current rules rather than the last one.
            window = 7
            since = datetime.now(timezone.utc) - timedelta(days=window)
            stats = store.tick_stats(since=since)
            counts = store.counts()
            examined = stats["examined"]
            proposed = stats["proposed"]
            rate = (proposed / examined) if examined else 0.0
            shared.CONSOLE.print(
                f"[bold]最近 {window} 天[/bold] {stats['passes']} 趟巡逻，"
                f"看了 {examined} 个会话，产出 {proposed} 条提议，"
                f"[bold]自己做了 {stats.get('executed', 0)} 件[/bold]"
            )
            shared.CONSOLE.print(
                f"[dim]跳过：温 {stats['skipped_warm']} · 旧 {stats['skipped_stale']} · "
                f"准入丢弃 {stats['dropped']} · 重复 {stats['duplicates']} · "
                f"过期 {stats['expired']}[/dim]"
            )
            shared.CONSOLE.print(
                f"[dim]每看一个会话产出 {rate:.3f} 条提议；"
                f"当前待答复 {counts.get('proposed', 0)}，"
                f"已答复 {counts.get('accepted', 0) + counts.get('declined', 0)}[/dim]"
            )
            return
        if action == "review":
            counts = store.counts()
            live = store.list_proposals(state="proposed", limit=20)
            if not live:
                shared.CONSOLE.print("[dim]没有待处理的提议。[/dim]")
            for proposal in live:
                shared.CONSOLE.print(
                    f"[bold]{proposal.id[:8]}[/bold] {proposal.action}"
                    f"\n  [dim]{proposal.reversibility} · 置信度 "
                    f"{proposal.confidence:.2f} · {proposal.reason}[/dim]"
                )
            shared.CONSOLE.print(
                "[dim]counts: "
                + ", ".join(f"{key} {value}" for key, value in sorted(counts.items()))
                + "[/dim]"
            )
            return
        if action == "act":
            # A person's command, never the agent's decision. This is the line
            # this whole delivery is drawn on: the agent proposes, a person acts.
            if not proposal_id:
                shared.CONSOLE.print("[red]act needs a proposal id[/red]")
                raise typer.Exit(1)
            from agent.autonomy.execute import execute_proposal
            from agent.scheduler import open_store as open_scheduler_store

            # A prefix, like `git`: `review` prints the first eight characters,
            # so eight has to be enough to name one. An ambiguous prefix is
            # refused rather than guessed -- picking the first match would act on
            # a proposal the person did not mean.
            matches = [
                row for row in store.list_proposals(limit=500)
                if row.id == proposal_id or row.id.startswith(proposal_id)
            ]
            if len(matches) > 1:
                shared.CONSOLE.print(
                    f"[red]{proposal_id} 匹配到 {len(matches)} 条提议，请给更长的前缀[/red]"
                )
                raise typer.Exit(1)
            target = matches[0] if matches else None
            if target is None:
                shared.CONSOLE.print(f"[red]no such proposal: {proposal_id}[/red]")
                raise typer.Exit(1)
            scheduler_store = open_scheduler_store()
            try:
                outcome = execute_proposal(
                    store=store,
                    proposal=target,
                    scheduler_store=scheduler_store,
                )
            finally:
                scheduler_store.close()
            if outcome["ok"]:
                shared.CONSOLE.print(
                    f"[green]已建任务[/green] {outcome['task_id'][:8]} "
                    f"[dim](只读档；由 {outcome['runner']} 的调度器接手才真正运行)[/dim]"
                )
            else:
                shared.CONSOLE.print(f"[yellow]{outcome['reason']}[/yellow]")
                raise typer.Exit(1)
            return
        if action == "decide":
            if not proposal_id:
                shared.CONSOLE.print("[red]decide needs a proposal id[/red]")
                raise typer.Exit(1)
            matches = [
                row for row in store.list_proposals(limit=500)
                if row.id == proposal_id or row.id.startswith(proposal_id)
            ]
            if len(matches) != 1:
                shared.CONSOLE.print(
                    f"[red]{proposal_id} 匹配到 {len(matches)} 条提议[/red]"
                )
                raise typer.Exit(1)
            ok = store.decide(matches[0].id, state, by="cli")
            shared.CONSOLE.print(
                "[green]recorded[/green]" if ok else "[yellow]already decided[/yellow]"
            )
            return
        shared.CONSOLE.print(f"[red]unknown action: {action}[/red]")
        raise typer.Exit(1)
    finally:
        store.close()


@app.command()
def scheduler(
    poll_seconds: Optional[float] = typer.Option(None, "--poll-seconds", min=0.1),
    lease_seconds: Optional[int] = typer.Option(None, "--lease-seconds", min=3),
    name: Optional[str] = typer.Option(
        None, "--name", help="Session name (default: ~/.agent)"
    ),
):
    """Run the persistent scheduler service."""
    if isinstance(name, str):
        agent_module._set_agent_home(shared.session_home(name))
    cfg, first_run = agent_module.load_config()
    _configure_runtime_logging()
    # Same reasoning as the gateway's: this process outlives the terminal it started in.
    log_path = logs_module.attach_file_log("scheduler")
    if first_run:
        shared.CONSOLE.print(f"[dim]Log: {logs_module.describe(log_path)}[/dim]")
    if first_run:
        if not agent_module._first_run_setup():
            raise typer.Exit(0)
        cfg, _ = agent_module.load_config()
    settings = SchedulerSettings.resolve(
        cfg, poll_seconds=poll_seconds, lease_seconds=lease_seconds
    )
    effective_poll = settings.poll_seconds
    effective_lease = settings.lease_seconds
    effective_max_concurrent = settings.max_concurrent_runs
    effective_signal_depth = settings.signal_max_depth

    async def _run():
        # Built here rather than by the builder: the builder answers "what is a
        # scheduler", and constructing a whole runtime is the composition root's job.
        components = await agent_module._build_components_async(cfg)
        try:
            service, store = _build_scheduler_service(
                cfg,
                poll_seconds=effective_poll,
                lease_seconds=effective_lease,
                max_concurrent_runs=effective_max_concurrent,
                signal_max_depth=effective_signal_depth,
                **_scheduler_services(components),
            )
        except BaseException:
            await agent_module._close_components(components)
            raise
        shared.CONSOLE.print(
            "[dim]Scheduler running "
            f"(poll={effective_poll}s, lease={effective_lease}s, "
            f"max_concurrent={effective_max_concurrent})[/dim]"
        )
        try:
            await service.run_forever()
        finally:
            await service.shutdown()
            store.close()
            await agent_module._close_components(components)

    asyncio.run(_run())


# ── Memory subcommands ────────────────────────────────────────────────────────


@memory_app.command("ls")
def memory_ls():
    """Show memory export summary.

    Through the composition's ``memory`` service, not a palace built here: these commands
    used to construct one directly, so a composition that replaced the service -- a
    profile pointing ``memory`` at another store -- was invisible to them.
    """
    cfg, _ = agent_module.load_config()
    with agent_module.memory_service(cfg) as memory:
        lines = [line for line in memory.read_index().splitlines() if line.strip()]
        table = Table(title="Memory Export")
        table.add_column("Metric")
        table.add_column("Value")
        table.add_row("Projection", "memory/memory.jsonl")
        table.add_row("Entries", str(len(lines)))
        shared.CONSOLE.print(table)


@memory_app.command("show")
def memory_show(
    path: str = typer.Argument(..., help="chapter/name (e.g. projects/myproject)"),
):
    """Show contents of a memory file."""
    parts = path.strip("/").split("/", 1)
    if len(parts) != 2:
        shared.CONSOLE.print("[red]Path must be chapter/name[/red]")
        raise typer.Exit(1)
    chapter, name = parts
    cfg, _ = agent_module.load_config()
    with agent_module.memory_service(cfg) as memory:
        content = memory.read(chapter, name)
    if content:
        shared.CONSOLE.print(Markdown(content))
    else:
        shared.CONSOLE.print(f"[yellow]No memory at {path}[/yellow]")


@memory_app.command("search")
def memory_search(query: str = typer.Argument(..., help="Search query")):
    """Search across all memory files."""
    cfg, _ = agent_module.load_config()
    with agent_module.memory_service(cfg) as memory:
        results = memory.search(query)
    if not results:
        shared.CONSOLE.print(f"[yellow]No results for '{query}'[/yellow]")
        return
    table = Table(title=f"Search: {query}")
    table.add_column("Path")
    table.add_column("Snippet")
    for r in results:
        table.add_row(r["path"], r["snippet"][:80])
    shared.CONSOLE.print(table)


@memory_app.command("tidy")
def memory_tidy():
    """Manually trigger AI-assisted memory reorganization."""
    cfg, _ = agent_module.load_config()

    async def _run():
        components = await agent_module._build_components_async(cfg)
        mem: MemoryPalace = require_service(components, "memory")
        mem.force_tidy()
        try:
            await mem.tidy()
        finally:
            await agent_module._close_components(components)

    asyncio.run(_run())


@memory_app.command("index")
def memory_index():
    """Show the memory JSONL export."""
    cfg, _ = agent_module.load_config()
    with agent_module.memory_service(cfg) as memory:
        shared.CONSOLE.print(Markdown(memory.read_index()))


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()
