"""What the agent noticed, what it may do about it, and what it actually did.

Why this module exists
----------------------
An autonomous agent's first honest question is not "what can it do" but "what
would it have done, and would I have agreed".  The patrol here answers the
first half from facts already on disk, admission answers the second, and every
candidate that survives either way becomes a row that says what it wanted to do
and why.

Which is where it ends for anything that could change the world outside the
workspace: that becomes a ``prepare`` row and waits for a person.  A
**reversible** candidate (a tool that declared ``read`` and nothing else) is the
one class admission answers ``auto`` for, and the patrol may hand it to an
executor without asking -- under a permission profile that denies the things
the classification said it could not do.

Everything else (the funnel, the admission policy, the push policy) is
calibrated *from* these rows.  Without them, "96.4% of ticks are filtered" is
somebody else's deployment, and "confidence 0.8" is a number with no evidence
behind it.

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
    "unowned_task_rows",
    "is_own_errand",
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

    * **reversible** -- the tool declares ``read`` and no capability that writes
      or acts.  Nothing outside the agent changed.
    * **workspace** -- the tool writes files (``output_write``).  Undoable by a
      person who can see the file, and it leaves a trace to review.
    * **irreversible** -- state writes, side effects, shell.  A message sent, a
      record deleted, a command run.  These wait for a person, always.

    **No declaration is not a clean bill of health.**  A tool that answers "no
    capabilities" -- an unknown name, a misspelled one, a plugin nobody
    classified -- did not say it was read-only, so it must not share an answer
    with a tool that did: ``reversible`` is exactly what admission turns into
    "do it without asking".  The empty set is therefore **irreversible**, and so
    is any declaration this function does not recognise as safe.  Only an
    explicit ``read`` earns the automatic path; the builtin table classifies
    every registered tool, so a real tool never lands on that branch.
    """
    declared = {str(item).strip().lower() for item in (capabilities or ())}
    if not declared:
        # Unknown tool, misspelled name, plugin with no declaration: it did not
        # say "read only", so it is treated as if it could do anything.
        return "irreversible"
    if declared & _IRREVERSIBLE_CAPABILITIES:
        return "irreversible"
    if declared & _WORKSPACE_ONLY_CAPABILITIES:
        return "workspace"
    if "read" in declared:
        return "reversible"
    # It declared something, but not a read.  A vocabulary this function does not
    # know is not evidence of safety, so the answer stays conservative.
    return "irreversible"


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
    #: The model's own confidence, kept as *evidence* rather than as the gate.  The
    #: gates are the confidence floor and the reversibility class; this number is
    #: what the calibration is done from later.
    confidence: float = 0.0
    state: str = "proposed"
    #: Free-form facts the proposal carried: how it was found, which rule raised
    #: it, what it would call.  A row without this cannot be audited.
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
    """What the patrol is allowed to do this tick.

    All bounds are hard caps, not expectations: the design principle this is the
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
    #: What may be done without asking.  ``None`` means the shipped
    #: :class:`AdmissionPolicy`: a reversible candidate may run, and anything
    #: that could change the world outside the workspace is prepared for a
    #: person instead.
    admission: Any = None


def default_capabilities_of(tool: str) -> frozenset[str]:
    """The capabilities a built-in tool declares, for the reversibility check.

    A lookup rather than an import-time table: the registry owns the vocabulary
    (``ToolRegistry._DEFAULT_TOOL_CAPABILITIES``), and copying it here would be a
    second place for "what this tool may do" to drift.  An unknown tool answers
    the empty set, which :func:`classify_reversibility` reads as ``irreversible``
    -- the safe direction: "no capabilities declared" is not "declared safe",
    and only the latter may run without asking a person.
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
    on_auto: Any = None,
) -> dict[str, Any]:
    """One pass: read the room, decide what may happen, write and act on the verdict.

    It does not call a model itself and does not run a tool: it reads the session
    rows it is handed, applies the two silence gates, and asks ``suggest`` --
    which in v1 is a rules function and in v2 may be a model -- for each
    remaining row.  Admission then decides each candidate's fate: a candidate it
    answers ``auto`` for is handed to ``on_auto`` (when the caller owns an
    executor) in priority order, and everything else becomes a row in the
    proposals table.  The function's own effect on the world is those rows plus
    whatever ``on_auto`` does; a caller that only wants to look passes no
    ``on_auto``.

    ``suggest`` is a callable ``(session) -> list[Proposal] | Proposal | None``.
    The patrol does the bookkeeping: which rows are eligible, the caps, the
    dedupe, the audit dict returned to the caller.
    """
    now = now or _now()
    options = options or PatrolOptions()
    if options.admission is None:
        options.admission = AdmissionPolicy()
    warm = timedelta(seconds=options.warm_window_seconds)
    stale = timedelta(seconds=options.stale_window_seconds)
    auto_queue: list[Proposal] = []
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
        "failed": 0,
        "proposals": [],
    }

    if options.max_sessions_per_tick <= 0:
        return result

    # Task rows before session rows, inside the same budget.
    #
    # The cap exists to bound the work one pass may do, not to rank what it looks
    # at. But a plain `sessions[:N]` ranks by list order, and the task rows come
    # last -- so on an install with twenty-odd sessions every row that carries a
    # *failure* would be dropped before it was ever read, while twenty quiet
    # conversations took its place. A task row exists only because something
    # already went wrong, so it is the cheapest possible thing to spend the
    # budget on first.
    task_rows = [row for row in sessions if str(row.get("status") or "") == "task"]
    others = [row for row in sessions if str(row.get("status") or "") != "task"]
    budget = options.max_sessions_per_tick
    selected = task_rows[:budget] + others[: max(0, budget - min(len(task_rows), budget))]

    examined = 0
    for session in selected:
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
            # Not a cap hit: one row's suggestion function raised.  Recorded under
            # its own key so "over_cap" keeps meaning exactly one thing.
            result["failed"] += 1
            result.setdefault("errors", []).append(str(exc))
            continue

        candidates = _as_list(raw)
        # Count the candidates this row's cap drops *once*, not once per kept
        # candidate -- the per-row cap is the literal thing being counted.
        if len(candidates) > options.max_proposals_per_tick:
            result["over_cap"] += len(candidates) - options.max_proposals_per_tick
        for proposal in candidates[: options.max_proposals_per_tick]:
            if not isinstance(proposal, Proposal):
                continue
            if not proposal.action.strip():
                continue
            # The pass stamps its own findings.
            #
            # `rule_candidates` builds each proposal with `datetime.now()`, which
            # is right when a rule is called directly and wrong when a pass runs
            # at a time somebody chose: a replayed or backfilled pass would write
            # timestamps from whenever it happened to execute, and a proposal is
            # an assertion about *when* the fact was seen. Found by a test that
            # passed on the day it was written and failed the next morning, which
            # is the signature of a clock leaking into policy.
            proposal.created_at = now
            proposal.expires_at = now + timedelta(seconds=DEFAULT_EXPIRY_SECONDS)
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
                prior_decision=prior,
                # The exact call, when the rule knows it.  A "prepare" row
                # without arguments is a question the reader has to reconstruct,
                # which is the thing the middle answer exists to avoid.
                arguments=(proposal.context or {}).get("arguments"),
            )
            if decision.answer == "drop":
                result["dropped"] += 1
                continue
            proposal.context["admission"] = decision.to_dict()
            if store.record(proposal):
                result["proposed"] += 1
                if decision.answer == "auto":
                    result["auto"] += 1
                    auto_queue.append(proposal)
                result["proposals"].append(proposal.to_row())
    # Act on what admission allowed, **one at a time and in priority order**.
    #
    # The pass itself still executes nothing: it hands candidates to whoever owns
    # an executor -- the patrol service owns one, this function does not -- so the
    # funnel stays pure and a caller that only wants to look passes nothing.
    #
    # Priority, not budget. There is no daily count here on purpose: the number
    # of candidates is bounded by the facts (a rule fires at most once per
    # session per fact, and an answered proposal never returns), so a cap would
    # only decide *which* finite set of work gets skipped -- and it would skip it
    # by arrival time rather than by importance. Ordering makes the important ones
    # first instead, which is what "do the work that is there" actually needs.
    if callable(on_auto):
        auto_queue.sort(
            key=lambda item: (
                int((item.context or {}).get("priority", 99))
                if isinstance(item.context, dict)
                else 99,
                # Oldest first inside a rank: the thing that has been waiting
                # longest is the one that has cost the most to leave alone.
                item.created_at,
            )
        )
        for proposal in auto_queue:
            try:
                if on_auto(proposal):
                    result["executed"] = int(result.get("executed", 0)) + 1
            except Exception as exc:  # noqa: BLE001 - one action must not stop the rest
                result.setdefault("errors", []).append(str(exc))
    result["examined"] = examined
    return result


