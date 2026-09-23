"""Provider-neutral token usage extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderUsage:
    """What one provider call cost, in the one vocabulary both providers share.

    ``input_tokens`` is the **whole prompt** -- cached and uncached together.
    That is the quantity the cost model and the token estimator both need:
    billed input for request *n* is ``h_n * c_hit + (|P_n| - h_n) * c_full``,
    where ``|P_n|`` is this field and ``h_n`` is ``cached_input_tokens``.  The
    two providers report it in incompatible pieces, so the disagreement is
    resolved in :func:`extract_provider_usage` -- once, at the boundary --
    instead of being rediscovered by every reader.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    #: Tokens this call *wrote into* the provider's cache.  Billed at a premium
    #: over an ordinary input token, and not a cache read, so it is counted in
    #: ``input_tokens`` and deliberately not in ``cached_input_tokens``.
    cache_creation_input_tokens: int = 0

    @property
    def uncached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cached_input_tokens)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _value(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _positive_int(value: Any) -> int:
    return int(value) if isinstance(value, int) and value > 0 else 0


def extract_provider_usage(response: Any) -> ProviderUsage:
    """Read Anthropic-, OpenAI-, and compatible-provider usage fields.

    The discriminator is which field names are present, not which provider is
    configured: a gateway may speak either format under any provider name, and
    the usage object is what it actually sent.

    * OpenAI-compatible -- ``prompt_tokens`` *is* the total, and
      ``prompt_cache_hit_tokens`` or ``prompt_tokens_details.cached_tokens``
      reports how much of it was served from cache.
    * Anthropic -- ``input_tokens`` is only the *uncached* remainder, and
      ``cache_read_input_tokens`` and ``cache_creation_input_tokens`` are
      disjoint from it.  Reading ``input_tokens`` alone therefore under-reports
      the prompt by the whole cached prefix, and a fully cache-hit call reports
      ``0`` -- which then reads as a request that cost nothing, and which the
      estimator calibrates against as though the payload had been empty.
    """
    usage = _value(response, "usage")
    if usage is None:
        return ProviderUsage()

    anthropic_uncached = _positive_int(_value(usage, "input_tokens"))
    openai_total = _positive_int(_value(usage, "prompt_tokens"))
    cache_read = _positive_int(_value(usage, "cache_read_input_tokens"))
    cache_creation = _positive_int(_value(usage, "cache_creation_input_tokens"))

    if openai_total:
        input_tokens = openai_total
    else:
        input_tokens = anthropic_uncached + cache_read + cache_creation

    output_tokens = _positive_int(_value(usage, "output_tokens"))
    if not output_tokens:
        output_tokens = _positive_int(_value(usage, "completion_tokens"))

    cached = max(
        cache_read,
        _positive_int(_value(usage, "prompt_cache_hit_tokens")),
    )
    prompt_details = _value(usage, "prompt_tokens_details")
    if prompt_details is not None:
        cached = max(cached, _positive_int(_value(prompt_details, "cached_tokens")))

    return ProviderUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
        cache_creation_input_tokens=cache_creation,
    )


__all__ = ["ProviderUsage", "extract_provider_usage"]
