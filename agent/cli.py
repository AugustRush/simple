from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import signal
import sys
from typing import Any, Optional

import typer
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

import agent as agent_module
from agent import shared
from agent.core.output import CliOutputSink
from agent.commands import CommandCoordinator, CommandRouter, register_builtin_commands
from agent.runtime import AgentCore, RuntimeComponents, RuntimeSessionState, TurnInput
from agent.shared import CancelToken

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
SkillCatalog = agent_module.SkillCatalog
StagingBuffer = agent_module.StagingBuffer
TriggerSpec = agent_module.TriggerSpec
_build_gateway_channels = agent_module._build_gateway_channels

app = typer.Typer(
    name="agent",
    help="Personal AI Agent with Memory Palace, Multi-Agent Orchestration, and Self-Evolution",
    add_completion=False,
)
memory_app = typer.Typer(help="Memory palace commands")
app.add_typer(memory_app, name="memory")
schedule_app = typer.Typer(help="Scheduled task commands")
app.add_typer(schedule_app, name="schedule")

_INTERACTION_LOGGER_NAMES = (
    "agent.channels.base",
    "agent.core.agent",
    "channels.feishu",
)


def _agent_core_for_components(components: dict):
    agent_core = components.get("agent_core")
    if agent_core is None or isinstance(agent_core, AgentCore):
        return AgentCore(RuntimeComponents(components))
    return agent_core


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


def _scheduler_store() -> SchedulerStore:
    return SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)


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


async def _build_scheduler_service(
    cfg: dict,
    *,
    poll_seconds: float,
    lease_seconds: int,
    max_concurrent_runs: int,
    components: Optional[dict] = None,
):
    owned_components = components is None
    if components is None:
        components = await agent_module._build_components_async(cfg)
    store = _scheduler_store()
    delivery = SchedulerDelivery(
        cfg=cfg,
        output_root=(components.get("output_dir") or shared.DEFAULT_OUTPUT_DIR)
        / "scheduler",
    )

    async def _agent_executor(task, run):
        ctx = AgentContext(system_prompt=components["system_prompt"])
        state = RuntimeSessionState(ctx=ctx)
        prompt = str(task.payload.get("prompt", "")).strip()
        if not prompt:
            raise RuntimeError(f"Scheduled task '{task.name}' has no prompt")
        execution = await _agent_core_for_components(components).handle_turn(
            TurnInput.from_text(prompt, channel_name="scheduler"),
            state,
        )
        result = execution.result
        if result.error:
            raise RuntimeError(result.error)
        content = result.text or ""
        summary = content.strip().splitlines()[0][:120] if content.strip() else task.name
        return ExecutionResult(summary=summary, text_output=content)

    async def _system_executor(task, run):
        job_name = str(task.payload.get("job_name", "")).strip()
        if job_name == "memory_tidy":
            memory: MemoryPalace = components["memory"]
            memory.force_tidy()
            await memory.tidy(components["client"], components["model"])
            return ExecutionResult(
                summary="memory tidied",
                text_output="",
            )
        raise RuntimeError(f"Unsupported system job: {job_name}")

    service = SchedulerService(
        store=store,
        agent_executor=_agent_executor,
        system_executor=_system_executor,
        delivery=delivery,
        poll_seconds=poll_seconds,
        lease_seconds=lease_seconds,
        max_concurrent_runs=max_concurrent_runs,
    )
    return service, store, components if owned_components else None


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


