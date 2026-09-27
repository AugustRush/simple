"""Build bounded provider inputs from session state and task intent."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable

from agent import shared


#: Meaningful only inside a scheduled run -- outside one it refuses, because
#: there is a person to tell instead.  Gated on *the run* rather than on words:
#: no sentence in a conversation makes it usable, and a schema that can only be
#: called in error is not worth the tokens.
#:
#: This is the only gate left, and it survives because it is a function of
#: session-stable state: a run's context carries its ``scheduler_run_id`` from
#: construction to the end, so the tool block is byte-identical for every call
#: the run makes.  Every *word*-keyed gate that used to sit here is gone -- see
#: `select_tools`.
_RUN_SELF_REPORT_TOOLS = {"report_outcome"}


@dataclass(frozen=True)
class ContextBudget:
    static_tokens: int
    message_tokens: int
    retrieval_tokens: int
    input_tokens: int


class ContextAssembler:
    """Select tools and allocate retrieval from one shared input budget."""

    def select_tools(
        self,
        tools: list[dict[str, Any]],
        *,
        scheduled_run: bool = False,
    ) -> list[dict[str, Any]]:
        """The schemas this agent instance offers, for the life of the session.

        The tool block sits ahead of every message in the request, so a
        provider's prefix cache can only reuse what precedes its first
        difference -- and the whole conversation sits behind it.  A set chosen
        from each turn's own words is therefore the most expensive thing this
        module could do: every turn that changes the set re-prices every
        message after it at full rate.  Measured on this machine, a head that
        moved on most turns pinned the cache read at ~7.4k of a ~100k payload.

        So membership is a function of the *agent instance* and nothing else.
        What the words mention is not a consideration here; what decides
        whether a call is allowed is the executor's capability check, which
        runs against the whole registry whether or not a schema was sent.  The
        one gate left keys on the run, which is session-stable state.

        ``query``/``required_skills``/``attachment_kinds`` used to be
        parameters here.  Dropping them is the point rather than an
        oversight: any of them re-introduces per-turn membership, and the
        ordering that once softened this ("always-on schemas first") cannot
        help, because the divergence it fails to avoid is still ahead of the
        body.
        """
        return [
            tool
            for tool in tools
            if scheduled_run
            or str(tool.get("name") or "") not in _RUN_SELF_REPORT_TOOLS
        ]

    def allocate(
        self,
        *,
        context_window: int,
        output_tokens: int,
        system_prompt: str,
        tools: list[dict[str, Any]],
        current_messages: list[dict[str, Any]],
        current_user_content: Any,
        estimate: Callable[[list[dict[str, Any]]], int],
        configured_retrieval_tokens: int = 0,
        static_tokens: Optional[int] = None,
    ) -> ContextBudget:
        """Size the retrieval budget inside the window that is left.

        ``static_tokens`` lets a caller that already knows the head's measured
        cost (the memory engine's per-fingerprint price, the same value the
        input budget charges) hand it over directly, so the head is never
        priced here by the body's calibration factor.  When it is absent the
        head is estimated through ``estimate`` like before.
        """
        if static_tokens is None:
            static = estimate([
                {"role": "system", "content": system_prompt},
                {
                    "role": "system",
                    "content": json.dumps(tools, ensure_ascii=False, sort_keys=True),
                },
            ])
        else:
            static = max(0, int(static_tokens))
        input_tokens = max(0, int(context_window) - int(output_tokens) - static)
        messages = estimate(current_messages)
        current = estimate([{"role": "user", "content": current_user_content}])
        # The newest request and a small protocol margin are mandatory. Retrieval
        # receives only a fraction of what remains after those costs.
        remaining = max(0, input_tokens - messages - current - 256)
        desired = (
            int(configured_retrieval_tokens)
            if configured_retrieval_tokens > 0
            else int(remaining * shared.RETRIEVAL_BUDGET_FRACTION)
        )
        retrieval = max(0, min(desired, remaining))
        return ContextBudget(
            static_tokens=static,
            message_tokens=messages + current,
            retrieval_tokens=retrieval,
            input_tokens=input_tokens,
        )


__all__ = ["ContextAssembler", "ContextBudget"]
