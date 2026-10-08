"""The context-window arithmetic, testable without an agent.

Five methods on ``BaseAgent`` computed one thing -- how much of the window each
part of a request may claim -- and two of the bugs they encode were expensive to
find.  Both are statements about arithmetic, so they belong somewhere arithmetic
can be tested: this module's subject, :class:`agent.core.token_budget.TokenBudget`,
takes its three collaborators as callables and reads its configuration from a
settings object.

The two bugs, asserted below:

1. **The head is priced by the head's own measurement.**  Re-deriving it through
   the body's calibration factor let a *body* change re-price a byte-identical
   *head* mid-turn -- measured on a real session as 82588 -> 68382 tokens,
   triggering emergency compaction the payload never needed.
2. **The reserve is not the configured cap.**  A cap is a ceiling on one
   response; the reserve is what the window holds back for the answer.  Letting
   the cap set the reserve gave a provider configured for long answers the
   smallest usable context.
"""

from __future__ import annotations

import pytest

from agent import shared
from agent.core.context_assembler import ContextAssembler
from agent.core.token_budget import OUTPUT_CAP_MARGIN, TokenBudget
from agent.memory.consolidation import estimate_message_tokens


class _Settings:
    """The settings shape ``TokenBudget`` reads; no agent, no registry."""

    def __init__(self, **kwargs) -> None:
        self.context_window = kwargs.get("context_window", 128_000)
        self.output_reserve = kwargs.get("output_reserve", 8192)
        self.max_tokens = kwargs.get("max_tokens", 8192)
        self.max_retrieval_tokens = kwargs.get("max_retrieval_tokens", 0)


class _Consolidation:
    """A consolidation engine that answers with fixed, recognisable numbers."""

    def __init__(self, head: int | None = None, full: int | None = None) -> None:
        self.head = head
        self.full = full
        self.head_calls: list = []

    def estimate_tokens(self, messages):
        if self.full is not None:
            return self.full
        return estimate_message_tokens(messages)

    def estimate_tokens_raw(self, messages):
        return estimate_message_tokens(messages)

    def estimate_head_tokens(self, fingerprint, system_prompt, tools):
        self.head_calls.append(fingerprint)
        if self.head is not None:
            return self.head
        return estimate_message_tokens(
            [{"role": "system", "content": system_prompt}]
        )


class _Manager:
    def __init__(self, consolidation) -> None:
        self.consolidation = consolidation


class _Ctx:
    def __init__(self, system_prompt: str = "sys", messages: list | None = None) -> None:
        self.system_prompt = system_prompt
        self.messages = messages if messages is not None else [{"role": "user", "content": "hi"}]
        self.metadata: dict = {}


def _budget(settings=None, consolidation=None, manager=None) -> TokenBudget:
    manager = manager or _Manager(consolidation or _Consolidation())
    return TokenBudget(
        context_manager_for=lambda ctx: manager,
        assembler_for=ContextAssembler,
        settings=settings or _Settings(),
    )


# ── The reserve is not the cap ───────────────────────────────────────────────


def test_the_input_budget_holds_back_the_reserve_not_the_cap() -> None:
    """A provider configured for long answers must not get a smaller context.

    ``max_tokens`` is 64000 here and ``output_reserve`` is 8192; sizing the
    reserve by the cap would take 64000 out of the window instead of 8192.
    """
    settings = _Settings(context_window=128_000, output_reserve=8192, max_tokens=64_000)
    budget = _budget(settings)
    ctx = _Ctx()

    with_reserve = budget.input_token_budget(ctx, [])
    payload = budget.payload_overhead(ctx, [])

    assert with_reserve == 128_000 - 8192 - payload
    assert with_reserve > 128_000 - 64_000 - payload


def test_an_explicit_output_budget_becomes_the_reserve() -> None:
    """Naming a budget is the same question asked from the other end."""
    settings = _Settings(context_window=128_000, output_reserve=8192)
    budget = _budget(settings)
    ctx = _Ctx()

    payload = budget.payload_overhead(ctx, [])
    assert budget.input_token_budget(ctx, [], output_max_tokens=20_000) == (
        128_000 - 20_000 - payload
    )


def test_configured_output_cap_resolves_none_to_the_configured_cap() -> None:
    budget = _budget(_Settings(max_tokens=4321))
    assert budget.configured_output_cap(None) == 4321
    assert budget.configured_output_cap(999) == 999


# ── The head is priced by the head ───────────────────────────────────────────


def test_the_head_price_comes_from_the_head_fingerprint() -> None:
    """Not re-derived through the body's factor, which is the defect."""
    consolidation = _Consolidation(head=777)
    budget = _budget(consolidation=consolidation)
    ctx = _Ctx()

    assert budget.payload_overhead(ctx, []) == 777
    assert consolidation.head_calls, "the head estimator must be the one asked"


def test_the_head_price_is_measured_once_per_distinct_head() -> None:
    """The memory engine caches per fingerprint; this asserts it is asked per head."""
    consolidation = _Consolidation(head=100)
    budget = _budget(consolidation=consolidation)
    ctx = _Ctx()

    tools_a = [{"name": "a"}]
    tools_b = [{"name": "b"}]
    budget.payload_overhead(ctx, tools_a)
    budget.payload_overhead(ctx, tools_a)
    budget.payload_overhead(ctx, tools_b)

    assert len(consolidation.head_calls) == 3
    # Two of them describe the same head, so the fingerprint repeats.
    assert consolidation.head_calls[0] == consolidation.head_calls[1]
    assert consolidation.head_calls[0] != consolidation.head_calls[2]


