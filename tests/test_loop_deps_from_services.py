"""The turn loop's dependencies come from services, not from an agent.

`LoopDeps` was obtainable only through `LoopDeps.of(agent)`, so a turn could not run without
an agent object -- which is what "the agent is the centre" means concretely.  The review's
target is the other shape: the loop takes `llm`, `registry` and the rest as services, with an
agent being one thing that happens to hold them.

**The interesting part of this change was smaller than it looked**, and the measurement is
worth recording: of the 25 dependencies, the four budget entries were already delegations to
`agent.core.token_budget.TokenBudget`, and the two clipping entries were already delegations
to `agent.core.bounded_text`.  The implementation had left the agent; only the *route* still
went through it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Settings:
    """The settings the loop needs, by name.  Not an agent."""

    supports_vision = True
    orchestration_policy_skill_id = "policy"
    max_tool_call_iterations = 7
    max_tokens = 1000
    max_truncation_continuations = 2
    llm_max_retries = 3
    llm_retry_base_delay = 0.5


class _Budget:
    """Stands in for `TokenBudget`, which is where the arithmetic actually lives.

    Faithful in the two ways that matter, and both were wrong here before:

    * it does **not** invent a ``token_budget(ctx, tools)`` member.  The real class has no
      such method, and this double having one hid a real ``AttributeError``: the field is the
      budget *object* -- the loop reads ``token_budget.retrieval_token_budget(...)`` from it.
    * it **does** carry ``retrieval_token_budget``, the one member the loop actually calls,
      so a route that produced something unusable fails here instead of in a turn.
    """

    def configured_output_cap(self, output_max_tokens=None):
        return 42

    def input_token_budget(self, ctx, tools, *, output_max_tokens=None):
        return 43

    def output_room(self, ctx, tools):
        return 44

    def retrieval_token_budget(self, ctx, tools, *, current_user_content=None):
        return 45


class _RealCtx:
    """The minimum a real `TokenBudget` asks of a context."""

    messages: list = []
    metadata: dict = {}
    system_prompt = ""


def _deps(**overrides):
    from agent.core.turn_loop import LoopDeps

    kwargs = dict(
        settings=_Settings(),
        transport=object(),
        registry=object(),
        context_assembler=object(),
        context_manager_for=lambda ctx: None,
        effective_model=lambda ctx=None: "m",
        format_agent_error=lambda *a, **k: "",
        create=object(),
        retry_policy=object(),
        recover_from_content_filter=lambda *a, **k: None,
        token_budget=_Budget(),
    )
    kwargs.update(overrides)
    return LoopDeps.from_services(**kwargs)


def test_the_dependencies_can_be_built_without_an_agent() -> None:
    """The claim, asserted: nothing in the construction needs an `agent` object."""
    deps = _deps()

    assert deps.supports_vision is True
    assert deps.max_tool_call_iterations == 7
    assert deps.max_tokens == 1000
    assert deps.orchestration_policy_skill_id == "policy"


def test_the_budget_entries_come_from_the_budget_itself() -> None:
    """The budget's own callables, and the budget itself.

    Three of the four entries are callables taken off the object; the fourth is the object,
    because the loop reads per-turn arithmetic from it.  Asserted by result *and* identity:
    the value shows the route works, and the identity shows there is one implementation rather
    than two that agree today.
    """
    budget = _Budget()
    deps = _deps(token_budget=budget)

    assert deps.configured_output_cap(None) == 42
    assert deps.input_token_budget(None, []) == 43
    assert deps.output_room(None, []) == 44
    assert deps.token_budget.retrieval_token_budget(None, []) == 45
    assert deps.token_budget is budget
    assert deps.configured_output_cap == budget.configured_output_cap


def test_the_pure_helpers_are_module_functions() -> None:
    """They read no instance state, so the loop can use them without an agent.

    `protected_turn_messages` was a staticmethod whose body read `ctx.metadata` and nothing
    else; `bounded_text` was already delegating to a module.  A helper that needs no instance
    is one that should not require one.
    """
    from agent.core.turn_helpers import protected_turn_messages

    class _Ctx:
        metadata: dict = {}

    assert protected_turn_messages(_Ctx()) == []

    _Ctx.metadata = {"_turn_request_message": {"role": "user"}}
    assert protected_turn_messages(_Ctx()) == [{"role": "user"}]


def test_the_agent_route_still_works_and_still_agrees() -> None:
    """Control: `of(agent)` is one adapter, not a deprecated path.

    Every existing caller has an agent.  What changed is that it is no longer the *only*
    route, so this asserts both exist rather than that one replaced the other.
    """
    from agent.core.turn_loop import LoopDeps

    assert hasattr(LoopDeps, "of")
    assert hasattr(LoopDeps, "from_services")

    source = (REPO_ROOT / "agent/core/turn_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "LoopDeps"
    )
    methods = {
        node.name for node in cls.body if isinstance(node, ast.FunctionDef)
    }
    assert {"of", "from_services"} <= methods


def test_the_struct_is_still_closed() -> None:
    """Both routes must fill the same frozen set -- a field only one route sets is a bug.

    The struct's stated purpose is "the set is visible and closed", and a second
    construction route is exactly how a field starts being optional by accident.
    """
    import dataclasses

    from agent.core.turn_loop import LoopDeps

    fields = {f.name for f in dataclasses.fields(LoopDeps)}
    deps = _deps()
    for name in fields:
        getattr(deps, name)  # raises if a field was left unset

    assert len(fields) == len(
        [f for f in dataclasses.fields(LoopDeps) if f.init]
    ), "every field is set through the constructor"


def test_both_routes_carry_the_real_budget_through() -> None:
    """No doubles: the real `TokenBudget` has to survive both routes.

    `_Budget` above is a stand-in, and a stand-in is exactly where this went wrong: it grew a
    ``token_budget(ctx, tools)`` method the real class does not have, so ``from_services``
    passed its test while raising ``AttributeError`` against a real budget, and nothing ever
    asked a route for the one member the loop calls
    (``deps.token_budget.retrieval_token_budget(...)``).

    Driven through the real class, on both routes, ending in that call.
    """
    import agent as agent_module
    from agent.core.turn_loop import LoopDeps

    agent = agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="fake-model", api_format="openai"
    )
    real = agent._token_budget
    assert real.__class__.__name__ == "TokenBudget"

    from_agent = LoopDeps.of(agent)
    from_services = _deps(token_budget=real)

    for route, deps in (("of(agent)", from_agent), ("from_services", from_services)):
        assert deps.token_budget is real, route
        budget = deps.token_budget.retrieval_token_budget(
            _RealCtx(), [], current_user_content="hello"
        )
        assert isinstance(budget, int) and budget >= 0, (route, budget)
