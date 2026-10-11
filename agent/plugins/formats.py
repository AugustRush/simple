"""One ecosystem = one adapter.  Nothing else in the loader knows a format.

Why this exists
---------------
The loader learned Claude Code's layout, then Codex's, then a second Codex
layout, and each time the knowledge went *into* it: a manifest-candidate list
here, a marketplace list there, an event-name table, an environment-name pair, a
rule about what counts as executable content.  Adding a fourth ecosystem meant
editing the loader in six places and the installer in two more -- and the two
drifted, which is how a plugin the runtime would load became one the installer
refused.

So the knowledge lives here instead, as data: where a format keeps its manifest,
where its marketplaces are, what its hook events are called, which environment
names its hook commands expect, which files are code.  The loader asks
:func:`manifest_candidates`, :func:`all_hook_events` and friends and never tests
a directory name itself.  A new ecosystem is one :class:`PluginFormat` literal
in this module plus the tests for it -- and, for an embedder, one
:func:`register_format` call.

What is *not* per-format
------------------------
The asset conventions (``skills/``, ``commands/``, ``agents/``) and the manifest
field names (``name``/``version``/``description``/``skills``/``mcpServers`` and
the snake_case aliases) are shared, because in practice they are: Claude Code,
Codex and this project's own manifest all read the same keys.  A format is what
differs, not what is spelled the same.

Order is precedence
-------------------
The tuple is ordered, and the order is the answer when a directory ships two
manifests or two marketplace files.  :data:`PLUGIN_FORMATS` is Codex, then
Claude Code, then this project's own, which is what the loader resolved before
this module existed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

__all__ = [
    "PluginFormat",
    "PLUGIN_FORMATS",
    "register_format",
    "manifest_candidates",
    "marketplace_candidates",
    "format_for_manifest",
    "all_hook_events",
    "hook_environment",
    "placeholder_environment",
    "looks_like_plugin",
    "executable_content",
    "CONVENTION_DIRS",
    "STANDALONE_SKILL_FILES",
    "merged_paths",
    "CLAUDE_CODE_HOOK_EVENTS",
    "CODEX_HOOK_EVENTS",
]

#: Asset directories every supported format uses the same way.  A format may
#: override one by declaring its own path; none does today.
CONVENTION_DIRS: Mapping[str, tuple[str, ...]] = {
    "skills": ("skills",),
    "commands": ("commands",),
    "agents": ("agents",),
}

#: A directory holding one of these at its root *is* a skill, without a
#: ``skills/`` directory.  This project reads SKILL.md; so does everyone else.
STANDALONE_SKILL_FILES: tuple[str, ...] = ("SKILL.md",)


@dataclass(frozen=True)
class PluginFormat:
    """One ecosystem's on-disk conventions.

    ``manifests``, ``marketplaces`` and the marker paths are relative and
    ordered: the first one that exists wins, and the first *format* that matches
    wins over the formats after it in :data:`PLUGIN_FORMATS`.

    ``hook_events`` translates that ecosystem's hook event names to this
    project's internal ``on_*`` names.  A name that is already internal passes
    through untouched, which is why this project's own manifest needs no entry.

    The ``*_environment`` tuples name the shell variables a hook command and a
    ``${...}`` placeholder may use for the plugin root, its data directory, and
    the project directory.  Claude Code spells them ``CLAUDE_*`` and Codex spells
    two of them ``PLUGIN_ROOT``/``PLUGIN_DATA``; both are set, because a plugin
    written for either has to find its own spelling.
    """

    key: str
    label: str
    manifests: tuple[str, ...] = ()
    marketplaces: tuple[str, ...] = ()
    hooks_files: tuple[str, ...] = ()
    mcp_files: tuple[str, ...] = ()
    #: Files that mark a directory as a plugin for the *installer*, when no
    #: manifest or marketplace is present (``__init__.py``, an asset directory).
    markers: tuple[str, ...] = ()
    #: Files whose presence means the plugin ships code that will run.
    executable_files: tuple[str, ...] = ()
    #: Directories whose presence means the same (a hook script directory).
    executable_dirs: tuple[str, ...] = ()
    #: Manifest keys whose presence means the same.
    executable_keys: tuple[str, ...] = ()
    hook_events: Mapping[str, str] = field(default_factory=dict)
    root_environment: tuple[str, ...] = ()
    data_environment: tuple[str, ...] = ()
    project_environment: tuple[str, ...] = ()


#: Claude Code's hook event vocabulary, as published.  Names this project has no
#: counterpart for are still translated and stored: dropping one would turn
#: "this hook does nothing yet" into "this hook is not recognised", which is a
#: different and louder failure.  ``tests/test_plugin_hook_dispatch.py`` owns the
#: list of which translated names actually have a dispatcher.
CLAUDE_CODE_HOOK_EVENTS: Mapping[str, str] = {
    "SessionStart": "on_session_start",
    "SessionEnd": "on_session_end",
    "Setup": "on_setup",
    "UserPromptSubmit": "on_prompt_submit",
    "UserPromptExpansion": "on_prompt_expansion",
    "PreToolUse": "on_pre_tool",
    "PostToolUse": "on_post_tool",
    "PostToolUseFailure": "on_post_tool_failure",
    "PostToolBatch": "on_post_tool_batch",
    "PermissionRequest": "on_permission_request",
    "PermissionDenied": "on_permission_denied",
    "Stop": "on_turn_end",
    "StopFailure": "on_turn_failure",
    "PreCompact": "on_pre_compact",
    "PostCompact": "on_post_compact",
    "Notification": "on_notification",
    "MessageDisplay": "on_message_display",
    "SubagentStart": "on_subagent_start",
    "SubagentStop": "on_subagent_stop",
    "TaskCreated": "on_task_created",
    "TaskCompleted": "on_task_completed",
    "ConfigChange": "on_config_change",
    "CwdChanged": "on_cwd_changed",
    "FileChanged": "on_file_changed",
    "InstructionsLoaded": "on_instructions_loaded",
    "WorktreeCreate": "on_worktree_create",
    "WorktreeRemove": "on_worktree_remove",
    "TeammateIdle": "on_teammate_idle",
    "Elicitation": "on_elicitation",
    "ElicitationResult": "on_elicitation_result",
}

#: Codex runs hook commands with these names, and its documentation writes
#: ``"$PLUGIN_ROOT/hooks/x.sh"``.  Setting only the Claude Code spelling made
#: every such hook expand to an empty path.
CODEX_HOOK_EVENTS: Mapping[str, str] = {
    name: CLAUDE_CODE_HOOK_EVENTS[name]
    for name in (
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
    )
}

_MCP_KEYS: tuple[str, ...] = ("mcpServers", "mcp_servers")
_HOOK_KEYS: tuple[str, ...] = ("hooks",)

NATIVE = PluginFormat(
    key="native",
    label="simple plugin.json",
    manifests=("plugin.json",),
    # This project's own marketplace spelling, still supported.
    marketplaces=("marketplace.json",),
    markers=("__init__.py",),
    executable_files=("__init__.py",),
    executable_keys=_MCP_KEYS + _HOOK_KEYS,
    root_environment=("CLAUDE_PLUGIN_ROOT",),
    data_environment=("CLAUDE_PLUGIN_DATA",),
    project_environment=("CLAUDE_PROJECT_DIR",),
)

CLAUDE_CODE = PluginFormat(
    key="claude-code",
    label="Claude Code plugin",
    manifests=(".claude-plugin/plugin.json",),
    marketplaces=(".claude-plugin/marketplace.json",),
    hooks_files=("hooks/hooks.json",),
    mcp_files=(".mcp.json",),
    markers=("skills", "commands", "agents"),
    executable_files=(".mcp.json", "hooks/hooks.json"),
    # ``.claude-plugin/hooks/`` is Claude Code's other home for hook scripts.
    executable_dirs=(".claude-plugin/hooks",),
    executable_keys=_MCP_KEYS + _HOOK_KEYS,
    hook_events=CLAUDE_CODE_HOOK_EVENTS,
    root_environment=("CLAUDE_PLUGIN_ROOT",),
    data_environment=("CLAUDE_PLUGIN_DATA",),
    project_environment=("CLAUDE_PROJECT_DIR",),
)

CODEX = PluginFormat(
    key="codex",
    label="Codex plugin",
    # The legacy ``.codex-plugin`` overlay first, then Agent Plugins v1's root
    # manifest.  Codex itself prefers the root manifest *when* it carries the
    # portable ``$schema`` and falls back to the overlay otherwise; both parse
    # with the same field names here and either one loads, so the flat order is
    # the overlay first -- which is what this loader did before the split -- and
    # the difference is which file supplies name/version when a plugin ships
    # both.  ``mcp_files``/``hooks_files`` are read by location, not by which
    # manifest won, so no component is lost either way.
    manifests=(".codex-plugin/plugin.json", "plugin.json"),
    marketplaces=(
        ".agents/plugins/marketplace.json",
        ".agents/plugins/api_marketplace.json",
    ),
    hooks_files=("hooks/hooks.json",),
    # ``.mcp.json`` is the legacy default, ``mcp.json`` the Agent Plugins one.
    mcp_files=(".mcp.json", "mcp.json"),
    markers=("skills", "commands", "agents"),
    executable_files=(".mcp.json", "mcp.json", "hooks/hooks.json"),
    executable_keys=_MCP_KEYS + _HOOK_KEYS,
    hook_events=CODEX_HOOK_EVENTS,
    root_environment=("PLUGIN_ROOT", "CODEX_PLUGIN_ROOT"),
    data_environment=("PLUGIN_DATA", "CODEX_PLUGIN_DATA"),
    project_environment=("CODEX_PROJECT_DIR",),
)

#: Ordered: first match wins.  Codex before Claude Code before this project's own
#: is the precedence the loader had before this module existed.
PLUGIN_FORMATS: list[PluginFormat] = [CODEX, CLAUDE_CODE, NATIVE]


def register_format(fmt: PluginFormat, *, position: int | None = None) -> None:
    """Add an ecosystem, or replace the entry with the same key.

    The loader reads this list on every call rather than caching it, so an
    embedder -- or a test that wants a fourth ecosystem -- plugs in without
    touching any other module.  ``position`` puts it earlier in precedence;
    the default appends it.
    """
    for index, existing in enumerate(PLUGIN_FORMATS):
        if existing.key == fmt.key:
            PLUGIN_FORMATS[index] = fmt
            return
    if position is None:
        PLUGIN_FORMATS.append(fmt)
    else:
        PLUGIN_FORMATS.insert(position, fmt)


def merged_paths(attribute: str) -> tuple[str, ...]:
    """Every format's ``attribute`` paths, de-duplicated, in precedence order."""
    return _relative_paths(attribute)


