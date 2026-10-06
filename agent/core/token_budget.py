"""How much of the context window each part of a request may claim.

Why this module exists
----------------------
Five methods on ``BaseAgent`` -- ``_input_token_budget``,
``_estimate_input_tokens``, ``_estimate_raw_body_tokens``, ``_payload_overhead``
and ``_output_room`` -- computed one thing: the size of the request relative to
the window.  They read three configuration values and one estimator off the
agent, and they called each other, so the arithmetic could only be exercised by
building an agent.

The arithmetic is the part worth isolating, because two of the bugs it encodes
were expensive to find and are easy to undo by accident:

- **The head must be priced by the head's own measurement.**  ``_payload_overhead``
  takes the system prompt and tool schemas to the memory engine's
  per-fingerprint price (``estimate_head_tokens``).  It used to re-derive that
  price through the shared calibration factor, which let a *body* composition
  change re-price a byte-identical *head* mid-turn: measured on a real session
  as 82588 -> 68382 tokens with the fingerprint unchanged, triggering emergency
  compaction the payload never needed.

- **The reserve is not the configured cap.**  A cap is a ceiling on one
  response; the reserve is what the window holds back for the answer.  Letting
  the cap set the reserve meant a provider configured for long answers silently
  got the smallest usable context.

Both are statements about arithmetic, and both are now testable without an
agent.  ``BaseAgent`` holds a :class:`TokenBudget` and delegates.

What is injected, and why
-------------------------
``context_manager_for`` and ``estimate`` are callables rather than resolved
values: the estimator belongs to the *scoped* context manager when a request
carries one, so which estimator is used is a per-call decision, not a
construction-time one.

``settings`` is the object ``max_retrieval_tokens`` is read from.  It is read
dynamically because that attribute is set after construction in at least one
place (``tests/test_consolidation.py`` sets it on a live agent), and freezing it
at construction would silently ignore the override.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from agent import shared
from agent.core.context_assembler import ContextAssembler
from agent.core.payload_shape import head_fingerprint
from agent.memory.consolidation import estimate_message_tokens

__all__ = ["TokenBudget", "OUTPUT_CAP_MARGIN"]

#: Slack subtracted from the response ceiling so a rounding difference between
#: this module's estimate and the provider's count cannot push the request over
#: the window.
OUTPUT_CAP_MARGIN = 256


class TokenBudget:
    """The window arithmetic for one agent, with its inputs made explicit."""

    __slots__ = ("_context_manager_for", "_assembler_for", "_settings")

    def __init__(
        self,
        *,
        context_manager_for: Callable[[Any], Any],
        assembler_for: Callable[[], ContextAssembler],
        settings: Any,
    ) -> None:
        self._context_manager_for = context_manager_for
        self._assembler_for = assembler_for
        self._settings = settings

    @property
    def context_assembler(self) -> ContextAssembler:
        """Resolved on use, not held: the agent may not have one yet."""
        return self._assembler_for()

    # -- configuration, read live -------------------------------------------
    #
    # Read through the settings object rather than copied at construction,
    # because both values are set *after* an agent is built in more than one
    # place (`tests/test_consolidation.py` sets `context_window` and
    # `output_reserve` on a live agent; the web channel resizes its window per
    # session).  A copied value would silently ignore those overrides, which is
    # the kind of bug this refactor keeps finding.

    @property
    def context_window(self) -> int:
        return int(getattr(self._settings, "context_window", 0) or 0)

    @property
    def output_reserve(self) -> int:
        return int(getattr(self._settings, "output_reserve", 0) or 0)

    # -- the estimator chain -------------------------------------------------

    def estimate_input_tokens(
        self,
        messages: list[dict],
        *,
        ctx: Any = None,
    ) -> int:
        """Tokens *messages* cost, via the scoped manager's estimator if it has one.

        Falls back to the same CJK-aware model the manager would have used,
        rather than ``len(json.dumps) / CHARS_PER_TOKEN``: that older fallback
        counted a Chinese character as a quarter of a token, so it under-reported
        exactly the payloads most likely to be too large -- and because the
        budget is derived from this same estimator, it moved the wall along with
        the measurement instead of correcting anything.
        """
        consolidation = self._consolidation_for(ctx)
        estimate = getattr(consolidation, "estimate_tokens", None)
        if callable(estimate):
            return estimate(messages)
        return estimate_message_tokens(messages)

    def estimate_raw_body_tokens(self, ctx: Any) -> int:
        """The body's cost under the raw character heuristic, uncorrected.

        The split-aware calibration path compares the provider's count against
        this raw estimate, so the learned correction stays a property of the
        estimator rather than feeding the factor its own output.  The fallback
        is already calibration-free, which keeps the no-manager path -- where
        the calibrated and raw estimates coincide -- self-consistent.
        """
        consolidation = self._consolidation_for(ctx)
        raw = getattr(consolidation, "estimate_tokens_raw", None)
        if callable(raw):
            return raw(ctx.messages)
        return estimate_message_tokens(ctx.messages)

    def payload_overhead(self, ctx: Any, tools: list[dict]) -> int:
        """Tokens the provider bills that are not in ``ctx.messages``.

        The system prompt and the tool schemas ride in every request and are
        charged on every call, so the window has to hold them even though they
        never appear in ``ctx.messages``.  Measuring them in one place is what
        keeps the input budget and the output cap from disagreeing about what
        the request already costs; see the module docstring for why the price
        comes from the head's fingerprint rather than from the body's factor.
        """
        consolidation = self._consolidation_for(ctx)
        estimate_head = getattr(consolidation, "estimate_head_tokens", None)
        if callable(estimate_head):
            return estimate_head(
                head_fingerprint(ctx.system_prompt, tools),
                ctx.system_prompt,
                tools,
            )
        overhead_messages = [
            {"role": "system", "content": ctx.system_prompt},
            {
                "role": "system",
                "content": json.dumps(tools, ensure_ascii=False, sort_keys=True),
            },
        ]
        return self.estimate_input_tokens(overhead_messages, ctx=ctx)

    # -- the budgets ---------------------------------------------------------

    def output_room(self, ctx: Any, tools: list[dict]) -> int:
        """Tokens the window can still give to the response.

        The window holds the system prompt, the tool schemas, the messages *and*
        the answer, so the answer's ceiling is whatever is left after the first
        three.  That is what makes a per-call cap possible without shrinking the
        conversation: the cap follows the input rather than deciding it.
        """
        return (
            self.context_window
            - self.payload_overhead(ctx, tools)
            - self.estimate_input_tokens(ctx.messages, ctx=ctx)
            - OUTPUT_CAP_MARGIN
        )

    def input_token_budget(
        self,
        ctx: Any,
        tools: list[dict],
        *,
        output_max_tokens: Optional[int] = None,
    ) -> int:
        """What the input may occupy, holding *reserve* back for the answer.

        When the caller names a specific output budget, that is the room it must
        leave instead of the configured reserve -- the two are the same question
        asked from opposite ends.
        """
        reserve = (
            self.output_reserve
            if output_max_tokens is None
            else int(output_max_tokens)
        )
        return self.context_window - reserve - self.payload_overhead(ctx, tools)

    def retrieval_token_budget(
        self,
        ctx: Any = None,
        tools: Optional[list[dict]] = None,
        current_user_content: Any = "",
    ) -> int:
        """How much of the window automatic retrieval may claim.

        Retrieval rides in the turn's own message, so an unbounded injection
        cannot occupy room permanently -- compaction can drop it -- but it is
        still charged in full on the call that carries it, and a large enough
        one drives the input budget negative and hard-fails the turn.  A
        fraction of the window keeps that impossible by construction rather
        than relying on stored memories staying short.
        """
        configured = int(getattr(self._settings, "max_retrieval_tokens", 0) or 0)
        if ctx is not None and tools is not None:
            budget = self.context_assembler.allocate(
                context_window=self.context_window,
                # The reserve, not the configured cap: retrieval competes with
                # the conversation for the same window, and sizing that window
                # by the output *cap* starved retrieval to roughly a ninth of
                # what it should have had (about 1k against about 9k here).
                output_tokens=self.output_reserve,
                system_prompt=ctx.system_prompt,
                tools=tools,
                # One home for the head's price: the same measured,
                # fingerprint-locked value the input budget uses.
                static_tokens=self.payload_overhead(ctx, tools),
                current_messages=ctx.messages,
                current_user_content=current_user_content,
                estimate=lambda messages: self.estimate_input_tokens(messages, ctx=ctx),
                configured_retrieval_tokens=configured,
            )
            ctx.metadata["context_budget"] = {
                "static_tokens": budget.static_tokens,
                "message_tokens": budget.message_tokens,
                "retrieval_tokens": budget.retrieval_tokens,
                "input_tokens": budget.input_tokens,
                "selected_tools": len(tools),
            }
            return budget.retrieval_tokens
        if configured > 0:
            return configured
        usable = max(0, self.context_window - self.output_reserve)
        return max(1024, int(usable * shared.RETRIEVAL_BUDGET_FRACTION))

    # -- internals -----------------------------------------------------------

    def _consolidation_for(self, ctx: Any) -> Any:
        """The consolidation engine of the manager serving *ctx*, if any."""
        manager = self._context_manager_for(ctx)
        return getattr(manager, "consolidation", None)

    def configured_output_cap(self, output_max_tokens: Optional[int]) -> int:
        """The cap this call would send if the window had room for all of it.

        ``None`` means "the configured cap".  Resolving that to a number before
        the budget is computed is the conflation this separation exists to
        remove: the same number then also becomes the input budget's reserve, so
        a provider configured for long answers gets the smallest usable context.
        """
        if output_max_tokens is None:
            return int(self._default_output_cap)
        return int(output_max_tokens)

    @property
    def _default_output_cap(self) -> int:
        return int(getattr(self._settings, "max_tokens", 0) or 0)
