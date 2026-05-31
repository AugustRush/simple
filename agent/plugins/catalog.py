from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Optional

from agent import shared
from agent.core.output import _active_event_collector

CONSOLE = shared.CONSOLE
PLUGINS_DIR = shared.PLUGINS_DIR
USER_PLUGINS_DIR = shared.USER_PLUGINS_DIR


def _safe_float(value: Any, default: float) -> float:
    """Parse *value* as float, returning *default* on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _emit_plugin_event(name: str, plugin_name: str = "", **fields: Any) -> None:
    collector = _active_event_collector.get()
    if collector is not None:
        collector.emit(name, plugin_name=plugin_name, **fields)

class AgentPlugin:
    """Protocol that plugin objects may implement (duck-typed).

    All methods are optional — implement only the hooks you need.
    ``PluginCatalog`` inspects each plugin with ``hasattr`` at dispatch time.

    Attributes:
        name:    Unique identifier for the plugin (used in log messages).
        version: Semver string (informational only).

    Lifecycle hooks:
        on_session_start(components: dict) -> None
            Synchronous.  Called once after all core components are built.
            Use it to capture references to client, model, memory, etc.

        on_prompt_submit(text: str, metadata: dict) -> Optional[HookResult]
            Async-compatible.  Fired when the user submits a message, BEFORE the
            agent sees it.  Return ``HookResult(action="block")`` to prevent the
            message from being processed, or inject ``context`` to prepend
            guidance to the prompt.

        on_turn_end(event: TurnEvent) -> Optional[HookResult]
            Async-compatible.  Fired after every assistant turn.
            Return ``HookResult(action="continue")`` to automatically run another
            turn with ``message`` as the next user prompt (agent self-feedback loop).

        on_session_end(event: SessionEvent) -> None
            Async-compatible.  Fired when the interactive session ends.

        on_pre_tool(event: PreToolEvent) -> Optional[HookResult]
            Async-compatible.  Return HookResult(action="block") to prevent
            the tool from executing.  Matchers in plugin.json can scope this
            to specific tool names.

        on_post_tool(event: PostToolEvent) -> Optional[HookResult]
            Async-compatible.  Purely observational.  Matchers in plugin.json
            can scope this to specific tool names.

    Prompt contribution:
        compose_system_prompt(current_prompt: str) -> str
            Return a **suffix** to append to the system prompt, or ``""``
            to contribute nothing.  The *current_prompt* argument is provided
            for context only — do NOT return it back.

    Slash commands:
        register_slash_commands() -> dict[str, Callable]
            Return {name: async handler(raw_cmd, components)}.

    Command hooks (plugin.json):
        Hooks may also be declared in ``plugin.json`` under a ``hooks`` key as
        external commands.  These run as subprocesses with event data on stdin
        and a JSON HookResult shape on stdout.  Each entry supports:
        ``matcher`` (regex), ``timeout`` (seconds), and ``command`` (shell string).
    """

    name: str = ""
    version: str = ""


@dataclass
class TurnEvent:
    """Emitted after each assistant turn completes."""

    user_input: str
    agent_response: str
    tool_calls: list[str]
    session_id: str = ""
    timestamp: str = ""
    turn_index: int = 0


@dataclass
class SessionEvent:
    """Emitted when the interactive session ends."""

    messages: list[dict]
    tools_used: list[str]
    session_id: str = ""
    timestamp: str = ""
    turn_count: int = 0


@dataclass
class PreToolEvent:
    """Emitted before a tool call executes."""

    tool_name: str
    tool_kwargs: dict


@dataclass
class PostToolEvent:
    """Emitted after a tool call completes."""

    tool_name: str
    tool_kwargs: dict
    result: str


@dataclass
class HookResult:
    """Return value from plugin hook methods.

    Actions:
        ``"noop"``     — no effect (default).
        ``"block"``    — prevent the operation (tool execution, prompt delivery).
        ``"continue"`` — (turn_end only) run another turn with ``message`` as prompt.
        ``"context"``  — inject ``context`` into the next turn's system instructions.
        ``"warning"``  — surface ``message`` as a user-visible warning.
    """

    action: str = "noop"
    message: str = ""
    context: str = ""


# Valid characters for plugin directory names (P0-3 safety).
_SAFE_PLUGIN_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass
class PluginMeta:
    """Structured metadata read from plugin.json (if present)."""

    name: str
    version: str = ""
    description: str = ""
    skills: list[str] = field(default_factory=list)  # relative paths to skills dirs
    commands: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    mcp_servers: list[dict] = field(default_factory=list)
    source: str = ""  # "builtin" or "user"
    enabled: bool = True
    hooks_config: dict[str, list[dict]] = field(default_factory=dict)
    hook_matchers: dict[str, re.Pattern] = field(default_factory=dict)
    hook_timeouts: dict[str, float] = field(default_factory=dict)
    path: Optional[Path] = None  # plugin directory on disk (for CLAUDE_PLUGIN_ROOT)


# ── Claude Code compatibility maps ──────────────────────────────────────────

# Event names: Claude Code (PascalCase) → internal (snake_case w/ on_ prefix).
_CC_EVENT_MAP = {
    # Lifecycle
    "SessionStart": "on_session_start",
    "SessionEnd": "on_session_end",
    "Setup": "on_setup",
    # Prompt flow
    "UserPromptSubmit": "on_prompt_submit",
    "UserPromptExpansion": "on_prompt_expansion",
    # Tool lifecycle
    "PreToolUse": "on_pre_tool",
    "PostToolUse": "on_post_tool",
    "PostToolUseFailure": "on_post_tool_failure",
    "PostToolBatch": "on_post_tool_batch",
    "PermissionRequest": "on_permission_request",
    "PermissionDenied": "on_permission_denied",
    # Turn lifecycle
    "Stop": "on_turn_end",
    "StopFailure": "on_turn_failure",
    "PreCompact": "on_pre_compact",
    "PostCompact": "on_post_compact",
    # Notifications & display
    "Notification": "on_notification",
    "MessageDisplay": "on_message_display",
    # Sub-agents
    "SubagentStart": "on_subagent_start",
    "SubagentStop": "on_subagent_stop",
    # Tasks
    "TaskCreated": "on_task_created",
    "TaskCompleted": "on_task_completed",
    # Reactive events
    "ConfigChange": "on_config_change",
    "CwdChanged": "on_cwd_changed",
    "FileChanged": "on_file_changed",
    "InstructionsLoaded": "on_instructions_loaded",
    # Worktree lifecycle
    "WorktreeCreate": "on_worktree_create",
    "WorktreeRemove": "on_worktree_remove",
    # Agent teams
    "TeammateIdle": "on_teammate_idle",
    # MCP elicitation
    "Elicitation": "on_elicitation",
    "ElicitationResult": "on_elicitation_result",
}

# Tool name translation: our tool name → Claude Code tool name.
# Used when matching a Claude Code matcher pattern against our tools.
_OUR_TO_CC_TOOL_NAME = {
    # File tools
    "read_file": "Read",
    "write_file": "Write",
    "list_files": "Glob",
    # Shell
    "shell": "Bash",
    # Memory & context
    "memory_read": "Read",
    "memory_write": "Write",
    "memory_search": "Grep",
    # Web
    "web_search": "WebSearch",
    "web_fetch": "WebFetch",
    # Plugin management
    "install_plugin": "Bash",
    "uninstall_plugin": "Bash",
    # Skills
    "activate_skill": "Skill",
    "create_skill": "Write",
    "update_skill": "Write",
    "delete_skill": "Bash",
    # Scheduling
    "schedule_create": "Task",
    "schedule_list": "Read",
    "schedule_delete": "Bash",
    # Agent
    "spawn_agent": "Task",
    "send_message": "Bash",
}


def _as_path_list(value: Any) -> list[str]:
    """Normalize Claude Code manifest path fields to a list of strings."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def _dedupe_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in paths:
        key = raw.strip().rstrip("/")
        if key.startswith("./"):
            key = key[2:]
        if key in {"", "."}:
            key = "."
        if key in seen:
            continue
        seen.add(key)
        out.append(raw)
    return out


