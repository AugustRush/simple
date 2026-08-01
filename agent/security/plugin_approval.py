"""Pending human approval for plugin installs.

Installing a plugin with executable content (Python entry point, MCP
server, hooks) is equivalent to running arbitrary code, so the interactive
CLI asks for consent through the same approval menu used for high-risk
shell commands.  Non-interactive channels (gateway/Feishu) create a pending
record that the coordinator redeems when the user replies "同意", mirroring
the shell confirmation flow.  Tokens and approved entries are expiring and
scoped to session/channel/user; the model can never fabricate approval.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Optional


_TTL_SECONDS = 5 * 60
_PENDING: dict[str, dict[str, Any]] = {}  # token -> record
_APPROVED: dict[str, float] = {}  # scope_key\0source -> expires_at


def _scope_key(scope: Any) -> str:
    session = str(getattr(scope, "session_id", "") or "")
    channel = str(getattr(scope, "channel_name", "") or "")
    user = str(getattr(scope, "user_id", "") or "")
    return f"{session}\0{channel}\0{user}"


def _now() -> float:
    return time.monotonic()


def _purge() -> None:
    now = _now()
    for token, record in list(_PENDING.items()):
        if record["expires_at"] <= now:
            _PENDING.pop(token, None)
    for key, expires_at in list(_APPROVED.items()):
        if expires_at <= now:
            _APPROVED.pop(key, None)


def plugin_install_record_pending(scope: Any, source: str) -> str:
    """Register one pending plugin install; returns its opaque token."""
    _purge()
    token = secrets.token_urlsafe(16)
    _PENDING[token] = {
        "scope_key": _scope_key(scope),
        "source": source,
        "expires_at": _now() + _TTL_SECONDS,
    }
    return token


def plugin_install_pending_for_scope(scope: Any) -> list[str]:
    """Return unexpired pending plugin sources for one scope."""
    _purge()
    key = _scope_key(scope)
    return [
        record["source"]
        for record in _PENDING.values()
        if record["scope_key"] == key
    ]


def plugin_install_approve_single(scope: Any) -> Optional[str]:
    """Approve the sole pending install for *scope*; return its source.

    Chat approval is unambiguous only when exactly one unexpired record
    exists for the scope.  Zero or several records return None so the
    approval message is forwarded as a normal turn instead of guessing.
    """
    _purge()
    key = _scope_key(scope)
    records = [
        (token, record)
        for token, record in _PENDING.items()
        if record["scope_key"] == key
    ]
    if len(records) != 1:
        return None
    token, record = records[0]
    _PENDING.pop(token, None)
    source = record["source"]
    plugin_install_mark_approved(scope, source)
    return source


def plugin_install_was_approved(scope: Any, source: str) -> bool:
    """True when the exact source was approved for this scope (unexpired)."""
    _purge()
    return f"{_scope_key(scope)}\0{source}" in _APPROVED


def plugin_install_mark_approved(scope: Any, source: str) -> None:
    _purge()
    _APPROVED[f"{_scope_key(scope)}\0{source}"] = _now() + _TTL_SECONDS


def plugin_install_clear_all() -> None:
    """Clear pending and approved records (test/teardown helper)."""
    _PENDING.clear()
    _APPROVED.clear()
