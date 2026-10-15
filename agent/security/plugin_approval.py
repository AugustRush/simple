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

The ``source`` names what is being approved: a plugin's canonical URL or path, or
``user_tool:<id>@<digest>`` for generated code.  A **plugin** approval is now
digest-qualified too (``plugin:<source>@<digest>``), so it lapses the moment the
code changes -- which is the property both kinds were always claiming and only one
of them had.  The digest covers what would run (see :mod:`agent.runtime.provenance`),
not where it is: editing a hook command asks again, moving the directory does not.
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


def _rule(source: str, digest: str = "") -> str:
    """``plugin:<source>`` -- or ``plugin:<source>@<digest>`` when a digest is given.

    The digest is the *content* of what would run (see :mod:`agent.runtime.provenance`),
    and it is what makes the answer an answer about this code rather than about this
    address.  Keyed by source alone, "总是允许" meant "always for whatever is at that URL
    from now on" -- an approval attached to nothing that ends up running, which the
    provenance design measured and this fixes.

    The spelling matches the one already used for generated tools
    (``user_tool:<id>@<digest>``), so a reader meets one convention, not two.

    A source with **no** digest keeps the old key exactly.  That is not a compatibility
    shim: a record written before digests existed says something true about the source, and
    re-keying it would silently invalidate a grant the human gave.  What it cannot do is
    match a digest-qualified ask, so a durable grant for an executable plugin is asked once
    more after this change -- the safe direction, and the only one that does not invent
    consent.
    """
    value = str(source) if not digest else f"{source}@{digest}"
    return consent.make_rule(consent.PLUGIN, value)


def plugin_install_record_pending(
    scope: Any, source: str, *, reason: str = "", digest: str = ""
) -> str:
    """Register one pending install/activation; returns its opaque token.

    ``reason`` is carried on the record so a card that is not shown at the
    moment of asking -- a chat channel lists outstanding questions afterwards --
    can still say why each one is being asked.
    """
    # The question is *shown* under the source and *remembered* by content.  That is the
    # split ``always_rule`` exists for, and it keeps two promises at once: the numbered list
    # a chat channel prints still reads as the source a human chose (``Pending.what`` is the
    # rule's value, so a digest in ``rule`` would leak a hash into the card), and a durable
    # "总是允许" is bound to the code that was shown rather than to the address it came from.
    # A ``once``/``session`` answer lands on the plain source, which is enough for a window
    # nothing can meaningfully change inside -- see ``plugin_install_was_approved``.
    return consent.SHELL.ask(
        kind=KIND,
        summary=str(source),
        rule=_rule(source),
        always_rule=_rule(source, digest) if digest else "",
        scope=scope,
        reason=str(reason or ""),
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


#: The second question, and it is a different one.
#:
#: ``plugin:<source>@<digest>`` means **a human said yes** to this code, in a dialog, having
#: been shown what it would run.  ``plugin-seen:<digest>`` means **this content has run on
#: this machine before** -- trust on first use, recorded by the loader, with nobody asked.
#: Keeping them as two rule kinds rather than one is the whole point: "who approved this"
#: has to stay answerable, and a single rule would make a machine's habit indistinguishable
#: from a person's decision.
#:
#: The digest is the entire value: not the plugin name and not its path.  A rename is not a
#: change of code, and a move is not a change of code -- what ran is what matters.
SEEN_KIND = "plugin-seen"

#: The marker that says "this deployment has had its first look".
#:
#: Without it, turning change detection on would leave every existing deployment with a
#: plugin tree nobody had recorded, and the honest choices would be a cliff (everything
#: inert until re-approved) or a silent blessing.  The marker makes the blessing a *named,
#: one-time, visible* act instead: on the first run the loader prints every plugin and what
#: it would run, records them, and from then on only a **change** is loud.
SEEN_BASELINE = "baseline"


def _seen_rule(digest: str) -> str:
    return consent.make_rule(SEEN_KIND, digest)


def plugin_content_was_seen(digest: str) -> bool:
    """Whether this exact content has run here before (trust on first use)."""
    if not digest:
        return False
    return consent.SHELL.approved(_seen_rule(digest), scope=None, workspace="")


def record_plugin_content_seen(digest: str) -> None:
    """Remember that this content ran here.  Durable, like every ``always`` answer."""
    if digest:
        consent.SHELL.remember(
            _seen_rule(digest), consent.ALWAYS, scope=None, workspace=""
        )


def seen_baseline_taken() -> bool:
    """Whether this deployment has already had its one-time first look."""
    return consent.SHELL.approved(
        consent.make_rule(SEEN_KIND, SEEN_BASELINE), scope=None, workspace=""
    )


def record_seen_baseline() -> None:
    consent.SHELL.remember(
        consent.make_rule(SEEN_KIND, SEEN_BASELINE),
        consent.ALWAYS,
        scope=None,
        workspace="",
    )


def plugin_install_was_approved(scope: Any, source: str, digest: str = "") -> bool:
    """True when this source, at this content, was approved for this scope (unexpired).

    ``workspace=""`` reads the global bucket: an install lands in the agent's
    own plugin directory rather than in a project, so "always" for it is a fact
    about this machine, not about one workspace.

    Two keys, in a deliberate order:

    1. ``plugin:<source>@<digest>`` -- a durable grant about **this code**.  This is the
       one that answers "I approved it in March, is it still the same thing".
    2. ``plugin:<source>`` -- the plain source, consulted **only** for a lifetime that
       cannot outlive the session.  A question is shown under the source (see
       :func:`plugin_install_record_pending`), so an answer redeemed from a pending
       record lands there; ``always`` does not, which is what stops a pre-digest ``always``
       grant from standing in for consent to whatever the URL serves now.
    """
    if consent.SHELL.approved(_rule(source, digest), scope=scope, workspace=""):
        return True
    if not digest:
        return False
    lifetime = consent.SHELL.lifetime_of(_rule(source), scope=scope, workspace="")
    return lifetime in (consent.ONCE, consent.SESSION)


def plugin_install_mark_approved(
    scope: Any, source: str, *, lifetime: str = consent.ONCE, digest: str = ""
) -> None:
    """Record an approval the sink just gave.

    ``lifetime`` defaults to a single answer.  ``consent.ALWAYS`` is what the
    card offers as "总是允许": today the human could not answer that for an
    install at all, so the same question came back after five minutes.
    """
    consent.SHELL.remember(
        _rule(source, digest), lifetime, scope=scope, workspace=""
    )


def plugin_install_clear_all() -> None:
    """Clear pending and approved records (test/teardown helper)."""
    consent.SHELL.clear()


__all__ = [
    "KIND",
    "SEEN_KIND",
    "SEEN_BASELINE",
    "plugin_content_was_seen",
    "record_plugin_content_seen",
    "seen_baseline_taken",
    "record_seen_baseline",
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
