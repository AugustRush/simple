from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Mapping, Optional

from agent import shared
from agent.core.events import bus_name
from agent.commands.registry import CommandRegistry
from agent.core.agent_registry import AgentRegistry, role_key
from agent.plugins.hook_dispatch import (
    HookDispatcher,
    _plugin_data_dir,
    _safe_float,
)
from agent.kernel.plugin import PluginHost
from agent.plugins import formats as plugin_formats
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


class AgentPlugin:
    """What ``PluginCatalog`` loads: an object with a ``name`` and, optionally, a ``register()``.

    **This used to be a list of hooks to implement.  It is not any more.**  Six
    ``on_*`` methods were documented here and dispatched by the catalog; no plugin
    implemented the last of them, and the kernel's ``setup(ctx)`` plus event
    subscriptions replaced every one of them.  Listing them again would be listing
    methods that are never called.

    What a plugin does now
    ----------------------
    ``setup(ctx)`` runs once, after the plugin's declarative assets are registered, and
    everything it registers unwinds with the plugin.  The names are services the
    composition provides, reached through the context:

        ctx.commands.add(name, handler, metadata={...})   # a slash command
        ctx.agents.add(name, {"body": "..."})             # a sub-agent role
        ctx.registry.register(tool_object)                # a tool
        ctx.provide("my_service", value)                  # something for other plugins
        ctx.on(TurnCompleted, handler)                    # a typed event
        ctx.on_unwind(close_something)                    # anything with a lifetime

    ``ctx.provide`` and ``ctx.on`` are the two that make a plugin tree a tree: one
    plugin offers a service, another declares ``inject = ("that_service",)`` and is
    activated when it appears.  A plugin whose ``inject`` is unmet stays **pending**
    and registers nothing.

    Events are per *domain*: ``TURN``, ``LLM``, ``TOOL`` and ``SESSION`` each name a set of
    event types (``agent/core/event_domains.py``) published on that domain's bus.  Both
    forms of subscription ask the table where the type lives, so both work:

        ctx.on(TurnCompleted, handler)       # one type
        TURN.attach(ctx, handler)            # every event of a domain, one handler

    ``ctx.on`` for a single type is usually the right one -- it says what the plugin
    actually reacts to, and the bus is resolved for it.

    What is still a method on the plugin
    ------------------------------------
        compose_system_prompt(current_prompt: str) -> str
            Return a **suffix** to append to the system prompt, or ``""``.  The
            *current_prompt* argument is for context only -- do NOT return it back.

        register_slash_commands() -> dict[str, Callable]
            Return ``{name: async handler(raw_cmd, components)}``.  Prefer
            ``ctx.commands.add`` in ``setup``, which unwinds with the plugin.

        teardown() -> None
            Synchronous.  Called when the plugin unloads, after the setup scope has
            unwound.  For what a scope cannot express: closing a subprocess handle,
            flushing a file, joining a thread.  This is the hook the old design had no
            equivalent of, which is why a removed plugin's MCP subprocess used to stay
            alive until shutdown.

    Declarative assets (``plugin.json``)
    ------------------------------------
    ``skills``, ``commands``, ``agents`` and ``mcpServers`` name paths the catalog reads
    on the plugin's behalf.  There is no ``ctx.add_*`` for these: a skill root or an MCP
    config is declared in the manifest, and a plugin's Python contributed one nowhere.

    Command hooks (``plugin.json``, ``hooks`` key)
    ----------------------------------------------
    External commands, run as subprocesses with event data on stdin and a JSON
    ``HookResult`` on stdout.  Each entry supports ``matcher`` (regex), ``timeout``
    (seconds) and ``command`` (shell string).  This is a *different* mechanism from the
    plugin's own Python -- it is the operator's, not the author's -- and it is the one
    the ``on_prompt_submit`` / ``on_pre_tool`` / ``on_post_tool`` / ``on_turn_end``
    event names below still belong to.
    """

    name: str = ""
    version: str = ""


# The hook payloads live in their own module (the loader should not be the thing a
# plugin imports to *name* a hook event); re-exported here because this is where
# they were defined and where callers and tests look for them.
from agent.plugins.hook_types import (  # noqa: E402  (kept beside their old home)
    HookResult,
    PostToolEvent,
    PreToolEvent,
    SessionEvent,
    TurnEvent,
)

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
#
# **Four of these have a command-hook dispatcher; the rest do not run.**  A hook declared
# in `plugin.json` for, say, `SessionStart` is translated, stored on the plugin's
# metadata, and never invoked -- silently.  That is a pre-existing gap, not one this
# table's last edit introduced, and `tests/test_plugin_hook_dispatch.py` now records
# which names are wired and why the others are not.  Add a name here and that test tells
# you it needs a decision.

# Tool name translation: our tool name → Claude Code tool name.
# Used when matching a Claude Code matcher pattern against our tools.
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