async def _interactive_loop(components: dict, cfg: dict):
    """Main interactive chat loop."""
    global _current_cancel_token
    agent: BaseAgent = components["agent"]
    plugin_catalog: PluginCatalog = components.get("plugin_catalog")  # type: ignore[assignment]
    if plugin_catalog is None:
        plugin_catalog = PluginCatalog(
            builtin_dir=shared.PLUGINS_DIR,
            user_dir=shared.USER_PLUGINS_DIR,
            plugin_config=cfg.get("plugins", {}),
            turn_hook_timeout_seconds=cfg.get("orchestration", {}).get(
                "turn_hook_timeout_seconds",
                shared.DEFAULT_TURN_HOOK_TIMEOUT_SECONDS,
            ),
        )
        plugin_catalog.discover_and_load()
        components["plugin_catalog"] = plugin_catalog
    system_prompt = components["system_prompt"]
    ctx_mgr: Optional[ContextManager] = components.get("context_manager")
    skill_catalog: SkillCatalog = components["skill_catalog"]

    ctx = AgentContext(system_prompt=system_prompt)
    # Track the user's first non-command message so it can be re-injected into
    # the system prompt after compaction (compact_messages drops early messages
    # to keep working memory bounded; this preserves the original task intent
    # without coupling task context to API message-list formatting rules).
    memory_worker = (
        agent_module.BackgroundMemoryWorker(
            ctx_mgr,
            components["client"],
            components["model"],
            agent.api_format,
            client_factory=lambda: agent_module.ModelClientFactory.from_config(
                cfg, announce=False
            )[0],
        )
        if ctx_mgr
        else None
    )
    if memory_worker:
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

    coordinator_factory = components.get("command_coordinator_factory")
    if callable(coordinator_factory):
        coordinator = coordinator_factory(
            cancel_token_factory=_new_cli_cancel_token,
        )
    else:
        router = components.get("command_router")
        if router is None:
            router = CommandRouter(skill_catalog=components.get("skill_catalog"))
            register_builtin_commands(router)
            get_commands = getattr(plugin_catalog, "get_slash_commands", None)
            if callable(get_commands):
                router.register_plugin_catalog(plugin_catalog)
            components["command_router"] = router
        coordinator = CommandCoordinator(
            _agent_core_for_components(components),
            router,
            components=components,
            config=cfg,
            cancel_token_factory=_new_cli_cancel_token,
        )

    # Queue orphaned staging files from previous sessions for background
    # recovery. Doing this synchronously would block startup on a network model
    # call before the user even sees the prompt.
    if ctx_mgr:
        staging_dir = shared.STAGING_DIR
        current_sid = ctx_mgr.staging.session_id
        orphans = [
            p
            for p in staging_dir.glob("*.jsonl")
            if p.stem != current_sid and p.stat().st_size > 0
        ]
        if orphans:
            shared.CONSOLE.print(
                f"[dim]💤 Queueing recovery for {len(orphans)} orphaned session(s)...[/dim]"
            )
            for orphan_path in orphans:
                ctx_mgr.enqueue_staging_job(
                    "orphan_recovery",
                    StagingBuffer(path=orphan_path, session_id=orphan_path.stem),
                )
            if memory_worker:
                memory_worker.wake()

    shared.CONSOLE.print(
        Panel(
            "[bold cyan]Personal Agent[/bold cyan]\n[dim]Type /help for commands[/dim]",
            title="Agent Ready",
            border_style="cyan",
        )
    )
    # Notify all plugins that the session has started.
    plugin_catalog.fire_session_start(components)

    old_sigint = signal.signal(signal.SIGINT, _cli_sigint_handler)
    try:
        while True:
            try:
                # Use asyncio.to_thread so the event loop stays alive (non-blocking
                # input). This is required for future multi-channel concurrency where
                # a second channel (Telegram, Feishu, …) runs in the same loop.
                user_input = await asyncio.to_thread(
                    Prompt.ask, "\n[bold green]You[/bold green]"
                )
            except (EOFError, KeyboardInterrupt):
                break

            if not user_input.strip():
                continue

            _turn_sink = CliOutputSink(shared.CONSOLE)
            try:
                ctx.metadata["skill_catalog"] = skill_catalog
                if not user_input.lstrip().startswith("/"):
                    shared.CONSOLE.print("[bold blue]Agent[/bold blue]: ", end="")
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
                shared.CONSOLE.print(f"\n[red]Error: {e}[/red]")
            finally:
                _current_cancel_token = None

    finally:
        signal.signal(signal.SIGINT, old_sigint)
        if memory_worker:
            memory_worker.stop()
            await memory_worker.wait()

        # Session-end consolidation runs inside the finally block so it is
        # protected against KeyboardInterrupt during the input loop.  A single
        # ^C is caught by the inner except and causes a normal break; the
        # finally block then runs this code before the process exits.
        # (A ^C^C that arrives *here* can still abort — that is user intent.)
        if ctx_mgr and ctx_mgr.should_session_end_sleep():
            shared.CONSOLE.print("[dim]💤 Session-end consolidation...[/dim]")
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
                            components["client"],
                            components["model"],
                            api_format=agent.api_format,
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

    shared.CONSOLE.print("\n[dim]Goodbye.[/dim]")