def live_queue_facts(live_sessions: Any, now: Optional[datetime] = None) -> dict[str, dict[str, Any]]:
    """Per-session queue facts, from the process that is actually serving them.

    A queue only exists while something is serving it: a session nobody is running
    has nothing waiting *in memory*. So this is not a lookup -- the caller has the
    live states or it does not, and a missing session means "no queue", which is
    different from "an empty queue".

    ``queued_since`` is the oldest waiting item's arrival, because the rule asks
    "how long has this been waiting" and the session's last activity is a
    different fact (a conversation somebody is typing in has a recent session and
    a queue that has not moved).
    """
    facts: dict[str, dict[str, Any]] = {}
    if not isinstance(live_sessions, dict):
        return facts
    for session_id, state in live_sessions.items():
        interjections = list(getattr(state, "pending_interjections", []) or [])
        restarts = list(getattr(state, "restart_queue", []) or [])
        pending = len(interjections) + len(restarts)
        if pending <= 0:
            continue
        arrivals = [
            float(entry.get("arrived_at") or 0.0)
            for entry in (*interjections, *restarts)
            if isinstance(entry, dict) and entry.get("arrived_at")
        ]
        entry: dict[str, Any] = {"pending": pending}
        if arrivals:
            oldest = min(arrivals)
            # arrived_at is an epoch float from `time.time()`; the rules compare
            # datetimes, so convert once, here.
            entry["queued_since"] = datetime.fromtimestamp(oldest, UTC).isoformat()
        facts[str(session_id)] = entry
    return facts


