"""RoutingTransport: model id -> provider client dispatch.

The model dropdown offers every configured provider's models, so a
model_override must reach the provider that actually owns the model —
not merely the active provider's client with a foreign model string.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent.core.transport import (
    AnthropicTransport,
    OpenAITransport,
    RoutingTransport,
    build_routing_transport,
)


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    # Anthropic-shaped entry point used by the transports under test.
    class messages:
        @staticmethod
        async def create(**kwargs: Any) -> dict[str, Any]:
            raise NotImplementedError

    async def chat_completions_create(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError


class _RecordingAnthropicTransport(AnthropicTransport):
    def __init__(self) -> None:
        super().__init__(client=None)
        self.created: list[str] = []

    async def create(self, *, model, max_tokens, system, messages, tools):
        self.created.append(model)
        return {"stop_reason": "end_turn", "owner": "anthropic-like"}


class _RecordingOpenAITransport(OpenAITransport):
    def __init__(self) -> None:
        super().__init__(client=None)
        self.created: list[str] = []

    async def create(self, *, model, max_tokens, system, messages, tools):
        self.created.append(model)
        return {"choices": [{"finish_reason": "stop"}], "owner": "openai-like"}


def test_routed_create_dispatches_by_model_owner():
    active = _RecordingAnthropicTransport()
    other = _RecordingOpenAITransport()
    routing = RoutingTransport(active, {"gpt-4o": other, "o4-mini": other})

    asyncio.run(routing.create(
        model="claude-opus-4-5", max_tokens=16, system="s",
        messages=[], tools=[],
    ))
    asyncio.run(routing.create(
        model="gpt-4o", max_tokens=16, system="s", messages=[], tools=[],
    ))
    asyncio.run(routing.create(
        model="totally-unknown-model", max_tokens=16, system="s",
        messages=[], tools=[],
    ))

    assert active.created == ["claude-opus-4-5", "totally-unknown-model"]
    assert other.created == ["gpt-4o"]


def test_build_routing_transport_prefers_active_provider_on_conflicts():
    made: list[tuple[str, str]] = []

    def factory(provider_cfg: dict, api_format: str) -> Any:
        made.append(api_format)
        return object()

    cfg = {
        "active_provider": "anthropic",
        "providers": {
            "openai": {
                "api_format": "openai",
                "models": ["gpt-4o", "shared-model"],
            },
            "anthropic": {
                "api_format": "anthropic",
                "models": ["claude-opus-4-5", "shared-model"],
            },
        },
    }
    routing = build_routing_transport(
        cfg, "anthropic", None, factory
    )

    assert routing.routes["claude-opus-4-5"] is routing.default
    assert routing.routes["gpt-4o"] is not routing.default
    # The active provider owns the ambiguous id; the dropdown shows it under
    # the active group, so routing must agree.
    assert routing.routes["shared-model"] is routing.default
    # Exactly one client built, for the one non-active provider.
    assert made == ["openai"]


def test_build_routing_transport_skips_providers_without_models():
    made: list[tuple[str, str]] = []

    def factory(provider_cfg: dict, api_format: str) -> Any:
        made.append(api_format)
        return object()

    cfg = {
        "active_provider": "anthropic",
        "providers": {
            "anthropic": {"api_format": "anthropic", "models": ["m1"]},
            "empty": {"api_format": "openai", "models": []},
            "default-only": {"api_format": "openai", "default_model": "d1"},
        },
    }
    routing = build_routing_transport(
        cfg, "anthropic", None, factory
    )
    assert set(routing.routes) == {"m1", "d1"}
    # default_model alone still routes, but only the non-active provider
    # needed a client.
    # default_model alone still routes, but only the non-active provider
    # needed a client.
    assert made == ["openai"]