@app.callback(invoke_without_command=True)
def main_callback(ctx: typer.Context):
    """Enter interactive chat when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        cfg, first_run = agent_module.load_config()
        if first_run:
            if not agent_module._first_run_setup():
                raise typer.Exit(0)
            # Reload after potential edits
            cfg, _ = agent_module.load_config()

        async def _run():
            try:
                components = await agent_module._build_components_async(cfg)
            except RuntimeError as exc:
                shared.CONSOLE.print(f"[red]Error: {exc}[/red]")
                raise typer.Exit(1)
            try:
                await _interactive_loop(components, cfg)
            finally:
                await agent_module._close_components(components)

        asyncio.run(_run())


@app.command()
def gateway(
    name: Optional[str] = typer.Option(
        None, "--name", help="Instance name for multi-tenant isolation (default: ~/.agent)"
    ),
):
    """Start all configured external channels (Feishu, etc.).

    Reads channel configuration from the agent home directory.
    Runs until interrupted (Ctrl-C) or all channels disconnect.

    Use --name to run multiple isolated instances::

        simple gateway --name prod    # -> ~/.agent/prod/
        simple gateway --name dev     # -> ~/.agent/dev/
        simple gateway                # -> ~/.agent/
    """
    if isinstance(name, str):
        shared._set_agent_home(Path.home() / f".agent-{name}")
    cfg, first_run = agent_module.load_config()
    _configure_runtime_logging()
    if first_run:
        if not agent_module._first_run_setup():
            raise typer.Exit(0)
        cfg, _ = agent_module.load_config()

    async def _run():
        try:
            components = await agent_module._build_components_async(cfg)
        except RuntimeError as exc:
            shared.CONSOLE.print(f"[red]Error: {exc}[/red]")
            raise typer.Exit(1)
        sched_cfg = cfg.get("scheduler", {})
        scheduler_poll = float(sched_cfg.get("poll_seconds", 30))
        scheduler_lease = int(sched_cfg.get("lease_seconds", 300))
        scheduler_max_concurrent = int(sched_cfg.get("max_concurrent_runs", 3))
        scheduler_task: Optional[asyncio.Task] = None
        scheduler_store = None
        scheduler_components = None
        try:
            channels = _build_gateway_channels(cfg)
            if not channels:
                shared.CONSOLE.print(
                    "[yellow]No channels configured or none could be initialised.\n"
                    "Add channels.feishu.enabled=true to ~/.agent/config.json[/yellow]"
                )
                return
            shared.CONSOLE.print(
                f"[dim]Gateway starting {len(channels)} channel(s). "
                "Press Ctrl-C to stop.[/dim]"
            )
            service, scheduler_store, scheduler_components = await _build_scheduler_service(
                cfg,
                poll_seconds=scheduler_poll,
                lease_seconds=scheduler_lease,
                max_concurrent_runs=scheduler_max_concurrent,
                components=components,
            )
            scheduler_task = asyncio.create_task(service.run_forever())
            runner = ChannelRunner(channels, components, cfg)
            await runner.run()
        finally:
            if scheduler_task is not None:
                scheduler_task.cancel()
                await asyncio.gather(scheduler_task, return_exceptions=True)
            if scheduler_store is not None:
                scheduler_store.close()
            if scheduler_components is not None:
                await agent_module._close_components(scheduler_components)
            await agent_module._close_components(components)

    asyncio.run(_run())


@app.command()
def chat(question: str = typer.Argument(..., help="Question or task for the agent")):
    """Single-turn chat with the agent."""
    cfg, first_run = agent_module.load_config()
    if first_run:
        if not agent_module._first_run_setup():
            raise typer.Exit(0)
        cfg, _ = agent_module.load_config()

    async def _run():
        components = await agent_module._build_components_async(cfg)
        ctx = AgentContext(system_prompt=components["system_prompt"])
        state = RuntimeSessionState(ctx=ctx)
        shared.CONSOLE.print("[bold blue]Agent[/bold blue]: ", end="")
        sink = CliOutputSink(shared.CONSOLE)
        try:
            execution = await _agent_core_for_components(components).handle_turn(
                TurnInput.from_text(question, channel_name="cli"),
                state,
                sink=sink,
            )
            result = execution.result
            shared.CONSOLE.print()
            if result.error:
                shared.CONSOLE.print(f"[red]Error: {result.error}[/red]")
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
    timezone_name: str = typer.Option("UTC", "--timezone", help="IANA timezone name"),
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
    timezone_name: str = typer.Option("UTC", "--timezone", help="IANA timezone name"),
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
    timezone_name: str = typer.Option("UTC", "--timezone", help="IANA timezone name"),
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
    timezone_name: str = typer.Option("UTC", "--timezone", help="IANA timezone name"),
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
    finally:
        store.close()
    shared.CONSOLE.print(f"[green]Paused[/green] {task_id}")


@schedule_app.command("resume")
def schedule_resume(task_id: str = typer.Argument(..., help="Task id")):
    store = _scheduler_store()
    try:
        store.set_enabled(task_id, True)
    finally:
        store.close()
    shared.CONSOLE.print(f"[green]Resumed[/green] {task_id}")


@schedule_app.command("delete")
def schedule_delete(task_id: str = typer.Argument(..., help="Task id")):
    store = _scheduler_store()
    try:
        store.delete_task(task_id)
    finally:
        store.close()
    shared.CONSOLE.print(f"[green]Deleted[/green] {task_id}")


@app.command()
def scheduler(
    poll_seconds: Optional[float] = typer.Option(None, "--poll-seconds", min=0.1),
    lease_seconds: Optional[int] = typer.Option(None, "--lease-seconds", min=3),
    name: Optional[str] = typer.Option(
        None, "--name", help="Instance name for multi-tenant isolation (default: ~/.agent)"
    ),
):
    """Run the persistent scheduler service."""
    if isinstance(name, str):
        shared._set_agent_home(Path.home() / f".agent-{name}")
    cfg, first_run = agent_module.load_config()
    if first_run:
        if not agent_module._first_run_setup():
            raise typer.Exit(0)
        cfg, _ = agent_module.load_config()
    sched_cfg = cfg.get("scheduler", {})
    effective_poll = float(poll_seconds or sched_cfg.get("poll_seconds", 30))
    effective_lease = int(lease_seconds or sched_cfg.get("lease_seconds", 300))
    effective_max_concurrent = int(sched_cfg.get("max_concurrent_runs", 3))

    async def _run():
        service, store, components = await _build_scheduler_service(
            cfg,
            poll_seconds=effective_poll,
            lease_seconds=effective_lease,
            max_concurrent_runs=effective_max_concurrent,
        )
        shared.CONSOLE.print(
            "[dim]Scheduler running "
            f"(poll={effective_poll}s, lease={effective_lease}s, "
            f"max_concurrent={effective_max_concurrent})[/dim]"
        )
        try:
            await service.run_forever()
        finally:
            store.close()
            await agent_module._close_components(components)

    asyncio.run(_run())


# ── Memory subcommands ────────────────────────────────────────────────────────


@memory_app.command("ls")
def memory_ls():
    """Show memory export summary."""
    memory = MemoryPalace()
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
    memory = MemoryPalace()
    content = memory.read(chapter, name)
    if content:
        shared.CONSOLE.print(Markdown(content))
    else:
        shared.CONSOLE.print(f"[yellow]No memory at {path}[/yellow]")


@memory_app.command("search")
def memory_search(query: str = typer.Argument(..., help="Search query")):
    """Search across all memory files."""
    memory = MemoryPalace()
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
        mem: MemoryPalace = components["memory"]
        mem.force_tidy()
        try:
            await mem.tidy(components["client"], components["model"])
        finally:
            await agent_module._close_components(components)

    asyncio.run(_run())


@memory_app.command("index")
def memory_index():
    """Show the memory JSONL export."""
    memory = MemoryPalace()
    shared.CONSOLE.print(Markdown(memory.read_index()))


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()