def _relative_paths(attribute: str) -> tuple[str, ...]:
    seen: list[str] = []
    for fmt in PLUGIN_FORMATS:
        for relative in getattr(fmt, attribute):
            if relative not in seen:
                seen.append(relative)
    return tuple(seen)


def manifest_candidates(root: Path) -> tuple[Path, ...]:
    """Every place a supported format keeps a manifest, in precedence order."""
    return tuple(root / relative for relative in _relative_paths("manifests"))


def marketplace_candidates(root: Path) -> tuple[Path, ...]:
    """Every place a supported format keeps a marketplace manifest."""
    return tuple(root / relative for relative in _relative_paths("marketplaces"))


def format_for_manifest(path: Path) -> Optional[PluginFormat]:
    """The format whose ``manifests`` entry names *path*, if any."""
    name = path.as_posix()
    for fmt in PLUGIN_FORMATS:
        for relative in fmt.manifests:
            if name.endswith(relative):
                return fmt
    return None


def all_hook_events() -> dict[str, str]:
    """The merged event vocabulary, later formats filling in earlier gaps."""
    merged: dict[str, str] = {}
    for fmt in PLUGIN_FORMATS:
        for external, internal in fmt.hook_events.items():
            merged.setdefault(external, internal)
    return merged


def hook_environment(
    plugin_root: Path, data_dir: Path, project_dir: Path
) -> dict[str, str]:
    """The shell environment a hook command gets, by every format's spelling."""
    env: dict[str, str] = {}
    for fmt in PLUGIN_FORMATS:
        for name in fmt.root_environment:
            env[name] = str(plugin_root)
        for name in fmt.data_environment:
            env[name] = str(data_dir)
        for name in fmt.project_environment:
            env[name] = str(project_dir)
    return env


