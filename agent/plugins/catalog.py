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
from agent.core.events import TOOL_EVENTS, ToolPostExecute, ToolPreExecute
from agent.commands.registry import CommandRegistry
from agent.core.agent_registry import AgentRegistry, role_key
from agent.core.output import _active_event_collector
from agent.kernel.plugin import PluginHost
from agent.plugins.profile_selection import profile_from_config
from agent.plugins.profiles import resolve_plugin_state

CONSOLE = shared.CONSOLE
PLUGINS_DIR = shared.PLUGINS_DIR
# No USER_PLUGINS_DIR mirror here on purpose: it is derived from AGENT_HOME,
# which `--name` rewrites after this module is imported.  Read
# `shared.USER_PLUGINS_DIR` at the point of use instead.  (PLUGINS_DIR is the
# package's own _builtin directory and does not move.)

# Synchronous plugin hooks run on a small dedicated pool.  A hook that blows
# past its timeout keeps its worker until it returns on its own (see
# ``_call_hook_with_timeout``), so the pool needs enough headroom that one
# misbehaving plugin cannot stall hooks for every other plugin.
PLUGIN_HOOK_POOL_WORKERS = 4


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

            ``components`` is the runtime's component mapping.  Read a *service*
            from it by name -- ``resolve(components, "skill_catalog")`` or
            ``require_service(components, "agent")`` from
            :mod:`agent.runtime.services` -- rather than with ``.get``, so the
            lookup names a service the runtime declares
            (``agent/runtime/core_services.py``) and a guard can see it.

            **Open question, recorded here because this is where it matters:** a
            plugin is handed the whole mapping, which means it can reach any
            service rather than only the ones it declared.  The alternative is for
            a plugin to declare ``inject = ("skill_catalog",)`` and be given the
            service, the way ``ctx.provide`` / ``ctx.on`` already work.  Deciding
            that changes the plugin API, so it is not decided here; what is decided
            is that the reads are *named* either way.

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

    Lifecycle (the resource-owning half):
        setup(ctx) -> None
            Synchronous.  Called once after the plugin's declarative assets are
            registered.  Register everything through *ctx* and it unwinds with
            the plugin: ``ctx.add_slash_command``, ``ctx.add_bundled_skill``,
            ``ctx.add_bundled_mcp``, ``ctx.add_agent_definition``,
            ``ctx.add_tool``, and ``ctx.on_unwind`` for anything else.

            Prefer this to the ``register_*`` methods for anything with a
            lifetime.  What is registered here is removed by
            :meth:`PluginCatalog.unload_plugin` without the catalog needing to
            know what kind of thing it was.

        teardown() -> None
            Synchronous.  Called when the plugin unloads, after
            ``on_session_end`` has fired and after the setup scope has unwound.
            For what a scope cannot express: closing a subprocess handle,
            flushing a file, joining a thread.

            This is the hook the old code had no equivalent of, which is why a
            removed plugin's MCP subprocess used to stay alive until shutdown.

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
    "set_identity": "Write",
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
        "CODEX_PLUGIN_ROOT": str(plugin_dir),
        "CODEX_PLUGIN_DATA": str(_plugin_data_dir(plugin_name)),
        "CODEX_PROJECT_DIR": str(Path.cwd()),
    }


_PLUGIN_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _substitute_plugin_vars(value: Any, env: dict[str, str]) -> Any:
    """Recursively substitute Claude/Codex plugin placeholders in configs."""
    if isinstance(value, str):
        def _replace(match: re.Match[str]) -> str:
            key = match.group(1)
            default = match.group(2)
            if key in env:
                return env[key]
            if key in os.environ:
                return os.environ[key]
            return default if default is not None else match.group(0)

        return _PLUGIN_VAR_RE.sub(_replace, value)
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


def _read_plugin_hooks(
    plugin_dir: Path, plugin_name: str, hooks_cfg: Any
) -> tuple[dict[str, list[dict]], dict[str, re.Pattern], dict[str, float]]:
    """Collect a manifest's hooks, their matchers and their timeouts.

    ``hooks`` may be a path, a list of paths, or an inline block; the three
    forms merge into one ``{hook_name: [entries]}`` mapping.
    """
    raw_hooks: dict[str, list[dict]] = {}
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
    hook_matchers: dict[str, re.Pattern] = {}
    hook_timeouts: dict[str, float] = {}
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
    return raw_hooks, hook_matchers, hook_timeouts
def _normalize_mcp_servers(plugin_dir: Path, data: dict, plugin_env: dict) -> list[dict]:
    """Normalise a manifest's ``mcp_servers`` into a list of server dicts.

    Accepts ``mcp_servers`` (snake_case) or ``mcpServers`` (camelCase), and a
    dict, a single path, or a list of paths/dicts.  Claude Code's marketplace
    block is a dict keyed by server name, so each key becomes the canonical name.
    Plugin variables are substituted into every server, and the plugin's own
    env wins over a server's on a collision.
    """
    mcp = data.get("mcp_servers")
    if mcp is None:
        mcp = data.get("mcpServers", [])
    if isinstance(mcp, dict):
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
    if not isinstance(mcp, list):
        return []
    substituted_mcp = []
    for item in mcp:
        if not isinstance(item, dict):
            continue
        normalized = _substitute_plugin_vars(item, plugin_env)
        server_env = dict(normalized.get("env", {}) or {})
        normalized["env"] = {**plugin_env, **server_env}
        substituted_mcp.append(normalized)
    return substituted_mcp
