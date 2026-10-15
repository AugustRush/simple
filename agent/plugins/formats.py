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

User-level skill roots
---------------------
``user_skill_roots`` is why the skill catalog does not hard-code one directory.
It is *not* symmetric across ecosystems, and pretending it were would be the
invention this module exists to avoid: Codex's user root is ``~/.codex/skills``
(its own home), Claude Code's is not listed here because this project has no
evidence for it, and ``~/.agents/skills`` is the vendor-neutral one DSH reads
alongside its ``~/.dsh/skills``.  The neutral pair lives in
:data:`SHARED_SKILL_ROOTS` rather than on a format, because no *plugin directory*
format owns it.

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
import os
from dataclasses import dataclass, field

from agent.runtime.provenance import Execution
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
    "executions",
    "CONVENTION_DIRS",
    "STANDALONE_SKILL_FILES",
    "merged_paths",
    "SkillRoot",
    "user_skill_roots",
    "shared_skill_roots",
    "SHARED_SKILL_ROOTS",
    "CLAUDE_CODE_HOOK_EVENTS",
    "CODEX_HOOK_EVENTS",
    "default_normalize_hook_entries",
    "default_translate_hook_output",
    "default_substitute_command_args",
    "strategy_for",
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
class SkillRoot:
    """One user-level skill root an ecosystem reads.

    ``system_dir`` names the subtree that holds the **ecosystem's own** bundled
    skills rather than the user's, and is skipped when the root is scanned.  Both
    ecosystems that have one call it ``.system``, and both were checked in their
    own source rather than guessed: Codex installs its embedded skills into
    ``CODEX_HOME/skills/.system`` and *rewrites that directory on startup*, and
    DSH marks its own home root ``skipSystem: true``.  Loading those into this
    agent's prompt would be adopting another product's instructions, which is not
    what "the user has these skills" means.
    """

    label: str
    path: str
    system_dir: str = ""


# ── The strategies: what an ecosystem's files *mean* ─────────────────────────
#
# These three were functions in the loader, and their bodies were Claude Code's
# conventions applied to every ecosystem -- so "one ecosystem = one adapter" held for
# the *paths* and not for the shapes.  Moved here and hung off :class:`PluginFormat`
# as overridable callables: the loader now asks the format instead of knowing.
#
# The defaults are this project's compatibility baseline.  It is Claude Code's shape
# (matcher groups around command hooks, ``$ARGUMENTS`` in a command body, the hook
# JSON output contract) because that is what Codex's plugin format also speaks for the
# names it has -- but a format whose files mean something else supplies its own
# callable, and the loader does not change.