def test_without_a_head_estimator_the_head_is_estimated_directly() -> None:
    """The no-manager path must still price the system prompt and the tools."""

    class _NoHead:
        def estimate_tokens(self, messages):
            return 42

    budget = _budget(consolidation=_NoHead())
    assert budget.payload_overhead(_Ctx(), [{"name": "t"}]) == 42


# ── Output room ──────────────────────────────────────────────────────────────


def test_output_room_subtracts_head_body_and_margin() -> None:
    consolidation = _Consolidation(head=1000, full=2000)
    budget = _budget(settings=_Settings(context_window=10_000), consolidation=consolidation)
    ctx = _Ctx()

    assert budget.output_room(ctx, []) == 10_000 - 1000 - 2000 - OUTPUT_CAP_MARGIN


def test_output_room_can_go_negative_when_the_request_already_fills_the_window() -> None:
    """Returned rather than clamped: the caller decides what to do about it.

    Clamping here would hide the condition the caller has to act on (emergency
    compaction), which is the failure mode of a budget that reports a floor.
    """
    consolidation = _Consolidation(head=5000, full=9000)
    budget = _budget(settings=_Settings(context_window=10_000), consolidation=consolidation)
    assert budget.output_room(_Ctx(), []) < 0


# ── The estimator chain ──────────────────────────────────────────────────────


def test_the_estimator_prefers_the_managers() -> None:
    budget = _budget(consolidation=_Consolidation(full=123))
    assert budget.estimate_input_tokens([{"role": "user", "content": "x"}]) == 123


def test_the_estimator_falls_back_to_the_cjk_aware_model() -> None:
    """Not ``len(json.dumps) / CHARS_PER_TOKEN``, which quarter-counted CJK."""

    class _NoEstimator:
        pass

    budget = _budget(manager=_Manager(_NoEstimator()))
    messages = [{"role": "user", "content": "中文内容"}]
    assert budget.estimate_input_tokens(messages) == estimate_message_tokens(messages)


def test_raw_body_tokens_prefers_the_raw_estimator() -> None:
    calls: list = []

    class _Raw:
        def estimate_tokens_raw(self, messages):
            calls.append(messages)
            return 55

    budget = _budget(consolidation=_Raw())
    assert budget.estimate_raw_body_tokens(_Ctx()) == 55
    assert calls, "the raw estimator must be the one asked"


# ── Configuration is read live ───────────────────────────────────────────────


def test_a_resized_window_is_honoured_without_rebuilding() -> None:
    """Both values are set after construction in real call sites.

    ``tests/test_consolidation.py`` and the web channel both resize a live
    agent, so a budget that copied these at construction would silently ignore
    the override.
    """
    settings = _Settings(context_window=100_000, output_reserve=1000)
    budget = _budget(settings)
    ctx = _Ctx()
    payload = budget.payload_overhead(ctx, [])
    assert budget.input_token_budget(ctx, []) == 100_000 - 1000 - payload

    settings.context_window = 200_000
    settings.output_reserve = 2000
    assert budget.input_token_budget(ctx, []) == 200_000 - 2000 - payload


def test_max_retrieval_tokens_is_read_live_too() -> None:
    """Set on a live agent in at least one test, so it cannot be frozen."""
    settings = _Settings(max_retrieval_tokens=0)
    budget = _budget(settings)
    usable = settings.context_window - settings.output_reserve
    assert budget.retrieval_token_budget() == max(
        1024, int(usable * shared.RETRIEVAL_BUDGET_FRACTION)
    )

    settings.max_retrieval_tokens = 4096
    assert budget.retrieval_token_budget() == 4096


def test_an_allocated_budget_is_recorded_on_the_context() -> None:
    """The metadata other code reads must still be written."""
    budget = _budget()
    ctx = _Ctx()
    result = budget.retrieval_token_budget(ctx, [])
    assert result > 0
    assert set(ctx.metadata["context_budget"]) == {
        "static_tokens",
        "message_tokens",
        "retrieval_tokens",
        "input_tokens",
        "selected_tools",
    }


# ── The loop takes the arithmetic from the budget ────────────────────────────


def test_the_loop_takes_its_budget_callables_from_the_budget() -> None:
    """The four budget entries in ``LoopDeps`` are the budget's own callables.

    They used to be four pass-through methods on the agent -- ``def _output_room(self, ...):
    return self._token_budget.output_room(...)`` -- so the arithmetic travelled
    loop -> agent -> budget to arrive back where it started.  The claim worth pinning is that
    there is **one** implementation: the same object, not two that happen to agree today.
    """
    import agent as agent_module
    from agent.core.turn_loop import LoopDeps

    agent = agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="fake-model", api_format="openai"
    )
    ctx = _Ctx()
    budget = agent._token_budget
    deps = LoopDeps.of(agent)

    assert deps.input_token_budget == budget.input_token_budget
    assert deps.output_room == budget.output_room
    assert deps.configured_output_cap == budget.configured_output_cap
    # The fourth entry is the budget itself: the loop reads per-turn arithmetic from it.
    assert deps.token_budget is budget
    # Identity alone would be satisfied by two broken copies of the same thing.
    assert deps.configured_output_cap(None) == agent.max_tokens
    assert deps.input_token_budget(ctx, []) == budget.input_token_budget(ctx, [])