def _plugin_data_dir(plugin_name: str) -> Path:
    """Claude Code-compatible persistent data directory for a plugin."""
    safe_name = re.sub(r"[^0-9A-Za-z_-]+", "-", plugin_name).strip("-") or "plugin"
    return shared.AGENT_HOME / "plugins" / "data" / safe_name


def _plugin_substitution_env(plugin_dir: Path, plugin_name: str) -> dict[str, str]:
    return {
        "CLAUDE_PLUGIN_ROOT": str(plugin_dir),
        "CLAUDE_PLUGIN_DATA": str(_plugin_data_dir(plugin_name)),
        "CLAUDE_PROJECT_DIR": str(Path.cwd()),
    }


def _substitute_plugin_vars(value: Any, env: dict[str, str]) -> Any:
    """Recursively substitute Claude Code plugin placeholders in configs."""
    if isinstance(value, str):
        out = value
        for key, replacement in env.items():
            out = out.replace("${" + key + "}", replacement)
        return out
    if isinstance(value, list):
        return [_substitute_plugin_vars(item, env) for item in value]
    if isinstance(value, dict):
        return {key: _substitute_plugin_vars(item, env) for key, item in value.items()}
    return value


def _normalize_claude_hook_entries(
    plugin_name: str,
    event_name: str,
    entries: Any,
) -> tuple[str, list[dict]]:
    """Translate Claude Code hook matcher groups to internal flat entries."""
    internal_event = _CC_EVENT_MAP.get(event_name, event_name)
    if not isinstance(entries, list):
        return internal_event, []
    flat: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        matcher = entry.get("matcher", "")
        nested = entry.get("hooks")
        if isinstance(nested, list):
            for inner in nested:
                if not isinstance(inner, dict):
                    continue
                if inner.get("type", "command") != "command":
                    continue
                cmd = str(inner.get("command", "") or "").strip()
                if not cmd:
                    continue
                normalized = dict(inner)
                normalized["type"] = "command"
                normalized["matcher"] = matcher
                normalized["command"] = cmd
                normalized["timeout"] = _safe_float(inner.get("timeout", 60.0), 60.0)
                flat.append(normalized)
            continue
        if entry.get("type", "command") != "command":
            continue
        cmd = str(entry.get("command", "") or "").strip()
        if not cmd:
            continue
        normalized = dict(entry)
        normalized["type"] = "command"
        normalized["command"] = cmd
        normalized["timeout"] = _safe_float(entry.get("timeout", 60.0), 60.0)
        flat.append(normalized)
    return internal_event, flat


def _merge_hooks_block(
    plugin_name: str,
    source: dict[str, Any],
    into: dict[str, list[dict]],
) -> None:
    for event_name, entries in source.items():
        internal_event, flat = _normalize_claude_hook_entries(plugin_name, event_name, entries)
        if flat:
            into.setdefault(internal_event, []).extend(flat)


def _read_claude_hooks_json(plugin_dir: Path) -> dict[str, list[dict]]:
    """Read ``hooks/hooks.json`` and translate to the internal hook shape.

    Claude Code's structure nests once more than ours:
        {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", ...}]}]}
    Our internal shape is flat:
        {"on_pre_tool": [{"matcher": "Bash", "type": "command", "command": ..., "timeout": ...}]}
    Event names are translated via _CC_EVENT_MAP; ignored events emit a warning.
    """
    hf = plugin_dir / "hooks" / "hooks.json"
    if not hf.exists():
        return {}
    try:
        data = json.loads(hf.read_text(encoding="utf-8"))
    except Exception as exc:
        shared.CONSOLE.print(
            f"[yellow]Plugin '{plugin_dir.name}': hooks.json unreadable: {exc}[/yellow]"
        )
        return {}
    cc_hooks = data.get("hooks", {})
    if not isinstance(cc_hooks, dict):
        return {}
    out: dict[str, list[dict]] = {}
    _merge_hooks_block(plugin_dir.name, cc_hooks, out)
    return out


def _merge_hooks_from_file(
    plugin_dir: Path, hooks_path: str, into: dict[str, list[dict]]
) -> None:
    """Read a hooks JSON file and merge its entries into *into*."""
    resolved = (plugin_dir / hooks_path).resolve()
    if not resolved.exists() or not resolved.is_file():
        return
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception as exc:
        shared.CONSOLE.print(
            f"[yellow]Plugin '{plugin_dir.name}': hooks file '{hooks_path}' "
            f"unreadable: {exc}[/yellow]"
        )
        return
    hooks_block = data if isinstance(data, dict) else {}
    # Handle both {"hooks": {...}} and bare {"EventName": [...]}
    source = hooks_block.get("hooks", hooks_block)
    if not isinstance(source, dict):
        return
    _merge_hooks_block(plugin_dir.name, source, into)


def _read_mcp_json(plugin_dir: Path, plugin_name: str | None = None) -> list[dict]:
    """Read ``.mcp.json`` at the plugin root and return MCP server configs.

    Claude Code's standard location for MCP servers bundled by a plugin.
    Format is ``{"mcpServers": {"name": {...}, ...}}`` — a dict keyed by
    server name.  Returns a flat list of configs each with ``"name"`` set.
    """
    mf = plugin_dir / ".mcp.json"
    if not mf.exists():
        return []
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except Exception as exc:
        shared.CONSOLE.print(
            f"[yellow]Plugin '{plugin_dir.name}': .mcp.json unreadable: {exc}[/yellow]"
        )
        return []
    servers = data.get("mcpServers", data)
    if not isinstance(servers, dict):
        return []
    env = _plugin_substitution_env(plugin_dir, plugin_name or plugin_dir.name)
    out: list[dict] = []
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            cfg = {}
        normalized = {"name": name, **cfg}
        normalized = _substitute_plugin_vars(normalized, env)
        server_env = dict(normalized.get("env", {}) or {})
        normalized["env"] = {**env, **server_env}
        out.append(normalized)
    return out


