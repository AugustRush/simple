"""One answer from the human, at three lifetimes, in one place.

Why this exists
---------------
Four things ask a human for consent -- a shell command, a plugin install, a
generated tool, a memory wipe -- and each grew its own record of the answer:

* ``shell._session_allowlist`` + ``shell._pending_tokens`` (RAM, five minutes,
  keyed by the byte-exact command);
* ``plugin_approval._PENDING`` + ``_APPROVED`` (RAM, five minutes, keyed
  ``session\\0channel\\0user\\0source``);
* ``user_tools``' ``.approved.json`` (disk, no expiry, keyed by content digest).

So "stop asking me" meant three different things depending on which question was
being answered, three scope keys were computed by hand, and the coordinator had
to try one store and then the other.  This is the single store.  The three
lifetimes below are the only distinction that ever mattered:

``once``
    this exact request, for five minutes.  What a plain "allow" buys.
``session``
    every matching request in this scope until the process exits.
``always``
    every matching request in this **workspace**, on disk.

A *rule* is what is remembered, and it names the thing the human was actually
granting rather than the bytes they happened to be looking at::

    program:npm                 every npm invocation
    command:pip install -r …    that one command line
    path:/Users/me/code         a directory
    plugin:https://…            that install source
    tool:user_tool:x@<digest>   that exact file content

That is the point of the module.  A byte-exact entry cannot answer "stop asking
me about npm", so the human is asked again on the next spelling; ``program:npm``
can, and ``always`` remembers it for the project rather than for the five
minutes after they said it.  The capability `/allow npm` has always granted
this, silently, for shell commands and for nothing else; exposing it on the
consent card is what removes the long tail of interruptions.

``always`` is keyed by workspace and not by scope, because a new conversation is
a new session: an answer that only survived *this* session would be asked again
tomorrow, which is exactly the repetition this exists to stop.  The trade-off is
that a workspace's trust is shared by whoever uses that workspace -- which is
the right reading of "this project", and is stated here rather than left
implicit.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

#: How long an answer lasts.
ONCE = "once"
SESSION = "session"
ALWAYS = "always"
LIFETIMES: tuple[str, ...] = (ONCE, SESSION, ALWAYS)

#: What a rule names.
COMMAND = "command"
PROGRAM = "program"
PATH = "path"
PLUGIN = "plugin"
TOOL = "tool"
KINDS: tuple[str, ...] = (COMMAND, PROGRAM, PATH, PLUGIN, TOOL)

#: How long an unanswered question stays answerable, and how long a ``once``
#: answer covers an identical repeat.  One number, because they are the same
#: window from the human's side: the interval in which "I just answered that"
#: is true.
PROMPT_TTL_SECONDS = 5 * 60

_TRUST_DIRNAME = "trust"
_LEDGER_VERSION = 1


def scope_key(scope: Any) -> str:
    """The identity a session-scoped answer is bound to.

    One construction, used by every consent kind.  It was written out three
    times in three modules with three different fallbacks before this existed.
    """
    session = str(getattr(scope, "session_id", "") or "")
    channel = str(getattr(scope, "channel_name", "") or "")
    user = str(getattr(scope, "user_id", "") or "")
    return f"{session}\0{channel}\0{user}" if scope is not None else ""


def make_rule(kind: str, value: str) -> str:
    """``"program"`` + ``"npm"`` -> ``"program:npm"``."""
    return f"{kind}:{str(value)}"


def rule_kind(rule: str) -> str:
    return str(rule).partition(":")[0]


def rule_value(rule: str) -> str:
    return str(rule).partition(":")[2]


def workspace_key(root: Any) -> str:
    """A stable, filesystem-safe name for one workspace root."""
    text = str(Path(str(root)).expanduser().resolve(strict=False))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Pending:
    """A question that has been put to the human and not yet answered."""

    token: str
    kind: str
    summary: str
    rule: str
    scope_key: str
    expires_at: float
    #: The lifetime a "yes" will be recorded at unless the human says otherwise.
    lifetime: str = ONCE
    #: The rule an ``always`` answer should record instead of :attr:`rule`, and
    #: the workspace it belongs to.
    #:
    #: A question can be asked for one reason and answered at a wider lifetime,
    #: and the two are not the same rule.  `python3 -c '…'` is asked because the
    #: code cannot be read, and what is worth remembering is that exact command;
    #: `pip install -r …` is asked because it changes the environment, and what
    #: is worth remembering is the *program*, since the next install is a
    #: different line.  The gate decides this when it asks, because that is the
    #: only moment the reason is known.
    always_rule: str = ""
    workspace: str = ""

    @property
    def what(self) -> str:
        """The rule's value: what the human is being asked about."""
        return rule_value(self.rule)