def _timeout_or(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def default_normalize_hook_entries(
    plugin_name: str, event_name: str, entries: Any
) -> "tuple[str, list[dict]]":
    """Translate an event's declared hooks into flat internal entries.

    The **event name** is looked up in the merged vocabulary rather than the format's
    own table: asked live, not snapshotted at import, so a format registered later (an
    embedder's, or a test's) brings its event vocabulary with it.  A name this project
    has no counterpart for passes through unchanged, so "this hook does nothing yet"
    stays distinguishable from "this hook is not recognised".

    The **entries** may be Claude Code's matcher groups (``{"matcher": ...,
    "hooks": [...]}``) or already-flat dicts; both are accepted, because both spellings
    appear in the wild and refusing the flat one would break a project's own manifest.
    """
    internal_event = all_hook_events().get(event_name, event_name)
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
                normalized["timeout"] = _timeout_or(inner.get("timeout", 60.0), 60.0)
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
        normalized["timeout"] = _timeout_or(entry.get("timeout", 60.0), 60.0)
        flat.append(normalized)
    return internal_event, flat


def default_translate_hook_output(raw: dict, event_name: str) -> Any:
    """Translate a command hook's JSON output into this runtime's ``HookResult``.

    The default reads the hook output contract the supported ecosystems share:
    ``continue: false`` stops a turn, ``decision: "block"`` refuses, and
    ``hookSpecificOutput.additionalContext`` injects context.  A format that answers
    some other way overrides this and returns its own ``HookResult`` (or ``None`` for
    "no effect").

    ``HookResult`` is imported inside the function because it lives in the loader,
    which imports this module: a module-level import would be a cycle for the sake of
    one name.
    """
    from agent.plugins.catalog import HookResult

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


def default_substitute_command_args(body: str, args_text: str) -> str:
    """Replace ``$ARGUMENTS`` and ``$1`` .. ``$N`` in a command body.

    ``args_text`` is the full string after the command name (so quoting survives);
    positional placeholders use whitespace-split tokens.
    """
    if not body:
        return body
    out = body.replace("$ARGUMENTS", args_text)
    tokens = args_text.split()
    # Highest index first, so $10 is not mangled by $1's substitution.
    for i in range(len(tokens), 0, -1):
        out = out.replace(f"${i}", tokens[i - 1])
    return out


@dataclass(frozen=True)
class PluginFormat:
    """One ecosystem's on-disk conventions, **and what to do with them**.

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
    #: Where this ecosystem keeps **user-level** skills.  Read by the skill catalog
    #: through :func:`user_skill_roots`: a skill the user installed for another
    #: agent is a skill the user has, and *which directory that is* is the
    #: ecosystem's convention -- the same reason manifests and hook events live here
    #: instead of in the loader.
    #:
    #: Plugin-*bundled* skills are a different thing and are not listed here: those
    #: arrive with a plugin directory and the loader already loads them.
    user_skill_roots: tuple["SkillRoot", ...] = ()
    # -- what the files *mean*, overridable per ecosystem ---------------------
    #: ``(plugin_name, event_name, entries) -> (internal_event, flat_entries)``.
    normalize_hook_entries: Callable[[str, str, Any], "tuple[str, list[dict]]"] = (
        default_normalize_hook_entries
    )
    #: ``(raw_output, event_name) -> HookResult | None``.
    translate_hook_output: Callable[[dict, str], Any] = default_translate_hook_output
    #: ``(body, args_text) -> str`` for a markdown command's placeholders.
    substitute_command_args: Callable[[str, str], str] = default_substitute_command_args


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
    # No user skill root: this project's own is ``<resource_root>/skills``, which
    # the catalog scans as its primary root (see agent/skills/catalog.py).  Listing
    # it here would scan it twice under a second name.
    executable_keys=_MCP_KEYS + _HOOK_KEYS,
    root_environment=("CLAUDE_PLUGIN_ROOT",),
    data_environment=("CLAUDE_PLUGIN_DATA",),
    project_environment=("CLAUDE_PROJECT_DIR",),
)

#: The vendor-neutral skill roots: no *plugin directory* format owns these, and
#: more than one ecosystem reads them.  ``~/.agents/skills`` is the one DSH calls
#: ``user-agents`` (its ``agentsHome`` is ``$DSH_AGENTS_HOME`` or ``~/.agents``,
#: read from its own source); ``~/.dsh/skills`` is DSH's own user root.  Codex
#: reads ``.agents/skills`` at the *project* level, which is a different root and
#: is not scanned here.
SHARED_SKILL_ROOTS: tuple[SkillRoot, ...] = (
    # No ``system_dir``: DSH reads this one without ``skipSystem``.
    SkillRoot(label="agents", path="~/.agents/skills"),
    SkillRoot(label="dsh", path="~/.dsh/skills", system_dir=".system"),
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
    # No ``user_skill_roots``: this project has no evidence for Claude Code's
    # user-level skills directory, and inventing one would make the catalog scan a
    # path nobody verified.  Add it here (one tuple) when there is evidence.
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
    # ``<codex_home>/skills``, with ``codex_home`` defaulting to ``~/.codex``
    # (read from the Codex source this loader was written against).  ``.system``
    # is Codex's own embedded-skill cache -- see :class:`SkillRoot`.
    user_skill_roots=(
        SkillRoot(label="codex", path="~/.codex/skills", system_dir=".system"),
    ),
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


def user_skill_roots() -> tuple[SkillRoot, ...]:
    """Every user-level skill root the known ecosystems read, path expanded.

    Read by the skill catalog, which otherwise has to know that Codex keeps skills
    in ``~/.codex`` and DSH in ``~/.agents``.  Order is the declaration order of
    :data:`PLUGIN_FORMATS`, then :data:`SHARED_SKILL_ROOTS`, and the **first**
    occurrence of a path wins: ``~/.agents/skills`` is the vendor-neutral one and
    scanning it once is the point, not once per ecosystem that reads it.  A format
    that names it again contributes nothing (its ``system_dir`` is not merged in,
    because the first declaration is the one with evidence behind it).

    Paths are expanded here rather than at import time, so a test (or a
    ``--name`` home switch) changes what they resolve to.  ``DSH_AGENTS_HOME`` is
    honoured for the agents root, because DSH honours it.
    """
    seen: dict[str, SkillRoot] = {}
    for fmt in PLUGIN_FORMATS:
        for root in fmt.user_skill_roots:
            seen.setdefault(root.path, root)
    for root in SHARED_SKILL_ROOTS:
        seen.setdefault(root.path, root)
    resolved: list[SkillRoot] = []
    for template, root in seen.items():
        path = template
        if template == "~/.agents/skills":
            agents_home = os.environ.get("DSH_AGENTS_HOME", "").strip()
            if agents_home:
                path = str(Path(agents_home).expanduser() / "skills")
        resolved.append(
            SkillRoot(
                label=root.label,
                path=str(Path(path).expanduser()),
                system_dir=root.system_dir,
            )
        )
    return tuple(resolved)


def shared_skill_roots() -> tuple[SkillRoot, ...]:
    """The subset of :func:`user_skill_roots` that exists on this machine.

    Existence is checked here because the catalog's ``_load_root`` tolerates a
    missing directory silently, and a caller that wants to *report* what it read
    cannot tell "absent" from "empty" after the fact.
    """
    return tuple(root for root in user_skill_roots() if Path(root.path).is_dir())


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


def strategy_for(plugin_dir: Optional[Path]) -> PluginFormat:
    """The format whose **strategies** govern *plugin_dir*.

    The loader's question, and a different one from :func:`format_for_manifest`: it
    is asked while reading a directory's hook and command files, where there is no
    manifest path to match.  Resolution order is the format list's own (which is the
    precedence order the loader already documents), then :data:`NATIVE` -- so a
    directory no ecosystem claimed is handled by this project's own conventions,
    which is exactly what happened before the strategies existed.

    Returning a format rather than ``None`` is the point: every caller wants
    *something* to ask, and a ``None`` branch at each call site is how a default
    quietly diverges from the real one.
    """
    if plugin_dir is not None:
        for fmt in PLUGIN_FORMATS:
            if any((plugin_dir / relative).exists() for relative in fmt.manifests):
                return fmt
    return NATIVE


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

    **Every** location, not the first one.  This returned as soon as one marker matched,
    which is the class of miss its own docstring calls worse than no gate: a plugin with a
    Python entry point *and* two MCP servers asked for consent to run one of the three
    things it ships.  Behaviour for a single-executable plugin is unchanged, which is what
    the installer's existing tests pin.
    """
    found = executions(root)
    if not found:
        return ""
    markers = [str(item.payload.get("marker", "")) for item in found]
    if describe is None:
        return "; ".join(markers)
    return "; ".join(describe(marker) for marker in markers)