def _read_plugin_json(plugin_dir: Path) -> Optional[PluginMeta]:
    """Read plugin.json from a plugin directory.  Returns None if absent.

    Detects either layout:
      - ``<plugin>/plugin.json``                 (this project's original format)
      - ``<plugin>/.claude-plugin/plugin.json``  (Claude Code / Codex format)

    Accepts both ``mcp_servers`` (snake_case) and ``mcpServers`` (camelCase).
    Skills default to the ``skills`` subdirectory when it exists, so a
    Claude Code plugin needs no manifest entry to expose its skills.
    """
    cc_pj = plugin_dir / ".claude-plugin" / "plugin.json"
    bare_pj = plugin_dir / "plugin.json"
    pj = cc_pj if cc_pj.exists() else bare_pj
    if not pj.exists():
        # No manifest at all — synthesise minimal meta if any standard
        # subdir exists (so claude-plugin packages without a plugin.json
        # still expose their skills/commands/agents).
        if (
            not (plugin_dir / "skills").is_dir()
            and not (plugin_dir / "commands").is_dir()
            and not (plugin_dir / "agents").is_dir()
            and not (plugin_dir / "SKILL.md").is_file()
        ):
            return None
        skills = ["skills"] if (plugin_dir / "skills").is_dir() else []
        if not skills and (plugin_dir / "SKILL.md").is_file():
            skills = ["."]
        commands = ["commands"] if (plugin_dir / "commands").is_dir() else []
        agents = ["agents"] if (plugin_dir / "agents").is_dir() else []
        return PluginMeta(
            name=plugin_dir.name,
            skills=skills,
            commands=commands,
            agents=agents,
        )
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
        plugin_name = str(data.get("name", plugin_dir.name) or plugin_dir.name)
        plugin_env = _plugin_substitution_env(plugin_dir, plugin_name)
        mcp = data.get("mcp_servers")
        if mcp is None:
            mcp = data.get("mcpServers", [])
        if isinstance(mcp, dict):
            # Claude Code marketplace ``mcpServers`` block is a dict
            # keyed by server name.  Flatten into a list while
            # preserving the key as the canonical name.
            mcp = [{"name": k, **(v if isinstance(v, dict) else {})}
                   for k, v in mcp.items()]
        elif isinstance(mcp, str):
            mcp_path = plugin_dir / mcp
            if mcp_path.exists():
                mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
                if isinstance(mcp, dict):
                    servers = mcp.get("mcpServers", mcp)
                    if isinstance(servers, dict):
                        mcp = [
                            {"name": k, **(v if isinstance(v, dict) else {})}
                            for k, v in servers.items()
                        ]
                    else:
                        mcp = [mcp]
            else:
                mcp = []
        elif isinstance(mcp, list):
            normalized_mcp = []
            for item in mcp:
                if isinstance(item, str):
                    mcp_path = plugin_dir / item
                    if not mcp_path.exists():
                        continue
                    loaded = json.loads(mcp_path.read_text(encoding="utf-8"))
                    servers = loaded.get("mcpServers", loaded) if isinstance(loaded, dict) else {}
                    if isinstance(servers, dict):
                        normalized_mcp.extend(
                            {"name": k, **(v if isinstance(v, dict) else {})}
                            for k, v in servers.items()
                        )
                elif isinstance(item, dict):
                    normalized_mcp.append(item)
            mcp = normalized_mcp
        if isinstance(mcp, list):
            substituted_mcp = []
            for item in mcp:
                if not isinstance(item, dict):
                    continue
                normalized = _substitute_plugin_vars(item, plugin_env)
                server_env = dict(normalized.get("env", {}) or {})
                normalized["env"] = {**plugin_env, **server_env}
                substituted_mcp.append(normalized)
            mcp = substituted_mcp
        raw_hooks: dict[str, list[dict]] = {}
        hook_matchers: dict[str, re.Pattern] = {}
        hook_timeouts: dict[str, float] = {}
        hooks_cfg = data.get("hooks")
        if isinstance(hooks_cfg, str):
            # Path string: "hooks": "./hooks/hooks.json" or "./my-hooks.json"
            _merge_hooks_from_file(plugin_dir, hooks_cfg, raw_hooks)
        elif isinstance(hooks_cfg, list):
            # Array of paths: "hooks": ["./a.json", "./b.json"]
            for hook_path in hooks_cfg:
                if isinstance(hook_path, str):
                    _merge_hooks_from_file(plugin_dir, hook_path, raw_hooks)
        elif isinstance(hooks_cfg, dict):
            _merge_hooks_block(plugin_name, hooks_cfg, raw_hooks)
        for hook_name, entries in raw_hooks.items():
            for entry in entries:
                if isinstance(entry.get("timeout"), (int, float)):
                    hook_timeouts[hook_name] = max(0.0, _safe_float(entry["timeout"], 60.0))
                matcher = str(entry.get("matcher", "") or "").strip()
                if matcher and matcher != "*":
                    try:
                        hook_matchers[hook_name] = re.compile(matcher)
                    except re.error:
                        pass
        # Skills path defaults to ``skills`` subdir when not declared and
        # the directory exists — matches Claude Code's convention-over-config.
        skills_field = _as_path_list(data.get("skills"))
        if (plugin_dir / "skills").is_dir():
            skills_field.insert(0, "skills")
        if not skills_field and (plugin_dir / "SKILL.md").is_file():
            skills_field = ["."]
        skills_field = _dedupe_paths(skills_field)
        commands_field = _as_path_list(data.get("commands"))
        if not commands_field and (plugin_dir / "commands").is_dir():
            commands_field = ["commands"]
        agents_field = _as_path_list(data.get("agents"))
        if not agents_field and (plugin_dir / "agents").is_dir():
            agents_field = ["agents"]
        return PluginMeta(
            name=plugin_name,
            version=data.get("version", ""),
            description=data.get("description", ""),
            skills=skills_field,
            commands=commands_field,
            agents=agents_field,
            mcp_servers=mcp if isinstance(mcp, list) else [],
            hooks_config=raw_hooks,
            hook_matchers=hook_matchers,
            hook_timeouts=hook_timeouts,
        )
    except Exception:
        return None


def _read_marketplace_manifest(dir_path: Path) -> Optional[list[tuple[Path, str]]]:
    """Read ``.claude-plugin/marketplace.json`` if present.

    A Claude Code marketplace is a repo whose root holds
    ``.claude-plugin/marketplace.json`` listing one or more plugins by
    relative ``source`` path.  Returns ``[(plugin_dir, plugin_name), ...]``
    when a marketplace is found, ``None`` otherwise.
    """
    if (dir_path / ".claude-plugin" / "plugin.json").exists() or (
        dir_path / "plugin.json"
    ).exists():
        return None
    mp = dir_path / ".claude-plugin" / "marketplace.json"
    if not mp.exists():
        return None
    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        return None
    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        return None
    out: list[tuple[Path, str]] = []
    for entry in plugins:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source", "") or "").strip()
        if not source:
            continue
        plugin_dir = (dir_path / source).resolve()
        if not plugin_dir.is_dir():
            continue
        name = str(entry.get("name", "") or plugin_dir.name)
        out.append((plugin_dir, name))
    return out


async def _maybe_await(value: Any) -> Any:
    """Await value if it is a coroutine, otherwise return it directly."""
    if asyncio.iscoroutine(value):
        return await value
    return value


def _substitute_command_args(body: str, args_text: str) -> str:
    """Replace ``$ARGUMENTS`` and ``$1`` .. ``$N`` placeholders.

    Mirrors Claude Code's command argument convention.  ``args_text`` is the
    full string after the command name (preserves quoting); positional
    ``$1`` etc. use whitespace-split tokens.
    """
    if not body:
        return body
    out = body.replace("$ARGUMENTS", args_text)
    tokens = args_text.split()
    # Replace highest-index first so $10 doesn't get mangled by $1's substitution.
    for i in range(len(tokens), 0, -1):
        out = out.replace(f"${i}", tokens[i - 1])
    return out


