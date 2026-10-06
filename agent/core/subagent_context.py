"""Composing a sub-agent's world: its system prompt, and its write scope.

Why this module exists
----------------------
The sub-agent group on ``BaseAgent`` is not uniformly coupled.  Two of its
members touch almost nothing -- ``_canonicalize_write_scope`` needs a workspace
root, ``_compose_sub_system_prompt`` needs a base prompt, a workspace root and
the active context -- while ``_execute_agent`` reaches for sixteen attributes
across 288 lines.  Extracting the cheap two first is what makes the expensive one
tractable later: after this, ``_execute_agent`` calls two functions instead of
two methods, and the part of it that decides *what a sub-agent is told* is
testable on its own.

The property worth protecting
-----------------------------
A sub-agent's system prompt is built from **its own** registry, never the
parent's active context.  That matters twice over:

- the tool list in the prompt must match the tools the sub-agent actually has,
  or the model is told about capabilities it will be refused; and
- the parent's per-turn retrieval injections must not leak in, because several
  sub-agents run at once and their prompts are not the parent's turn.

The skill catalog is passed through only when it is a real ``SkillCatalog``:
metadata may hold a test stub with no ``summary_lines``, and a prompt builder
that trusts it would fail inside a sub-agent rather than where the stub was put.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from agent.pathing import canonicalize_user_path, resolve_workspace_path

__all__ = [
    "canonicalize_write_scope",
    "compose_sub_system_prompt",
    "propagate_sub_metadata",
]


def canonicalize_write_scope(
    raw_scope: str,
    *,
    workspace_root: Optional[Path],
    output_dir: Optional[Path],
) -> Path:
    """Resolve a declared write scope to a concrete path.

    With a workspace root, the scope is resolved through the shared path policy
    so the same rules apply as everywhere else (and an out-of-root scope is
    refused rather than silently allowed).  Without one, there is no project to
    be relative to, so the user's own path is taken as written.
    """
    if workspace_root is not None:
        resolved, _root_kind = resolve_workspace_path(
            raw_scope,
            workspace_root=workspace_root,
            output_dir=output_dir,
        )
        return resolved
    return canonicalize_user_path(raw_scope, base_dir=Path.cwd())


def compose_sub_system_prompt(
    sub_registry: Any,
    *,
    base_system_prompt: str,
    workspace_root: Optional[Path],
    active_context: Any = None,
    system_suffix: str = "",
    compose: Callable[..., str],
    skill_catalog_type: type,
) -> tuple[str, Any]:
    """The system prompt a sub-agent runs under, and the context it belongs to.

    ``compose`` is :func:`agent.config._compose_system_prompt`, injected rather
    than imported so this module does not pull in the configuration layer.

    ``skill_catalog_type`` is the real ``SkillCatalog`` class, used to reject a
    stub before it reaches the prompt builder; see the module docstring.
    """
    output_dir_str = sub_registry._context.get("output_dir")
    output_dir_path = Path(output_dir_str) if output_dir_str else None

    skill_catalog_for_prompt = None
    if active_context:
        candidate = active_context.metadata.get("skill_catalog")
        if isinstance(candidate, skill_catalog_type):
            skill_catalog_for_prompt = candidate

    sys_prompt = compose(
        base_system_prompt,
        sub_registry,
        workspace_root,
        output_dir=output_dir_path,
        skill_catalog=skill_catalog_for_prompt,
    )
    if system_suffix:
        sys_prompt += f"\n\n{system_suffix}"
    return sys_prompt, active_context

#: Metadata keys a sub-agent inherits from the turn that spawned it.
#:
#: A whitelist rather than a copy, because ``metadata`` also carries per-turn
#: state that must *not* travel: the parent's retrieval injections, its model
#: override, and its own operation bookkeeping.  Copying everything would give a
#: sub-agent its parent's turn -- several run at once, so they would share and
#: overwrite each other's entries.
_INHERITED_METADATA_KEYS = (
    "skill_catalog",
    "required_skills",
    "cancel_token",
    "context_manager",
    "session_id",
)

#: Keys whose value is copied shallowly, so a child mutating its list cannot
#: rewrite the parent's.
_COPIED_METADATA_KEYS = frozenset({"required_skills"})


def propagate_sub_metadata(
    sub_ctx: Any,
    active_ctx: Any = None,
) -> None:
    """Give *sub_ctx* the parts of the parent turn it legitimately inherits.

    ``session_id`` is in the list for a specific reason: without it the child
    turn has no session of its own, and anything that needs one -- the
    per-conversation request header a gateway may require -- falls back to a
    per-process value, so the main turn and its sub-agents would report two
    different conversations for one conversation's work.
    """
    if not active_ctx:
        return
    for key in _INHERITED_METADATA_KEYS:
        if key not in active_ctx.metadata:
            continue
        value = active_ctx.metadata[key]
        if key in _COPIED_METADATA_KEYS:
            value = list(value)
        sub_ctx.metadata[key] = value
