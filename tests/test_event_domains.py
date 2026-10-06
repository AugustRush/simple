"""Event domains: vocabulary over the one bus, not a bus per domain.

The bus dispatches by exact event class, so two domains on one bus cannot see each
other's events.  That is what makes a domain a *naming* concern, and what makes a
bus per domain a way to lose a listener rather than a way to isolate one.

Two properties are worth pinning because breaking either is silent:

- only the ``tool`` domain is blocking, because only that event has a caller
  waiting for a verdict.  A domain that inherited it would stop every listener
  behind the first truthy return, with no local symptom.
- subscribing to a domain returns *one* unsubscribe callable.  A plugin unloads as
  a unit, and a registration left behind is called forever, raising into the
  isolation handler.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.core.event_domains import (
    DOMAINS,
    LLM,
    TOOL,
    TURN,
    Domain,
    LLMRequestStarted,
    LLMResponseParsed,
    LLMResponseReceived,
    TurnFinished,
    TurnStarted,
    domain,
    domain_names,
)
from agent.core.events import TOOL_EVENTS, EventBus, ToolPostExecute, ToolPreExecute


def _async_listener(seen: list):
    async def listener(event):
        seen.append(event)
        return None

    return listener


# ── The table ────────────────────────────────────────────────────────────────


def test_the_domains_are_keyed_by_their_own_name() -> None:
    for name, declared in DOMAINS.items():
        assert declared.name == name


def test_the_known_domains_are_exposed() -> None:
    assert set(domain_names()) == {"tool", "turn", "llm"}


def test_every_domain_declares_at_least_one_event() -> None:
    """An empty domain would be a subscription that quietly registers nothing."""
    for declared in DOMAINS.values():
        assert declared.event_types, f"{declared.name} has no events"


def test_no_event_type_belongs_to_two_domains() -> None:
    """Otherwise a listener would be called twice for one event."""
    seen: dict[type, str] = {}
    for declared in DOMAINS.values():
        for event_type in declared.event_types:
            assert event_type not in seen, (
                f"{event_type.__name__} is in both {seen.get(event_type)!r} and "
                f"{declared.name!r}"
            )
            seen[event_type] = declared.name


def test_resolving_a_known_domain_returns_it() -> None:
    assert domain("tool") is TOOL
    assert domain("turn") is TURN
    assert domain("llm") is LLM


def test_resolving_an_unknown_domain_raises() -> None:
    """A typo'd domain must not look like a plugin that works."""
    with pytest.raises(KeyError) as caught:
        domain("nope")
    assert "nope" in str(caught.value)
    assert "tool" in str(caught.value), "it names the known ones"


def test_a_domain_serialises() -> None:
    payload = LLM.to_payload()
    assert payload["name"] == "llm"
    assert payload["blocking"] is False
    assert payload["events"] == [
        "LLMRequestStarted",
        "LLMResponseReceived",
        "LLMResponseParsed",
        "LLMRetryScheduled",
    ]


# ── Only the tool domain blocks ──────────────────────────────────────────────


def test_only_the_tool_domain_is_blocking() -> None:
    """A gate has a caller waiting for a verdict; a notification does not.

    If another domain inherited it, a listener returning a truthy value would stop
    every listener behind it -- and nothing in that listener would look wrong.
    """
    blocking = {d.name for d in DOMAINS.values() if d.blocking}
    assert blocking == {"tool"}


def test_the_blocking_domain_holds_the_gateable_event() -> None:
    assert ToolPreExecute in TOOL.event_types


def test_the_tool_domain_reuses_the_existing_event_types() -> None:
    """Not copies: the executor publishes these very classes."""
    assert set(TOOL.event_types) == {ToolPreExecute, ToolPostExecute}


# ── Subscription ─────────────────────────────────────────────────────────────


def test_subscribing_covers_every_event_in_the_domain() -> None:
    bus = EventBus()
    seen: list = []
    TURN.subscribe(bus, lambda event: seen.append(event))
    asyncio.run(bus.emit(TurnStarted(agent_id="a", tools_enabled=True, streamed=False)))
    asyncio.run(bus.emit(TurnFinished(agent_id="a", status="ok", steps=1)))
    assert len(seen) == 2


def test_subscribing_returns_one_unsubscribe_for_all_of_them() -> None:
    """A plugin unloads as a unit; per-type bookkeeping is how one is forgotten."""
    bus = EventBus()
    unsubscribe = LLM.subscribe(bus, lambda event: None)
    assert bus.has_listeners(LLMRequestStarted)
    assert bus.has_listeners(LLMResponseReceived)
    unsubscribe()
    assert not bus.has_listeners(LLMRequestStarted)
    assert not bus.has_listeners(LLMResponseReceived)


def test_unsubscribing_twice_is_harmless() -> None:
    bus = EventBus()
    unsubscribe = TURN.subscribe(bus, lambda event: None)
    unsubscribe()
    unsubscribe()  # must not raise


