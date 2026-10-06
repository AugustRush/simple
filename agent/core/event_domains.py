"""Event domains: named sets of event types a listener can subscribe to together.

Why this is a layer over the existing bus and not a second bus
--------------------------------------------------------------
``EventBus`` already dispatches by exact event class, so two domains sharing one
bus cannot see each other's events: a listener registered for ``LLMResponse`` is
never called for ``ToolPostExecute``.  A bus per domain would therefore add a way
to *lose* a listener -- register on the wrong instance and nothing arrives, which
is the failure the tool bus's module-level comment already warns about ("a
per-instance bus would mean a plugin's gate applied to whichever construction path
happened to pass one").

So a domain is vocabulary, not transport: it names the event types that belong to
one part of the turn, and it can subscribe a listener to all of them at once,
returning one unsubscribe callable.  That last part matters because a plugin
subscribes several handlers and unloads as a unit -- the tool catalog already
builds a tuple of unsubscribes and calls them together, and this makes the same
shape available per domain.

Two systems, two jobs
---------------------
The turn loop already announces its phases through ``_emit_event``, which writes
to an :class:`~agent.core.events.EventCollector` -- a *persisted runtime log*.  A
typed event is a different job: an in-process extension point a plugin subscribes
to.  Both are kept.  These types are the extension half, and nothing here changes
what the log records.

The refusal semantic is why ``tool`` is declared here too
---------------------------------------------------------
``ToolPreExecute`` is the one event a listener can *block*, and
``EventBus.emit_pre_execute`` is the only delivery that stops early.  A domain that
silently inherited that -- an LLM listener whose truthy return stopped every
listener behind it -- would be a bug with no local symptom.  The domains are
declared as data so the distinction is visible in one table, and
``tests/test_event_domains.py`` asserts that only ``tool`` carries it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from agent.core.events import EventBus, ToolPostExecute, ToolPreExecute

__all__ = [
    "Domain",
    "TOOL",
    "TURN",
    "LLM",
    "DOMAINS",
    "domain",
    "domain_names",
    "TurnStarted",
    "TurnFinished",
    "LLMRequestStarted",
    "LLMResponseReceived",
]


# ── turn/* ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TurnStarted:
    """A turn began: one user message, one or more steps."""

    agent_id: str
    tools_enabled: bool
    streamed: bool


@dataclass(frozen=True)
class TurnFinished:
    """A turn ended, successfully or not.

    ``status`` is the turn's own trace status -- ``"ok"``, ``"cancelled"``,
    ``"tool_loop_stuck"`` and so on -- passed through rather than mapped, because
    the log and the extension point describing one turn with two different
    vocabularies is how they drift.
    """

    agent_id: str
    status: str
    steps: int


# ── llm/* ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LLMRequestStarted:
    """A model call is about to be made.

    ``attempt`` is one-based and counts the *retries* of this step: a transient
    failure that is retried announces a second request, which is what makes a
    flaky provider visible to a listener without it parsing logs.
    """

    agent_id: str
    model: str
    tools_offered: int
    attempt: int = 1


@dataclass(frozen=True)
class LLMResponseReceived:
    """A model call returned."""

    agent_id: str
    stop_reason: str
    duration_ms: float
    tool_uses: int = 0


@dataclass(frozen=True)
class Domain:
    """A named group of event types belonging to one part of a turn.

    ``blocking`` records whether a listener may refuse the action being announced.
    Only the tool pipeline can: a gate on a tool call has a caller waiting for a
    verdict, while everything else is a notification.
    """

    name: str
    event_types: tuple[type, ...]
    blocking: bool = False

    def subscribe(
        self, bus: EventBus, listener: Callable[[Any], Any]
    ) -> Callable[[], None]:
        """Register *listener* for every event type in this domain.

        Returns one callable that removes all of them, so a plugin that unloads
        does not have to track each registration -- the failure mode being a
        listener left on a bus after its plugin is gone, called forever and
        raising into the isolation handler.
        """
        undo: list[Callable[[], None]] = [
            bus.on(event_type, listener) for event_type in self.event_types
        ]

        def unsubscribe() -> None:
            for remove in undo:
                remove()

        return unsubscribe

    def to_payload(self) -> dict[str, Any]:
        """Public shape, so a UI or a doc lists domains without hardcoding."""
        return {
            "name": self.name,
            "blocking": self.blocking,
            "events": [event_type.__name__ for event_type in self.event_types],
        }


TOOL = Domain(
    name="tool",
    event_types=(ToolPreExecute, ToolPostExecute),
    blocking=True,
)

TURN = Domain(
    name="turn",
    event_types=(TurnStarted, TurnFinished),
    blocking=False,
)

LLM = Domain(
    name="llm",
    event_types=(LLMRequestStarted, LLMResponseReceived),
    blocking=False,
)

DOMAINS: dict[str, Domain] = {d.name: d for d in (TOOL, TURN, LLM)}


def domain(name: str) -> Domain:
    """The domain called *name*.  Raises for an unknown one.

    Loud rather than returning an empty domain: a listener subscribed to a typo'd
    domain would be registered for nothing and never called, which looks exactly
    like a plugin that works.
    """
    try:
        return DOMAINS[name]
    except KeyError:
        raise KeyError(
            f"unknown event domain {name!r}; known: {', '.join(domain_names())}"
        ) from None


def domain_names() -> tuple[str, ...]:
    return tuple(DOMAINS)