def _hook_result_from_json(raw: dict[str, Any], event_name: str) -> Optional[HookResult]:
    """Translate Claude Code hook JSON output to the local HookResult shape."""
    if raw.get("continue") is False:
        return HookResult(
            action="block",
            message=str(raw.get("stopReason", "") or "stopped by hook"),
        )
    decision = str(raw.get("decision", "") or "")
    if decision == "block":
        reason = str(raw.get("reason", "") or raw.get("message", "") or "blocked")
        if event_name == "on_turn_end":
            return HookResult(action="continue", message=reason)
        return HookResult(action="block", message=reason)
    specific = raw.get("hookSpecificOutput")
    if not isinstance(specific, dict):
        if any(key in raw for key in ("action", "message", "context")):
            return HookResult(
                action=str(raw.get("action", "noop")),
                message=str(raw.get("message", "")),
                context=str(raw.get("context", "")),
            )
        return None
    additional_context = str(specific.get("additionalContext", "") or "")
    if event_name == "on_pre_tool":
        permission = str(specific.get("permissionDecision", "") or "")
        if permission in {"deny", "block"}:
            return HookResult(
                action="block",
                message=str(
                    specific.get("permissionDecisionReason", "")
                    or specific.get("reason", "")
                    or "blocked"
                ),
            )
        if additional_context:
            return HookResult(action="context", context=additional_context)
    if event_name in {"on_post_tool", "on_prompt_submit", "on_turn_end"}:
        if additional_context:
            return HookResult(action="context", context=additional_context)
    return None


def _make_markdown_command_handler(
    *,
    plugin_name: str,
    cmd_key: str,
    body: str,
    description: str,
) -> Callable:
    """Build a slash-command handler that injects the substituted body
    as the next user input.

    Handlers that return a non-empty string from the CLI dispatch loop are
    treated as "use this as the next user_input" — the loop then runs a
    normal turn with that body.  No tools are invoked here; the body itself
    drives the agent.
    """
    async def _handler(raw_cmd: str, components: dict) -> str:
        # raw_cmd is "cmd_key arg1 arg2" — strip the command prefix.
        if raw_cmd.startswith(cmd_key):
            args_text = raw_cmd[len(cmd_key):].lstrip()
        else:
            parts = raw_cmd.split(maxsplit=1)
            args_text = parts[1] if len(parts) > 1 else ""
        return _substitute_command_args(body, args_text)

    _handler.__plugin_name__ = plugin_name  # type: ignore[attr-defined]
    _handler.__cmd_key__ = cmd_key  # type: ignore[attr-defined]
    _handler.__doc__ = description or f"Run /{cmd_key}"
    return _handler


async def _maybe_await_with_timeout(value: Any, timeout_seconds: float) -> Any:
    if timeout_seconds > 0:
        return await asyncio.wait_for(_maybe_await(value), timeout=timeout_seconds)
    return await _maybe_await(value)