def _plugin_substitution_env(plugin_dir: Path, plugin_name: str) -> dict[str, str]:
    """``${...}`` values for a manifest or MCP config, by every format's spelling."""
    return plugin_formats.placeholder_environment(
        plugin_dir, _plugin_data_dir(plugin_name), Path.cwd()
    )


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


def _merge_hooks_block(
    plugin_dir: Path,
    plugin_name: str,
    source: dict[str, Any],
    into: dict[str, list[dict]],
) -> None:
    """Merge a hooks block through the format that governs *plugin_dir*.

    The translation is the **format's** strategy, not this loader's knowledge: the
    shapes (matcher groups, event vocabulary) are an ecosystem's conventions, and a
    loader that hardcoded them could not accept an ecosystem whose files mean
    something else without a branch here.
    """
    strategy = plugin_formats.strategy_for(plugin_dir)
    for event_name, entries in source.items():
        internal_event, flat = strategy.normalize_hook_entries(
            plugin_name, event_name, entries
        )
        if flat:
            into.setdefault(internal_event, []).extend(flat)


def _read_default_hooks_json(plugin_dir: Path) -> dict[str, list[dict]]:
    """Read each format's default hooks file and translate it.

    ``hooks/hooks.json`` is where both Claude Code and Codex keep hooks that the
    manifest never mentions, so a reader that only looked at the manifest's
    ``hooks`` key let a plugin run shell nobody was asked about.  The file list
    comes from the adapters; a format that keeps hooks elsewhere adds its path
    there rather than here.

    The structure nests once more than ours::

        {"hooks": {"PreToolUse": [{"matcher": "Bash",
                                   "hooks": [{"type": "command", ...}]}]}}

    and ours is flat::

        {"on_pre_tool": [{"matcher": "Bash", "type": "command",
                          "command": ..., "timeout": ...}]}
    """
    out: dict[str, list[dict]] = {}
    for relative in plugin_formats.merged_paths("hooks_files"):
        hooks_file = plugin_dir / relative
        if not hooks_file.exists():
            continue
        try:
            data = json.loads(hooks_file.read_text(encoding="utf-8"))
        except Exception as exc:
            shared.CONSOLE.print(
                f"[yellow]Plugin '{plugin_dir.name}': {relative} unreadable: {exc}[/yellow]"
            )
            continue
        block = data.get("hooks", {})
        if not isinstance(block, dict):
            continue
        _merge_hooks_block(plugin_dir, plugin_dir.name, block, out)
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
    _merge_hooks_block(plugin_dir, plugin_dir.name, source, into)


