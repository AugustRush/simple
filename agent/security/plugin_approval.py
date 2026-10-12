"""Pending human approval for installs and activations, keyed by source.

Installing a plugin with executable content (Python entry point, MCP server,
hooks) is equivalent to running arbitrary code, and activating a generated tool
runs Python inside the live agent process.  Both ask the human first: an
interactive client shows the approval card at the moment of the side effect, and
a chat channel gets a pending record the coordinator redeems when the user
replies "同意", mirroring the shell confirmation flow.

This module used to *be* that record -- two module dictionaries, a five-minute
TTL, and a third copy of the session/channel/user scope key.  It is now a thin
adapter over :mod:`agent.security.consent`, which is the one place an answer is
remembered, so that "always" means the same thing here as it does for a shell
command and survives a restart.  The public functions are unchanged because they
have callers in the tool layer, the coordinator and five test modules; what
changed is where the answer lives.

The ``source`` is an opaque key naming what is being approved: a plugin's
canonical URL or path, or ``user_tool:<id>@<digest>`` for generated code, so the
approval lapses the moment that code changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from agent.security import consent

#: Kept for readers of the old module: the window in which an unanswered
#: question stays answerable, now owned by the store.
_TTL_SECONDS = consent.PROMPT_TTL_SECONDS

#: Every source-keyed approval shares this kind.  It is what keeps an install
#: question out of the shell command list once both live in the same store --
#: the coordinator asks each kind for its own pending records, rather than
#: relying on which module it happened to call.
KIND = "install_or_activate"


def _scope_key(scope: Any) -> str:
    """The scope string a source-keyed answer is bound to."""
    return consent.scope_key(scope)


def _rule(source: str) -> str:
    return consent.make_rule(consent.PLUGIN, str(source))


def plugin_install_record_pending(scope: Any, source: str) -> str:
    """Register one pending install/activation; returns its opaque token."""
    return consent.SHELL.ask(
        kind=KIND,
        summary=str(source),
        rule=_rule(source),
        scope=scope,
    )


def plugin_install_pending_records(scope: Any) -> list[consent.Pending]:
    """Unexpired pending install/activation records for one scope, in order.

    The numbered list a chat channel shows is built from these, so several
    questions are answerable as "1"/"2" instead of by copying a token.
    """
    return [
        record
        for record in consent.SHELL.pending(scope)
        if record.kind == KIND
    ]


def plugin_install_pending_for_scope(scope: Any) -> list[str]:
    """Return unexpired pending sources for one scope."""
    return [record.what for record in plugin_install_pending_records(scope)]


def plugin_install_approve_single(scope: Any) -> Optional[str]:
    """Approve the sole pending install for *scope*; return its source.

    Chat approval is unambiguous only when exactly one unexpired record exists
    for the scope.  Zero or several records return None so the approval message
    is forwarded as a normal turn instead of guessing.
    """
    records = plugin_install_pending_records(scope)
    if len(records) != 1:
        return None
    approved = consent.SHELL.approve(records[0].token, scope=scope)
    return approved.what if approved is not None else None


def plugin_install_approve_record(
    token: str, scope: Any, *, lifetime: str = consent.ONCE
) -> Optional[str]:
    """Approve one named pending record; return its source.

    This is the by-number path: the ambiguity that made a bare "yes" unusable
    is resolvable by pointing at the record, so several pending questions are
    answerable in one reply.
    """
    approved = consent.SHELL.approve(token, scope=scope, lifetime=lifetime)
    return approved.what if approved is not None else None


def plugin_install_was_approved(scope: Any, source: str) -> bool:
    """True when the exact source was approved for this scope (unexpired).

    ``workspace=""`` reads the global bucket: an install lands in the agent's
    own plugin directory rather than in a project, so "always" for it is a fact
    about this machine, not about one workspace.
    """
    return consent.SHELL.approved(_rule(source), scope=scope, workspace="")


def plugin_install_mark_approved(
    scope: Any, source: str, *, lifetime: str = consent.ONCE
) -> None:
    """Record an approval the sink just gave.

    ``lifetime`` defaults to a single answer.  ``consent.ALWAYS`` is what the
    card offers as "总是允许": today the human could not answer that for an
    install at all, so the same question came back after five minutes.
    """
    consent.SHELL.remember(
        _rule(source), lifetime, scope=scope, workspace=""
    )


def plugin_install_clear_all() -> None:
    """Clear pending and approved records (test/teardown helper)."""
    consent.SHELL.clear()


__all__ = [
    "KIND",
    "_TTL_SECONDS",
    "plugin_install_approve_record",
    "plugin_install_approve_single",
    "plugin_install_clear_all",
    "plugin_install_mark_approved",
    "plugin_install_pending_for_scope",
    "plugin_install_pending_records",
    "plugin_install_record_pending",
    "plugin_install_was_approved",
]