#: How a file or manifest key is classified.  Explicit rather than inferred from the
#: suffix, because "this is code" is the ecosystem's claim and this is where the claim is
#: translated into a mechanism a person can be asked about.
def _execution_kind(marker: str) -> str:
    if marker.endswith(".py"):
        return "python-entry"
    if ":" in marker:
        _, _, key = marker.partition(":")
        return "mcp-stdio" if key in _MCP_KEYS else "hook-command"
    if "mcp" in marker:
        return "mcp-stdio"
    if "hook" in marker:
        return "hook-command"
    return "executable-file"


def _mcp_servers(data: Any) -> list[dict[str, Any]]:
    """The server entries of an MCP declaration, in either shape this tree accepts.

    Claude Code's files and this project's own manifest agree on the mapping form
    (``{"mcpServers": {"name": {"command": ..., "args": [...]}}}``); the list form
    (``[{"name": ..., "command": ...}]``) is what a plugin manifest in this tree may also
    write.  Both are read, because a gate that understands one of the two spellings of the
    same thing is how this file's docstring describes a miss.
    """
    entries: list[dict[str, Any]] = []
    if isinstance(data, dict):
        for name, config in data.items():
            entry = dict(config) if isinstance(config, dict) else {}
            entry.setdefault("name", name)
            entries.append(entry)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                entries.append(dict(item))
    return entries[:20]