@dataclass(frozen=True)
class Remembered:
    """An answer that is currently in force."""

    rule: str
    lifetime: str
    scope_key: str = ""
    workspace: str = ""
    #: When a ``once`` answer lapses; ``0.0`` for the lifetimes that do not.
    expires_at: float = 0.0


@dataclass
class ConsentStore:
    """Answers, the questions that produced them, and nothing else.

    ``ledger_root`` is the directory ``always`` answers are written to.  The
    default is ``None`` -- in memory only -- so a store that nobody configured
    cannot persist a grant; ``configure`` points the process-wide store at the
    agent's own ``trust`` directory.

    ``now`` is a wall clock rather than a monotonic counter, because one of the
    three lifetimes is written to disk: a stamp that means something only inside
    the process that produced it cannot describe a record meant to outlive it.
    """

    ledger_root: Optional[Path] = None
    now: Callable[[], float] = time.time
    _once: dict[tuple[str, str], float] = field(default_factory=dict)
    _session: set[tuple[str, str]] = field(default_factory=set)
    _always: dict[tuple[str, str], float] = field(default_factory=dict)
    _pending: dict[str, Pending] = field(default_factory=dict)
    _loaded: set[str] = field(default_factory=set)

    # ── ledger ──────────────────────────────────────────────────────────
    #
    # Persistence is opt-in.  ``ledger_root`` is ``None`` for a store that lives
    # only in memory, which is the default so that a test -- or any caller that
    # did not ask for disk -- can never write a grant into the user's real
    # ``~/.agent``.  Production points one store at ``~/.agent/trust`` once
    # (``agent/bootstrap.py``), and every consent kind shares it.

    def _ledger_path(self, workspace: str) -> Optional[Path]:
        root = self.ledger_root
        if root is None:
            return None
        name = workspace_key(workspace) if workspace else "global"
        return Path(root) / f"{name}.json"

    def _load(self, workspace: str) -> None:
        if workspace in self._loaded:
            return
        self._loaded.add(workspace)
        path = self._ledger_path(workspace)
        if path is None or not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt ledger must not become a grant: an unreadable record is
            # "no record", which means the question is asked again.
            return
        if not isinstance(payload, dict):
            return
        rules = payload.get("rules")
        if not isinstance(rules, dict):
            return
        for rule, entry in rules.items():
            at = entry.get("at") if isinstance(entry, dict) else None
            if isinstance(rule, str):
                self._always[(workspace, rule)] = float(at or 0.0)

    def _flush(self, workspace: str) -> None:
        path = self._ledger_path(workspace)
        if path is None:
            return
        rules = {
            rule: {"at": at}
            for (entry_workspace, rule), at in self._always.items()
            if entry_workspace == workspace
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if rules:
                path.write_text(
                    json.dumps(
                        {"version": _LEDGER_VERSION, "rules": rules},
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
            else:
                # Nothing left to remember: remove the file rather than leave an
                # empty grant behind, so `/trust` and a reader agree.
                path.unlink(missing_ok=True)
        except OSError:
            # Failing to persist must not fail the action the human approved;
            # the answer holds for this process and is asked again next time.
            pass

    # ── answers ─────────────────────────────────────────────────────────

    def remember(
        self,
        rule: str,
        lifetime: str,
        *,
        scope: Any = None,
        workspace: Any = None,
        expires_at: Optional[float] = None,
        at: Optional[float] = None,
    ) -> None:
        """Record that *rule* may proceed, at *lifetime*.

        ``expires_at`` overrides when a ``once`` answer lapses.  The shell gate
        needs it because its entry is bounded by the *question's* own deadline
        rather than by the moment of the answer: a human who takes four minutes
        to decide gets one minute of a once approval, not five.
        """
        rule = str(rule)
        if lifetime not in LIFETIMES:
            lifetime = ONCE
        if lifetime == ALWAYS:
            key = str(workspace or "")
            self._load(key)
            self._always[(key, rule)] = self._now(at)
            self._flush(key)
            return
        key = scope_key(scope)
        if lifetime == SESSION:
            self._session.add((key, rule))
        else:
            self._once[(key, rule)] = (
                expires_at
                if expires_at is not None
                else self._now(at) + PROMPT_TTL_SECONDS
            )

    def lifetime_of(
        self,
        rule: str,
        *,
        scope: Any = None,
        workspace: Any = None,
        at: Optional[float] = None,
    ) -> str:
        """The longest lifetime currently in force for *rule*, or ``""``.

        Longest first, because the three are nested rather than exclusive: an
        ``always`` answer also satisfies today's question, and reporting the
        weaker one would make an answer look like it lapses when it does not.
        """
        rule = str(rule)
        self._forget_expired(at)
        if workspace is not None:
            stored = str(workspace or "")
            self._load(stored)
            if (stored, rule) in self._always:
                return ALWAYS
        key = scope_key(scope)
        if (key, rule) in self._session:
            return SESSION
        if (key, rule) in self._once:
            return ONCE
        return ""

    def approved(
        self,
        rule: str,
        *,
        scope: Any = None,
        workspace: Any = None,
        at: Optional[float] = None,
    ) -> bool:
        """True when *rule* may proceed without asking."""
        return bool(
            self.lifetime_of(rule, scope=scope, workspace=workspace, at=at)
        )

    def revoke(
        self,
        rule: Optional[str] = None,
        *,
        scope: Any = None,
        workspace: Any = None,
    ) -> int:
        """Drop one rule (or every rule) at the lifetimes the caller means.

        Both bounds are honoured, which matters because the same rule text can
        exist in several places: ``always`` is keyed by workspace and the
        expiring lifetimes by scope, so revoking "pip in this project" must not
        silently revoke it in another project or for another user.  Passing
        neither bound clears the rule everywhere, which is what teardown wants.
        """
        removed = 0
        scope_bound = scope_key(scope) if scope is not None else None
        workspace_bound = str(workspace) if workspace is not None else None
        for key in [
            key
            for key in self._always
            if (rule is None or key[1] == rule)
            and (workspace_bound is None or key[0] == workspace_bound)
        ]:
            self._always.pop(key, None)
            removed += 1
        for entry in [
            entry
            for entry in self._once
            if (rule is None or entry[1] == rule)
            and (scope_bound is None or entry[0] == scope_bound)
        ]:
            self._once.pop(entry, None)
            removed += 1
        for entry in [
            entry
            for entry in self._session
            if (rule is None or entry[1] == rule)
            and (scope_bound is None or entry[0] == scope_bound)
        ]:
            self._session.discard(entry)
            removed += 1
        if workspace_bound is not None:
            self._load(workspace_bound)
            self._flush(workspace_bound)
        else:
            for stored in list(self._loaded):
                self._flush(stored)
        return removed

    def revoke_scope(self, scope: Any = None, *, at: Optional[float] = None) -> int:
        """Drop every expiring/session answer for one scope; return how many.

        This is the escape hatch a "never ask me again" answer needs, and what
        the web session-revoke endpoint calls.  ``always`` answers are *not*
        included: they are keyed by workspace rather than by scope, and revoking
        them belongs to the surface that shows them (``/trust``), not to a
        session reset that would silently drop the project's whole history of
        answers.
        """
        self._forget_expired(at)
        key = scope_key(scope)
        removed = 0
        for store in (self._once,):
            for entry in [entry for entry in store if entry[0] == key]:
                store.pop(entry, None)
                removed += 1
        for entry in [entry for entry in self._session if entry[0] == key]:
            self._session.discard(entry)
            removed += 1
        return removed

    def clear_transient(self) -> None:
        """Forget every ``once``/``session`` answer and every open question.

        The shell gate's global reset: its answers and its questions, at every
        scope, without touching what a workspace has been granted.
        """
        self._once.clear()
        self._session.clear()
        self._pending.clear()

    def remembered(
        self,
        *,
        scope: Any = None,
        workspace: Any = None,
        at: Optional[float] = None,
    ) -> list[Remembered]:
        """Everything currently in force, shortest-lived first.

        ``always`` rows come last and only when a *workspace* is named: they are
        not a fact about a scope, and listing one workspace's grants under every
        session would make a per-project listing wrong everywhere.
        """
        self._forget_expired(at)
        key = scope_key(scope)
        rows: list[Remembered] = []
        once = sorted(
            (
                (expires_at, rule)
                for (stored, rule), expires_at in self._once.items()
                if stored == key
            ),
            key=lambda pair: pair,
        )
        for expires_at, rule in once:
            rows.append(
                Remembered(
                    rule=rule,
                    lifetime=ONCE,
                    scope_key=key,
                    expires_at=expires_at,
                )
            )
        for stored, rule in sorted(self._session):
            if stored == key:
                rows.append(
                    Remembered(rule=rule, lifetime=SESSION, scope_key=stored)
                )
        if workspace is not None:
            stored = str(workspace or "")
            self._load(stored)
            for (entry_workspace, rule) in sorted(self._always):
                if entry_workspace == stored:
                    rows.append(
                        Remembered(
                            rule=rule, lifetime=ALWAYS, workspace=stored
                        )
                    )
        return rows

    # ── questions ───────────────────────────────────────────────────────

    def _now(self, at: Optional[float] = None) -> float:
        """The clock for one call.

        ``at`` exists because the shell gate's own tests drive time: every one
        of its entry points takes a ``now`` datetime, and threading it here is
        what let the gate keep its seams while its records moved into this
        store.  ``None`` means "the real clock".
        """
        return self.now() if at is None else float(at)

    def _forget_expired(self, at: Optional[float] = None) -> None:
        now = self._now(at)
        for key, expires_at in list(self._once.items()):
            if expires_at <= now:
                self._once.pop(key, None)

    def ask(
        self,
        *,
        kind: str,
        summary: str,
        rule: str,
        scope: Any = None,
        lifetime: str = ONCE,
        at: Optional[float] = None,
        expires_at: Optional[float] = None,
        always_rule: str = "",
        workspace: str = "",
    ) -> str:
        """Record a question and return its token.

        An identical question already outstanding reuses its token rather than
        minting a new one: the human's later answer has to be matchable to the
        request that prompted it, and a retry that minted a fresh token each
        time could never be answered at all.

        ``always_rule`` and ``workspace`` say what *this* question's "always"
        answer should mean; see :class:`Pending`.
        """
        self._forget_expired(at)
        key = scope_key(scope)
        rule = str(rule)
        now = self._now(at)
        for token, record in list(self._pending.items()):
            if record.expires_at <= now:
                self._pending.pop(token, None)
            elif (
                record.scope_key == key
                and record.rule == rule
                and record.kind == kind
            ):
                return record.token
        token = secrets.token_urlsafe(16)
        self._pending[token] = Pending(
            token=token,
            kind=str(kind),
            summary=str(summary),
            rule=rule,
            scope_key=key,
            expires_at=(
                expires_at
                if expires_at is not None
                else now + PROMPT_TTL_SECONDS
            ),
            lifetime=lifetime if lifetime in LIFETIMES else ONCE,
            always_rule=str(always_rule or ""),
            workspace=str(workspace or ""),
        )
        return token

    def pending(
        self, scope: Any = None, *, at: Optional[float] = None
    ) -> list[Pending]:
        """Outstanding questions for one scope, oldest first."""
        self._forget_expired(at)
        key = scope_key(scope)
        now = self._now(at)
        rows = [
            record
            for record in self._pending.values()
            if record.scope_key == key and record.expires_at > now
        ]
        return sorted(rows, key=lambda record: record.expires_at)

    def note_lifetime(self, token: str, lifetime: str) -> bool:
        """Record the lifetime the human chose, before redemption.

        The UI knows the human's intent but not the pending record; the caller
        that redeems knows the record but not the intent.  This is the hand-off
        between the two -- what ``shell_pending_mark_session_scope`` was for.
        """
        record = self._pending.get(str(token))
        if record is None or lifetime not in LIFETIMES:
            return False
        self._pending[str(token)] = Pending(
            token=record.token,
            kind=record.kind,
            summary=record.summary,
            rule=record.rule,
            scope_key=record.scope_key,
            expires_at=record.expires_at,
            lifetime=lifetime,
            always_rule=record.always_rule,
            workspace=record.workspace,
        )
        return True

    def get(self, token: str, *, scope: Any = None) -> Optional[Pending]:
        """One outstanding question by token, *without* asking whether it expired.

        The expiry question belongs to redemption, not to every reader: a
        surface that wants to record the lifetime the human chose is asking
        "is this the record I was shown", and answering that with a clock --
        especially a different one than the caller used to mint it -- turns a
        valid intent into a silent refusal.
        """
        record = self._pending.get(str(token))
        if record is None:
            return None
        if scope is not None and record.scope_key != scope_key(scope):
            return None
        return record

    def approve(
        self,
        token: str,
        *,
        scope: Any = None,
        workspace: Any = None,
        lifetime: Optional[str] = None,
        at: Optional[float] = None,
        expires_at: Optional[float] = None,
    ) -> Optional[Pending]:
        """Redeem one token; return the question it answered, or ``None``.

        The scope is re-checked here rather than trusted from the token, so an
        answer can never cross a session, channel or user boundary.
        """
        self._forget_expired(at)
        token = str(token)
        record = self._pending.get(token)
        if record is None:
            return None
        if record.expires_at <= self._now(at):
            self._pending.pop(token, None)
            return None
        if scope is not None and record.scope_key != scope_key(scope):
            return None
        self._pending.pop(token, None)
        chosen = lifetime or record.lifetime
        if chosen == ALWAYS:
            # The question's own "always" rule, in the workspace the question
            # was asked in -- not the rule that answered it once.
            self.remember(
                record.always_rule or record.rule,
                ALWAYS,
                scope=scope,
                workspace=record.workspace,
                at=at,
            )
            return record
        self.remember(
            record.rule,
            chosen,
            scope=scope,
            workspace=workspace,
            expires_at=(
                expires_at if expires_at is not None else record.expires_at
            ),
            at=at,
        )
        return record

    def reject(
        self, token: str, *, scope: Any = None, at: Optional[float] = None
    ) -> bool:
        """Retire one token because the human refused it.

        A refusal has to be terminal: leaving the record pending means a later,
        unrelated "yes" redeems the very request that was just declined.
        """
        token = str(token)
        record = self._pending.get(token)
        if record is None:
            return False
        if scope is not None and record.scope_key != scope_key(scope):
            return False
        self._pending.pop(token, None)
        return True

    def answer_single(
        self, scope: Any = None, *, at: Optional[float] = None
    ) -> Optional[Pending]:
        """Redeem the *sole* outstanding question for a scope, else ``None``.

        Ambiguity is not guessed at: with zero or several outstanding questions
        a short reply like "yes" has no determinate referent, and the caller
        says so instead of picking one.
        """
        rows = self.pending(scope, at=at)
        if len(rows) != 1:
            return None
        return self.approve(rows[0].token, scope=scope, at=at)

    def answer_at(
        self, index: int, scope: Any = None, *, at: Optional[float] = None
    ) -> Optional[Pending]:
        """Redeem the *index*-th outstanding question (1-based) for a scope.

        This is what replaces "go and copy a token": the same ambiguity that
        made a bare "yes" unusable is resolvable by number, so several pending
        questions are answerable in one reply.
        """
        rows = self.pending(scope, at=at)
        if not 1 <= index <= len(rows):
            return None
        return self.approve(rows[index - 1].token, scope=scope, at=at)

    def summary(
        self, scope: Any = None, *, at: Optional[float] = None
    ) -> list[str]:
        """One line per outstanding question, numbered, for a chat reply."""
        return [
            f"{position}. {record.summary}"
            for position, record in enumerate(
                self.pending(scope, at=at), start=1
            )
        ]

    def clear(self) -> None:
        """Forget everything.  Tests and teardown only."""
        self._once.clear()
        self._session.clear()
        self._pending.clear()
        self._always.clear()
        self._loaded.clear()


#: The process-wide store.  One instance, because the whole point is that every
#: consent kind writes the same record; tests construct their own.
SHELL = ConsentStore()


def default_ledger_root() -> Path:
    """Where ``always`` answers live for this agent home.

    Resolved at call time, not import time: the home is chosen per session
    (``~/.agent``, ``~/.agent-<name>``), so a path frozen at import would write
    every session's trust into whichever home happened to be first.
    """
    from agent import shared

    return Path(shared.AGENT_HOME).expanduser() / _TRUST_DIRNAME


def configure(ledger_root: Optional[Path]) -> None:
    """Point the process-wide store at (or away from) a trust ledger."""
    SHELL.ledger_root = ledger_root


__all__ = [
    "ALWAYS",
    "COMMAND",
    "ConsentStore",
    "KINDS",
    "LIFETIMES",
    "ONCE",
    "PATH",
    "PLUGIN",
    "PROGRAM",
    "PROMPT_TTL_SECONDS",
    "Pending",
    "Remembered",
    "SHELL",
    "SESSION",
    "TOOL",
    "configure",
    "default_ledger_root",
    "make_rule",
    "rule_kind",
    "rule_value",
    "scope_key",
    "workspace_key",
]