def placeholder_environment(
    plugin_root: Path, data_dir: Path, project_dir: Path
) -> dict[str, str]:
    """``${...}`` names a manifest or MCP config may use, by every spelling."""
    return hook_environment(plugin_root, data_dir, project_dir)


def looks_like_plugin(root: Path) -> bool:
    """Whether *root* holds anything the loader would recognise as a plugin.

    The installer asks this, and it has to accept **every** layout the loader
    does: a plugin the runtime would happily load but the installer refuses is a
    compatibility bug with no upside.
    """
    for candidate in manifest_candidates(root):
        if candidate.exists():
            return True
    for candidate in marketplace_candidates(root):
        if candidate.exists():
            return True
    for fmt in PLUGIN_FORMATS:
        for marker in fmt.markers:
            if (root / marker).exists():
                return True
    for name in STANDALONE_SKILL_FILES:
        if (root / name).is_file():
            return True
    for directories in CONVENTION_DIRS.values():
        for directory in directories:
            if (root / directory).is_dir():
                return True
    return False


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def executable_content(
    root: Path,
    *,
    describe: Optional[Callable[[str], str]] = None,
) -> str:
    """Describe the code this plugin would run, or "" when it is declarative.

    Every format's own idea of "this is code" is checked: a plugin ships what its
    ecosystem says it ships, not what this project happens to look for.  A gate
    that misses a location is worse than no gate, because it reports consent that
    was never asked for.
    """
    for fmt in PLUGIN_FORMATS:
        for relative in fmt.executable_files:
            if (root / relative).exists():
                return describe(relative) if describe else relative
        for relative in fmt.executable_dirs:
            directory = root / relative
            if directory.is_dir() and any(directory.iterdir()):
                return describe(relative) if describe else relative
        for relative in fmt.manifests:
            manifest = root / relative
            if not manifest.is_file():
                continue
            data = _read_json(manifest)
            for key in fmt.executable_keys:
                if data.get(key):
                    return describe(f"{relative}:{key}") if describe else key
    return ""