def _hook_commands(data: Any, *, limit: int = 20) -> list[str]:
    """Every ``command`` string in a hooks declaration, however it is nested.

    Walked rather than addressed by path: both ecosystems nest command hooks two or three
    levels deep and neither promises the shape, so a fixed path would read nothing on the
    other one -- and "nothing" would render as an empty promise on the confirmation the
    user is answering.
    """
    found: list[str] = []

    def walk(node: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(node, dict):
            command = node.get("command")
            if isinstance(command, str) and command:
                found.append(command)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def executions(root: Path) -> tuple[Execution, ...]:
    """Everything this plugin would run, from the formats' own markers.

    The typed half of the installer's question.  ``executable_content`` says *that* there
    is code; this says what it is and, where the plugin's own files spell it out, the
    concrete command -- which is what a person being asked for consent is actually being
    asked about.

    Payloads carry ``marker`` as well as the concrete fields, so the installer's existing
    renderer has one input instead of a second scan of the same files: one truth, one
    description, no way for the two to disagree.

    Order is the formats' declaration order, deduplicated by marker: the same ``.mcp.json``
    is declared by more than one ecosystem, and a plugin that ships it runs one server, not
    one per format that knows the filename.
    """
    seen: set[str] = set()
    found: list[Execution] = []

    def add(marker: str, payload: dict[str, Any]) -> None:
        if marker in seen:
            return
        seen.add(marker)
        found.append(
            Execution(
                kind=_execution_kind(marker),
                payload={"marker": marker, **payload},
            )
        )

    def payload_for(relative: str, path: Path) -> dict[str, Any]:
        """What a marker's file actually runs, when its content says so.

        One pass, because the alternative -- adding a bare marker here and rewriting that
        entry later when the convention-files loop reaches it -- is two places that have to
        agree about what one entry is.  A file that is not readable JSON keeps the path,
        which is still the truthful answer: the thing exists and will run.
        """
        payload: dict[str, Any] = {"path": str(path)}
        if path.suffix == ".py":
            return payload
        data = _read_json(path) if path.suffix == ".json" else {}
        if not data:
            return payload
        if "hook" in relative:
            payload["commands"] = _hook_commands(data.get("hooks", data))
        elif "mcp" in relative:
            payload["servers"] = _mcp_servers(
                data.get("mcpServers") or data.get("mcp_servers") or data
            )
        return payload

    for fmt in PLUGIN_FORMATS:
        for relative in fmt.executable_files:
            path = root / relative
            if path.exists():
                add(relative, payload_for(relative, path))
        for relative in fmt.executable_dirs:
            directory = root / relative
            if directory.is_dir() and any(directory.iterdir()):
                add(
                    relative,
                    {
                        "path": str(directory),
                        "files": sorted(
                            child.name for child in directory.iterdir() if child.is_file()
                        )[:20],
                    },
                )
        for relative in fmt.manifests:
            manifest = root / relative
            if not manifest.is_file():
                continue
            data = _read_json(manifest)
            for key in fmt.executable_keys:
                value = data.get(key)
                if not value:
                    continue
                payload: dict[str, Any] = {"path": str(manifest)}
                if key in _MCP_KEYS:
                    payload["servers"] = _mcp_servers(value)
                else:
                    payload["commands"] = _hook_commands(value)
                add(f"{relative}:{key}", payload)

    return tuple(found)
