"""The turn loop announcing its phases to subscribers.

Two systems announce a turn and they do different jobs: ``_emit_event`` writes to an
:class:`EventCollector`, a *persisted runtime log*, while these events are an
in-process extension point a plugin subscribes to.  Nothing here changes what the
log records.

What makes this worth its own tests is that emission sits on the hot path of every
turn, which forces two properties:

- **a listener cannot change the turn.**  The bus isolates failures, so a
  subscriber that raises is reported and skipped.  Were that untrue, mounting a
  plugin could break a turn it only meant to observe.
- **no listener means no work.**  The emitter checks ``has_listeners`` before
  constructing an event, so a composition that mounts nothing pays a dict lookup.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.core.event_domains import (
    LLM,
    LLMRequestStarted,
    LLMResponseParsed,
    LLMResponseReceived,
    LLMRetryScheduled,
    TurnFinished,
    TurnStarted,
)
from agent.core.events import EventBus, LLM_EVENTS, TURN_EVENTS


def _response(stop_reason: str, text: str, tool_calls=None):
    """A transport response in the shape ``_parse_response`` expects."""
    from agent import shared

    return shared._OAIResponse(
        [shared._OAIChoice(stop_reason, shared._OAIMsg(text, tool_calls))]
    )


class _Ctx:
    agent_id = "a1"
    tools_enabled = True
    metadata: dict = {}
    messages: list = []
    system_prompt = "sys"


# ── The bus the loop publishes to ────────────────────────────────────────────


def test_the_turn_and_llm_buses_are_module_level() -> None:
    """A bus owned by one construction leaves a plugin's listener unreachable.

    The turn loop is built per agent and the scheduler builds its own, so a
    per-instance bus would mean a listener registered once could be absent for the
    very turns it was mounted for.
    """
    assert isinstance(TURN_EVENTS, EventBus)
    assert isinstance(LLM_EVENTS, EventBus)
    assert TURN_EVENTS is not LLM_EVENTS, "separate names, so a reader can see which"


def test_a_listener_on_one_bus_never_sees_the_other_domain() -> None:
    """The isolation the bus already provides, through the domain tables."""
    seen: list = []
    unsubscribe = LLM.subscribe(LLM_EVENTS, lambda event: seen.append(event))
    try:
        asyncio.run(TURN_EVENTS.emit(TurnStarted("a", True, False)))
        assert seen == []
    finally:
        unsubscribe()


def test_a_raising_listener_does_not_break_the_emitter() -> None:
    """Mounting a plugin must not be able to break a turn it only observes."""
    bus = EventBus()
    seen: list = []

    def broken(event):
        raise RuntimeError("subscriber exploded")

    bus.on(TurnFinished, broken)
    bus.on(TurnFinished, lambda event: seen.append(event))
    asyncio.run(bus.emit(TurnFinished(agent_id="a", status="ok", steps=1)))
    assert len(seen) == 1, "the second listener still ran"


def test_an_async_listener_is_awaited() -> None:
    bus = EventBus()
    seen: list = []

    async def listener(event):
        seen.append(event)

    bus.on(TurnStarted, listener)
    asyncio.run(bus.emit(TurnStarted("a", True, False)))
    assert len(seen) == 1


# ── The emitter's own contract ───────────────────────────────────────────────


def test_the_emitter_skips_construction_without_listeners() -> None:
    """Asserted with a payload that would raise if it were built.

    ``has_listeners`` is checked before the event is constructed, and this proves
    it: a subscriber that would explode on construction never gets that far.
    """
    from agent.core.agent import _emit_domain_event

    class _Explodes:
        def __init__(self, **fields):
            raise AssertionError("an event was built with no listeners")

    asyncio.run(_emit_domain_event(EventBus(), _Explodes))
    asyncio.run(_emit_domain_event(None, _Explodes))


def test_the_emitter_delivers_when_someone_is_listening() -> None:
    from agent.core.agent import _emit_domain_event

    bus = EventBus()
    seen: list = []
    bus.on(TurnStarted, lambda event: seen.append(event))
    asyncio.run(
        _emit_domain_event(
            bus, TurnStarted, agent_id="a", tools_enabled=True, streamed=False
        )
    )
    assert len(seen) == 1


def test_the_emitter_tolerates_no_bus() -> None:
    """A composition without a bus must still run turns."""
    from agent.core.agent import _emit_domain_event

    asyncio.run(_emit_domain_event(None, TurnStarted, agent_id="a"))


# ── Events a turn produces ───────────────────────────────────────────────────


def test_a_tool_using_turn_announces_start_and_finish() -> None:
    """End to end through the real loop, with the events captured.

    Driven through ``send_message`` because the point is that the *loop* emits
    them: a helper that works in isolation but is never called from a turn would
    satisfy every test above and none of the users.
    """
    import agent as agent_module

    registry = agent_module.ToolRegistry()

    @registry.tool(
        name="noop", description="does nothing", parameters={"type": "object", "properties": {}}
    )
    def noop() -> str:
        return "noop result"

    agent = agent_module.BaseAgent(
        object(), registry, model="m", api_format="openai"
    )
    agent._base_system_prompt = "sys"

    calls = {"n": 0}

    async def fake_create(ctx, tools, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            from agent import shared

            return _response(
                "tool_calls",
                "",
                [
                    shared._OAITC(
                        "1", shared._OAIFunc("noop", "{}")
                    )
                ],
            )
        return _response("stop", "done")

    bus = EventBus()
    llm_bus = EventBus()
    agent._event_bus = bus
    agent._llm_event_bus = llm_bus
    agent._create = fake_create

    turn_events: list = []
    llm_events: list = []
    bus.on(TurnStarted, lambda event: turn_events.append(event))
    bus.on(TurnFinished, lambda event: turn_events.append(event))
    llm_bus.on(LLMRequestStarted, lambda event: llm_events.append(event))
    llm_bus.on(LLMResponseReceived, lambda event: llm_events.append(event))

    ctx = agent_module.AgentContext(system_prompt="sys")
    result = asyncio.run(agent.send_message(ctx, "go"))

    assert result.error is None
    kinds = [type(event).__name__ for event in turn_events]
    assert kinds == ["TurnStarted", "TurnFinished"], kinds
    assert turn_events[0].agent_id == ctx.agent_id
    assert turn_events[1].steps >= 1


def test_a_listener_that_raises_does_not_fail_the_turn() -> None:
    """The property that makes mounting a plugin safe on the hot path."""
    import agent as agent_module

    agent = agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="m", api_format="openai"
    )
    agent._base_system_prompt = "sys"

    async def fake_create(ctx, tools, **kwargs):
        return _response("stop", "done")

    bus = EventBus()
    agent._event_bus = bus
    agent._llm_event_bus = EventBus()
    agent._create = fake_create
    bus.on(TurnStarted, lambda event: (_ for _ in ()).throw(RuntimeError("boom")))

    ctx = agent_module.AgentContext(system_prompt="sys")
    result = asyncio.run(agent.send_message(ctx, "go"))
    assert result.error is None, "a broken subscriber must not fail the turn"
    assert result.content == "done"


# ── The retry event ──────────────────────────────────────────────────────────


def test_a_retry_announces_the_attempt_and_the_delay() -> None:
    """A flaky provider becomes visible without a listener parsing logs."""
    event = LLMRetryScheduled(
        agent_id="a", model="m", attempt=1, delay_seconds=1.5, error="503"
    )
    assert event.attempt == 1
    assert event.delay_seconds == 1.5
    assert event.error == "503"


def test_the_retry_event_is_part_of_the_llm_domain() -> None:
    assert LLMRetryScheduled in LLM.event_types


def test_a_request_started_carries_no_attempt_count() -> None:
    """Two callers reach the model without the retry wrapper; see the docstring.

    Putting the count there would either miss those callers or double-emit for the
    wrapped ones, which is why a retry is its own event.
    """
    assert "attempt" not in LLMRequestStarted.__dataclass_fields__


def test_a_retry_is_announced_between_the_failed_call_and_the_next() -> None:
    """Driven through the real wrapper so the wiring, not just the type, is tested."""
    import agent as agent_module

    agent = agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="m", api_format="openai"
    )
    agent.llm_max_retries = 1
    agent.llm_retry_base_delay = 0.0  # keep the test fast

    bus = EventBus()
    agent._llm_event_bus = bus
    seen: list = []
    bus.on(LLMRetryScheduled, lambda event: seen.append(event))

    class _Transient(RuntimeError):
        pass

    agent._is_llm_retryable = lambda exc: True
    attempts = {"n": 0}

    async def flaky(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _Transient("503 from upstream")
        return "ok"

    result = asyncio.run(agent._with_llm_retry(flaky))
    assert result == "ok"
    assert len(seen) == 1, "exactly one retry was scheduled"
    assert seen[0].attempt == 1
    assert "503" in seen[0].error


def test_a_call_that_succeeds_announces_no_retry() -> None:
    import agent as agent_module

    agent = agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="m", api_format="openai"
    )
    bus = EventBus()
    agent._llm_event_bus = bus
    seen: list = []
    bus.on(LLMRetryScheduled, lambda event: seen.append(event))

    async def fine(*args, **kwargs):
        return "ok"

    asyncio.run(agent._with_llm_retry(fine))
    assert seen == []


# ── The response event's payload ─────────────────────────────────────────────


def test_a_received_response_reports_only_its_duration() -> None:
    """It has no stop reason; the parser produces that later."""
    event = LLMResponseReceived(agent_id="a", duration_ms=42.0)
    assert event.duration_ms == 42.0
    assert "stop_reason" not in LLMResponseReceived.__dataclass_fields__


def test_a_parsed_response_reports_the_normalised_reason() -> None:
    event = LLMResponseParsed(agent_id="a", stop_reason="tool_use", tool_uses=2)
    assert event.stop_reason == "tool_use"
    assert event.tool_uses == 2


def test_a_parsed_response_defaults_to_no_tool_uses() -> None:
    """A plain answer is the common case; a caller should not have to say so."""
    assert LLMResponseParsed(
        agent_id="a", stop_reason="end_turn"
    ).tool_uses == 0


def test_the_call_path_announces_a_request_and_a_response() -> None:
    """Through the real ``_create``, so a caller reaching the model directly
    -- two of them exist -- is covered rather than only the retry-wrapped path."""
    import agent as agent_module

    agent = agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="m", api_format="openai"
    )

    class _Transport:
        async def create(self, **kwargs):
            return _response("stop", "hi")

    agent._transport = _Transport()
    agent._prepare_provider_context = lambda *a, **k: None
    agent._observe_provider_usage = lambda *a, **k: None

    bus = EventBus()
    agent._llm_event_bus = bus
    seen: list = []
    bus.on(LLMRequestStarted, lambda event: seen.append(event))
    bus.on(LLMResponseReceived, lambda event: seen.append(event))

    ctx = agent_module.AgentContext(system_prompt="sys")
    asyncio.run(agent._create(ctx, [{"name": "t"}]))

    kinds = [type(event).__name__ for event in seen]
    assert kinds == ["LLMRequestStarted", "LLMResponseReceived"], kinds
    assert seen[0].tools_offered == 1
    assert seen[1].duration_ms >= 0


def test_a_failed_call_announces_the_request_but_no_response() -> None:
    """The response event means a response arrived; announcing one on failure
    would make a listener count calls that never returned."""
    import agent as agent_module

    agent = agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="m", api_format="openai"
    )

    class _Transport:
        async def create(self, **kwargs):
            raise RuntimeError("upstream down")

    agent._transport = _Transport()
    agent._prepare_provider_context = lambda *a, **k: None
    agent._observe_provider_usage = lambda *a, **k: None

    bus = EventBus()
    agent._llm_event_bus = bus
    seen: list = []
    bus.on(LLMRequestStarted, lambda event: seen.append(event))
    bus.on(LLMResponseReceived, lambda event: seen.append(event))

    ctx = agent_module.AgentContext(system_prompt="sys")
    with pytest.raises(RuntimeError):
        asyncio.run(agent._create(ctx, []))

    assert [type(event).__name__ for event in seen] == ["LLMRequestStarted"]