def _read_plugin_json(plugin_dir: Path) -> Optional[PluginMeta]:
    """Read plugin.json from a plugin directory.  Returns None if absent.

    Detects either layout:
      - ``<plugin>/plugin.json``                 (this project's original format)
      - ``<plugin>/.claude-plugin/plugin.json``  (Claude Code format)
      - ``<plugin>/.codex-plugin/plugin.json``   (Codex format)

    Accepts both ``mcp_servers`` (snake_case) and ``mcpServers`` (camelCase).
    Skills default to the ``skills`` subdirectory when it exists, so a
    Claude Code plugin needs no manifest entry to expose its skills.
    """
    manifest_candidates = [
        plugin_dir / ".codex-plugin" / "plugin.json",
        plugin_dir / ".claude-plugin" / "plugin.json",
        plugin_dir / "plugin.json",
    ]
    pj = next((candidate for candidate in manifest_candidates if candidate.exists()), None)
    if pj is None:
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
        mcp = _normalize_mcp_servers(plugin_dir, data, plugin_env)
        raw_hooks, hook_matchers, hook_timeouts = _read_plugin_hooks(
            plugin_dir, plugin_name, data.get("hooks")
        )
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
            mcp_servers=mcp,
            hooks_config=raw_hooks,
            hook_matchers=hook_matchers,
            hook_timeouts=hook_timeouts,
        )
    except Exception:
        return None


def _has_plugin_manifest(dir_path: Path) -> bool:
    return any(
        candidate.exists()
        for candidate in (
            dir_path / ".codex-plugin" / "plugin.json",
            dir_path / ".claude-plugin" / "plugin.json",
            dir_path / "plugin.json",
        )
    )


def _local_marketplace_source_path(entry: dict[str, Any]) -> str:
    source = entry.get("source", "")
    if isinstance(source, str):
        return source.strip()
    if not isinstance(source, dict):
        return ""
    source_kind = str(source.get("source", "") or "").strip().lower()
    if source_kind and source_kind != "local":
        return ""
    return str(source.get("path", "") or "").strip()


def _read_marketplace_manifest(dir_path: Path) -> Optional[list[tuple[Path, str]]]:
    """Read a supported marketplace manifest if present.

    Supports Claude Code's ``.claude-plugin/marketplace.json`` and Codex's
    root ``marketplace.json``.  Only local relative plugin sources are expanded;
    remote marketplace entries belong to the installer, not the runtime loader.
    """
    if _has_plugin_manifest(dir_path):
        return None
    mp = next(
        (
            candidate
            for candidate in (
                dir_path / ".claude-plugin" / "marketplace.json",
                dir_path / "marketplace.json",
            )
            if candidate.exists()
        ),
        None,
    )
    if mp is None:
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
        source = _local_marketplace_source_path(entry)
        if not source:
            continue
        plugin_dir = (dir_path / source).resolve()
        if not plugin_dir.is_dir():
            continue
        name = str(entry.get("name", "") or plugin_dir.name)
        out.append((plugin_dir, name))
    return out


def _is_plugin_container(dir_path: Path) -> bool:
    """Return True when *dir_path* itself is a plugin or marketplace root."""
    if _has_plugin_manifest(dir_path):
        return True
    return any(
        candidate.exists()
        for candidate in (
            dir_path / ".claude-plugin" / "marketplace.json",
            dir_path / "marketplace.json",
        )
    )


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