def test_a_domain_listener_is_not_called_for_another_domain() -> None:
    """The isolation the single bus already provides, asserted rather than assumed."""
    bus = EventBus()
    seen: list = []
    TURN.subscribe(bus, lambda event: seen.append(event))
    asyncio.run(bus.emit(LLMRequestStarted(agent_id="a", model="m", tools_offered=0)))
    asyncio.run(bus.emit(ToolPostExecute(tool_name="t", arguments={}, result="r")))
    assert seen == []


def test_two_domains_can_share_one_bus() -> None:
    """Which is the point: no second bus, so no listener registered on the wrong one."""
    bus = EventBus()
    turns: list = []
    calls: list = []
    TURN.subscribe(bus, lambda event: turns.append(event))
    LLM.subscribe(bus, lambda event: calls.append(event))
    asyncio.run(bus.emit(TurnStarted(agent_id="a", tools_enabled=True, streamed=False)))
    asyncio.run(bus.emit(LLMRequestStarted(agent_id="a", model="m", tools_offered=3)))
    assert len(turns) == 1
    assert len(calls) == 1


def test_an_async_listener_is_awaited() -> None:
    bus = EventBus()
    seen: list = []
    TURN.subscribe(bus, _async_listener(seen))
    asyncio.run(bus.emit(TurnFinished(agent_id="a", status="ok", steps=2)))
    assert len(seen) == 1


def test_a_failing_listener_does_not_stop_the_next_one() -> None:
    """The bus's isolation contract, reached through a domain subscription."""
    bus = EventBus()
    seen: list = []

    def broken(event):
        raise RuntimeError("listener exploded")

    TURN.subscribe(bus, broken)
    TURN.subscribe(bus, lambda event: seen.append(event))
    asyncio.run(bus.emit(TurnFinished(agent_id="a", status="ok", steps=1)))
    assert len(seen) == 1, "the second listener still ran"


# ── The tool bus is unchanged ────────────────────────────────────────────────


def test_the_tool_bus_is_still_the_module_level_one() -> None:
    """The executor is built in several places; listeners must reach all of them."""
    from agent.core import events as events_module

    assert TOOL_EVENTS is events_module.TOOL_EVENTS
    assert isinstance(TOOL_EVENTS, EventBus)


def test_the_tool_refusal_semantic_still_short_circuits() -> None:
    bus = EventBus()
    later: list = []

    async def refuse(event):
        return "not allowed"

    bus.on(ToolPreExecute, refuse)
    bus.on(ToolPreExecute, lambda event: later.append(event))
    reason = asyncio.run(bus.emit_pre_execute(ToolPreExecute("t", {})))
    assert reason == "not allowed"
    assert later == [], "a gate a later listener can argue with is not a gate"


# ── Payloads are data ────────────────────────────────────────────────────────


def test_turn_events_carry_what_a_listener_needs() -> None:
    assert TurnStarted("a", True, False).agent_id == "a"
    assert TurnFinished("a", "cancelled", 3).status == "cancelled"


def test_llm_events_carry_what_a_listener_needs() -> None:
    started = LLMRequestStarted(agent_id="a", model="m", tools_offered=2)
    assert started.tools_offered == 2
    received = LLMResponseReceived(agent_id="a", duration_ms=12.5)
    assert received.duration_ms == 12.5
    parsed = LLMResponseParsed(agent_id="a", stop_reason="tool_use", tool_uses=1)
    assert parsed.tool_uses == 1


def test_events_are_frozen() -> None:
    with pytest.raises(Exception):
        TurnStarted("a", True, False).agent_id = "b"  # type: ignore[misc]


def test_a_domain_is_frozen() -> None:
    with pytest.raises(Exception):
        Domain("x", ()).name = "y"  # type: ignore[misc]


# ── The guard: one bus, not one per domain ───────────────────────────────────


def _event_bus_constructions(source: str) -> list[int]:
    import ast

    tree = ast.parse(source)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "EventBus")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "EventBus")
        )
    ]


def test_no_module_builds_a_second_event_bus() -> None:
    """A bus per domain is a way to lose a listener, not a way to isolate one.

    The executor is constructed in several places (the turn loop, the scheduler,
    tests), so a bus that is not the shared one means a plugin's gate applies only
    to whichever construction path happened to be handed that instance.  Scanned
    at the source because the symptom -- a listener that is simply never called --
    has no runtime signal.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "agent"
    owner = "agent/core/events.py"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(root.parent))
        if rel == owner:
            continue
        if _event_bus_constructions(path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert not offenders, (
        "an EventBus is constructed outside agent/core/events.py:\n  "
        + "\n  ".join(offenders)
        + "\n\nSubscribe to the shared bus (see agent/core/event_domains.py) "
        "instead of building another one."
    )


def test_the_owner_does_build_the_bus() -> None:
    """Control: the guard is vacuous if construction is not detected at all."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "agent/core/events.py").read_text()
    assert _event_bus_constructions(source), "the detector found no construction"
