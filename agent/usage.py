"""Provider-neutral token usage extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

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
    """Read Anthropic-, OpenAI-, and compatible-provider usage fields."""

    usage = _value(response, "usage")
    if usage is None:
        return ProviderUsage()

    input_tokens = _positive_int(_value(usage, "input_tokens"))
    if not input_tokens:
        input_tokens = _positive_int(_value(usage, "prompt_tokens"))

    output_tokens = _positive_int(_value(usage, "output_tokens"))
    if not output_tokens:
        output_tokens = _positive_int(_value(usage, "completion_tokens"))

    cached = max(
        _positive_int(_value(usage, "cache_read_input_tokens")),
        _positive_int(_value(usage, "prompt_cache_hit_tokens")),
    )
    prompt_details = _value(usage, "prompt_tokens_details")
    if prompt_details is not None:
        cached = max(cached, _positive_int(_value(prompt_details, "cached_tokens")))

    return ProviderUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
    )


__all__ = ["ProviderUsage", "extract_provider_usage"]