def _command_metadata(
    handler: Callable,
    explicit: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Return portable command metadata without retaining caller-owned values."""

    source = explicit or {}
    result: dict[str, Any] = {}
    for field_name in ("aliases", "scopes"):
        value = source.get(
            field_name,
            getattr(
                handler,
                f"__command_{field_name}__",
                getattr(handler, field_name, None),
            ),
        )
        if value:
            result[field_name] = (value,) if isinstance(value, str) else tuple(value)
    for field_name in ("usage", "description", "concurrency"):
        value = source.get(
            field_name,
            getattr(
                handler,
                f"__command_{field_name}__",
                getattr(handler, field_name, None),
            ),
        )
        if value:
            result[field_name] = str(value)
    accepts_interjections = source.get(
        "accepts_interjections",
        getattr(
            handler,
            "__command_accepts_interjections__",
            getattr(handler, "accepts_interjections", None),
        ),
    )
    if accepts_interjections is not None:
        result["accepts_interjections"] = bool(accepts_interjections)
    return result


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
        # cancel() only helps while the call is still queued — a thread that
        # has already entered the hook cannot be interrupted.  Say so, because
        # the worker it occupies is unavailable until the hook returns on its
        # own, and a hook that never returns costs the pool a slot for good.
        if not future.cancel():
            shared.CONSOLE.print(
                f"[dim]Plugin hook "
                f"'{getattr(hook, '__qualname__', hook)}' exceeded "
                f"{timeout_seconds:.2f}s and is still running; its worker "
                f"thread stays blocked until it returns[/dim]"
            )
        raise
    return await _maybe_await_with_timeout(result, timeout_seconds)


#: Distinguishes the absence of a default from a default of None.
_NO_DEFAULT = object()


def _add_bundled(store: list, entry: Any) -> Any:
    """Append *entry* to *store* and return the undo that takes it back.

    By identity, not equality: two plugins may contribute an equal-looking skills root
    or MCP config, and removing the wrong one would silently disable a plugin that is
    still loaded.  Written as a pair because the alternative -- appending here and
    remembering to remove there -- is the bookkeeping a scope exists to remove, and the
    one place it survived.
    """
    store.append(entry)

    def forget() -> None:
        for index, item in enumerate(store):
            if item is entry:
                del store[index]
                return

    return forget


class _CatalogPlugin:
    """Adapts a discovered plugin object to the kernel's protocol.

    A plugin written for this catalog already has a ``name``, an ``inject`` and a
    ``setup(ctx)`` -- which is the kernel's protocol.  What the adapter adds is what
    the *catalog* knows and the plugin does not: its metadata, the directory it came
    from, and the declarative assets that directory holds.

    Assets are registered from :meth:`setup` rather than by the loader so that a
    plugin whose dependencies are unmet registers **nothing**.  The host does not
    call ``setup`` until ``inject`` is satisfied, so "pending" and "registered
    nothing" are the same state by construction, instead of two code paths that have
    to agree.

    Everything else is forwarded, so the hook methods the catalog calls
    (``on_session_start``, ``on_turn_end``, ``on_pre_tool``, ...) reach the real
    plugin without this class knowing their names.
    """

    def __init__(
        self,
        *,
        catalog: Any,
        name: str,
        plugin: Any,
        meta: Any,
        plugin_dir: Path,
    ) -> None:
        self._catalog = catalog
        self._plugin = plugin
        self._meta = meta
        self._plugin_dir = plugin_dir
        self.name = name

    # -- identity ------------------------------------------------------------

    @property
    def meta(self) -> Any:
        return self._meta

    @property
    def plugin(self) -> Any:
        return self._plugin

    @property
    def inject(self) -> tuple[str, ...]:
        """The plugin's declared dependencies, and nothing added.

        The adapter used to prepend ``assets`` -- a contribution service the catalog
        provided up front -- because gating on ``inject`` cannot express "a service
        provided during your own setup".  That service is gone: measurement showed no
        plugin ever called it (its only caller was this adapter), so a plugin that
        wants to contribute a command or a role declares ``inject = ("commands",)`` or
        ``("agents",)`` and gets the real registry.
        """
        return tuple(str(d) for d in (getattr(self._plugin, "inject", ()) or ()))

    # -- lifecycle -----------------------------------------------------------

    def setup(self, ctx: Any) -> None:
        # Recorded here rather than at discovery because a discovered plugin is not
        # a loaded one: the catalog's readers ask "what is loaded", and a pending
        # plugin that appeared in that answer would be reported as contributing
        # something it has not contributed.
        #
        # And recorded as an *effect*, so the catalog's own bookkeeping unwinds with
        # everything else.  Without this the kernel's cascade would dispose a
        # dependent's scope while the catalog still listed it -- a plugin reported
        # as loaded that has no services and no listeners.
        self._catalog._plugins[self.name] = (self._plugin, self._meta)
        ctx.on_unwind(lambda: self._catalog._plugins.pop(self.name, None))
        ctx.provide("plugin.meta", self._meta, replace=True)
        self._register_declarative_assets(ctx)
        real = getattr(self._plugin, "setup", None)
        if callable(real):
            real(ctx)

    def teardown(self) -> None:
        real = getattr(self._plugin, "teardown", None)
        if callable(real):
            real()

    def _register_declarative_assets(self, ctx: Any) -> None:
        """Everything the plugin's files declare, each reversible through *ctx*.

        Last before the plugin's own ``setup``, so its registration sees a catalog
        that already holds what its files declared.
        """
        catalog = self._catalog
        meta = self._meta
        plugin_dir = self._plugin_dir

        # Skills and MCP configs are the two contributions with no service of their
        # own: each has exactly one production reader (bootstrap, at composition time)
        # and no plugin asks for them by name.  So the append and its undo are written
        # here rather than behind a service that only this method would call.
        for skills_rel in meta.skills:
            skills_path = (plugin_dir / skills_rel).resolve()
            if skills_path.is_dir():
                ctx.on_unwind(
                    _add_bundled(catalog._bundled_skills, (self.name, skills_path))
                )

        for mcp_cfg in meta.mcp_servers:
            if isinstance(mcp_cfg, dict) and mcp_cfg.get("name"):
                ctx.on_unwind(_add_bundled(catalog._bundled_mcp, (self.name, mcp_cfg)))
        for mcp_cfg in _read_mcp_json(plugin_dir, self.name):
            if isinstance(mcp_cfg, dict) and mcp_cfg.get("name"):
                ctx.on_unwind(_add_bundled(catalog._bundled_mcp, (self.name, mcp_cfg)))

        for undo in catalog._load_command_files(
            plugin_dir, self.name, meta.commands
        ):
            ctx.on_unwind(undo)
        for undo in catalog._load_agent_files(plugin_dir, self.name, meta.agents):
            ctx.on_unwind(undo)

        # Claude Code's ``hooks/hooks.json`` merges into the plugin's own hook
        # config, with a plugin.json entry winning on the same event key.  Merged
        # here because the config is the plugin's; left un-undone because nothing
        # reads a removed plugin's hooks -- the catalog looks them up by name.
        for event_name, entries in _read_claude_hooks_json(plugin_dir).items():
            meta.hooks_config.setdefault(event_name, []).extend(entries)

        meta.path = plugin_dir

        # Slash commands the plugin declares in Python, as opposed to the markdown
        # files handled above.
        if hasattr(self._plugin, "register_slash_commands"):
            for cmd_key, handler in (
                self._plugin.register_slash_commands() or {}
            ).items():
                if catalog._commands.has(cmd_key):
                    existing = catalog._commands.owner(cmd_key) or "?"
                    shared.CONSOLE.print(
                        f"[yellow]Plugin '{self.name}': slash command "
                        f"'{cmd_key}' conflicts with plugin "
                        f"'{existing}' -- overriding[/yellow]"
                    )
                ctx.on_unwind(
                    catalog._commands.add(
                        cmd_key,
                        handler,
                        metadata=_command_metadata(handler),
                        owner=self.name,
                    )
                )

    def __getattr__(self, item: str) -> Any:
        # Only reached for names this adapter does not define, which is exactly the
        # hook methods and any plugin-specific attribute the catalog reads.
        return getattr(self._plugin, item)


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
        event_bus: Any = None,
        components: Optional[dict] = None,
    ) -> None:
        self._builtin_dir = builtin_dir
        self._user_dir = user_dir
        self._plugin_config = plugin_config or {}
        #: The composition to load under, named by the ``plugins`` section's
        #: reserved ``_profile`` key.  Resolved once, at construction: an unknown
        #: name raises here rather than silently loading the default set, so a
        #: run under a narrowed composition cannot quietly get everything.
        self._plugin_profile = profile_from_config(self._plugin_config)
        self._turn_hook_timeout_seconds = max(0.0, float(turn_hook_timeout_seconds))
        self._reload_lock = asyncio.Lock()
        self._hook_executor = ThreadPoolExecutor(
            max_workers=PLUGIN_HOOK_POOL_WORKERS,
            thread_name_prefix="agent-plugin-hook",
        )
        # name → (plugin_object, PluginMeta)
        self._plugins: dict[str, tuple[Any, PluginMeta]] = {}
        #: Slash commands, as one registry rather than three parallel dicts.  See
        #: :mod:`agent.commands.registry` for why: the handler, its ``/help`` metadata
        #: and its owner are one record, so they cannot disagree about which keys
        #: exist -- and the add/remove pair lives in one place.
        self._commands = CommandRegistry()
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
        #: Sub-agent roles, as a registry rather than a bare dict.  See
        #: :mod:`agent.core.agent_registry`.
        self._agent_defs = AgentRegistry()
        # name → PluginInstall.  The install owns the scope that unwinds
        # everything a plugin registered through ``setup(ctx)``, so unloading is
        # "dispose the scope", not "enumerate the five kinds of contribution".
        # ``_installs`` and ``_pending`` are the host's, reached through the
        # properties below: the kernel owns the tree, and a second copy here would
        # be a second source of truth for "what is loaded".
        # Backs those scopes.  A plugin-provided service lands here and is
        # removed with the plugin rather than with the process.
        #
        # Imported here rather than at module scope: ``agent.runtime`` pulls in
        # ``agent.runtime.contracts``, which imports the skill catalog, which
        # imports the tool registry, which imports this module.  A lazy import
        # breaks that cycle at the one point where it is not needed yet.
        from agent.runtime.services import ServiceRegistry

        #: The kernel for this catalog: services, events, the plugin tree, and
        #: the lifecycle.  The registry **adopts the runtime's component mapping**
        #: when it is given one, so a plugin-provided service and a runtime service
        #: live in one namespace -- ``inject = ("registry",)`` can name the tool
        #: registry the same way it names a plugin's service.
        #:
        #: Adopting rather than copying is what keeps the two views from drifting:
        #: ``RuntimeComponents.services`` and this registry are then the same
        #: storage, so a plugin that replaces a service replaces it for the
        #: runtime too.  That is the point of "replacement is a first-class act".
        _registry = ServiceRegistry.adopt(components) if isinstance(components, dict) else ServiceRegistry()
        self._host = PluginHost(services=_registry)
        self._services = self._host.services
        #: The capability services, provided so a plugin reaches them by name.
        #: ``ctx.commands`` and ``ctx.agents`` are the plan's idiom -- a plugin
        #: registers a capability *on a service* -- and they replace the ``assets``
        #: contribution service, which no plugin ever called: its only caller was this
        #: catalog's own adapter, and two of its five methods had no caller at all.
        self._host.services.provide("commands", self._commands)
        self._host.services.provide("agents", self._agent_defs)
        #: The *core* services, in a container of their own.  Distinct from
        #: ``_services`` above, which holds what plugins provide: the two are
        #: different populations, and conflating them would let a plugin's service
        #: shadow a core one by name.  Set by ``register_core_services``.
        self._core_services: Any = None
        # The tool-event subscription.  This is what replaces the executor
        # holding this catalog: the executor publishes ToolPreExecute /
        # ToolPostExecute to a bus, and the plugin hooks run as listeners.
        # Defaults to the shared bus so production wiring needs no changes.
        self._event_bus = event_bus if event_bus is not None else TOOL_EVENTS
        self._event_subscriptions: list[Callable[[], None]] = [
            self._event_bus.on(ToolPreExecute, self._on_tool_pre_execute),
            self._event_bus.on(ToolPostExecute, self._on_tool_post_execute),
        ]
        # One bus for the composition: the kernel host publishes the typed events a
        # plugin subscribes to through ``ctx.bus``, and the catalog's hook dispatch
        # listens on the same one.  Two buses would mean a plugin's subscription and
        # the thing that fires it could be on different objects -- a listener that
        # is simply never called, which is the failure mode with no symptom.
        self._host.events = self._event_bus

    def _run_hook_sync(self, hook: Callable, event: Any, timeout: float) -> None:
        """Run one plugin hook to completion from synchronous code.

        Unload is synchronous -- ``discover_and_load`` is, and reload calls
        unload from inside itself -- so a hook cannot simply be awaited.  Two
        cases, because there is no single call that works for both:

        - **No running loop.**  The common case for a tool or a CLI command.
          ``asyncio.run`` drives it, including the async-hook and timeout
          machinery.
        - **A running loop.**  ``asyncio.run`` cannot be used: it leaves the
          coroutine unawaited and warns.  A sync hook is called directly here,
          bounded by nothing, and an async hook is skipped rather than left
          pending -- a plugin that must run async teardown has
          ``ctx.on_unwind``, which is invoked synchronously by the scope.
        """
        if inspect.iscoroutinefunction(hook):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                shared.CONSOLE.print(
                    f"[dim]Plugin session_end skipped: an async hook cannot be "
                    f"driven from inside a running event loop[/dim]"
                )
                return
            asyncio.run(
                _call_hook_with_timeout(
                    hook,
                    event,
                    timeout_seconds=timeout,
                    executor=self._hook_executor,
                )
            )
            return
        # A sync hook: run it on the plugin pool, bounded by the timeout.
        if timeout <= 0:
            hook(event)
            return
        future = self._hook_executor.submit(hook, event)
        future.result(timeout=timeout)

    def register_core_services(self, components: Any) -> None:
        """Point this catalog at the runtime's container for core services.

        Its own :attr:`_services` holds what plugins *provide*; this holds what
        the runtime *has*.  Called by whoever wires the catalog up, so the four
        reads in ``_reload_locked`` resolve through the container rather than
        through a ``components`` mapping -- which is what makes a core service
        replaceable from a plugin's point of view.
        """
        from agent.runtime.contracts import RuntimeComponents

        self._core_services = (
            components
            if isinstance(components, RuntimeComponents)
            else RuntimeComponents(components)
        )

    def _core(self, name: str, default: Any = None) -> Any:
        """One core service, absence tolerated.

        Returns *default* rather than raising, matching the ``components.get``
        it replaces: every call site branches on a missing value, and a
        composition legitimately has no ``mcp_client``.  The container is what
        makes the absence *distinguishable* for a caller that wants that.
        """
        if self._core_services is None:
            # Not wired: an unsupported way to run, but a missing call to
            # ``register_core_services`` must not turn into an AttributeError in
            # the middle of a reload.
            return default
        return self._core_services.lookup(name, default)

    def resolve_service(self, name: str, default: Any = _NO_DEFAULT) -> Any:
        """A service some plugin provided, or *default*.

        The read half of :meth:`PluginContext.provide`.  It goes through the
        catalog's registry rather than a single install's scope so a plugin sees
        everything loaded before it, not just its own registrations.

        A missing name with *no* default raises, because a silent ``None`` is how
        an absent dependency turns into an ``AttributeError`` three frames away
        from the thing that was actually missing.  ``default=None`` is a value and
        returns ``None`` -- the two had to be distinguished, which is what the
        sentinel is for.
        """
        from agent.runtime.services import MissingServiceError

        if default is _NO_DEFAULT:
            return self._services.get(name)  # raises MissingServiceError
        return self._services.get(name, default)

    def mount(self, plugin: Any) -> Any:
        """Mount a plugin the catalog did not discover, and return its install.

        The composition root uses this for the services that are plugins but not
        files: the agent loop, and anything else the runtime mounts itself.  Public
        rather than reaching into the host, because the host is the catalog's own --
        a caller that pokes ``_host`` breaks on any stand-in for this class, which is
        exactly what happened to a test double when the loop was first mounted.
        """
        return self._host.load(plugin)

    @property
    def _installs(self) -> dict[str, Any]:
        """The loaded plugins, owned by the kernel host."""
        return self._host.installs

    @property
    def _pending(self) -> dict[str, Any]:
        """The plugins waiting for a dependency, owned by the kernel host."""
        return self._host.pending_installs

    # ── Discovery ─────────────────────────────────────────────────────────────

    def _is_plugin_enabled(self, name: str) -> bool:
        """Whether this plugin loads in this composition.

        Resolved through :mod:`agent.plugins.profiles` rather than read out of the
        raw ``plugins`` section, so the decision has a name, a default and a
        documented precedence.  The old body understood one key and stated none of
        that: the default was load-everything, and the only way to know what a run
        would load was to read the file it happened to be handed.
        """
        return resolve_plugin_state(
            name, config=self._plugin_config, profile=self._plugin_profile
        ).enabled

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

        Returns the names of plugins that actually **activated**, which is not
        the same as the plugins discovered: one that declares an ``inject`` the
        composition does not supply stays pending and registers nothing, so
        listing it as loaded would be a lie the caller acts on.  Use
        :meth:`pending_report` to see what it is waiting for.
        """
        self._plugins.clear()
        self._pending.clear()
        self._commands.clear()
        self._bundled_skills.clear()
        self._bundled_mcp.clear()
        self._agent_defs.clear()
        self._plugin_source_dirs.clear()
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
            entry_dirs = [search_dir] if _is_plugin_container(search_dir) else [
                entry_dir
                for entry_dir in sorted(search_dir.iterdir())
                if entry_dir.is_dir()
            ]
            for entry_dir in entry_dirs:
                for plugin_dir, plugin_name_hint in self._expand_marketplace(entry_dir):
                    self._load_one_plugin(
                        plugin_dir,
                        source=source,
                        name_hint=plugin_name_hint,
                        source_dir_name=entry_dir.name,
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

    def _remove_plugin_owned_state(
        self,
        plugin_name: str,
    ) -> None:
        """Remove what the catalog knows about a plugin that is not a contribution.

        Every contribution a plugin makes -- a service, a command, a skill root, an MCP
        config, an agent definition, a tool -- recorded its own removal on that
        plugin's scope, so disposing the scope is what takes them back.  Filtering the
        lists by plugin name as well would be a *second* mechanism for one thing, and
        two mechanisms are how they come to disagree: one removes what the other
        forgot, silently, until a change makes their difference visible.

        What is left is discovery metadata: which directory the plugin came from and
        the record that it was loaded.  Those are the catalog's own bookkeeping about
        a plugin rather than the plugin's contributions, so nothing owns their removal
        but the catalog.
        """
        self._plugin_source_dirs.pop(plugin_name, None)
        self._plugins.pop(plugin_name, None)

    def unload_plugin(self, plugin_name: str) -> dict:
        """Unload one plugin: fire its end hook, unwind its scope, tear it down.

        The one code path for unloading, in the only order that lets a plugin
        release everything it holds:

        1. ``on_session_end`` -- while its registrations still exist, because a
           hook that cannot see its own resources cannot clean them up.  This
           is the leak the old eviction had: a plugin removed by ``reload``
           never got this call at all.
        2. the scope -- every registration made through ``setup``'s ``ctx``.
        3. ``teardown`` -- the escape hatch for what a scope cannot express,
           such as the MCP subprocess handle the old code documented as
           unclosable.
        4. the catalog assets and the module cache entry.

        Returns a summary rather than raising, because the callers are tools and
        HTTP handlers that have to report a result either way.
        """
        install = self._installs.get(plugin_name)
        if install is not None and install.unloaded:
            return {"ok": False, "error": f"already unloaded: {plugin_name}"}
        if install is None:
            # Two different refusals, and the difference matters to a caller
            # reporting a result: unloading twice is harmless (a parent may have
            # taken the plugin down already), while a name that never existed is a
            # typo.
            if self._host.was_unloaded(plugin_name):
                return {"ok": False, "error": f"already unloaded: {plugin_name}"}
            if plugin_name not in self._plugins:
                return {"ok": False, "error": f"no such plugin: {plugin_name}"}

        plugin, meta = self._plugins.get(plugin_name, (None, None))

        # 1. The end hook, while the plugin's registrations still exist: a hook
        #    that cannot see its own resources cannot release them.
        if plugin is not None and hasattr(plugin, "on_session_end"):
            self._fire_session_end_for(plugin, meta, plugin_name)

        # 2. The kernel takes the tree down: dependents first, then the plugin's
        #    own children, then ``teardown``, then its scope.  Assets registered
        #    through ``ctx`` unwind here rather than by filtering lists on name.
        #
        #    Order note: the kernel runs ``teardown`` *before* disposing the scope,
        #    where this method used to do the opposite.  That is a deliberate change
        #    of behaviour -- a teardown that cannot see its own registrations cannot
        #    release what depends on them -- and it is why ``teardown`` may now
        #    observe the services and listeners it registered.
        self._host.unload(plugin_name)

        # 3. What the kernel does not own: the catalog's own bookkeeping and the
        #    import cache (dropped so a re-install picks up code changes on disk).
        self._remove_plugin_owned_state(plugin_name)
        sys.modules.pop(f"_agent_plugin_{plugin_name}", None)
        return {"ok": True, "unloaded": plugin_name}

    def _fire_session_end_for(
        self, plugin: Any, meta: Optional[PluginMeta], plugin_name: str
    ) -> None:
        """Fire one plugin's ``on_session_end``, honouring its timeout.

        Synchronous because the hook itself may be sync or async and
        ``_call_hook_with_timeout`` runs sync hooks on the dedicated plugin pool
        and drives coroutine hooks to completion.  Unload is therefore
        symmetric with :meth:`discover_and_load`, which is also synchronous; a
        plugin needing async teardown has ``ctx.on_unwind`` for it.

        A copy of the per-plugin body of :meth:`fire_session_end` rather than a
        call to it, because that method iterates every plugin and this one must
        touch exactly one.
        """
        timeout = self._hook_timeout(meta, "on_session_end") if meta else (
            self._turn_hook_timeout_seconds
        )
        event = SessionEvent(messages=[], tools_used=[], session_id="")
        try:
            self._run_hook_sync(plugin.on_session_end, event, timeout)
        except asyncio.TimeoutError:
            shared.CONSOLE.print(
                f"[dim]Plugin '{plugin_name}' session_end timed out after "
                f"{timeout:.2f}s[/dim]"
            )
        except Exception as exc:
            shared.CONSOLE.print(
                f"[dim]Plugin session_end error ({plugin_name}): {exc}[/dim]"
            )

    def _load_one_plugin(
        self,
        plugin_dir: Path,
        *,
        source: str,
        name_hint: str,
        source_dir_name: str = "",
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

        # A user plugin with the same plugin name replaces the built-in plugin
        # as a unit. Assets omitted by the replacement must not survive.
        if plugin_name in self._plugins:
            self._remove_plugin_owned_state(plugin_name)

        # Slash commands the plugin declares in Python are registered by the
        # adapter, during activation -- not here.  Registering them at discovery
        # would give a plugin whose dependencies are unmet a visible slash command
        # with no undo: pending *and* registered, which is the state the gate in
        # the kernel exists to prevent.
        if source_dir_name:
            self._plugin_source_dirs[plugin_name] = source_dir_name

        # The declarative assets and the plugin's own ``setup`` both run inside
        # the adapter's activation, so a plugin whose dependencies are unmet
        # registers *nothing* -- see ``_CatalogPlugin.setup``.  The host gates on
        # the adapter's ``inject`` and keeps the object pending, which also means
        # an activation needs no re-read from disk: the plugin object is already
        # here.
        install = self._host.load(
            _CatalogPlugin(
                catalog=self,
                name=plugin_name,
                plugin=plugin,
                meta=meta,
                plugin_dir=plugin_dir,
                    ),
            # Same name means replacement, which is this catalog's historical
            # behaviour for a user plugin overriding a builtin and for a reload.
            replace=True,
        )
        if install.pending:
            # Discovered, not loaded: its ``setup`` has not run, so it registered
            # nothing and must not be reported as loaded.  ``pending_report()`` says
            # what it is waiting for.
            self._plugins.pop(plugin_name, None)

    def pending_plugins(self) -> list[str]:
        """Names of plugins waiting for a service the composition does not supply."""
        return sorted(self._host.pending())

    def pending_report(self) -> dict[str, list[str]]:
        """What each pending plugin is waiting for, by name.

        Reported rather than logged and forgotten: a plugin that never activates
        registers nothing, and "nothing happened" is the hardest kind of silence
        to debug.
        """
        return {
            name: [dep for dep in pending.inject if not self._services.has(dep)]
            for name, pending in sorted(self._pending.items())
        }

    # ── Declarative commands / agents (Claude Code convention) ──────────

    def _load_command_files(
        self,
        plugin_dir: Path,
        plugin_name: str,
        command_paths: list[str] | None = None,
    ) -> list[Callable[[], None]]:
        """Register every ``commands/*.md`` as a namespaced slash command.

        Returns the undo for each registration rather than removing them later by
        plugin name: a caller that can reverse what it just did does not need to be
        told which plugin it was acting for, and a sixth kind of contribution
        cannot be added without its undo (the failure the name-filtering version
        had: a registration with no matching filter outlives its plugin).

        The command key is always ``<plugin>:<stem>`` (matches Claude Code's
        ``/plugin:command`` namespace).  The handler returns a string built
        from the markdown body with ``$ARGUMENTS`` / ``$1``..``$N`` placeholders
        substituted; the CLI loop treats a returned string as the next user
        input, so the agent processes the command body as a turn.
        """
        paths = command_paths if command_paths else (
            ["commands"] if (plugin_dir / "commands").is_dir() else []
        )
        undos: list[Callable[[], None]] = []
        if not paths:
            return undos
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

            if self._commands.has(cmd_key):
                existing_owner = self._commands.owner(cmd_key) or "?"
                shared.CONSOLE.print(
                    f"[yellow]Plugin '{plugin_name}': slash command "
                    f"'/{cmd_key}' conflicts with plugin "
                    f"'{existing_owner}' — overriding[/yellow]"
                )
            # One write, one undo, both from the registry: the three parallel dicts
            # this used to touch by hand are one record there.
            undos.append(
                self._commands.add(
                    cmd_key,
                    handler,
                    metadata=_command_metadata(handler, explicit=metadata),
                    owner=plugin_name,
                )
            )
        return undos

    def _load_agent_files(
        self,
        plugin_dir: Path,
        plugin_name: str,
        agent_paths: list[str] | None = None,
    ) -> list[Callable[[], None]]:
        undos: list[Callable[[], None]] = []
        paths = agent_paths if agent_paths else (
            ["agents"] if (plugin_dir / "agents").is_dir() else []
        )
        if not paths:
            return undos
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
            key = role_key(plugin_name, stem)
            # One write and one undo from the registry, so the ``k=key`` closure bug
            # the hand-written version had to guard against cannot be written here:
            # there is no loop variable captured by a lambda.
            undos.append(
                self._agent_defs.add(
                    key,
                    {
                        "plugin": plugin_name,
                        "name": str(metadata.get("name", "") or stem),
                        "description": str(metadata.get("description", "") or ""),
                        "body": body,
                        "path": agent_file,
                    },
                )
            )
        return undos

    def get_agent_definition(self, ref: str) -> Optional[dict]:
        """Lookup an agent definition by ``plugin:<plugin>:<agent>`` key.

        Returns ``None`` if the ref does not match a declared agent.  Used by
        BaseAgent._execute_agent when the requested role names a plugin agent.
        """
        return self._agent_defs.get(ref)

    def list_agent_definitions(self) -> list[dict[str, Any]]:
        """Snapshot of all registered agent definitions (for /agents listing)."""
        return self._agent_defs.all()

    # ── Hot reload ───────────────────────────────────────────────────────

    def close(self) -> None:
        """Release the hook thread pool.  Safe to call more than once.

        Queued-but-unstarted hooks are dropped.  Hooks already running cannot
        be interrupted, so this does not wait for them — a plugin that hangs
        would otherwise hold up shutdown indefinitely.
        """
        for unsubscribe in self._event_subscriptions:
            unsubscribe()
        self._event_subscriptions.clear()
        self._hook_executor.shutdown(wait=False, cancel_futures=True)

    async def reload(self, components: dict) -> dict:
        """Serialize plugin reload transactions for this catalog instance."""

        async with self._reload_lock:
            return await self._reload_locked(components)

    async def _reload_locked(self, components: dict) -> dict:
        # The caller hands over the runtime's components on every reload, so this
        # is where the container gets bound.  Doing it here rather than asking
        # every caller to remember a second call keeps the existing API: a reload
        # with components is a reload that can resolve them.
        if components is not None:
            self.register_core_services(components)

        """Re-scan plugin directories and apply diffs in place.

        Re-imports Python plugins (sys.modules cache cleared), re-registers
        all slash commands, agent definitions, and bundled assets, and adds
        any newly-declared MCP servers to the running ``mcp_client``.

        Eviction now goes through :meth:`unload_plugin`, so a removed plugin's
        ``on_session_end`` fires, its scope unwinds, and its ``teardown`` gets a
        chance to close what a scope cannot -- the two leaks this method used to
        document.  What reload still cannot do is reload a plugin's code
        in-place for the plugins that stay loaded: it clears the import cache
        and re-discovers, so a changed plugin is a new object with a new scope,
        while the old one is unloaded first.

        Returns a summary dict suitable for surfacing to the user.
        """
        # Snapshot pre-reload state.
        old_plugin_names = set(self._plugins.keys())
        old_mcp = {
            (pname, cfg.get("name", "?"))
            for pname, cfg in self._bundled_mcp
        }
        old_discovery_state = {
            "plugins": dict(self._plugins),
            # One snapshot, because the registry holds handler, metadata and owner
            # together.  The reload rollback used to restore three dicts separately,
            # which is three chances to restore two of them.
            "slash_commands": self._commands.snapshot(),

            "bundled_skills": list(self._bundled_skills),
            "bundled_mcp": list(self._bundled_mcp),
            "plugin_source_dirs": dict(self._plugin_source_dirs),
            "agent_defs": self._agent_defs.snapshot(),
        }
        # Unload every currently-loaded plugin before re-discovering, so a
        # plugin that is about to disappear gets its end hook, its scope
        # unwind, and its teardown.  Without this, eviction was a table rebuild
        # that left the plugin's non-registration resources behind -- the
        # documented MCP-subprocess leak.
        #
        # Unloading all of them rather than only the removed set is deliberate:
        # re-discovery re-creates every plugin from a freshly imported module,
        # so each one genuinely becomes a new install, and the old install's
        # scope must not stay open beside it.
        for name in sorted(old_plugin_names):
            self.unload_plugin(name)

        # Re-discover.  This clears all internal state and rebuilds it.
        loaded = self.discover_and_load()
        new_plugin_names = set(loaded)
        added = new_plugin_names - old_plugin_names
        removed = old_plugin_names - new_plugin_names

        command_router = self._core("command_router")
        if command_router is not None:
            try:
                command_router.register_plugin_catalog(self)
            except Exception:
                self._plugins = old_discovery_state["plugins"]
                self._commands.restore(old_discovery_state["slash_commands"])
                self._bundled_skills = old_discovery_state["bundled_skills"]
                self._bundled_mcp = old_discovery_state["bundled_mcp"]
                self._plugin_source_dirs = old_discovery_state[
                    "plugin_source_dirs"
                ]
                self._agent_defs.restore(old_discovery_state["agent_defs"])
                raise

        # Reload skill catalog: clear and rewalk both user/builtin trees,
        # then re-attach every plugin's bundled skills root.
        skill_catalog = self._core("skill_catalog")
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
        mcp_client = self._core("mcp_client")
        registry = self._core("registry")
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
        return self._commands.handlers()

    def get_slash_command_metadata(self) -> dict[str, dict[str, Any]]:
        """Return detached metadata snapshots for registered slash commands."""
        return self._commands.metadata()

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

    async def _on_tool_pre_execute(self, event: ToolPreExecute) -> Optional[str]:
        """Bus listener for :class:`ToolPreExecute`.

        Adapts the typed event to the existing hook chain rather than
        duplicating it, so a plugin's ``on_pre_tool`` and its ``plugin.json``
        command hooks keep running exactly as before.  Returns the refusal
        reason, or None.
        """
        result = await self.fire_pre_tool(
            PreToolEvent(tool_name=event.tool_name, tool_kwargs=event.arguments)
        )
        if result.action == "block":
            return result.message or "blocked"
        return None

    async def _on_tool_post_execute(self, event: ToolPostExecute) -> None:
        """Bus listener for :class:`ToolPostExecute`; purely observational."""
        await self.fire_post_tool(
            PostToolEvent(
                tool_name=event.tool_name,
                tool_kwargs=event.arguments,
                result=event.result,
            )
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
