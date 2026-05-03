from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import sys
from typing import Any, Optional

import typer
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

import agent as agent_module
from agent import shared
from agent.core.output import CliOutputSink, _active_sink
from agent.runtime import RuntimeComponents, RuntimeSessionState, TurnInput, TurnRunner

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
RalphTask = agent_module.RalphTask
RALPH_COMPLETION_PROMISE = agent_module.RALPH_COMPLETION_PROMISE
RALPH_DEFAULT_MAX_ITERATIONS = agent_module.RALPH_DEFAULT_MAX_ITERATIONS
SchedulerDelivery = agent_module.SchedulerDelivery
SchedulerService = agent_module.SchedulerService
SchedulerStore = agent_module.SchedulerStore
SkillCatalog = agent_module.SkillCatalog
StagingBuffer = agent_module.StagingBuffer
TurnEvent = agent_module.TurnEvent
TriggerSpec = agent_module.TriggerSpec
_build_gateway_channels = agent_module._build_gateway_channels
_new_id = agent_module._new_id
_load_ralph_task = agent_module._load_ralph_task
_save_ralph_task = agent_module._save_ralph_task
_with_task_context = agent_module._with_task_context
prepare_user_message_for_skills = agent_module.prepare_user_message_for_skills

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


def _turn_runner_for_components(components: dict):
    turn_runner = components.get("turn_runner")
    if turn_runner is None or isinstance(turn_runner, TurnRunner):
        return TurnRunner(RuntimeComponents(components))
    return turn_runner


def _configure_runtime_logging() -> None:
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    for logger_name in _INTERACTION_LOGGER_NAMES:
        logging.getLogger(logger_name).setLevel(logging.INFO)

