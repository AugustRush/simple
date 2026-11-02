"""What the agent *would* have done, written down instead of done.

Why this module exists
----------------------
An autonomous agent's first honest question is not "what can it do" but "what
would it have done, and would I have agreed".  Neither question can be answered
by running the loop -- a loop that acts has already spent the attention it was
supposed to save.  So the first version of autonomy here **proposes and records,
and does not execute**: every candidate the patrol finds becomes a row, and the
row says what it wanted to do and why.

That is the whole design decision.  Everything else (the funnel, the cost
budget, the push policy) is calibrated *from* these rows.  Without them, "96.4%
of ticks are filtered" is somebody else's deployment, and "confidence 0.8" is a
number with no evidence behind it.

What a candidate is, and is not
-------------------------------
A row is a **proposal**, not a plan and not a log entry:

* it has a **source** -- which session or event raised it -- so the same
  suggestion can be recognised as the same one;
* it has an **expiry**, because a suggestion about yesterday is not a
  suggestion about today;
* it has a **disposition** (`proposed` / `accepted` / `declined` / `expired` /
  `acted`), and *declined is a fact worth keeping*: the failure mode this table
  exists to prevent is the same suggestion coming back in different words, which
  is what happens when "somebody already said no" lives only in a context window.

Why reversibility is not a model's opinion
------------------------------------------
``reversible`` is derived from the **tool's declared capabilities**
(``read`` / ``output_write`` / ``state_write`` / ``side_effect`` / ``shell``), not
from prose the model wrote about itself.  The party being supervised does not get
to grade its own homework: a model that says "this is safe" is exactly the
failure this check exists to catch.  The mapping lives in one function below, so
"what counts as reversible" is a decision somebody made once, in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from .admission import AdmissionPolicy, decide_admission

__all__ = [
    "AUTONOMY_STATES",
    "DEFAULT_EXPIRY_SECONDS",
    "Proposal",
    "classify_reversibility",
    "MAX_PROPOSAL_ACTION_CHARS",
    "MAX_PROPOSAL_REASON_CHARS",
    "MAX_PROPOSALS_PER_TICK",
]

#: The dispositions a candidate can end in.  ``proposed`` is the only non-final
#: one; the rest are kept so a suggestion that was declined is recognisably the
#: same suggestion the next time it is raised.
AUTONOMY_STATES = ("proposed", "accepted", "declined", "expired", "acted")

#: How long a proposal stays live.  A day: long enough that a morning patrol's
#: suggestion is still actionable in the evening, short enough that a stale
#: suggestion does not outlive the situation that raised it.
DEFAULT_EXPIRY_SECONDS = 24 * 60 * 60

#: Bounds, because these strings come from a model and land in a table that is
#: read on every patrol.  A proposal is a sentence, not a transcript.
MAX_PROPOSAL_ACTION_CHARS = 200
MAX_PROPOSAL_REASON_CHARS = 300

#: How many candidates one patrol may write.  A tick that produces forty
#: suggestions is not finding work, it is flooding the table the next tick reads.
MAX_PROPOSALS_PER_TICK = 5

UTC = timezone.utc


def _iso(value: Optional[datetime]) -> str:
    return value.astimezone(UTC).isoformat() if value is not None else ""


def _now() -> datetime:
    return datetime.now(UTC)


#: Capabilities that mean "this changes something outside the agent's own
#: scratch space".  A tool that declares only ``read`` cannot have done damage;
#: one that declares ``side_effect`` or ``shell`` can have done anything.
_IRREVERSIBLE_CAPABILITIES = frozenset({"side_effect", "shell", "state_write"})

#: ``output_write`` writes files, which is reversible in the sense that matters
#: here: the file is in the workspace, a person can see it, and nothing outside
#: it changed.  It is still recorded as "reversible with a visible trace", which
#: is the distinction a reviewer needs.
_WORKSPACE_ONLY_CAPABILITIES = frozenset({"output_write"})


def classify_reversibility(capabilities: Any) -> str:
    """``"reversible"``, ``"workspace"`` or ``"irreversible"`` from a tool's own declaration.

    The three answers exist because "can this be undone" is not a yes/no:

    * **reversible** -- reads only.  Nothing outside the agent changed.
    * **workspace** -- writes files inside the workspace.  Undoable by a person
      who can see the file, and it leaves a trace to review.
    * **irreversible** -- state writes, side effects, shell.  A message sent, a
      record deleted, a command run.  These wait for a person, always.
    """
    declared = {str(item).strip().lower() for item in (capabilities or ())}
    if declared & _IRREVERSIBLE_CAPABILITIES:
        return "irreversible"
    if declared & _WORKSPACE_ONLY_CAPABILITIES:
        return "workspace"
    return "reversible"


@dataclass
class Proposal:
    """One thing the agent would have done, and the reason it would have done it."""

    id: str = field(default_factory=lambda: uuid4().hex)
    #: Which session or event raised it.  The deduplication key, together with
    #: the action: "the same suggestion about the same thing" is what must not
    #: come back after being declined.
    source: str = ""
    #: Why this candidate exists, in the agent's own words -- what it noticed.
    reason: str = ""
    #: What it would do, as one sentence a person can disagree with.
    action: str = ""
    #: The tool it would call, when that is known.  This is what makes the
    #: reversibility classification possible at all.
    tool: str = ""
    #: ``reversible`` / ``workspace`` / ``irreversible`` -- from the tool's
    #: declaration, never from the model's opinion of itself.
    reversibility: str = "irreversible"
    #: The model's own confidence, kept as *evidence* rather than as a gate.  The
    #: gate is the cost budget and the reversibility class; this number is what
    #: the calibration is done from later.
    confidence: float = 0.0
    state: str = "proposed"
    #: Free-form facts the proposal carried: how it was found, which rule passed
    #: it, what it estimated the cost at.  A row without this cannot be audited.
    context: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now)
    expires_at: datetime = field(
        default_factory=lambda: _now() + timedelta(seconds=DEFAULT_EXPIRY_SECONDS)
    )
    #: Set when a person (or the loop) answered it, with the words used.
    decided_at: Optional[datetime] = None
    decided_by: str = ""

    def is_live(self, now: Optional[datetime] = None) -> bool:
        return self.state == "proposed" and (now or _now()) < self.expires_at

    def dedupe_key(self) -> tuple[str, str]:
        """What makes this the same suggestion as an earlier one.

        Source plus action, lowercased and stripped: the *same* thing, asked
        twice, is not two proposals.  Deliberately not the reason -- the reason
        is how a model rephrases itself, and that is exactly what drifts.
        """
        return (
            self.source.strip().lower(),
            " ".join(self.action.split()).strip().lower(),
        )

    def to_row(self) -> dict[str, Any]:
        """The shape the store writes.  Kept here so the two cannot drift."""
        return {
            "id": self.id,
            "source": self.source,
            "reason": self.reason[:MAX_PROPOSAL_REASON_CHARS],
            "action": self.action[:MAX_PROPOSAL_ACTION_CHARS],
            "tool": self.tool,
            "reversibility": self.reversibility,
            "confidence": float(self.confidence),
            "state": self.state,
            "context_json": self.context,
            "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at),
            "decided_at": _iso(self.decided_at),
            "decided_by": self.decided_by,
        }


# ── the patrol: a tick that proposes but never executes ─────────────────────


@dataclass
class PatrolOptions:
    """What the shadow patrol is allowed to do this tick.

    All costs are hard caps, not expectations: the design principle this is the
    first test of is "autonomy spends bounded attention, or it spends none".
    """

    #: Sessions with a turn in the last N seconds are "warm" and skipped: a
    #: conversation somebody is in does not need a second voice suggesting
    #: things.  0 disables the check (every session is eligible).
    warm_window_seconds: int = 15 * 60
    #: Sessions silent for longer than this are "stale" and skipped -- a week of
    #: silence is a person who left, and proposing into it is noise.
    stale_window_seconds: int = 7 * 24 * 60 * 60
    #: How many sessions the patrol may examine in one tick.
    max_sessions_per_tick: int = 20
    #: How many proposals one tick may write (the store enforces its own cap too).
    max_proposals_per_tick: int = 5
    #: What may be done without asking. Defaults to "nothing": shadow mode, which
    #: is what this delivery is.
    admission: Any = None


def default_capabilities_of(tool: str) -> frozenset[str]:
    """The capabilities a built-in tool declares, for the reversibility check.

    A lookup rather than an import-time table: the registry owns the vocabulary
    (``ToolRegistry._DEFAULT_TOOL_CAPABILITIES``), and copying it here would be a
    second place for "what this tool may do" to drift.  An unknown tool answers
    the empty set, which classifies as irreversible -- the safe direction.
    """
    try:
        from agent.tools.runtime import ToolRegistry

        return frozenset(ToolRegistry._DEFAULT_TOOL_CAPABILITIES.get(("builtin", tool), ()))
    except Exception:
        return frozenset()


def patrol_tick(
    *,
    store: Any,
    sessions: list[dict[str, Any]],
    now: Optional[datetime] = None,
    options: Optional[PatrolOptions] = None,
    suggest: Any = None,
    capabilities_of: Any = None,
) -> dict[str, Any]:
    """One shadow pass: read the room, propose what *would* be done, write nothing else.

    This is the whole first delivery.  It does not call a model, does not run a
    tool, does not touch a session's context.  It reads the session directory
    (sizes and activity), applies the two silence gates, and asks ``suggest`` --
    which in v1 is a rules function and in v2 may be a model -- for each
    remaining session.  Whatever it says becomes a row in the proposals table,
    and the loop's only effect on the world is that row.

    ``suggest`` is a callable ``(session) -> list[Proposal] | Proposal | None``.
    The patrol does the bookkeeping: which sessions are eligible, the caps, the
    dedupe, the audit dict returned to the caller.
    """
    now = now or _now()
    options = options or PatrolOptions()
    if options.admission is None:
        options.admission = AdmissionPolicy()
    warm = timedelta(seconds=options.warm_window_seconds)
    stale = timedelta(seconds=options.stale_window_seconds)
    result: dict[str, Any] = {
        "tick_at": _iso(now),
        "examined": 0,
        "eligible": 0,
        "skipped_warm": 0,
        "skipped_stale": 0,
        "proposed": 0,
        "duplicates": 0,
        "dropped": 0,
        "auto": 0,
        "over_cap": 0,
        "proposals": [],
    }

    if options.max_sessions_per_tick <= 0:
        return result

    examined = 0
    for session in sessions[: options.max_sessions_per_tick]:
        examined += 1
        last_active = _parse_dt(session.get("last_activity") or session.get("created_at"))
        if last_active is None:
            last_active = now
        age = now - last_active

        if options.warm_window_seconds and age <= warm:
            result["skipped_warm"] += 1
            continue
        if options.stale_window_seconds and age > stale:
            result["skipped_stale"] += 1
            continue
        result["eligible"] += 1

        if suggest is None:
            continue
        try:
            raw = suggest(
                session,
                now=now,
                capabilities_of=capabilities_of or default_capabilities_of,
            )
        except Exception as exc:  # noqa: BLE001 - the patrol must not die on one session
            result["over_cap"] += 1  # reuse: a failure is also not a proposal
            result.setdefault("errors", []).append(str(exc))
            continue

        candidates = _as_list(raw)
        for proposal in candidates[: options.max_proposals_per_tick]:
            result["over_cap"] += max(0, len(candidates) - options.max_proposals_per_tick)
            if not isinstance(proposal, Proposal):
                continue
            if not proposal.action.strip():
                continue
            # Declined is a fact, not a gap: a suggestion somebody already
            # answered must not come back in new words.
            prior = store.already_decided(proposal.source, proposal.action)
            if prior:
                result["duplicates"] += 1
                continue
            # Admission decides what may happen *to* the candidate, and it is asked
            # here rather than at execution time so the row can carry the answer:
            # a "prepare" row is the queue a person answers, and a "drop" row is
            # not written at all.
            decision = decide_admission(
                proposal,
                now=now,
                policy=options.admission,
                auto_actions_today=result["proposed"],
                proposals_today=result["proposed"],
                prior_decision=prior,
            )
            if decision.answer == "drop":
                result["dropped"] += 1
                continue
            proposal.context["admission"] = decision.to_dict()
            if store.record(proposal):
                result["proposed"] += 1
                if decision.answer == "auto":
                    # Even "auto" does not execute here: this delivery has no
                    # execution path at all. The row records what *would* run.
                    result["auto"] += 1
                result["proposals"].append(proposal.to_row())
    result["examined"] = examined
    return result


def _as_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    return [raw]


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
