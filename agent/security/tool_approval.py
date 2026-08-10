"""Human approval for activating user-authored Python tools.

Loading a user tool executes its module inside the live agent process, which
is the same authority level as installing a plugin with a Python entry point.
So it reuses that consent machinery: an interactive terminal shows the
approval menu at the moment of the side effect, and a chat channel gets a
pending record the coordinator redeems when the user replies "同意".

Approval is recorded against a hash of the file's contents (see
``agent.tools.user_tools``), so it authorizes the exact code a human saw and
lapses the moment that code changes.
"""

from __future__ import annotations

from typing import Any, Optional

SOURCE_PREFIX = "user_tool:"


def approval_source(tool_id: str, digest: str) -> str:
    """Opaque consent key naming both the tool and the code being approved."""
    return f"{SOURCE_PREFIX}{tool_id}@{str(digest or '')[:12]}"


def is_tool_source(source: str) -> bool:
    return str(source or "").startswith(SOURCE_PREFIX)


def describe_tool_source(source: str) -> str:
    """Human-facing tool id from an approval source key."""
    if not is_tool_source(source):
        return str(source or "")
    return str(source)[len(SOURCE_PREFIX):].split("@", 1)[0]


def authorization_scope() -> Any:
    """Consent scope for the turn currently executing."""
    from agent.core.agent import _active_agent_context
    from agent.security.shell import ShellAuthorizationScope

    active_context = _active_agent_context.get()
    metadata = active_context.metadata if active_context is not None else {}
    return ShellAuthorizationScope(
        str(metadata.get("session_id") or "default"),
        str(metadata.get("channel_name") or "cli"),
        str(metadata.get("user_id") or ""),
    )


async def confirm_tool_activation(
    *,
    tool_id: str,
    digest: str,
    reason: str,
    scope: Optional[Any] = None,
) -> tuple[bool, bool]:
    """Ask the human before a generated tool becomes callable.

    Returns ``(approved, needs_pending)``.  An interactive sink decides now and
    a decline is final; a non-interactive sink leaves a pending record so a
    later approval reply can redeem it.
    """
    from agent.core.output import _APPROVAL_LOCK, _active_sink
    from agent.security.plugin_approval import (
        plugin_install_mark_approved,
        plugin_install_was_approved,
    )

    consent_scope = scope if scope is not None else authorization_scope()
    source = approval_source(tool_id, digest)

    async with _APPROVAL_LOCK:
        if plugin_install_was_approved(consent_scope, source):
            return True, False
        sink = _active_sink.get()
        if sink is None:
            return False, False
        interactive = bool(getattr(sink, "interactive_confirmation", False))
        approved = await sink.on_tool_confirmation(
            "create_tool",
            command=f"activate user tool '{tool_id}'",
            risk_level="high",
            reason=reason,
            confirmation_token="",
            scope=consent_scope,
        )
        if approved:
            plugin_install_mark_approved(consent_scope, source)
            return True, False
        return False, not interactive


__all__ = [
    "SOURCE_PREFIX",
    "approval_source",
    "authorization_scope",
    "confirm_tool_activation",
    "describe_tool_source",
    "is_tool_source",
]