async def _call_hook_with_timeout(
    hook: Callable,
    *args: Any,
    timeout_seconds: float,
    executor: Optional[ThreadPoolExecutor] = None,
) -> Any:
    """Call a plugin hook with timeout covering sync and async hooks."""
    if timeout_seconds <= 0:
        return await _maybe_await(hook(*args))
    if inspect.iscoroutinefunction(hook):
        return await asyncio.wait_for(
            _maybe_await(hook(*args)),
            timeout=timeout_seconds,
        )

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(executor, partial(hook, *args))
    try:
        result = await asyncio.wait_for(future, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        future.cancel()
        raise
    return await _maybe_await_with_timeout(result, timeout_seconds)


class PluginCatalog:
    """Discovers, loads, and orchestrates agent plugins from disk.

    Built-in plugins are loaded from ``shared.PLUGINS_DIR`` (package builtin plugins).
    User plugins are loaded from ``shared.USER_PLUGINS_DIR`` (~/.agent/plugins/).

    Each plugin directory must contain ``__init__.py`` with a top-level
    ``register() -> plugin`` function that returns an object implementing
    any subset of the AgentPlugin protocol (duck-typed, no base class needed).

    An optional ``plugin.json`` in the directory provides structured metadata
    (name, version, description, skills path, mcp_servers).

    User plugins with the same name as a built-in plugin override the built-in.
    Plugins can be disabled in config.json via ``plugins.<name>.enabled = false``.
    """

    def __init__(
        self,
        builtin_dir: Path,
        user_dir: Optional[Path] = None,
        plugin_config: Optional[dict] = None,
        turn_hook_timeout_seconds: float = shared.DEFAULT_TURN_HOOK_TIMEOUT_SECONDS,
    ) -> None:
        self._builtin_dir = builtin_dir
        self._user_dir = user_dir
        self._plugin_config = plugin_config or {}
        self._turn_hook_timeout_seconds = max(0.0, float(turn_hook_timeout_seconds))
        self._hook_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="agent-plugin-hook"
        )
        # name → (plugin_object, PluginMeta)
        self._plugins: dict[str, tuple[Any, PluginMeta]] = {}
        self._slash_commands: dict[str, Callable] = {}
        # Skills bundled by plugins: list of (plugin_name, skills_root_path)
        self._bundled_skills: list[tuple[str, Path]] = []
        # MCP configs bundled by plugins: list of (plugin_name, server_config_dict)
        self._bundled_mcp: list[tuple[str, dict]] = []
        # plugin_name → source directory entry name (the directory under
        # USER_PLUGINS_DIR or PLUGINS_DIR that this plugin was loaded from).
        self._plugin_source_dirs: dict[str, str] = {}
        # Agent definitions discovered from <plugin>/agents/*.md.
        # Key format: "plugin:<plugin>:<agent>".  Value: dict with keys
        # ``name``, ``description``, ``body``, ``plugin``, ``path``.
        self._agent_defs: dict[str, dict[str, Any]] = {}

    # ── Discovery ─────────────────────────────────────────────────────────────

    def _is_plugin_enabled(self, name: str) -> bool:
        """Check config.json plugins section for enabled status."""
        pcfg = self._plugin_config.get(name, {})
        if isinstance(pcfg, dict):
            return pcfg.get("enabled", True)
        return True

    def discover_and_load(self) -> list[str]:
        """Scan plugin directories and load all valid plugins.

        Supports three on-disk layouts:
        1. Native: ``<dir>/plugin.json`` + ``__init__.py`` with ``register()``.
        2. Claude Code: ``<dir>/.claude-plugin/plugin.json`` + standard
           subdirectories (skills/ commands/ agents/ hooks/).  No Python
           required — declarative-only plugins are fully supported.
        3. Marketplace: ``<dir>/.claude-plugin/marketplace.json`` listing
           multiple plugins via ``plugins[].source``.  Each entry is expanded
           into its own plugin load.

        Failures in individual plugins are reported but do not abort startup.
        Returns a list of successfully loaded plugin names.
        """
        self._plugins.clear()
        self._slash_commands.clear()
        self._bundled_skills.clear()
        self._bundled_mcp.clear()
        self._agent_defs.clear()
        self._plugin_source_dirs.clear()
        _slash_command_owners: dict[str, str] = {}  # cmd_key → owning plugin name

        # Auto-create user plugins directory
        if self._user_dir:
            self._user_dir.mkdir(parents=True, exist_ok=True)

        # Load builtin first, then user (user overrides builtin)
        search_dirs: list[tuple[Path, str]] = [(self._builtin_dir, "builtin")]
        if self._user_dir:
            search_dirs.append((self._user_dir, "user"))

        for search_dir, source in search_dirs:
            if not search_dir or not search_dir.is_dir():
                continue
            for entry_dir in sorted(search_dir.iterdir()):
                if not entry_dir.is_dir():
                    continue
                for plugin_dir, plugin_name_hint in self._expand_marketplace(entry_dir):
                    self._load_one_plugin(
                        plugin_dir,
                        source=source,
                        name_hint=plugin_name_hint,
                        source_dir_name=entry_dir.name,
                        slash_command_owners=_slash_command_owners,
                    )
        return [name for name in self._plugins]

    @staticmethod
    def _expand_marketplace(entry_dir: Path) -> list[tuple[Path, str]]:
        """Yield (plugin_dir, name_hint) entries.

        For a marketplace dir, returns one entry per declared sub-plugin.
        For a plain plugin dir, returns ``[(entry_dir, entry_dir.name)]``.
        """
        market = _read_marketplace_manifest(entry_dir)
        if market is not None:
            return market
        return [(entry_dir, entry_dir.name)]

    def _load_one_plugin(
        self,
        plugin_dir: Path,
        *,
        source: str,
        name_hint: str,
        source_dir_name: str = "",
        slash_command_owners: dict[str, str],
    ) -> None:
        # P0-3: reject directory names that could collide with real modules.
        if not _SAFE_PLUGIN_NAME.match(name_hint):
            shared.CONSOLE.print(
                f"[yellow]Plugin '{name_hint}': unsafe name — skipped[/yellow]"
            )
            return

        init_file = plugin_dir / "__init__.py"
        has_python = init_file.exists()
        meta = _read_plugin_json(plugin_dir)

        # Nothing recognisable here — neither a manifest, declarative assets,
        # nor a Python entry point.  Quietly skip.
        if meta is None and not has_python:
            return

        plugin_name = (meta.name if meta else "") or name_hint

        # Check enable/disable in config
        if not self._is_plugin_enabled(plugin_name):
            return

        plugin: Any = None
        python_failed = False
        if has_python:
            mod_name = f"_agent_plugin_{name_hint}"
            try:
                spec = importlib.util.spec_from_file_location(
                    mod_name,
                    init_file,
                    submodule_search_locations=[str(plugin_dir)],
                )
                if spec is None or spec.loader is None:
                    return
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
                if hasattr(mod, "register"):
                    plugin = mod.register()
                elif meta is None:
                    shared.CONSOLE.print(
                        f"[yellow]Plugin '{name_hint}': has __init__.py but no "
                        "register() and no declarative assets — skipped[/yellow]"
                    )
                    return
            except Exception as exc:
                python_failed = True
                shared.CONSOLE.print(
                    f"[yellow]Plugin '{name_hint}' Python load failed: {exc} "
                    "(declarative assets still loaded if any)[/yellow]"
                )

        # If Python load failed AND we have no real manifest, nothing
        # useful is left — skip registration entirely (matches the
        # pre-refactor behavior for python-only plugins that crash).
        if python_failed and meta is None:
            return

        # Synthesise meta from Python attributes if no manifest was present.
        if meta is None:
            meta = PluginMeta(
                name=getattr(plugin, "name", "") or plugin_name,
                version=getattr(plugin, "version", "") if plugin else "",
                description=getattr(plugin, "description", "") if plugin else "",
            )
            plugin_name = meta.name
        meta.source = source

        # Slash commands contributed by the Python plugin (declarative
        # commands/*.md registration arrives in commit 2).
        if plugin is not None and hasattr(plugin, "register_slash_commands"):
            for cmd_key, handler in (
                plugin.register_slash_commands() or {}
            ).items():
                if cmd_key in self._slash_commands:
                    existing_owner = slash_command_owners.get(cmd_key, "?")
                    shared.CONSOLE.print(
                        f"[yellow]Plugin '{plugin_name}': slash command "
                        f"'/{cmd_key}' conflicts with plugin "
                        f"'{existing_owner}' — overriding[/yellow]"
                    )
                self._slash_commands[cmd_key] = handler
                slash_command_owners[cmd_key] = plugin_name

        # Record (user overrides builtin with the same plugin name).
        self._plugins[plugin_name] = (plugin, meta)
        if source_dir_name:
            self._plugin_source_dirs[plugin_name] = source_dir_name

        # Bundled skills (declarative — works for both formats).
        for skills_rel in meta.skills:
            skills_path = (plugin_dir / skills_rel).resolve()
            if skills_path.is_dir():
                self._bundled_skills.append((plugin_name, skills_path))

        # Bundled MCP configs — from plugin.json and .mcp.json.
        for mcp_cfg in meta.mcp_servers:
            if isinstance(mcp_cfg, dict) and mcp_cfg.get("name"):
                self._bundled_mcp.append((plugin_name, mcp_cfg))
        for mcp_cfg in _read_mcp_json(plugin_dir, plugin_name):
            if isinstance(mcp_cfg, dict) and mcp_cfg.get("name"):
                self._bundled_mcp.append((plugin_name, mcp_cfg))

        # Declarative commands and agents (Claude Code / Codex convention).
        self._load_command_files(plugin_dir, plugin_name, slash_command_owners, meta.commands)
        self._load_agent_files(plugin_dir, plugin_name, meta.agents)

        # Merge Claude Code's hooks/hooks.json (if present) into hooks_config.
        # Existing plugin.json hooks win on the same event key.
        cc_hooks = _read_claude_hooks_json(plugin_dir)
        for event_name, entries in cc_hooks.items():
            existing = meta.hooks_config.setdefault(event_name, [])
            existing.extend(entries)
            # Compile matchers per-entry below; the legacy per-event matcher
            # cache is not used for CC-style multi-entry events.
        meta.path = plugin_dir

    # ── Declarative commands / agents (Claude Code convention) ──────────

    def _load_command_files(
        self,
        plugin_dir: Path,
        plugin_name: str,
        slash_command_owners: dict[str, str],
        command_paths: list[str] | None = None,
    ) -> None:
        """Register every ``commands/*.md`` as a namespaced slash command.

        The command key is always ``<plugin>:<stem>`` (matches Claude Code's
        ``/plugin:command`` namespace).  The handler returns a string built
        from the markdown body with ``$ARGUMENTS`` / ``$1``..``$N`` placeholders
        substituted; the CLI loop treats a returned string as the next user
        input, so the agent processes the command body as a turn.
        """
        paths = command_paths if command_paths else (
            ["commands"] if (plugin_dir / "commands").is_dir() else []
        )
        if not paths:
            return
        from agent.skills.catalog import parse_skill_markdown

        cmd_files: list[Path] = []
        for rel in paths:
            target = (plugin_dir / rel).resolve()
            if target.is_file() and target.suffix == ".md":
                cmd_files.append(target)
            elif target.is_dir():
                cmd_files.extend(sorted(target.rglob("*.md")))
        for cmd_file in sorted(set(cmd_files)):
            if cmd_file.name.lower() == "readme.md":
                continue
            stem = cmd_file.stem
            # Strip a ``.prompt`` suffix (Claude Code marketplace convention).
            if stem.endswith(".prompt"):
                stem = stem[: -len(".prompt")]
            cmd_key = f"{plugin_name}:{stem}"

            try:
                raw = cmd_file.read_text(encoding="utf-8")
            except Exception as exc:
                shared.CONSOLE.print(
                    f"[yellow]Plugin '{plugin_name}': command {cmd_file.name} "
                    f"unreadable: {exc}[/yellow]"
                )
                continue
            metadata, body = parse_skill_markdown(raw)
            handler = _make_markdown_command_handler(
                plugin_name=plugin_name,
                cmd_key=cmd_key,
                body=body,
                description=str(metadata.get("description", "") or ""),
            )

            if cmd_key in self._slash_commands:
                existing_owner = slash_command_owners.get(cmd_key, "?")
                shared.CONSOLE.print(
                    f"[yellow]Plugin '{plugin_name}': slash command "
                    f"'/{cmd_key}' conflicts with plugin "
                    f"'{existing_owner}' — overriding[/yellow]"
                )
            self._slash_commands[cmd_key] = handler
            slash_command_owners[cmd_key] = plugin_name

    def _load_agent_files(
        self,
        plugin_dir: Path,
        plugin_name: str,
        agent_paths: list[str] | None = None,
    ) -> None:
        paths = agent_paths if agent_paths else (
            ["agents"] if (plugin_dir / "agents").is_dir() else []
        )
        if not paths:
            return
        from agent.skills.catalog import parse_skill_markdown

        agent_files: list[Path] = []
        for rel in paths:
            target = (plugin_dir / rel).resolve()
            if target.is_file() and target.suffix == ".md":
                agent_files.append(target)
            elif target.is_dir():
                agent_files.extend(sorted(target.rglob("*.md")))
        for agent_file in sorted(set(agent_files)):
            if agent_file.name.lower() == "readme.md":
                continue
            stem = agent_file.stem
            try:
                raw = agent_file.read_text(encoding="utf-8")
            except Exception as exc:
                shared.CONSOLE.print(
                    f"[yellow]Plugin '{plugin_name}': agent {agent_file.name} "
                    f"unreadable: {exc}[/yellow]"
                )
                continue
            metadata, body = parse_skill_markdown(raw)
            key = f"plugin:{plugin_name}:{stem}"
            self._agent_defs[key] = {
                "plugin": plugin_name,
                "name": str(metadata.get("name", "") or stem),
                "description": str(metadata.get("description", "") or ""),
                "body": body,
                "path": agent_file,
            }

    def get_agent_definition(self, ref: str) -> Optional[dict]:
        """Lookup an agent definition by ``plugin:<plugin>:<agent>`` key.

        Returns ``None`` if the ref does not match a declared agent.  Used by
        BaseAgent._execute_agent when the requested role names a plugin agent.
        """
        return self._agent_defs.get(ref)

    def list_agent_definitions(self) -> list[dict[str, Any]]:
        """Snapshot of all registered agent definitions (for /agents listing)."""
        return [dict(value, key=key) for key, value in self._agent_defs.items()]

    # ── Hot reload ───────────────────────────────────────────────────────

    async def reload(self, components: dict) -> dict:
        """Re-scan plugin directories and apply diffs in place.

        Re-imports Python plugins (sys.modules cache cleared), re-registers
        all slash commands, agent definitions, and bundled assets, and adds
        any newly-declared MCP servers to the running ``mcp_client``.

        Limitations (v1):
        - Cannot disconnect an MCP stdio process for a removed plugin —
          its tools are unregistered from the ToolRegistry so the model
          no longer sees them, but the subprocess stays alive until the
          agent shuts down.
        - Plugin ``on_session_end`` hooks are NOT fired on eviction; reload
          is a developer-facing notification, not a clean shutdown.

        Returns a summary dict suitable for surfacing to the user.
        """
        # Snapshot pre-reload state.
        old_plugin_names = set(self._plugins.keys())
        old_mcp = {
            (pname, cfg.get("name", "?"))
            for pname, cfg in self._bundled_mcp
        }
        # Drop sys.modules entries so re-imported Python plugins pick up
        # any code changes on disk.  Safe to drop for plugins that were
        # never python-only (no-op).
        for name in old_plugin_names:
            sys.modules.pop(f"_agent_plugin_{name}", None)

        # Re-discover.  This clears all internal state and rebuilds it.
        loaded = self.discover_and_load()
        new_plugin_names = set(loaded)
        added = new_plugin_names - old_plugin_names
        removed = old_plugin_names - new_plugin_names

        # Reload skill catalog: clear and rewalk both user/builtin trees,
        # then re-attach every plugin's bundled skills root.
        skill_catalog = components.get("skill_catalog")
        if skill_catalog is not None:
            try:
                skill_catalog.load_all()
                for pname, sroot in self._bundled_skills:
                    skill_catalog._load_root(sroot, source=f"plugin:{pname}")
                if hasattr(skill_catalog, "_rebuild_aliases"):
                    skill_catalog._rebuild_aliases()
                if hasattr(skill_catalog, "invalidate"):
                    skill_catalog.invalidate()
                else:
                    # Fallback for test fakes / old catalogs without invalidate().
                    skill_catalog._dirty = True  # noqa: SLF001
                    if hasattr(skill_catalog, "_prompt_generation"):
                        skill_catalog._prompt_generation += 1
            except Exception as exc:
                shared.CONSOLE.print(
                    f"[yellow]Plugin reload: skill catalog refresh failed: {exc}[/yellow]"
                )

        # MCP: connect newly-declared servers; unregister tools belonging
        # to plugins that were removed.  We cannot kill the stdio process
        # for the latter — see the docstring limitation.
        mcp_client = components.get("mcp_client")
        registry = components.get("registry")
        new_mcp_servers: list[dict] = [
            cfg for pname, cfg in self._bundled_mcp
            if (pname, cfg.get("name", "?")) not in old_mcp
        ]
        connected_mcp_names: list[str] = []
        if new_mcp_servers and mcp_client is not None:
            try:
                await mcp_client.connect_from_config(
                    {"mcp_servers": new_mcp_servers}
                )
                connected_mcp_names = [
                    str(c.get("name", "?")) for c in new_mcp_servers
                ]
            except Exception as exc:
                shared.CONSOLE.print(
                    f"[yellow]Plugin reload: MCP connect failed: {exc}[/yellow]"
                )
        if removed and registry is not None:
            # Unregister tools whose source prefix matches removed plugins'
            # bundled MCP server names.  Best-effort: requires us to have
            # tracked the (plugin → server name) mapping before clearing.
            for pname, server_name in old_mcp:
                if pname in removed and hasattr(registry, "unregister_by_source_prefix"):
                    registry.unregister_by_source_prefix(f"mcp:{server_name}")

        # Re-fire on_session_start for every plugin: discover_and_load
        # re-instantiates them all (via fresh sys.modules import + register()),
        # so even pre-existing plugins received a new object that has never
        # seen the session-start signal.  Without this, e.g. the evolution
        # plugin's engine reference would be None until next restart.
        self.fire_session_start(components)

        return {
            "ok": True,
            "added_plugins": sorted(added),
            "removed_plugins": sorted(removed),
            "newly_connected_mcp": connected_mcp_names,
            "total_loaded": len(loaded),
        }

    def get_loaded_names_for_directory(self, dir_name: str) -> set[str]:
        """Return the set of loaded plugin names that originate from *dir_name*."""
        return {
            name for name, src_dir in self._plugin_source_dirs.items()
            if src_dir == dir_name
        }

    def get_bundled_skills(self) -> list[tuple[str, Path]]:
        """Return list of (plugin_name, skills_root_path) for bundled skills."""
        return list(self._bundled_skills)

    def get_bundled_mcp(self) -> list[tuple[str, dict]]:
        """Return list of (plugin_name, mcp_server_config) for bundled MCP servers."""
        return list(self._bundled_mcp)

    def list_plugins(self) -> list[PluginMeta]:
        """Return metadata for all loaded plugins."""
        return [meta for _, meta in self._plugins.values()]

    # ── Prompt composition ─────────────────────────────────────────────────────

    def compose_all_prompts(self, base: str) -> str:
        """Let each loaded plugin append a suffix to the composed system prompt."""
        result = base
        for plugin, _meta in self._plugins.values():
            if not hasattr(plugin, "compose_system_prompt"):
                continue
            try:
                suffix = plugin.compose_system_prompt(result)
                if suffix:
                    result = result.rstrip() + "\n\n" + suffix.strip()
            except Exception as exc:
                _pname = getattr(plugin, "name", "?")
                shared.CONSOLE.print(
                    f"[dim]Plugin '{_pname}' compose_system_prompt error: {exc}[/dim]"
                )
        return result

    # ── Slash commands ─────────────────────────────────────────────────────────

    def get_slash_commands(self) -> dict[str, Callable]:
        """Return mapping of command name → async handler(raw_cmd, components)."""
        return dict(self._slash_commands)

    # ── Hook helpers ────────────────────────────────────────────────────────────

    def _hook_timeout(self, meta: PluginMeta, hook_name: str) -> float:
        """Return the per-hook timeout override, or the global default."""
        return meta.hook_timeouts.get(hook_name, self._turn_hook_timeout_seconds)

    def _matches_tool(
        self, meta: PluginMeta, hook_name: str, tool_name: str
    ) -> bool:
        """Check whether *hook_name* on *meta* has a matcher for *tool_name*.

        No matcher → always matches (opt-in to filtering by declaring one).
        Honors both our snake_case names and Claude Code PascalCase names
        when a CC plugin's matcher targets the latter.
        """
        matcher = meta.hook_matchers.get(hook_name)
        if matcher is None:
            return True
        if matcher.search(tool_name):
            return True
        cc_name = _OUR_TO_CC_TOOL_NAME.get(tool_name)
        return bool(cc_name and matcher.search(cc_name))

    @staticmethod
    def _entry_matches_tool(entry: dict, tool_name: str) -> bool:
        """Per-entry matcher check (used for Claude Code hook entries that
        carry their own matcher string instead of a per-event compiled one)."""
        matcher_str = str(entry.get("matcher", "") or "").strip()
        if not matcher_str or matcher_str == "*":
            return True
        try:
            pattern = re.compile(matcher_str)
        except re.error:
            return matcher_str == tool_name or matcher_str == _OUR_TO_CC_TOOL_NAME.get(tool_name)
        if pattern.search(tool_name):
            return True
        cc_name = _OUR_TO_CC_TOOL_NAME.get(tool_name)
        return bool(cc_name and pattern.search(cc_name))

    def _hook_env(self, meta: PluginMeta, event_payload: dict) -> dict[str, str]:
        """Build Claude Code-compatible environment variables for hook exec."""
        env = dict(os.environ)
        if meta.path is not None:
            env["CLAUDE_PLUGIN_ROOT"] = str(meta.path)
        # Workspace = current cwd; matches what CC reports.
        env["CLAUDE_PROJECT_DIR"] = os.getcwd()
        tool_name = str(event_payload.get("tool_name", "") or "")
        if tool_name:
            env["TOOL_NAME"] = _OUR_TO_CC_TOOL_NAME.get(tool_name, tool_name)
        tool_kwargs = event_payload.get("tool_kwargs")
        if tool_kwargs is not None:
            try:
                env["TOOL_INPUT"] = json.dumps(tool_kwargs, ensure_ascii=False)
            except Exception:
                env["TOOL_INPUT"] = str(tool_kwargs)
        result = event_payload.get("result")
        if result is not None:
            env["TOOL_OUTPUT"] = str(result)
        text = event_payload.get("text")
        if text is not None and event_payload.get("event") == "on_prompt_submit":
            env["CLAUDE_USER_PROMPT"] = str(text)
        return env

    async def _run_command_hooks(
        self,
        event_name: str,
        event_payload: dict,
        meta: PluginMeta,
    ) -> list[HookResult]:
        """Execute command-type hooks declared in plugin.json for *event_name*."""
        results: list[HookResult] = []
        entries = meta.hooks_config.get(event_name, [])
        tool_name = str(event_payload.get("tool_name", "") or "")
        env = self._hook_env(meta, event_payload)
        for entry in entries:
            cmd = str(entry.get("command", "") or "").strip()
            if not cmd:
                continue
            # Per-entry matcher (Claude Code style).  Legacy entries with no
            # matcher field always pass and remain compatible with the older
            # per-event matcher cache on PluginMeta.
            if tool_name and "matcher" in entry and not self._entry_matches_tool(entry, tool_name):
                continue
            timeout = max(0.0, _safe_float(entry.get("timeout"), self._hook_timeout(meta, event_name)))
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdin_data = json.dumps(event_payload, ensure_ascii=False).encode("utf-8")
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(stdin_data), timeout=max(1.0, timeout)
                )
                if proc.returncode == 2:
                    reason = stderr_bytes.decode(errors="replace").strip() or "blocked"
                    results.append(HookResult(action="block", message=reason))
                    continue
                try:
                    raw = json.loads(stdout_bytes.decode("utf-8"))
                    if isinstance(raw, dict):
                        parsed_result = _hook_result_from_json(raw, event_name)
                        if parsed_result is not None:
                            results.append(parsed_result)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            except asyncio.TimeoutError:
                shared.CONSOLE.print(
                    f"[dim]Plugin '{meta.name}' command hook '{event_name}' "
                    f"timed out after {timeout:.2f}s[/dim]"
                )
            except Exception as exc:
                shared.CONSOLE.print(
                    f"[dim]Plugin '{meta.name}' command hook '{event_name}' "
                    f"error: {exc}[/dim]"
                )
        return results

    # ── Lifecycle event firing ─────────────────────────────────────────────────

    async def fire_prompt_submit(
        self, text: str, metadata: Optional[dict] = None
    ) -> HookResult:
        """Fire before the user message reaches the agent.

        Returns the first blocking result, or the last non-noop result.
        """
        meta_dict = metadata or {}
        result = HookResult()
        for plugin, meta in self._plugins.values():
            # Command hooks for this event
            cmd_results = await self._run_command_hooks(
                "on_prompt_submit",
                {"event": "on_prompt_submit", "text": text, "metadata": meta_dict},
                meta,
            )
            for r in cmd_results:
                if r.action == "block":
                    return r
                if r.action != "noop":
                    result = r

            # Python in-process hooks
            if not hasattr(plugin, "on_prompt_submit"):
                continue
            try:
                r = await _call_hook_with_timeout(
                    plugin.on_prompt_submit,
                    text,
                    meta_dict,
                    timeout_seconds=self._hook_timeout(meta, "on_prompt_submit"),
                    executor=self._hook_executor,
                )
                if isinstance(r, HookResult):
                    if r.action == "block":
                        _emit_plugin_event(
                            "hook_blocked",
                            plugin_name=meta.name,
                            hook_name="on_prompt_submit",
                            reason=r.message,
                        )
                        return r
                    if r.action == "context":
                        _emit_plugin_event(
                            "hook_context_injected",
                            plugin_name=meta.name,
                            hook_name="on_prompt_submit",
                        )
                    if r.action != "noop":
                        result = r
            except asyncio.TimeoutError:
                shared.CONSOLE.print(
                    f"[dim]Plugin '{meta.name}' prompt_submit timed out[/dim]"
                )
            except Exception as exc:
                shared.CONSOLE.print(
                    f"[dim]Plugin prompt_submit error ({meta.name}): {exc}[/dim]"
                )
        return result

    def fire_session_start(self, components: dict) -> None:
        """Synchronous session-start notification; called before the input loop."""
        for plugin, _meta in self._plugins.values():
            if not hasattr(plugin, "on_session_start"):
                continue
            try:
                plugin.on_session_start(components)
            except Exception as exc:
                shared.CONSOLE.print(f"[dim]Plugin session_start error: {exc}[/dim]")

    async def fire_turn_end(self, event: TurnEvent) -> list[HookResult]:
        """Notify all plugins after each assistant turn; collect HookResults.

        A result with ``action="continue"`` signals the caller to run another
        turn with ``message`` as the next user prompt.
        """
        results: list[HookResult] = []
        for plugin, meta in self._plugins.values():
            cmd_results = await self._run_command_hooks(
                "on_turn_end",
                {
                    "event": "on_turn_end",
                    "user_input": event.user_input,
                    "agent_response": event.agent_response,
                    "tool_calls": event.tool_calls,
                    "session_id": event.session_id,
                    "timestamp": event.timestamp,
                    "turn_index": event.turn_index,
                },
                meta,
            )
            for r in cmd_results:
                if r.action != "noop":
                    _emit_plugin_event(
                        "hook_fired",
                        plugin_name=meta.name,
                        hook_name="on_turn_end",
                        source="command",
                        action=r.action,
                        message=r.message[:200] if r.message else "",
                    )
                if r.context:
                    _emit_plugin_event(
                        "hook_context_injected",
                        plugin_name=meta.name,
                        hook_name="on_turn_end",
                        source="command",
                    )
            results.extend(cmd_results)

            if not hasattr(plugin, "on_turn_end"):
                continue
            try:
                r = await _call_hook_with_timeout(
                    plugin.on_turn_end,
                    event,
                    timeout_seconds=self._hook_timeout(meta, "on_turn_end"),
                    executor=self._hook_executor,
                )
                if isinstance(r, HookResult):
                    if r.action != "noop":
                        _emit_plugin_event(
                            "hook_fired",
                            plugin_name=meta.name,
                            hook_name="on_turn_end",
                            action=r.action,
                            message=r.message[:200] if r.message else "",
                        )
                    if r.context:
                        _emit_plugin_event(
                            "hook_context_injected",
                            plugin_name=meta.name,
                            hook_name="on_turn_end",
                        )
                    results.append(r)
            except asyncio.TimeoutError:
                shared.CONSOLE.print(
                    f"[dim]Plugin '{meta.name}' turn_end timed out after "
                    f"{self._hook_timeout(meta, 'on_turn_end'):.2f}s[/dim]"
                )
            except Exception as exc:
                shared.CONSOLE.print(f"[dim]Plugin turn_end error ({meta.name}): {exc}[/dim]")
        return results

    async def fire_session_end(self, event: SessionEvent) -> None:
        """Notify all plugins when the interactive session ends."""
        for plugin, meta in self._plugins.values():
            timeout = self._hook_timeout(meta, "on_session_end")
            if not hasattr(plugin, "on_session_end"):
                continue
            try:
                await _call_hook_with_timeout(
                    plugin.on_session_end,
                    event,
                    timeout_seconds=timeout,
                    executor=self._hook_executor,
                )
            except asyncio.TimeoutError:
                shared.CONSOLE.print(
                    f"[dim]Plugin '{meta.name}' session_end timed out after "
                    f"{timeout:.2f}s[/dim]"
                )
            except Exception as exc:
                shared.CONSOLE.print(
                    f"[dim]Plugin session_end error ({meta.name}): {exc}[/dim]"
                )

    async def fire_pre_tool(self, event: PreToolEvent) -> HookResult:
        """Fire before a tool call; first blocking result short-circuits the chain.

        Matchers declared in plugin.json scoped to ``on_pre_tool`` are honoured:
        a plugin without a matcher is called for every tool; a plugin with a
        matcher is only called when the tool name matches.
        """
        for plugin, meta in self._plugins.values():
            if not self._matches_tool(meta, "on_pre_tool", event.tool_name):
                continue

            cmd_results = await self._run_command_hooks(
                "on_pre_tool",
                {
                    "event": "on_pre_tool",
                    "tool_name": event.tool_name,
                    "tool_kwargs": event.tool_kwargs,
                },
                meta,
            )
            for r in cmd_results:
                if r.action == "block":
                    return r

            if not hasattr(plugin, "on_pre_tool"):
                continue
            try:
                r = await _call_hook_with_timeout(
                    plugin.on_pre_tool,
                    event,
                    timeout_seconds=self._hook_timeout(meta, "on_pre_tool"),
                    executor=self._hook_executor,
                )
                if isinstance(r, HookResult) and r.action == "block":
                    _emit_plugin_event(
                        "hook_blocked",
                        plugin_name=meta.name,
                        hook_name="on_pre_tool",
                        tool_name=event.tool_name,
                        reason=r.message,
                    )
                    return r
            except asyncio.TimeoutError:
                shared.CONSOLE.print(
                    f"[dim]Plugin '{meta.name}' pre_tool timed out after "
                    f"{self._hook_timeout(meta, 'on_pre_tool'):.2f}s[/dim]"
                )
            except Exception as exc:
                shared.CONSOLE.print(
                    f"[dim]Plugin pre_tool error ({meta.name}): {exc}[/dim]"
                )
        return HookResult()

    async def fire_post_tool(self, event: PostToolEvent) -> HookResult:
        """Fire after a tool call completes; last non-noop context wins.

        Matchers declared in plugin.json scoped to ``on_post_tool`` are honoured.
        """
        result = HookResult()
        for plugin, meta in self._plugins.values():
            if not self._matches_tool(meta, "on_post_tool", event.tool_name):
                continue

            cmd_results = await self._run_command_hooks(
                "on_post_tool",
                {
                    "event": "on_post_tool",
                    "tool_name": event.tool_name,
                    "tool_kwargs": event.tool_kwargs,
                    "result": event.result,
                },
                meta,
            )
            for r in cmd_results:
                if r.context:
                    result = r

            if not hasattr(plugin, "on_post_tool"):
                continue
            try:
                r = await _call_hook_with_timeout(
                    plugin.on_post_tool,
                    event,
                    timeout_seconds=self._hook_timeout(meta, "on_post_tool"),
                    executor=self._hook_executor,
                )
                if isinstance(r, HookResult) and r.context:
                    result = r
            except asyncio.TimeoutError:
                shared.CONSOLE.print(
                    f"[dim]Plugin '{meta.name}' post_tool timed out after "
                    f"{self._hook_timeout(meta, 'on_post_tool'):.2f}s[/dim]"
                )
            except Exception as exc:
                shared.CONSOLE.print(
                    f"[dim]Plugin post_tool error ({meta.name}): {exc}[/dim]"
                )
        return result
