from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Mapping

from agent import shared
from agent.pathing import canonicalize_user_path, path_contains

from .models import CommandContext, CommandDescriptor, CommandRequest, CommandResult
from .router import CommandRouter


def _error(message: str) -> CommandResult:
    return CommandResult(response_text=message, level="error")


def _warning(message: str) -> CommandResult:
    return CommandResult(response_text=message, level="warning")


def _component(context: CommandContext, name: str) -> Any:
    return context.components.get(name)


def _field(value: Any, name: str, default: Any = "") -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _require_no_args(request: CommandRequest, usage: str) -> CommandResult | None:
    if request.args:
        return _error(f"Usage: {usage}")
    return None


def _context_manager(context: CommandContext) -> Any:
    manager = getattr(context.session_state, "context_manager", None)
    return manager if manager is not None else _component(context, "context_manager")


def _sessions_file(context: CommandContext) -> Path:
    configured = context.components.get("sessions_file")
    if configured is None:
        configured = context.config.get("sessions_file")
    return Path(configured) if configured is not None else shared.SESSIONS_FILE


def _load_sessions(context: CommandContext) -> list[dict[str, Any]]:
    path = _sessions_file(context)
    if not path.is_file():
        return []
    sessions: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(item, dict):
                    sessions.append(item)
    except OSError:
        return []
    return sessions


def _score(value: Mapping[str, Any]) -> str:
    raw = value.get("objective_score")
    if raw is None:
        raw = value.get("score")
    if raw is None:
        return "?"
    try:
        return f"{float(raw):.1f}"
    except (TypeError, ValueError):
        return "?"


async def _memory_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    invalid = _require_no_args(request, "/memory")
    if invalid is not None:
        return invalid
    memory = _component(context, "memory")
    read_index = getattr(memory, "read_index", None)
    if not callable(read_index):
        return _error("Memory is not available.")
    try:
        entries = sum(1 for line in str(read_index()).splitlines() if line.strip())
    except Exception:
        return _error("Memory is not available.")
    return CommandResult(
        response_text=(
            "## Memory Export\n\n"
            "- Projection: `memory/memory.jsonl`\n"
            f"- Entries: {entries}"
        )
    )


async def _context_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    invalid = _require_no_args(request, "/context")
    if invalid is not None:
        return invalid
    manager = _context_manager(context)
    stats_method = getattr(manager, "stats", None)
    if not callable(stats_method):
        return _error("Context manager is not available.")
    try:
        stats = stats_method()
        categories = stats.get("category_names", ())
        category_names = ", ".join(str(item) for item in categories) or "none"
        needs_consolidation = "yes" if stats.get("needs_consolidation") else "no"
        text = (
            "## Context Manager (LTM)\n\n"
            f"- Dynamic Categories: {stats.get('dynamic_categories', 0)}/"
            f"{stats.get('max_categories', 0)}\n"
            f"- Total Categories: {stats.get('total_categories', 0)}\n"
            f"- Total Entries: {stats.get('total_entries', 0)}\n"
            f"- Category Names: {category_names}\n"
            f"- Staged Turns: {stats.get('staged_turns', 0)}\n"
            f"- Needs Consolidation: {needs_consolidation}\n"
            f"- Idle: {stats.get('idle_elapsed_s', 0)}s / "
            f"{stats.get('idle_threshold_s', 0)}s"
        )
    except Exception:
        return _error("Context manager is not available.")
    return CommandResult(response_text=text)


async def _sessions_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    invalid = _require_no_args(request, "/sessions")
    if invalid is not None:
        return invalid
    sessions = _load_sessions(context)
    if not sessions:
        return _warning("No session history found.")
    lines = ["## Recent Sessions", "", "Session ID | Timestamp | Score | Summary"]
    lines.append("--- | --- | --- | ---")
    for session in reversed(sessions[-20:]):
        session_id = str(session.get("session_id", "?"))[:12]
        timestamp = str(session.get("timestamp", "?"))[:19]
        summary = str(session.get("task_summary", ""))[:60] or "-"
        lines.append(f"{session_id} | {timestamp} | {_score(session)} | {summary}")
    return CommandResult(response_text="\n".join(lines))


def _session_turns(context: CommandContext, prefix: str) -> list[Any]:
    manager = _context_manager(context)
    store = getattr(manager, "store", None)
    get_turns = getattr(store, "get_turns_for_session", None)
    if not callable(get_turns):
        return []
    try:
        turns = get_turns(prefix)
    except Exception:
        return []
    return list(turns or ())