def is_own_errand(payload: Any) -> bool:
    """Whether a scheduled task is the agent's own work rather than a person's.

    The patrol executes a reversible candidate by creating a one-off task, and
    that task carries ``origin: "autonomy"`` in its payload (see
    ``execute.ExecutionOutcome``). Those tasks are invisible to the funnel: their
    failure belongs on the proposal that started them, not in a new round of
    "something failed, go and read it". Without this the two facts feed each
    other -- a failed autonomous read is attention, attention is a proposal, the
    proposal is another autonomous read -- and every cycle has a new task id, so
    dedupe cannot break it.

    A payload is free-form, so this answers ``False`` for anything it cannot read:
    treating a person's task as the agent's own would hide a real failure, while
    the opposite mistake only costs one redundant proposal.
    """
    return isinstance(payload, dict) and payload.get("origin") == "autonomy"


def unowned_task_rows(
    tasks: list[dict[str, Any]],
    *,
    attention_counts: dict[str, int],
    placed_task_ids: set[str],
    latest_run_at: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """Rows for tasks whose unreviewed failures belong to no session row.

    A failed run is a fact about a *task*, but the patrol's rules fire per
    session.  A task created without a session id -- every task the patrol
    itself executes is created that way -- has no conversation to hang its
    attention on, so ``failed-run-unreviewed`` cannot see the loss, and "no
    proposals" reads exactly like "nothing failed".  These rows give such a task
    its own source (``task:<id>``) so the rule can fire for it.

    ``tasks`` carries the plain facts, one dict per task: ``task_id``, ``name``,
    ``last_run_at`` and ``created_at`` (ISO strings).  ``latest_run_at`` maps a
    task id to its newest run's time, and that is the row's ``last_activity``;
    the fallbacks are the task's own timestamps.  It has to be a real time:
    the patrol's warm/stale gates compare activity, and a fabricated "now" would
    make the row permanently warm and therefore never examined.

    ``placed_task_ids`` is the set of tasks already shown under a session row.
    A task in it is skipped here, so one failure cannot become two proposals
    with different sources -- which would also walk around the dedupe.
    """
    rows: list[dict[str, Any]] = []
    latest = latest_run_at or {}
    for task in tasks:
        task_id = str(task.get("task_id") or "").strip()
        if not task_id or task_id in placed_task_ids:
            continue
        attention = int(attention_counts.get(task_id) or 0)
        if attention <= 0:
            continue
        rows.append(
            {
                "session_id": f"task:{task_id}",
                "title": str(task.get("name") or "").strip() or task_id[:8],
                "last_activity": str(
                    latest.get(task_id)
                    or task.get("last_run_at")
                    or task.get("created_at")
                    or ""
                ),
                "status": "task",
                "tasks": [
                    {
                        "task_id": task_id,
                        "name": str(task.get("name") or ""),
                        "attention": attention,
                        "failures": list(task.get("failures") or []),
                        "timeout_seconds": int(task.get("timeout_seconds") or 0),
                        "retry_policy": dict(task.get("retry_policy") or {}),
                    }
                ],
            }
        )
    return rows


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