def _read_mcp_json(plugin_dir: Path, plugin_name: str | None = None) -> list[dict]:
    """Read a plugin root's MCP server file and return its server configs.

    Both spellings of the file are read (see :data:`MCP_MANIFEST_FILES`), and the
    first one that exists wins -- a plugin that ships both is describing one set
    of servers, not two.  Format is ``{"mcpServers": {"name": {...}, ...}}`` — a
    dict keyed by server name.  Returns a flat list of configs each with
    ``"name"`` set.
    """
    mf = next(
        (
            candidate
            for name in plugin_formats.merged_paths("mcp_files")
            if (candidate := plugin_dir / name).exists()
        ),
        None,
    )
    if mf is None:
        return []
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except Exception as exc:
        shared.CONSOLE.print(
            f"[yellow]Plugin '{plugin_dir.name}': {mf.name} unreadable: {exc}[/yellow]"
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
        _merge_hooks_block(plugin_dir, plugin_name, hooks_cfg, raw_hooks)
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
#: The command-hook events this runtime can actually run.  Each one is the internal
#: event name a ``fire_*`` dispatcher passes to ``_run_command_hooks``; nothing else
#: executes a command hook, so this tuple *is* the capability.
#:
#: ``tests/test_plugin_hook_dispatch.py`` reads the dispatched set back out of this
#: module's source and compares it with this tuple, so adding a dispatcher without
#: updating the list -- or listing one that does not dispatch -- fails there rather
#: than at the first plugin whose hook silently never ran.
COMMAND_HOOK_EVENTS: tuple[str, ...] = (
    "on_prompt_submit",
    "on_turn_end",
    "on_pre_tool",
    "on_post_tool",
)


#: The boundary between this runtime's **two ways of observing a turn**, written down
#: once so a plugin author has one place to read.
#:
#: ``ctx.on(Type, handler)`` (``agent.kernel.context``) is this runtime's event API:
#: typed, published on a domain bus, and what an in-tree plugin should use.  It carries
#: the composition's events -- ``ToolPreExecute``, ``TurnCompleted``, ``SessionFinished``
#: -- and it is where a *new* event belongs.
#:
#: The string hooks below are the **cross-ecosystem** surface.  They exist because other
#: plugin formats describe a plugin as a set of named methods (``on_pre_tool``,
#: ``on_turn_end``) and a set of shell commands in a manifest, neither of which can be a
#: typed subscription: the command form runs an external process, and the method form is
#: what an ecosystem's own documentation tells its authors to write.  So the loader
#: translates them, dispatches them, and reports the ones nothing dispatches
#: (:func:`inert_command_hooks`) rather than letting a plugin install a hook that never
#: runs.
#:
#: The two are not being merged, and the reason is not effort: a shell command cannot be
#: a kernel subscription, and a plugin written against another product's documentation
#: must not have to learn this runtime's types.  What they must not do is *drift* -- each
#: entry names the typed event it mirrors, or says why it has none, and
#: ``tests/test_plugin_hook_dispatch.py`` checks both directions.
@dataclass(frozen=True)
class LoaderHook:
    """One string hook the loader dispatches, and the typed event it mirrors."""

    name: str
    #: ``module:ClassName`` a plugin could subscribe to instead, or ``None`` when the
    #: fact has no typed counterpart.
    typed_event: Optional[str]
    #: What the string form adds over the typed one -- read only for the entries whose
    #: ``typed_event`` is ``None``.
    reason: str = ""


LOADER_HOOK_BOUNDARY: tuple[LoaderHook, ...] = (
    LoaderHook(
        name="on_pre_tool",
        typed_event="agent.core.events:ToolPreExecute",
    ),
    LoaderHook(
        name="on_post_tool",
        typed_event="agent.core.events:ToolPostExecute",
    ),
    LoaderHook(
        name="on_turn_end",
        typed_event="agent.core.event_domains:TurnCompleted",
    ),
    LoaderHook(
        name="on_session_end",
        typed_event="agent.core.event_domains:SessionFinished",
    ),
    LoaderHook(
        name="on_prompt_submit",
        typed_event=None,
        reason=(
            "a *gate* rather than an observation: it runs before the turn exists and may "
            "refuse the message, so there is no event to publish -- an event is a fact, "
            "and 'this message was rejected' would be a fact about a turn that never "
            "started.  A code plugin with a typed subscription cannot express it; an "
            "ecosystem plugin does, as a command hook whose exit code refuses"
        ),
    ),
)


def inert_command_hooks(
    hooks_config: Optional[Mapping[str, list[dict]]],
) -> tuple[str, ...]:
    """Declared command-hook events that no dispatcher will ever run, sorted.

    An ecosystem defines the event, this runtime translates and stores it, and
    nothing fires it: the plugin installs, the manifest validates, and the command
    never runs.  That is the silent kind of unsupported -- so the names are
    produced here, where a caller can report them, instead of only being known to a
    test.
    """
    return tuple(
        sorted(
            str(event)
            for event, entries in (hooks_config or {}).items()
            if str(event) not in COMMAND_HOOK_EVENTS
            and any(
                str((entry or {}).get("command") or "").strip()
                for entry in (entries or ())
            )
        )
    )


def _plugin_hook_config(
    plugin_dir: Path, meta: Optional[PluginMeta] = None
) -> dict[str, list[dict]]:
    """Every command hook a plugin declares: the manifest's, plus each default hooks file.

    One function because two readers need the same answer -- the activation report
    and the installer's approval message -- and a ``hooks/hooks.json`` that only one
    of them read would be the silent promise this exists to surface.

    Why hooks are *not* a capability service, unlike skills and MCP configs.  Those two
    were collections the loader kept on a plugin's behalf and the composition root
    attached afterwards, so a plugin's contribution happened in somebody else's hands.
    A hook is different in both directions: it lives on the contributing plugin's own
    :class:`PluginMeta` (there is no central list to drift from) and its only reader is
    the loader, which *dispatches* it -- the firing, the event names and the environment
    are loader behaviour, not a value another implementation could be handed.  A service
    here would be a service whose sole caller is the thing that provides it.
    """
    config: dict[str, list[dict]] = {}
    for event, entries in ((meta.hooks_config if meta is not None else None) or {}).items():
        config.setdefault(str(event), []).extend(entries)
    for event, entries in _read_default_hooks_json(plugin_dir).items():
        config.setdefault(str(event), []).extend(entries)
    return config


def plugin_inert_hooks(plugin_dir: Path) -> tuple[str, ...]:
    """Command-hook events this plugin *directory* declares that nothing will run.

    Read from the directory rather than from a mounted plugin: the installer needs
    the answer before anything is loaded, and for a plugin that may never be.  It
    reads the same two sources the loader does -- the manifest and the format's
    default hooks file -- because a plugin whose hooks live in the file is exactly
    the case the installer used to miss.
    """
    return inert_command_hooks(_plugin_hook_config(plugin_dir, _read_plugin_json(plugin_dir)))


def _read_plugin_json(plugin_dir: Path) -> Optional[PluginMeta]:
    """Read a plugin manifest.  Returns None when no format has one here.

    *Where* a manifest lives is the format adapters' business
    (``agent/plugins/formats.py``): this reads whichever candidate they name
    first, so a new ecosystem is a registration rather than a branch here.

    Accepts both ``mcp_servers`` (snake_case) and ``mcpServers`` (camelCase) --
    the field names are shared even where the locations are not.  Skills default
    to the ``skills`` subdirectory when it exists, so a plugin needs no manifest
    entry to expose its skills.
    """
    pj = next(
        (candidate for candidate in plugin_formats.manifest_candidates(plugin_dir) if candidate.exists()),
        None,
    )
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
    """Whether a supported format keeps a manifest here -- asked of the adapters."""
    return any(
        candidate.exists()
        for candidate in plugin_formats.manifest_candidates(dir_path)
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

    The candidate paths come from the format adapters, in their precedence
    order.  Only local relative plugin sources are expanded; remote marketplace
    entries belong to the installer, not the runtime loader.
    """
    if _has_plugin_manifest(dir_path):
        return None
    mp = next(
        (
            candidate
            for candidate in plugin_formats.marketplace_candidates(dir_path)
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
        for candidate in plugin_formats.marketplace_candidates(dir_path)
    )


async def _maybe_await(value: Any) -> Any:
    """Await value if it is a coroutine, otherwise return it directly."""
    if asyncio.iscoroutine(value):
        return await value
    return value


def _make_markdown_command_handler(
    *,
    strategy: Any,
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
        return strategy.substitute_command_args(body, args_text)

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


# ── What a bundle's files can declare, as data ───────────────────────────────
#
# This used to be a sequence of per-kind blocks in ``_CatalogPlugin`` with a second
# sequence in its ``inject`` property that had to agree with it: a kind that
# contributed through a capability service but forgot to declare it would never run,
# because the service was absent at the moment the plugin activated and nothing
# re-checked.  One record per kind carries both facts, so they cannot disagree, and
# "which kinds exist" is answerable from one place instead of by reading two.
#
# The kinds the *loader* registers rather than a service carry the reason: the same
# decision the design note records (a floor is a decision, not debt).


@dataclass(frozen=True)
class DeclarativeKind:
    """One kind of contribution a plugin directory's files can declare."""

    key: str
    #: The capability service the contribution goes through, or ``None`` for a kind
    #: the loader registers itself (or that is not a contribution at all).
    service: Optional[str]
    #: Whether this bundle declares any of this kind -- what decides the ``inject``
    #: entry.  Only meaningful together with ``service``.
    present: Callable[[Any], bool]
    #: ``(adapter, ctx) -> None``: register what the files declared, reversibly.
    register: Callable[[Any, Any], None]
    #: For the kinds with no service: why the loader is the one that registers it.
    reason: str = ""


def _register_skills(adapter: Any, ctx: Any) -> None:
    """Skill roots, through the capability service, attributed and unwound."""
    for skills_rel in adapter._meta.skills:
        skills_path = (adapter._plugin_dir / skills_rel).resolve()
        if skills_path.is_dir():
            ctx.skill_catalog.add_root(skills_path)


def _register_mcp_servers(adapter: Any, ctx: Any) -> None:
    for mcp_cfg in adapter._mcp_declarations():
        ctx.contributed_mcp_servers.add(mcp_cfg)


def _register_command_files(adapter: Any, ctx: Any) -> None:
    for undo in adapter._catalog._load_command_files(
        adapter._plugin_dir, adapter.name, adapter._meta.commands
    ):
        ctx.on_unwind(undo)


def _register_agent_files(adapter: Any, ctx: Any) -> None:
    for undo in adapter._catalog._load_agent_files(
        adapter._plugin_dir, adapter.name, adapter._meta.agents
    ):
        ctx.on_unwind(undo)


def _merge_hook_files(adapter: Any, ctx: Any) -> None:
    """Merge ``hooks/hooks.json`` and report the events nothing dispatches.

    Merged rather than contributed because the config is the plugin's own ``meta``;
    left un-undone because nothing reads a removed plugin's hooks -- the catalog
    looks them up by name.
    """
    plugin_dir = adapter._plugin_dir
    meta = adapter._meta
    for event_name, entries in _read_default_hooks_json(plugin_dir).items():
        meta.hooks_config.setdefault(event_name, []).extend(entries)
    # Reported *after* the merge above, because a plugin's hooks live in two places
    # and the file is the one the manifest never mentions -- reporting before it would
    # have said "nothing dead" for exactly the plugins whose hooks are all in
    # ``hooks/hooks.json`` (measured while writing this test).
    inert = inert_command_hooks(meta.hooks_config)
    if inert:
        shown = ", ".join(inert[:4]) + ("…" if len(inert) > 4 else "")
        shared.CONSOLE.print(
            f"[dim]Plugin '{adapter.name}': {len(inert)} hook event(s) have no "
            f"dispatcher in this runtime and will not run: {shown}[/dim]"
        )
    if meta.hooks_config:
        # The only thing the tool bridge is for.  See `_ensure_event_bridge`.
        adapter._catalog._ensure_event_bridge()


def _register_python_commands(adapter: Any, ctx: Any) -> None:
    """Slash commands the plugin declares *in Python*, not in markdown files."""
    plugin = adapter._plugin
    if not hasattr(plugin, "register_slash_commands"):
        return
    catalog = adapter._catalog
    for declared, handler in (plugin.register_slash_commands() or {}).items():
        cmd_key = adapter._plugin_command_key(str(declared))
        if catalog._commands.has(cmd_key):
            existing = catalog._commands.owner(cmd_key) or "?"
            shared.CONSOLE.print(
                f"[yellow]Plugin '{adapter.name}': slash command '{cmd_key}' "
                f"conflicts with plugin '{existing}' -- overriding[/yellow]"
            )
        ctx.on_unwind(
            catalog._commands.add(
                cmd_key,
                handler,
                metadata=_command_metadata(handler),
                owner=adapter.name,
            )
        )


DECLARATIVE_KINDS: tuple[DeclarativeKind, ...] = (
    DeclarativeKind(
        key="skills",
        service="skill_catalog",
        present=lambda adapter: bool(adapter._meta.skills),
        register=_register_skills,
    ),
    DeclarativeKind(
        key="mcp_servers",
        service="contributed_mcp_servers",
        present=lambda adapter: bool(adapter._mcp_declarations()),
        register=_register_mcp_servers,
    ),
    DeclarativeKind(
        key="commands",
        service=None,
        present=lambda adapter: bool(adapter._meta.commands),
        register=_register_command_files,
        reason=(
            "the loader's markdown adapter registers a bundle's slash commands while "
            "loading that bundle, and the reload rollback restores the registry from a "
            "snapshot -- so `commands` is a floor and this kind has no inject entry"
        ),
    ),
    DeclarativeKind(
        key="agents",
        service=None,
        present=lambda adapter: bool(adapter._meta.agents),
        register=_register_agent_files,
        reason=(
            "the same floor as `commands`: roles are registered during the load and "
            "restored by the reload rollback"
        ),
    ),
    DeclarativeKind(
        key="hooks",
        service=None,
        present=lambda adapter: bool(adapter._meta.hooks_config),
        register=_merge_hook_files,
        reason=(
            "loader *configuration*, not a contribution: the config sits on the "
            "contributing plugin (there is no central list to drift from) and its only "
            "reader is the loader, which dispatches it -- a service here would have "
            "exactly one caller, the thing that provides it"
        ),
    ),
    DeclarativeKind(
        key="python_commands",
        service=None,
        present=lambda adapter: hasattr(adapter._plugin, "register_slash_commands"),
        register=_register_python_commands,
        reason=(
            "declared on the plugin object rather than in a manifest file; registered "
            "by the loader like the markdown kind, for the same floor reason"
        ),
    ),
)


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
        #: Read once: ``inject`` and the contribution both ask, and reading the files
        #: twice could disagree.
        self._mcp_cache: Any = None

    # -- identity ------------------------------------------------------------

    @property
    def meta(self) -> Any:
        return self._meta

    @property
    def plugin(self) -> Any:
        return self._plugin

    @property
    def inject(self) -> tuple[str, ...]:
        """The plugin's declared dependencies, plus what its **files** need.

        One contribution, one dependency: a bundle that ships skills contributes them
        through ``skill_catalog``, so the kernel has to hold the bundle until that
        service exists -- otherwise the contribution is silently skipped, because the
        adapter's ``setup`` only runs once.  The requirement is read off
        :data:`DECLARATIVE_KINDS` rather than restated here, so a new kind cannot
        contribute through a service it forgot to declare.

        The adapter used to prepend ``assets`` -- a contribution service the catalog
        provided up front -- because gating on ``inject`` cannot express "a service
        provided during your own setup".  That service is gone: measurement showed no
        plugin ever called it (its only caller was this adapter), so a plugin that
        wants to contribute a command or a role declares ``inject = ("commands",)`` or
        ``("agents",)`` and gets the real registry.
        """
        declared = [str(d) for d in (getattr(self._plugin, "inject", ()) or ())]
        for kind in DECLARATIVE_KINDS:
            if kind.service and kind.present(self) and kind.service not in declared:
                declared.append(kind.service)
        return tuple(declared)

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

    def _mcp_declarations(self) -> "list[dict]":
        """The MCP servers this bundle's files declare, manifest entries winning.

        One server, one registration: a manifest that names its MCP file
        (``"mcpServers": "./.mcp.json"`` -- Codex's documented form, and legal in Claude
        Code) used to register the file's contents *and* have the reader find the same
        file, so the same server was offered twice.  Read once and cached, because
        ``inject`` asks whether there is anything to contribute before ``setup`` does it.
        """
        if self._mcp_cache is not None:
            return self._mcp_cache
        declared: list[dict] = []
        for mcp_cfg in self._meta.mcp_servers or ():
            if isinstance(mcp_cfg, dict) and mcp_cfg.get("name"):
                declared.append(mcp_cfg)
        seen = {str(cfg.get("name")) for cfg in declared}
        for mcp_cfg in _read_mcp_json(self._plugin_dir, self.name):
            if not isinstance(mcp_cfg, dict) or not mcp_cfg.get("name"):
                continue
            if str(mcp_cfg["name"]) in seen:
                continue
            seen.add(str(mcp_cfg["name"]))
            declared.append(mcp_cfg)
        self._mcp_cache = declared
        return declared

    def _register_declarative_assets(self, ctx: Any) -> None:
        """Everything the plugin's files declare, each reversible through *ctx*.

        Last before the plugin's own ``setup``, so its registration sees a catalog
        that already holds what its files declared.

        A loop over :data:`DECLARATIVE_KINDS`, not a per-kind sequence: the kinds are
        data, so adding one does not mean editing this method and hoping the ``inject``
        property agrees.  What is *not* uniform lives on the kind -- the capability
        service it goes through, whether this bundle declares it, and (for the kinds
        with no service) the reason the loader is the one that registers it.
        """
        for kind in DECLARATIVE_KINDS:
            kind.register(self, ctx)

        # Bookkeeping for the manifest, not a contribution: the path a diagnostic or
        # an ``installed at`` line reports.
        self._meta.path = self._plugin_dir

    def _plugin_command_key(self, declared: str) -> str:
        """The name a plugin's Python-declared command is registered under.

        A plugin does not own the bare namespace.  Its markdown commands are
        already namespaced by the plugin -- ``<plugin>:<stem>``, derived from the
        path -- and its Python ones have to be too, or two plugins can claim the
        same word: a user plugin declaring ``stats`` took over the built-in
        evolution plugin's ``/stats``, and *which* one won depended on whether it
        had been installed before or after the process started, so the same
        command meant different things before and after a hot reload.  The
        console warning below was the only notice, and a person in a chat UI
        never sees it.

        Built-in plugins are the exception, because they are the app's own
        vocabulary: ``/evolve`` and ``/generate-tool`` are documented in the
        README, and prefixing them would break a documented interface to buy
        consistency it does not need -- a built-in ships with the app and is
        tested with it.

        A key that already carries a namespace is left alone, so an author who
        wants ``mine:cmd`` gets it rather than ``plugin:mine:cmd``.
        """
        if ":" in declared or self._meta.source == "builtin":
            return declared
        return f"{self.name}:{declared}"

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
        service_policy: Any = None,
    ) -> None:
        self._builtin_dir = builtin_dir
        self._user_dir = user_dir
        self._plugin_config = plugin_config or {}
        #: Which names a plugin may touch.  Wired by the composition rather than
        #: defaulted here: the catalog is also built by tests that invent service
        #: names (``greeter``), and a loader that imposed a vocabulary on them would
        #: be imposing the application's.  ``None`` is the permissive embedder path.
        self._service_policy = service_policy
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
        # MCP configs bundled by plugins: list of (plugin_name, server_config_dict)
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
        from agent.kernel.service import ServiceRegistry

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
        #: The composition's answer to "is this name a plugin's to touch", or
        #: ``None`` for an embedder that published no vocabulary.  The kernel asks
        #: this before every plugin read and provide; the kernel itself has no list.
        self._host.service_policy = self._service_policy
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
        #
        # **Resolved from the composition, not from the module.**  A composition that
        # provides its own buses (see ``default_buses``) makes the executor publish on
        # *its* tool bus; a catalog left on the process-wide default then subscribes the
        # ``hooks.json`` bridge to a bus nothing publishes to.  The gate is loaded,
        # reported, and never called -- the failure this codebase warns about most -- and
        # every existing test of the bridge called ``fire_pre_tool`` directly, so none of
        # them could see it.  ``_bus_for`` reads the same names ``Domain.attach`` does.
        self._event_bus = event_bus if event_bus is not None else self._bus_for("tool")
        #: The hook surface: dispatch, matchers, hook environments, the command-hook
        #: subprocess runner and the tool-event bridge.  Handed the **live** plugin
        #: table, so a plugin loaded after this line is dispatched to, and the catalog's
        #: own executor, whose lifetime it releases in :meth:`close`.
        self._hooks = HookDispatcher(
            plugins=self._plugins,
            event_bus=self._event_bus,
            bus_for=self._bus_for,
            executor=self._hook_executor,
            turn_hook_timeout_seconds=self._turn_hook_timeout_seconds,
        )
        # One bus for the composition: the kernel host publishes the typed events a
        # plugin subscribes to through ``ctx.bus``, and the catalog's hook dispatch
        # listens on the same one.  Two buses would mean a plugin's subscription and
        # the thing that fires it could be on different objects -- a listener that
        # is simply never called, which is the failure mode with no symptom.
        self._host.events = self._event_bus
        # The composition owns the domains, so it is the composition that tells the kernel
        # where an event type is published -- see `Context.on`.  This is what makes
        # ``ctx.on(ToolPreExecute, ...)`` land on the tool bus without the plugin having to
        # know that there is more than one.
        from agent.core.event_domains import bus_for_type

        self._host.event_bus_lookup = bus_for_type

    def _bus_for(self, domain: str) -> Any:
        """The bus *domain* is published and subscribed on, in **this** composition.

        The same rule ``Domain.attach`` follows, from the same source: the composition's
        component mapping, which this catalog's registry adopted, names its buses
        ``event_bus.<domain>``.  A catalog that resolves them per call cannot drift from
        the plugins that subscribe through the domain table.

        The fallback asks the domain table for the **process default**
        (``bus_for(domain, None)``) rather than keeping a second table here.  A copy would
        be a list to keep in step with ``event_domains`` -- and the way it goes wrong is
        silent, since a stale copy is still a bus that accepts listeners.
        """
        provided = self._services.get(bus_name(domain), None)
        if provided is not None:
            return provided
        from agent.core.event_domains import bus_for, domain as domain_named

        return bus_for(domain_named(domain))

    def register_core_services(self, components: Any) -> None:
        """Point this catalog at the runtime's container for core services.

        Kept as a call because a caller may hand over a *different* container than the
        mapping this catalog adopted at construction (a reload binds the runtime's
        ``RuntimeComponents``).  When it is the same mapping -- the normal path --
        :meth:`_core` answers from the host registry anyway.

        The docstring here used to claim the two held "different populations, and
        conflating them would let a plugin's service shadow a core one".  That was
        wrong in both directions: the registry **adopts** the component mapping, so
        they are one storage, and "a plugin replaces a service for the runtime too" is
        the documented point of replacement.  What is true is that a plugin cannot take
        a core name by *accident* -- the registry refuses a second ``provide`` without
        ``replace=True`` -- which is a rule about provisioning, not two containers.
        """
        from agent.runtime.contracts import RuntimeComponents

        self._core_services = (
            components
            if isinstance(components, RuntimeComponents)
            else RuntimeComponents(components)
        )

    def _core(self, name: str, default: Any = None) -> Any:
        """One core service, absence tolerated.

        Returns *default* rather than raising, matching the ``components.get`` it
        replaces: every call site branches on a missing value, and a composition
        legitimately has no ``mcp_client``.

        Falls back to this catalog's own registry when no container was handed over.
        That is the same storage -- the registry adopts the component mapping -- so the
        answer is the real one rather than a default.  It used to return *default*
        instead, and since ``register_core_services`` is called only by the reload
        path, every read before the first reload silently answered "absent": a
        composition that had the service, a caller that could not see it, and no error
        either way.
        """
        if self._core_services is not None:
            return self._core_services.lookup(name, default)
        return self._services.get(name, default)

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
        from agent.kernel.service import MissingServiceError

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

        What counts as a plugin, a marketplace and a manifest is the format
        adapters' business (``agent/plugins/formats.py``): native
        ``plugin.json`` with ``__init__.py``, Claude Code's ``.claude-plugin``
        layout with its convention subdirectories, Codex's two manifest
        locations, and whichever marketplace files those formats declare -- each
        expanded into its own plugin load.

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

        # (The step that fired a Python ``on_session_end`` here is gone: no plugin
        # implements it, and the ordering concern it existed for -- "a hook that cannot
        # see its own resources cannot release them" -- is the kernel's own sequence
        # below, which runs `teardown` before the scope unwinds.)

        # The kernel takes the tree down: dependents first, then the plugin's
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
            # Drop the cached bytecode for this file before importing it.
            #
            # Python validates a `.pyc` against the source's **mtime and size**, so an edit
            # that keeps the same size and lands in the same clock tick is invisible: the
            # stale bytecode is loaded and the plugin runs its old code, with no error and
            # nothing to see.  Measured, twice, on a plugin whose only change was a string
            # literal: `'v1' -> 'v2'` kept `v1`, `'v1' -> 'v2-longer'` took effect.
            #
            # Unconditionally, not only on reload: the window is narrow but a hot-reload
            # feature that silently keeps old code is worse than one that never claimed to
            # reload.  The cost is recompiling one file, and the module name is already
            # namespaced, so nothing else is compiling it.
            try:
                stale = importlib.util.cache_from_source(str(init_file))
                Path(stale).unlink(missing_ok=True)
            except (OSError, NotImplementedError, ValueError):
                pass
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

    def core_plugin_selection(self) -> tuple[str, ...] | None:
        """Which of the composition's own plugins this profile includes.

        Read here rather than in the composition root so the profile is resolved in one
        place.  ``None`` means "all of them" -- the default a deployment gets when it says
        nothing, and the behaviour every existing composition has.
        """
        return self._plugin_profile.core

    def service_replacements(self) -> dict[str, str]:
        """Service name -> the plugin the profile says provides it instead."""
        return dict(self._plugin_profile.service_replacements)

    def who_provides(self, service: str) -> str | None:
        """The plugin whose ``setup`` provided *service*, or ``None``.

        Read off the installs rather than guessed, so the composition can check a declared
        replacement instead of trusting it.
        """
        for name, install in self._installs.items():
            if service in install.provided_names:
                return name
        return None

    def flush_pending(self) -> tuple[str, ...]:
        """Re-check the composition's waiters.  See ``PluginHost.flush_pending``."""
        return self._host.flush_pending()

    def failed_report(self) -> dict[str, str]:
        """Plugins that were refused, by name, with the error.

        The counterpart to :meth:`pending_report`, and the half that was missing: a plugin
        whose ``setup`` raised is in neither ``loaded`` nor ``pending``, so a composition
        that lost one to an exception looked exactly like one that had never been given it.
        """
        return self._host.failed()

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
                strategy=plugin_formats.strategy_for(plugin_dir),
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
        # The bridge first, so nothing routes a tool call into a hook chain while the
        # plugins are coming down.
        self._hooks.close()
        # Then the plugins themselves.  Without this, closing a catalog released its own
        # subscriptions and nothing else: a plugin's `TOOL.attach` stayed live, so a
        # **closed** catalog went on gating tools through plugins it no longer owned.
        # Found by asking who was still listening on the process-wide tool bus after a
        # catalog was closed -- the answer was a plugin the catalog had loaded.
        self._host.unload_all()

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
        # Through ``_core``, like ``mcp_client``/``registry`` just below: the reload
        # path's services come from the *components mapping the caller hands over*
        # (bound by ``register_core_services``), not from this catalog's own registry.
        mcp_registry = self._core("contributed_mcp_servers")
        old_mcp = {
            (owner, cfg.get("name", "?"))
            for owner, cfg in (
                mcp_registry.records() if mcp_registry is not None else ()
            )
        }
        old_discovery_state = {
            "plugins": dict(self._plugins),
            # One snapshot, because the registry holds handler, metadata and owner
            # together.  The reload rollback used to restore three dicts separately,
            # which is three chances to restore two of them.
            "slash_commands": self._commands.snapshot(),

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
                self._plugin_source_dirs = old_discovery_state[
                    "plugin_source_dirs"
                ]
                self._agent_defs.restore(old_discovery_state["agent_defs"])
                raise

        # Re-read the skill catalog: the plugins that bundled skills were unloaded
        # above and re-discovered below, so their roots were taken back and attached
        # again by their own scopes -- this is only the intrinsic layers, plus a replay
        # of whatever records are attached.  It used to walk the loader's own list of
        # plugin skill paths and poke ``_rebuild_aliases``/``_dirty``/``_prompt_generation``
        # directly, with a fallback branch for catalogs that lacked them.
        skill_catalog = self._core("skill_catalog")
        if skill_catalog is not None:
            try:
                skill_catalog.reload()
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
            cfg
            for owner, cfg in (
                mcp_registry.records() if mcp_registry is not None else ()
            )
            if (owner, cfg.get("name", "?")) not in old_mcp
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

        # No re-fire of a session-start signal: `discover_and_load` re-instantiates
        # every plugin, and the host runs each one's `setup` again -- so a plugin reads
        # the composition anew.  The old signal existed because the hook protocol handed
        # plugins their dependencies *once*, at a moment the reload had to reproduce by
        # hand.
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


    # ── Hook dispatch ────────────────────────────────────────────────────────
    # The dispatch machinery -- the five dispatchers, the matchers, the hook
    # environments, the subprocess runner and the tool-event bridge -- lives in
    # :mod:`agent.plugins.hook_dispatch`.  The loader had grown to own discovery, the
    # registries, the reload transaction *and* this, and the hook surface is the one
    # piece with no dependency on loading at all: it reads a plugin table and runs
    # things.
    #
    # These are delegates rather than removals because three of them are on
    # ``PluginCatalogService`` and every in-tree caller uses the catalog: the boundary
    # can move without moving every caller, and the protocol stays the loader's.

    def _ensure_event_bridge(self) -> None:
        self._hooks.ensure_event_bridge()

    async def fire_prompt_submit(
        self, text: str, metadata: Optional[dict] = None
    ) -> HookResult:
        return await self._hooks.fire_prompt_submit(text, metadata)

    async def fire_turn_end(self, event: TurnEvent) -> list[HookResult]:
        return await self._hooks.fire_turn_end(event)

    async def fire_session_end(self, event: SessionEvent) -> None:
        return await self._hooks.fire_session_end(event)

    async def fire_pre_tool(self, event: PreToolEvent) -> HookResult:
        return await self._hooks.fire_pre_tool(event)

    async def fire_post_tool(self, event: PostToolEvent) -> HookResult:
        return await self._hooks.fire_post_tool(event)
