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
``EventBus.emit_blocking`` is the only delivery that stops early.  A domain that
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
    "bus_for",
    "TurnStarted",
    "TurnFinished",
    "TurnCompleted",
    "SessionFinished",
    "LLMRequestStarted",
    "LLMResponseReceived",
    "LLMResponseParsed",
    "LLMRetryScheduled",
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
class TurnCompleted:
    """A turn finished, with what the turn *was* rather than how it went.

    Distinct from :class:`TurnFinished`, and the distinction was measured: that one
    carries ``status`` and ``steps`` -- an outcome, for a log or a dashboard -- while
    this carries the prompt, the response and the tools used, which is what a plugin
    needs in order to *look at* a turn.  ``TurnFinished`` will not do: a subscriber
    wanting the text finds three fields and none of them is text.

    It exists because the one real plugin needed it.  ``EvolutionPlugin`` learns from
    corrections, which means reading the user's input against the agent's answer, and
    the hook protocol it still uses passes exactly these four things.  The kernel's
    turn vocabulary had no equivalent, so migrating that plugin was blocked on
    vocabulary rather than on mechanism -- see
    ``docs/superpowers/plans/2026-10-02-plugin-kernel-status.md``.

    Emitted *before* the turn's request is cleared from the context metadata, because
    that is where ``user_input`` lives; clearing first and emitting after would leave
    this event describing every turn as an empty prompt.
    """

    agent_id: str
    user_input: str
    response: str
    tool_calls: tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionFinished:
    """An interactive session ended, with the transcript a plugin would score.

    The second type the hook protocol had and the kernel did not.  ``EvolutionPlugin``
    scores a session when it ends -- it reads ``messages`` and ``tools_used`` -- and the
    kernel's three domains were ``tool``, ``turn`` and ``llm``, none of which is a
    session.  A turn is finished many times per session; this is finished once.

    ``messages`` is the transcript as the session had it, not a copy: a listener that
    mutates it is mutating what the session recorded, and the one real consumer only
    reads.  Copied per emit would be a deep copy of a whole conversation on a path that
    runs once, which is affordable but pointless while nobody mutates.
    """

    agent_id: str
    messages: list[dict]
    tools_used: list[str]
    session_id: str = ""
    turn_count: int = 0


@dataclass(frozen=True)
class LLMRequestStarted:
    """A model call is about to be made.

    Emitted by the call itself, not by the retry wrapper, because two callers
    reach the model without that wrapper (a continuation and the direct path at
    ``agent.py:2001``).  Announcing from the wrapper would have missed them.
    """

    agent_id: str
    model: str
    tools_offered: int


@dataclass(frozen=True)
class LLMRetryScheduled:
    """A transient failure is about to be retried after a delay.

    A separate event rather than a second :class:`LLMRequestStarted` with a higher
    ``attempt``, because the attempt count is only knowable inside the retry
    wrapper while the request is only knowable inside the call.  Merging them
    would mean passing one of the two through a context variable, or emitting one
    of them twice.
    """

    agent_id: str
    model: str
    attempt: int
    delay_seconds: float
    error: str


@dataclass(frozen=True)
class LLMResponseReceived:
    """A model call returned.

    Deliberately carries no ``stop_reason``.  The call knows a response arrived and
    how long it took, but the normalised stop reason is produced by the transport's
    parser, which runs later -- reading a field off the provider's own object here
    would report the *provider's* vocabulary (``"stop"``, ``"tool_calls"``) under a
    name the rest of the codebase reads as normalised.  A test caught exactly that:
    the field came back empty because the attribute lives on a choice, not on the
    response.

    The parsed form is :class:`LLMResponseParsed`.
    """

    agent_id: str
    duration_ms: float
    tool_calls: int = 0


@dataclass(frozen=True)
class LLMResponseParsed:
    """The response was understood: stop reason and tool calls are known.

    Separate from :class:`LLMResponseReceived` because the two happen at different
    layers, and collapsing them would force one of the layers to state something it
    does not know.
    """

    agent_id: str
    stop_reason: str
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

    def attach(self, ctx: Any, listener: Callable[[Any], Any]) -> Callable[[], None]:
        """Subscribe *listener* to this domain and bind the undo to *ctx*.

        The form a plugin uses, because the bus is **not** the context's: this
        codebase keeps one bus per domain, and ``ctx.bus`` is a single bus for
        events the composition itself defines.  Registering a turn handler on the
        context's bus would produce a listener that is never called -- the failure
        mode with no symptom, indistinguishable from a plugin that works.

        The domain table therefore owns the answer to "which bus", which is where
        :func:`bus_for` already keeps it, and the caller gets one call that both
        subscribes and makes the subscription reversible.
        """
        undo = self.subscribe(bus_for(self), listener)
        ctx.on_unwind(undo)
        return undo

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
    event_types=(TurnStarted, TurnFinished, TurnCompleted),
    blocking=False,
)

LLM = Domain(
    name="llm",
    event_types=(
        LLMRequestStarted,
        LLMResponseReceived,
        LLMResponseParsed,
        LLMRetryScheduled,
    ),
    blocking=False,
)

SESSION = Domain(
    name="session",
    event_types=(SessionFinished,),
    blocking=False,
)

DOMAINS: dict[str, Domain] = {d.name: d for d in (TOOL, TURN, LLM, SESSION)}


def bus_for(domain: Domain) -> EventBus:
    """The bus *domain* publishes to.

    Kept here, next to the domain table, so a new domain has one place to say
    where it lives -- and so the answer is checkable rather than a convention a
    subscriber has to guess.  Imported lazily because ``agent.core.events`` builds
    its buses at import time and this module is imported from there.
    """
    from agent.core import events as buses

    _BY_NAME = {
        "tool": buses.TOOL_EVENTS,
        "turn": buses.TURN_EVENTS,
        "session": buses.SESSION_EVENTS,
        "llm": buses.LLM_EVENTS,
    }
    try:
        return _BY_NAME[domain.name]
    except KeyError:
        raise KeyError(
            f"no bus registered for domain {domain.name!r}; known: "
            f"{', '.join(sorted(_BY_NAME))}"
        ) from None


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