async def _session_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    prefix = request.args.strip()
    if not prefix:
        return _error("Usage: /session <session_id_prefix>")
    found = next(
        (
            session
            for session in _load_sessions(context)
            if str(session.get("session_id", "")).startswith(prefix)
        ),
        None,
    )
    if found is not None:
        tools = found.get("tools_used", ())
        if not isinstance(tools, (list, tuple)):
            tools = ()
        lines = [
            "## Session Details",
            "",
            f"- Session: {found.get('session_id', '?')}",
            f"- Timestamp: {found.get('timestamp', '?')}",
            f"- Score: {_score(found)}",
            f"- Summary: {found.get('task_summary', '?')}",
            f"- Tools Used: {', '.join(str(tool) for tool in tools) or 'none'}",
            f"- Corrections: {found.get('correction_count', 0)}",
        ]
        return CommandResult(response_text="\n".join(lines))

    turns = _session_turns(context, prefix)
    if turns:
        lines = [f"## Session {prefix} Turns", ""]
        for turn in turns[-10:]:
            role = str(_field(turn, "role", "?"))
            content = str(_field(turn, "content", ""))[:120]
            lines.append(f"- **{role}:** {content}")
        return CommandResult(response_text="\n".join(lines))
    return _error(f"Session not found: {prefix}")


def _message_content_text(content: Any) -> str:
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    media_found = False
    for block in content:
        if isinstance(block, Mapping) and block.get("type") == "text":
            text = str(block.get("text", "")).strip()
            if text:
                parts.append(text)
        else:
            media_found = True
    if media_found:
        parts.append("[media content]")
    return "\n".join(parts) or "[media content]"


