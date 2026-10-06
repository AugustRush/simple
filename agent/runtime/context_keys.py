"""The vocabulary of ``ToolRegistry`` context keys, and who fills each one.

Why this exists
---------------
``ToolRegistry._context`` is the seam through which tools resolve what they need:
``registry.get_context("output_dir")`` and thirty-odd siblings.  It is **not** a
legacy path waiting to be replaced -- it is the mechanism, and it is already the
narrow one.  The broad version, ``get_context("components")``, handed every tool
the entire bootstrap result and was removed in Task 4; what is left is a set of
named dependencies, bridged from the component mapping by explicit
``registry.set_context(...)`` calls.

What was missing is the contract.  Nothing said which keys exist, so the
vocabulary was implicit in a hundred call sites, and a key that a reader asks for
and no writer supplies looks exactly like a key whose value happens to be absent.
That is the same gap P0 closed for module-level state, one layer down, so it is
closed the same way: an explicit list, and a scanner that compares it against the
source.

Why the scan uses the AST and not a regex
-----------------------------------------
Because a regex finds prose.  Three keys in this file's first draft --
``"components"`` among them -- appeared "written" only inside docstrings explaining
that they no longer are.  The scanner reads ``Call`` nodes and blanked string
literals, so a sentence about a key cannot be mistaken for a use of one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

__all__ = [
    "ContextKey",
    "CONTEXT_KEYS",
    "BY_KEY",
    "entry_for",
    "declared_names",
    "suspicious",
]


@dataclass(frozen=True)
class ContextKey:
    """One context key, what fills it, and what reads it.

    ``writers`` is the reason this is a ledger rather than a set: a key read by
    tools and written by nobody returns its default for ever, which is
    indistinguishable from a key whose value is legitimately absent.  Naming the
    writer is what makes that checkable.
    """

    key: str
    writers: tuple[str, ...]
    readers: tuple[str, ...]
    #: Set when the value is filled by a path the scanner cannot see -- a
    #: ``derive(context={...})`` literal, or a session component built elsewhere.
    note: str = ""
    #: True when a reader exists that the scanner did not count, so the missing
    #: writer is a known blind spot rather than a defect.
    partial: bool = False


#: Every key, grouped by the part of the runtime that owns it.
#:
#: The writer and reader lists are **generated from the same AST scan the
#: guard runs**, then reviewed.  Hand-writing them was tried first and the
#: guard rejected the result: it found five keys this ledger had missed
#: entirely and had the writers wrong for several more.  A ledger transcribed
#: by hand is a second copy of the truth, which is the thing this file exists
#: to avoid.
CONTEXT_KEYS: tuple[ContextKey, ...] = (
    # ── paths: resolved at startup, read by tools at call time ──
    ContextKey(
        key="workspace_root",
        writers=("agent/bootstrap.py", "agent/commands/builtin.py",),
        readers=("agent/tools/builtin_tools.py", "agent/tools/user_tool_runner.py",),
    ),
    ContextKey(
        key="output_dir",
        writers=("agent/bootstrap.py",),
        readers=("agent/core/agent.py", "agent/skills/catalog.py", "agent/tools/builtin_tools.py", "agent/tools/user_tool_runner.py",),
    ),
    ContextKey(
        key="resource_home",
        writers=("agent/bootstrap.py",),
        readers=("agent/cli.py", "agent/tools/builtin_tools.py",),
    ),
    ContextKey(
        key="user_skills_dir",
        writers=("agent/bootstrap.py",),
        readers=("agent/config.py",),
    ),
    ContextKey(
        key="user_tools_dir",
        writers=("agent/bootstrap.py",),
        readers=("agent/config.py",),
    ),
    ContextKey(
        key="user_plugins_dir",
        writers=("agent/bootstrap.py",),
        readers=("agent/tools/builtin_tools.py",),
    ),

    # ── services and catalogues ──
    ContextKey(
        key="registry",
        writers=("agent/bootstrap.py",),
        readers=("agent/tools/builtin_tools.py",),
    ),
    ContextKey(
        key="skill_catalog",
        writers=("agent/bootstrap.py",),
        readers=("agent/tools/builtin_tools.py",),
    ),
    ContextKey(
        key="plugin_catalog",
        writers=("agent/bootstrap.py",),
        readers=("agent/tools/builtin_tools.py",),
    ),
    ContextKey(
        key="command_router",
        writers=("agent/bootstrap.py",),
        readers=("agent/tools/builtin_tools.py",),
    ),
    ContextKey(
        key="mcp_client",
        writers=("agent/bootstrap.py",),
        readers=("agent/tools/builtin_tools.py",),
    ),
    ContextKey(
        key="file_service",
        writers=("agent/commands/builtin.py",),
        readers=("agent/tools/builtin_tools.py",),
    ),
    ContextKey(
        key="file_access_policy",
        writers=("agent/bootstrap.py", "agent/commands/builtin.py",),
        readers=("agent/tools/builtin_tools.py", "agent/tools/user_tool_runner.py",),
    ),

    # ── shell permissions and policy ──
    ContextKey(
        key="shell_permission_level",
        writers=("agent/bootstrap.py",),
        readers=("agent/tools/builtin_tools.py",),
    ),
    ContextKey(
        key="shell_sandbox_mode",
        writers=("agent/bootstrap.py",),
        readers=("agent/bootstrap.py", "agent/tools/builtin_tools.py", "agent/tools/user_tool_runner.py",),
    ),
    ContextKey(
        key="shell_allowed_commands",
        writers=("agent/bootstrap.py", "agent/commands/builtin.py",),
        readers=("agent/tools/builtin_tools.py",),
    ),
    ContextKey(
        key="shell_blocked_commands",
        writers=("agent/bootstrap.py",),
        readers=("agent/tools/builtin_tools.py",),
    ),
    ContextKey(
        key="shell_secret_paths",
        writers=("agent/bootstrap.py",),
        readers=("agent/tools/builtin_tools.py", "agent/tools/user_tool_runner.py",),
    ),
    ContextKey(
        key="shell_devices",
        writers=("agent/bootstrap.py",),
        readers=("agent/bootstrap.py", "agent/tools/builtin_tools.py", "agent/tools/user_tool_runner.py",),
    ),
    ContextKey(
        key="supports_vision",
        writers=("agent/bootstrap.py",),
        readers=("agent/config.py",),
    ),

    # ── per-turn and per-scope ──
    ContextKey(
        key="turn_request",
        writers=("agent/core/agent.py",),
        readers=("agent/core/agent.py",),
    ),
    ContextKey(
        key="capability_profile",
        writers=(),
        readers=("agent/tools/builtin_tools.py",),
        note=(
            "Filled as a literal in the derive(context={...}) call that builds a "
            "sub-agent's registry view, so the scanner's set_context pattern does "
            "not see it."
        ),
        partial=True,
    ),
    ContextKey(
        key="write_scope",
        writers=(),
        readers=("agent/tools/builtin_tools.py", "agent/tools/user_tool_runner.py",),
        note=(
            "Same as capability_profile: a derive(context={...}) literal, present "
            "only when the scope is narrowed."
        ),
        partial=True,
    ),
    ContextKey(
        key="user_tool_catalog",
        writers=(),
        readers=("agent/tools/user_tools.py",),
        note=(
            "Bootstrap builds it and the session-component path publishes it; the "
            "scanner does not follow the dict it travels in."
        ),
        partial=True,
    ),
    ContextKey(
        key="user_tools_enabled",
        writers=(),
        readers=("agent/tools/user_tools.py",),
        note=(
            "As above. Read through resolve_catalog(), so a missing writer would "
            "show as approvals-required rather than as an error -- which is why "
            "it is named rather than assumed."
        ),
        partial=True,
    ),

    # ── per-session endpoints and credentials ──
    ContextKey(
        key="web_proxy",
        writers=("agent/bootstrap.py",),
        readers=("agent/tools/builtin_tools.py",),
    ),
    ContextKey(
        key="tavily_api_key",
        writers=("agent/bootstrap.py",),
        readers=("agent/tools/builtin_tools.py",),
    ),
    ContextKey(
        key="audio_transcription_command",
        writers=("agent/bootstrap.py",),
        readers=("agent/tools/builtin_tools.py",),
    ),)

BY_KEY: dict[str, ContextKey] = {entry.key: entry for entry in CONTEXT_KEYS}


def entry_for(key: str) -> ContextKey:
    """The ledger entry for *key*.  Raises for an undeclared one.

    Loud rather than returning a blank entry: a key that reaches this function
    undeclared is exactly the drift the ledger exists to catch, and an empty
    record would hide it.
    """
    try:
        return BY_KEY[key]
    except KeyError:
        raise KeyError(
            f"context key {key!r} is not declared in CONTEXT_KEYS; add it to "
            f"agent/runtime/context_keys.py with its writer and reader"
        ) from None


def declared_names() -> tuple[str, ...]:
    return tuple(entry.key for entry in CONTEXT_KEYS)


def suspicious(
    entries: Iterable[ContextKey] | None = None,
) -> tuple[ContextKey, ...]:
    """Keys with no writer and no recorded reason.

    The check that would catch a reader asking for something nobody fills -- the
    failure mode a plain ``dict`` lookup turns into a silent default.
    """
    source = CONTEXT_KEYS if entries is None else tuple(entries)
    return tuple(e for e in source if not e.writers and not e.partial)