async def _ralph_task_loop(
    agent: "BaseAgent",
    task: RalphTask,
    system_prompt: str,
    skill_catalog: "SkillCatalog",
    ctx_mgr: Optional["ContextManager"],
) -> RalphTask:
    """Ralph-mode autonomous task loop.

    Runs up to task.max_iterations iterations. Each iteration gets a fresh
    AgentContext (preventing context rot) but receives the full task state and
    recent progress summary. Completion is determined externally — either by a
    promise token in the output or by a verify_command exit code — not by LLM
    self-assessment.

    Context handling contract:
    - No mark_activity() / staging during iterations → background consolidation
      does not fire mid-task, keeping iterations uninterrupted.
    - After the task ends (any status), all iterations are staged as a single
      summary entry and consolidation is enqueued once. This ensures LTM learns
      from the Ralph session without fragmenting it into mid-task chunks.
    """
    all_summaries: list[str] = []

    def _build_iter_prompt(t: RalphTask) -> str:
        criteria_text = "\n".join(f"- {c}" for c in t.completion_criteria)
        progress_text = ""
        if t.progress:
            recent = t.progress[-3:]  # only last 3 to keep token cost bounded
            progress_text = "\n\n## Recent Progress\n" + "\n".join(
                f"- Iteration {p['iteration']}: {p['summary']}" for p in recent
            )
        return (
            f"## Current Task\n{t.goal}\n\n"
            f"## Acceptance Criteria\n{criteria_text}\n\n"
            f"Once all criteria are satisfied, output at the end of your reply: `{t.completion_promise}`\n"
            f"{progress_text}\n\n"
            f"This is iteration {t.current_iteration} of {t.max_iterations}."
        )

    for i in range(task.max_iterations):
        task.current_iteration = i + 1
        shared.CONSOLE.print(
            f"\n[dim]── Ralph iteration {task.current_iteration}/{task.max_iterations} ──[/dim]"
        )

        # Fresh AgentContext per iteration — prevents context rot across iterations.
        iter_ctx = AgentContext(system_prompt=system_prompt)
        iter_ctx.metadata["skill_catalog"] = skill_catalog

        collected: list[str] = []

        def _stream_cb(chunk: str, _col: list = collected) -> None:
            shared.CONSOLE.print(chunk, end="", markup=False)
            _col.append(chunk)

        shared.CONSOLE.print("[bold blue]Agent[/bold blue]: ", end="")
        result = await agent.send_message(
            iter_ctx, _build_iter_prompt(task), _stream_cb
        )
        shared.CONSOLE.print()

        if result.error:
            shared.CONSOLE.print(f"[red]Error: {result.error}[/red]")

        iter_summary = result.content[:300] if result.content else "(no output)"
        all_summaries.append(f"Iter {task.current_iteration}: {iter_summary}")

        # ── Notify plugins so evolution / correction detection works in Ralph ─
        if agent.plugin_catalog:
            try:
                await agent.plugin_catalog.fire_turn_end(
                    TurnEvent(
                        user_input=_build_iter_prompt(task),
                        agent_response=result.content or "",
                        tool_calls=result.tool_calls_made,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        turn_index=task.current_iteration,
                    )
                )
            except Exception:
                pass  # plugin errors must not abort the task loop

        # ── External completion check 1: promise token ────────────────────────
        if task.completion_promise in result.content:
            task.status = "complete"
            task.progress.append(
                {
                    "iteration": task.current_iteration,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "tool_calls": result.tool_calls_made,
                    "summary": iter_summary,
                    "completed_by": "promise",
                }
            )
            _save_ralph_task(task)
            break

        # ── External completion check 2: verify command ───────────────────────
        if task.verify_command:
            v_out: bytes = b""
            v_err: bytes = b""
            try:
                verify_proc = await asyncio.create_subprocess_shell(
                    task.verify_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                v_out, v_err = await asyncio.wait_for(
                    verify_proc.communicate(), timeout=60
                )
                v_exit = verify_proc.returncode
            except asyncio.TimeoutError:
                v_exit = -1
                shared.CONSOLE.print("[yellow]Verify command timed out (60s)[/yellow]")
            except Exception as ve:
                v_exit = -1
                shared.CONSOLE.print(f"[yellow]Verify command error: {ve}[/yellow]")

            if v_exit == 0:
                shared.CONSOLE.print("[green]Verify passed (exit 0)[/green]")
                task.status = "complete"
                task.progress.append(
                    {
                        "iteration": task.current_iteration,
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "tool_calls": result.tool_calls_made,
                        "summary": iter_summary,
                        "completed_by": "verify_command",
                    }
                )
                _save_ralph_task(task)
                break
            else:
                # Capture verification output and append to iter_summary so it
                # flows into task.progress and becomes visible in the next
                # iteration's prompt via _build_iter_prompt(recent[-3:]).
                # Without this, the agent knows verification failed but not why,
                # making subsequent iterations effectively blind retries.
                verify_output = (v_err or v_out).decode("utf-8", errors="replace")
                verify_snippet = verify_output[-600:].strip()
                if verify_snippet:
                    iter_summary += (
                        f"\n\nverify_failed (exit {v_exit}):\n{verify_snippet}"
                    )
                    shared.CONSOLE.print(
                        f"[yellow]Verify failed (exit {v_exit}), continuing[/yellow]\n"
                        f"[dim]{verify_snippet[:200]}[/dim]"
                    )
                else:
                    shared.CONSOLE.print(
                        f"[yellow]Verify failed (exit {v_exit}), continuing[/yellow]"
                    )

        task.progress.append(
            {
                "iteration": task.current_iteration,
                "ts": datetime.now(timezone.utc).isoformat(),
                "tool_calls": result.tool_calls_made,
                "summary": iter_summary,
            }
        )
        _save_ralph_task(task)

    if task.status == "running":
        task.status = "max_iterations_reached"
        _save_ralph_task(task)

    # ── Post-task: stage the full run and enqueue one consolidation job ───────
    # Done once after the loop ends (not per-iteration) to avoid fragmenting the
    # task narrative in LTM and to prevent background consolidation from firing
    # mid-task (which would use a separate API call on incomplete context).
    if ctx_mgr and all_summaries:
        goal_line = f"[Ralph/{task.id}] goal: {task.goal} | status: {task.status} | iters: {task.current_iteration}/{task.max_iterations}"
        ctx_mgr.staging.append("user", goal_line)
        ctx_mgr.staging.append("assistant", "\n".join(all_summaries[-5:]))
        ctx_mgr.mark_activity()
        if ctx_mgr.should_enqueue_consolidation():
            ctx_mgr.enqueue_consolidation("ralph_task_end")

    return task

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
        skill_catalog: SkillCatalog = components["skill_catalog"]
        ctx = AgentContext(system_prompt=components["system_prompt"])
        ctx.metadata["skill_catalog"] = skill_catalog
        prompt = str(task.payload.get("prompt", "")).strip()
        if not prompt:
            raise RuntimeError(f"Scheduled task '{task.name}' has no prompt")
        result = await _turn_runner_for_components(components).run(
            TurnInput.from_text(prompt, channel_name="scheduler"),
            ctx,
        )
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


async def _interactive_loop(components: dict, cfg: dict):
    """Main interactive chat loop."""
    agent: BaseAgent = components["agent"]
    memory: MemoryPalace = components["memory"]
    evolution: Optional[EvolutionEngine] = components.get("evolution")
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
    user_tool_catalog: UserToolCatalog = components["user_tool_catalog"]

    ctx = AgentContext(system_prompt=system_prompt)
    # Expose ctx in components so plugin slash-command handlers can update it.
    components["ctx"] = ctx
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

            # Handle slash commands
            if user_input.startswith("/"):
                raw_cmd = user_input[1:].strip()
                cmd = raw_cmd.lower()
                if cmd in ("quit", "exit", "q"):
                    break
                elif cmd in ("help", "?"):
                    _help_table = Table(title="Commands", show_header=False, box=None)
                    _help_table.add_column("Command", style="cyan", no_wrap=True)
                    _help_table.add_column("Description")
                    for _hcmd, _hdesc in [
                        ("/help", "Show this help"),
                        ("/memory", "Show memory export summary"),
                        ("/context", "Show LTM context manager stats"),
                        ("/sessions", "List recent session history"),
                        ("/session <id>", "View session details"),
                        ("/tools", "List all available tools"),
                        ("/skills", "List available skills"),
                        ("/plugins", "List loaded plugins"),
                        ("/model [name]", "Show or switch active model (session only)"),
                        ("/ralph <goal>", "Run Ralph autonomous task loop"),
                        ("  --max N", "  Max iterations (default 10)"),
                        ("  --verify <cmd>", "  Shell command to verify completion"),
                        ("/ralph list", "List all Ralph autonomous tasks"),
                        ("/ralph resume <id>", "Resume a paused Ralph task"),
                        ("/evolve", "Trigger system-prompt self-evolution"),
                        ("/generate-tool <desc>", "Generate a new user tool"),
                        ("/quit", "Exit the agent"),
                    ]:
                        _help_table.add_row(_hcmd, _hdesc)
                    shared.CONSOLE.print(_help_table)
                    continue
                elif cmd == "memory":
                    lines = [
                        line
                        for line in memory.read_index().splitlines()
                        if line.strip()
                    ]
                    table = Table(title="Memory Export")
                    table.add_column("Metric")
                    table.add_column("Value")
                    table.add_row("Projection", "memory/memory.jsonl")
                    table.add_row("Entries", str(len(lines)))
                    shared.CONSOLE.print(table)
                    continue
                elif cmd == "context":
                    if ctx_mgr:
                        stats = ctx_mgr.stats()
                        table = Table(title="Context Manager (LTM)")
                        table.add_column("Metric")
                        table.add_column("Value")
                        table.add_row(
                            "Dynamic Categories",
                            f"{stats['dynamic_categories']}/{stats['max_categories']}",
                        )
                        table.add_row(
                            "Total Categories", str(stats["total_categories"])
                        )
                        table.add_row("Total Entries", str(stats["total_entries"]))
                        table.add_row(
                            "Category Names",
                            ", ".join(stats["category_names"]) or "—",
                        )
                        table.add_row(
                            "Staged Turns",
                            str(stats["staged_turns"]),
                        )
                        table.add_row(
                            "Needs Consolidation",
                            "yes" if stats["needs_consolidation"] else "no",
                        )
                        table.add_row(
                            "Idle",
                            f"{stats['idle_elapsed_s']}s / {stats['idle_threshold_s']}s",
                        )
                        shared.CONSOLE.print(table)
                    else:
                        shared.CONSOLE.print("[yellow]Context manager not available.[/yellow]")
                    continue
                elif cmd == "sessions" or cmd == "history":
                    sessions_data: list[dict] = []
                    if shared.SESSIONS_FILE.exists():
                        with open(shared.SESSIONS_FILE, encoding="utf-8") as f:
                            for line in f:
                                try:
                                    sessions_data.append(json.loads(line.strip()))
                                except Exception:
                                    pass
                    if not sessions_data:
                        shared.CONSOLE.print("[yellow]No session history found.[/yellow]")
                    else:
                        table = Table(title="Recent Sessions")
                        table.add_column("Session ID", style="cyan", no_wrap=True)
                        table.add_column("Timestamp")
                        table.add_column("Score")
                        table.add_column("Summary")
                        for s in reversed(sessions_data[-20:]):
                            sid = str(s.get("session_id", "?"))[:12]
                            ts = str(s.get("timestamp", "?"))[:19]
                            score_val = s.get("objective_score") or s.get("score")
                            score = f"{float(score_val):.1f}" if score_val is not None else "?"
                            summary = str(s.get("task_summary", ""))[:60]
                            table.add_row(sid, ts, score, summary or "\u2014")
                        shared.CONSOLE.print(table)
                    continue
                elif cmd.startswith("session "):
                    parts = raw_cmd.split(None, 1)
                    if len(parts) < 2 or not parts[1].strip():
                        shared.CONSOLE.print(
                            "[yellow]Usage: /session <session_id_prefix>[/yellow]"
                        )
                    else:
                        prefix = parts[1].strip()
                        found = None
                        if shared.SESSIONS_FILE.exists():
                            with open(shared.SESSIONS_FILE, encoding="utf-8") as f:
                                for line in f:
                                    try:
                                        s = json.loads(line.strip())
                                        if str(s.get("session_id", "")).startswith(prefix):
                                            found = s
                                            break
                                    except Exception:
                                        pass
                        if found is None and ctx_mgr and hasattr(ctx_mgr, "store"):
                            turns_list = ctx_mgr.store.get_turns_for_session(prefix)
                            if turns_list:
                                shared.CONSOLE.print(
                                    f"[cyan]Session {prefix} turns:[/cyan]"
                                )
                                for t in turns_list[-10:]:
                                    shared.CONSOLE.print(
                                        f"[dim]{t.get('role', '?')}: "
                                        f"{str(t.get('content', ''))[:120]}[/dim]"
                                    )
                            else:
                                shared.CONSOLE.print(
                                    f"[yellow]Session not found: {prefix}[/yellow]"
                                )
                        elif found is not None:
                            score_val = found.get("objective_score") or found.get("score")
                            score = f"{float(score_val):.1f}" if score_val is not None else "?"
                            tools = found.get("tools_used", [])
                            details = (
                                f"[bold]Session:[/bold] {found.get('session_id', '?')}\n"
                                f"[bold]Timestamp:[/bold] {found.get('timestamp', '?')}\n"
                                f"[bold]Score:[/bold] {score}\n"
                                f"[bold]Summary:[/bold] {found.get('task_summary', '?')}\n"
                                f"[bold]Tools Used:[/bold] {', '.join(tools) if tools else 'none'}\n"
                                f"[bold]Corrections:[/bold] {found.get('correction_count', 0)}"
                            )
                            shared.CONSOLE.print(
                                Panel(details, title="Session Details")
                            )
                        else:
                            shared.CONSOLE.print(
                                f"[yellow]Session not found: {prefix}[/yellow]"
                            )
                    continue
                # ── Plugin-contributed slash commands (checked before built-ins) ──
                plugin_cmds = plugin_catalog.get_slash_commands()
                matched_plugin_key: Optional[str] = None
                for _key in plugin_cmds:
                    if cmd == _key or cmd.startswith(_key + " "):
                        matched_plugin_key = _key
                        break
                if matched_plugin_key is not None:
                    await plugin_cmds[matched_plugin_key](raw_cmd, components)
                    continue
                elif cmd == "tools":
                    _tool_list = components["registry"].list_tools()
                    _tools_table = Table(title="Available Tools")
                    _tools_table.add_column("Tool", style="cyan")
                    for _t in _tool_list:
                        _tools_table.add_row(_t)
                    shared.CONSOLE.print(_tools_table)
                    continue
                elif cmd == "skills":
                    skills = skill_catalog.list_skills()
                    if not skills:
                        shared.CONSOLE.print("[yellow]No skills found.[/yellow]")
                    else:
                        table = Table(title="Available Skills")
                        table.add_column("ID")
                        table.add_column("Source")
                        table.add_column("Description")
                        for bundle in skills:
                            table.add_row(
                                bundle.id,
                                bundle.source,
                                bundle.description or "—",
                            )
                        shared.CONSOLE.print(table)
                    continue
                elif cmd == "plugins":
                    plugins = plugin_catalog.list_plugins()
                    if not plugins:
                        shared.CONSOLE.print("[yellow]No plugins loaded.[/yellow]")
                    else:
                        table = Table(title="Loaded Plugins")
                        table.add_column("Name")
                        table.add_column("Version")
                        table.add_column("Source")
                        table.add_column("Description")
                        for pm in plugins:
                            table.add_row(
                                pm.name,
                                pm.version or "—",
                                pm.source,
                                pm.description or "—",
                            )
                        shared.CONSOLE.print(table)
                        shared.CONSOLE.print(
                            "[dim]Tip: set plugins.<name>.enabled = false "
                            "in config.json to disable a plugin[/dim]"
                        )
                    continue
                elif cmd.startswith("mode "):
                    # Kept as a hidden override for debugging; not advertised
                    shared.CONSOLE.print(
                        "[dim](manual mode override removed — routing is automatic)[/dim]"
                    )
                    continue
                elif cmd == "model" or cmd.startswith("model "):
                    parts = cmd.split(None, 1)
                    provider_cfg = cfg.get("providers", {}).get(
                        cfg.get("active_provider", ""), {}
                    )
                    available = provider_cfg.get(
                        "models", [provider_cfg.get("default_model", agent.model)]
                    )
                    if len(parts) == 1:
                        # List available models
                        table = Table(title="Models")
                        table.add_column("Model")
                        table.add_column("Active")
                        for m in available:
                            mark = (
                                "[bold green]✓[/bold green]" if m == agent.model else ""
                            )
                            table.add_row(m, mark)
                        shared.CONSOLE.print(table)
                    else:
                        new_model = parts[1].strip()
                        agent.set_model(new_model)
                        shared.CONSOLE.print(
                            f"[green]Switched to model: {new_model}[/green] "
                            "[dim](session only — not persisted)[/dim]"
                        )
                    continue
                elif cmd == "ralph" or cmd.startswith("ralph "):
                    # /ralph <goal> [--max N] [--verify <shell_cmd>]
                    # /ralph list
                    # /ralph resume <task_id>
                    sub_parts = raw_cmd.split(None, 1)
                    sub = sub_parts[1].strip() if len(sub_parts) > 1 else ""
                    sub_cmd = sub.split(None, 1)[0].lower() if sub else ""

                    if sub_cmd == "list":
                        tasks_dir = shared.AGENT_HOME / "tasks"
                        task_files = sorted(tasks_dir.glob("*.json")) if tasks_dir.is_dir() else []
                        if not task_files:
                            shared.CONSOLE.print("[yellow]No Ralph tasks found.[/yellow]")
                        else:
                            table = Table(title="Ralph Tasks")
                            table.add_column("ID", style="cyan")
                            table.add_column("Status")
                            table.add_column("Goal")
                            table.add_column("Iterations")
                            for tf in task_files:
                                try:
                                    t = json.loads(tf.read_text())
                                    sid = tf.stem[:12]
                                    status = str(t.get("status", "?"))
                                    goal = str(t.get("goal", ""))[:60]
                                    iters = f"{t.get('current_iteration',0)}/{t.get('max_iterations',0)}"
                                    status_color = "green" if status == "complete" else "yellow"
                                    table.add_row(
                                        sid,
                                        f"[{status_color}]{status}[/{status_color}]",
                                        goal,
                                        iters,
                                    )
                                except Exception:
                                    table.add_row(tf.stem[:12], "corrupt", "\u2014", "\u2014")
                            shared.CONSOLE.print(table)
                        continue

                    if sub_cmd == "resume":
                        resume_parts = sub.split(None, 1)
                        if len(resume_parts) < 2:
                            shared.CONSOLE.print("[yellow]Usage: /ralph resume <task_id>[/yellow]")
                        else:
                            task_id = resume_parts[1].strip()
                            task = _load_ralph_task(task_id)
                            if task is None:
                                shared.CONSOLE.print(
                                    f"[yellow]Task not found: {task_id}[/yellow]"
                                )
                            elif task.status == "complete":
                                shared.CONSOLE.print(
                                    f"[yellow]Task already complete: {task.id[:12]}[/yellow]"
                                )
                            else:
                                task.status = "running"
                                shared.CONSOLE.print(
                                    f"[cyan]Resuming Ralph task {task.id[:12]}: {task.goal}[/cyan]"
                                )
                                _ralph_sink = CliOutputSink(shared.CONSOLE)
                                _ralph_token = _active_sink.set(_ralph_sink)
                                try:
                                    task = await _ralph_task_loop(
                                        agent, task, system_prompt,
                                        skill_catalog, ctx_mgr,
                                    )
                                finally:
                                    _active_sink.reset(_ralph_token)
                                status_color = "green" if task.status == "complete" else "yellow"
                                shared.CONSOLE.print(
                                    f"[{status_color}]Ralph complete | status: {task.status} | "
                                    f"iterations: {task.current_iteration}/{task.max_iterations}[/{status_color}]"
                                )
                        continue

                    # /ralph <goal> [--max N] [--verify <shell_cmd>]
                    if not sub:
                        shared.CONSOLE.print(
                            "[yellow]Usage: /ralph <goal> [--max N] [--verify <cmd>][/yellow]\n"
                            "[dim]Subcommands: list | resume <id>[/dim]\n"
                            "[dim]Example: /ralph 'make all tests pass' --max 10 --verify 'pytest tests/'[/dim]"
                        )
                        continue

                    goal_str = parts[1].strip()
                    max_iters = RALPH_DEFAULT_MAX_ITERATIONS
                    verify_cmd: Optional[str] = None

                    # Parse --max N
                    max_match = re.search(r"--max\s+(\d+)", goal_str)
                    if max_match:
                        max_iters = int(max_match.group(1))
                        goal_str = (
                            goal_str[: max_match.start()].rstrip()
                            + goal_str[max_match.end() :]
                        )

                    # Parse --verify <cmd> (everything after --verify to end of string)
                    verify_match = re.search(r"--verify\s+(.+)$", goal_str)
                    if verify_match:
                        verify_cmd = verify_match.group(1).strip().strip("'\"")
                        goal_str = goal_str[: verify_match.start()].rstrip()

                    goal_str = goal_str.strip().strip("'\"")
                    if not goal_str:
                        shared.CONSOLE.print("[yellow]Goal cannot be empty.[/yellow]")
                        continue

                    task = RalphTask(
                        id=_new_id(),
                        goal=goal_str,
                        completion_criteria=[
                            f"Goal achieved: {goal_str}",
                            "Output contains the completion promise token",
                        ],
                        verify_command=verify_cmd,
                        completion_promise=RALPH_COMPLETION_PROMISE,
                        max_iterations=max_iters,
                    )
                    _save_ralph_task(task)
                    shared.CONSOLE.print(
                        f"[cyan]Ralph mode started | id: {task.id} | max_iters: {max_iters}"
                        + (f" | verify: {verify_cmd}" if verify_cmd else "")
                        + "[/cyan]"
                    )
                    _ralph_sink = CliOutputSink(shared.CONSOLE)
                    _ralph_token = _active_sink.set(_ralph_sink)
                    try:
                        task = await _ralph_task_loop(
                            agent,
                            task,
                            system_prompt,
                            skill_catalog,
                            ctx_mgr,
                        )
                    finally:
                        _active_sink.reset(_ralph_token)
                    status_color = "green" if task.status == "complete" else "yellow"
                    shared.CONSOLE.print(
                        f"[{status_color}]Ralph complete | status: {task.status} | "
                        f"iterations: {task.current_iteration}/{task.max_iterations}[/{status_color}]"
                    )
                    continue
                else:
                    normalized_input, required_skills = prepare_user_message_for_skills(
                        user_input, skill_catalog
                    )
                    if required_skills:
                        user_input = normalized_input
                        ctx.metadata["required_skills"] = required_skills
                    else:
                        shared.CONSOLE.print(f"[yellow]Unknown command: {user_input}[/yellow]")
                        continue
            else:
                normalized_input, required_skills = prepare_user_message_for_skills(
                    user_input, skill_catalog
                )
                if required_skills:
                    user_input = normalized_input
                    ctx.metadata["required_skills"] = required_skills
                else:
                    ctx.metadata.pop("required_skills", None)

            # Mark activity so idle timer resets and dirty flag is set
            if ctx_mgr:
                ctx_mgr.mark_activity()

            # Record the first non-command user message as the task context so it
            # can be re-injected into the system prompt after compaction occurs.
            state.ensure_task_context(user_input)

            # Create a fresh per-turn sink and register it in the ContextVar so
            # tool helpers (_run_tool_uses) can find it without param threading.
            _turn_sink = CliOutputSink(shared.CONSOLE)
            _sink_token = _active_sink.set(_turn_sink)

            try:
                shared.CONSOLE.print("[bold blue]Agent[/bold blue]: ", end="")
                ctx.metadata["skill_catalog"] = skill_catalog

                # Hot-reload: recompose system prompt when skill catalog was mutated
                if skill_catalog.consume_dirty():
                    refreshed = agent_module._compose_system_prompt(
                        components["base_system_prompt"],
                        components["registry"],
                        components.get("workspace_root"),
                        components.get("output_dir"),
                        skill_catalog=skill_catalog,
                        plugin_catalog=plugin_catalog,
                    )
                    components["system_prompt"] = refreshed
                    ctx.system_prompt = _with_task_context(
                        refreshed, state.task_context
                    )

                turn_runner = _turn_runner_for_components(components)
                turn_input = TurnInput.from_text(user_input, channel_name="cli")
                result = await turn_runner.run(
                    turn_input,
                    ctx,
                    stream_callback=_turn_sink.sync_stream_cb,
                )
                # on_turn_complete: renders markdown if no streaming happened,
                # always prints trailing newline, and clears _streamed buffer.
                tool_calls = list(result.tool_calls)
                _turn_sink.on_turn_complete(
                    result.text or "", tool_calls
                )
                if result.error:
                    shared.CONSOLE.print(f"[red]Error: {result.error}[/red]")

                await turn_runner.complete_turn(turn_input, state, result)

            except Exception as e:
                shared.CONSOLE.print(f"\n[red]Error: {e}[/red]")
            finally:
                # Always reset the sink ContextVar after each turn so stale
                # references cannot bleed into the next turn.
                _active_sink.reset(_sink_token)

    finally:
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
                ctx_mgr.enqueue_consolidation("session_end")
                while ctx_mgr.pending_jobs():
                    await ctx_mgr.process_one_job(
                        components["client"],
                        components["model"],
                        api_format=agent.api_format,
                    )
                ctx.messages = ctx_mgr.compact_messages(ctx.messages)
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
        skill_catalog: SkillCatalog = components["skill_catalog"]
        normalized_question, required_skills = prepare_user_message_for_skills(
            question, skill_catalog
        )
        if required_skills:
            ctx.metadata["required_skills"] = required_skills
        ctx.metadata["skill_catalog"] = skill_catalog
        shared.CONSOLE.print("[bold blue]Agent[/bold blue]: ", end="")
        try:
            result = await _turn_runner_for_components(components).run(
                TurnInput.from_text(normalized_question, channel_name="cli"),
                ctx,
                stream_callback=lambda chunk: shared.CONSOLE.print(
                    chunk, end="", markup=False
                ),
            )
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
    lease_seconds: Optional[int] = typer.Option(None, "--lease-seconds", min=1),
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