async def _export_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    invalid = _require_no_args(request, "/export")
    if invalid is not None:
        return invalid
    session_context = getattr(context.session_state, "ctx", None)
    messages = getattr(session_context, "messages", None)
    if not messages:
        return _warning("No messages to export.")
    output_dir = _component(context, "output_dir") or shared.DEFAULT_OUTPUT_DIR
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = Path(output_dir).expanduser().resolve(strict=False) / f"session_{timestamp}.md"
    lines = [f"# Session Export - {timestamp}", ""]
    try:
        for message in messages:
            role = str(_field(message, "role", "?")).upper()
            content = _message_content_text(_field(message, "content", ""))
            lines.extend((f"## {role}", "", content, ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        return _error("Unable to export session.")
    return CommandResult(
        response_text=f"Exported {len(messages)} messages to {path}",
        attachments=(path,),
    )


async def _tools_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    invalid = _require_no_args(request, "/tools")
    if invalid is not None:
        return invalid
    list_tools = getattr(_component(context, "registry"), "list_tools", None)
    if not callable(list_tools):
        return _error("Tool registry is not available.")
    try:
        tools = [str(tool) for tool in list_tools()]
    except Exception:
        return _error("Tool registry is not available.")
    if not tools:
        return _warning("No tools found.")
    return CommandResult(response_text="## Available Tools\n\n" + "\n".join(f"- {tool}" for tool in tools))


async def _skills_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    invalid = _require_no_args(request, "/skills")
    if invalid is not None:
        return invalid
    list_skills = getattr(_component(context, "skill_catalog"), "list_skills", None)
    if not callable(list_skills):
        return _error("Skill catalog is not available.")
    try:
        skills = list(list_skills())
    except Exception:
        return _error("Skill catalog is not available.")
    if not skills:
        return _warning("No skills found.")
    lines = ["## Available Skills", "", "ID | Source | Description", "--- | --- | ---"]
    for skill in skills:
        lines.append(
            f"{_field(skill, 'id', '?')} | {_field(skill, 'source', '-')} | "
            f"{_field(skill, 'description', '') or '-'}"
        )
    return CommandResult(response_text="\n".join(lines))


async def _plugins_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    invalid = _require_no_args(request, "/plugins")
    if invalid is not None:
        return invalid
    list_plugins = getattr(_component(context, "plugin_catalog"), "list_plugins", None)
    if not callable(list_plugins):
        return _error("Plugin catalog is not available.")
    try:
        plugins = list(list_plugins())
    except Exception:
        return _error("Plugin catalog is not available.")
    if not plugins:
        return _warning("No plugins loaded.")
    lines = [
        "## Loaded Plugins",
        "",
        "Name | Version | Source | Description",
        "--- | --- | --- | ---",
    ]
    for plugin in plugins:
        lines.append(
            f"{_field(plugin, 'name', '?')} | {_field(plugin, 'version', '') or '-'} | "
            f"{_field(plugin, 'source', '-')} | {_field(plugin, 'description', '') or '-'}"
        )
    return CommandResult(response_text="\n".join(lines))


def _configured_models(context: CommandContext) -> list[str]:
    providers = context.config.get("providers", {})
    if not isinstance(providers, Mapping):
        return []
    active_provider = context.config.get("active_provider", "")
    provider = providers.get(active_provider, {})
    if not isinstance(provider, Mapping):
        return []
    configured = provider.get("models", ())
    if isinstance(configured, str):
        configured = [configured]
    models = [str(model) for model in configured if str(model)]
    default = provider.get("default_model")
    if not models and default:
        models.append(str(default))
    return list(dict.fromkeys(models))


def _active_model(context: CommandContext, models: list[str]) -> str:
    override = getattr(context.session_state, "model_override", None)
    agent = _component(context, "agent")
    return str(override or getattr(agent, "model", None) or (models[0] if models else ""))


async def _model_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    models = _configured_models(context)
    active = _active_model(context, models)
    if not request.args:
        if not models:
            return _warning("No models configured for the active provider.")
        lines = ["## Models", ""]
        for model in models:
            lines.append(f"- {model}{' (active)' if model == active else ''}")
        return CommandResult(response_text="\n".join(lines))
    requested = request.args.strip()
    if requested not in models:
        available = ", ".join(models) or "none"
        return _error(f"Unknown model: {requested}. Available models: {available}")
    try:
        context.session_state.model_override = requested
    except Exception:
        return _error("Session model selection is not available.")
    return CommandResult(response_text=f"Switched to model: {requested} (session only)")


async def _quit_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    invalid = _require_no_args(request, f"/{request.name}")
    if invalid is not None:
        return invalid
    return CommandResult(action="exit_cli")


async def _send_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    raw_path = request.args.strip()
    if not raw_path:
        return _error("Usage: /send <path>")
    output_dir = _component(context, "output_dir")
    if output_dir is None:
        return _error("Output directory is not available.")
    try:
        output_root = Path(output_dir).expanduser().resolve(strict=False)
        expanded = os.path.expandvars(raw_path)
        path = canonicalize_user_path(expanded, base_dir=output_root)
    except (OSError, RuntimeError, ValueError):
        return _error("Invalid file path.")
    if not path_contains(output_root, path):
        return _error("File is outside the output directory.")
    if not path.is_file():
        return _error(f"File not found: {raw_path}")
    return CommandResult(response_text=f"Sending file: {path}", attachments=(path,))


async def _coordinator_owned_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    return _error(f"Command /{request.name} must be handled by the command coordinator.")


def _builtin_descriptors(router: CommandRouter) -> tuple[CommandDescriptor, ...]:
    async def help_handler(
        request: CommandRequest, context: CommandContext
    ) -> CommandResult:
        invalid = _require_no_args(request, "/help")
        if invalid is not None:
            return invalid
        return CommandResult(response_text=router.help_text(request.channel_name))

    return (
        CommandDescriptor(
            "help",
            help_handler,
            usage="/help",
            description="Show commands available in this channel",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "memory",
            _memory_handler,
            usage="/memory",
            description="Show memory export summary",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "context",
            _context_handler,
            usage="/context",
            description="Show long-term context statistics",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "sessions",
            _sessions_handler,
            aliases=("history",),
            usage="/sessions",
            description="List recent session history",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "session",
            _session_handler,
            usage="/session <id>",
            description="View session details by ID prefix",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "export",
            _export_handler,
            usage="/export",
            description="Export the current session to Markdown",
            concurrency="idle_only",
        ),
        CommandDescriptor(
            "tools",
            _tools_handler,
            usage="/tools",
            description="List available tools",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "skills",
            _skills_handler,
            usage="/skills",
            description="List available skills",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "plugins",
            _plugins_handler,
            usage="/plugins",
            description="List loaded plugins",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "model",
            _model_handler,
            usage="/model [name]",
            description="Show or switch the session model",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "quit",
            _quit_handler,
            aliases=("exit", "q"),
            usage="/quit",
            description="Exit the CLI",
            scopes=frozenset({"cli"}),
            concurrency="anytime",
        ),
        CommandDescriptor(
            "send",
            _send_handler,
            usage="/send <path>",
            description="Send a file from the output directory",
            scopes=frozenset({"feishu"}),
            concurrency="anytime",
        ),
        CommandDescriptor(
            "cancel",
            _coordinator_owned_handler,
            usage="/cancel [graceful|new task]",
            description="Cancel the current task (Ctrl+C interrupts a blocking CLI operation)",
            concurrency="interrupt",
        ),
        CommandDescriptor(
            "now",
            _coordinator_owned_handler,
            usage="/now <message>",
            description="Apply an urgent interjection or start it next",
            concurrency="interrupt",
        ),
    )


def register_builtin_commands(router: CommandRouter) -> CommandRouter:
    """Register transport-neutral built-ins on one router instance."""

    for descriptor in _builtin_descriptors(router):
        router.register_core(descriptor)
    return router


__all__ = ["register_builtin_commands"]
