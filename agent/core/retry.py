"""Retrying a model call, with a policy rather than an agent.

Why this is its own module, and why the policy is small
-------------------------------------------------------
The turn loop's retry leg needs three values and one predicate: how many attempts,
how long the first backoff is, which bus a scheduled retry is announced on, and
whether a given exception is retryable at all.  It was reached as
``agent._with_llm_retry``, which meant the caller had to hold an agent -- and
``agent/core/llm_attempt.py`` documents that it deliberately does *not* import
``agent.core.agent``.

The first attempt at this extraction handed the whole loop dependency surface
(twenty-three members) to every helper, and that is what broke: a wide struct is
right for the loop as a whole and wrong for its parts, because it makes each part
claim a dependency on everything.  This module therefore takes
:class:`RetryPolicy` -- three values -- and the predicate as a module-level
function, so a caller needs a retry policy and nothing else.

What is *not* here: the backoff schedule is the trivial doubling the original had,
and it stays inline for that reason.  A schedule abstraction would be a place for
the two to disagree about what "exponential" means, for no reader's benefit.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from agent.core.llm_errors import is_llm_retryable

logger = logging.getLogger(__name__)

__all__ = ["RetryPolicy", "with_llm_retry"]


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to retry a model call, and how to announce it.

    Frozen because a caller that could change the attempt ceiling mid-turn would
    make the number of attempts depend on where in the loop the exception landed.
    """

    #: Total attempts, so ``0`` means one call with no retry.  Named for attempts
    #: rather than retries because that is what the range below counts, and an
    #: off-by-one here is a silent extra call against the provider.
    max_retries: int
    #: Backoff before the first retry; doubled each subsequent attempt.
    base_delay: float
    #: The bus a scheduled retry is announced on.  Absence is the caller's business,
    #: not an error: a turn with no observer must still retry.
    event_bus: Any = None
    #: Which exceptions are worth another attempt.  Part of the policy rather than a
    #: module constant the call site reaches for, because "is this transient?" is a
    #: judgement the composition gets to make -- a provider with a different notion
    #: of transient supplies its own, and a test supplies a permissive one instead of
    #: reaching into the object under test to override a method.
    is_retryable: Callable[[Exception], bool] = is_llm_retryable


async def with_llm_retry(
    policy: RetryPolicy,
    fn: Callable[..., Any],
    *args: Any,
    can_retry: Optional[Callable[[], bool]] = None,
    **kwargs: Any,
) -> Any:
    """Call ``fn(*args, **kwargs)``, retrying on a transient LLM API error.

    ``can_retry`` is consulted before each retry.  Streaming callers use it to veto
    a replay once output has already reached the user, because a replay would
    duplicate what they have already seen -- so a retry that is refused here is
    re-raised rather than silently skipped.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(policy.max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt >= policy.max_retries or not policy.is_retryable(exc):
                raise
            if can_retry is not None and not can_retry():
                logger.warning(
                    "LLM API error after partial output; not retrying "
                    "(a replay would duplicate what the user already saw): %s",
                    exc,
                )
                raise
            delay = policy.base_delay * (2 ** attempt)
            logger.warning(
                "LLM API error (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1,
                policy.max_retries,
                delay,
                exc,
            )
            await _announce_retry(policy, attempt, delay, exc)
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


async def _announce_retry(policy: RetryPolicy, attempt: int, delay: float, exc: Exception) -> None:
    """Publish ``LLMRetryScheduled`` if anything is listening.

    Imported here rather than at module scope so this module does not need
    ``agent.core.event_domains`` at import time -- the domain table builds buses on
    import, and a retry helper is not a reason to build them.
    """
    if policy.event_bus is None:
        return
    from agent.core.event_domains import LLMRetryScheduled
    from agent.core.turn_context import active_agent_context, active_agent_id, emit_domain_event

    ctx = active_agent_context.get()
    await emit_domain_event(
        policy.event_bus,
        LLMRetryScheduled,
        agent_id=active_agent_id(),
        model=ctx.metadata.get("_model", "") if ctx is not None else "",
        attempt=attempt + 1,
        delay_seconds=delay,
        error=str(exc),
    )
