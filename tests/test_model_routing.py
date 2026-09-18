"""RoutingTransport: model id -> provider client dispatch.

The model dropdown offers every configured provider's models, so a
model_override must reach the provider that actually owns the model —
not merely the active provider's client with a foreign model string.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent.core.transport import (
    AnthropicTransport,
    OpenAITransport,
    RoutingTransport,
    build_routing_transport,
    routable_model_ids,
    routing_table,
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
        self.streamed: list[str] = []
        self.chatted: list[str] = []

    async def create(self, *, model, max_tokens, system, messages, tools):
        self.created.append(model)
        return {"stop_reason": "end_turn", "owner": "anthropic-like"}

    async def stream(self, *, model, max_tokens, system, messages, tools, callback):
        self.streamed.append(model)
        return {"stop_reason": "end_turn", "owner": "anthropic-like"}, "anthropic-like"

    async def simple_chat(self, *, model, max_tokens, system, prompt):
        self.chatted.append(model)
        return "anthropic-like"


class _RecordingOpenAITransport(OpenAITransport):
    def __init__(self) -> None:
        super().__init__(client=None)
        self.created: list[str] = []
        self.streamed: list[str] = []
        self.chatted: list[str] = []

    async def create(self, *, model, max_tokens, system, messages, tools):
        self.created.append(model)
        return {"choices": [{"finish_reason": "stop"}], "owner": "openai-like"}

    async def stream(self, *, model, max_tokens, system, messages, tools, callback):
        self.streamed.append(model)
        return {"choices": [{"finish_reason": "stop"}]}, "openai-like"

    async def simple_chat(self, *, model, max_tokens, system, prompt):
        self.chatted.append(model)
        return "openai-like"


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
    assert made == ["openai"]


def test_routing_transport_detects_truncated_tool_protocol_for_routed_model():
    """A routed OpenAI response must keep its truncation signal.

    The base ModelTransport reports False for every provider; without an
    override the routing layer would drop truncated-tool-call recovery for
    every OpenAI-format model it dispatches to, because the agent asks the
    transport it actually used.
    """
    from agent import shared

    partial = shared._OAIResponse(
        [
            shared._OAIChoice(
                "length",
                shared._OAIMsg(
                    "creating file",
                    [
                        shared._OAITC(
                            "call-1",
                            shared._OAIFunc(
                                "write_file", '{"path":"a.html","content":"<html>'
                            ),
                        )
                    ],
                ),
            )
        ]
    )
    active = _RecordingAnthropicTransport()
    other = _RecordingOpenAITransport()
    routing = RoutingTransport(active, {"gpt-4o": other})

    assert routing.has_incomplete_tool_calls(partial, model="gpt-4o") is True
    # The active (Anthropic) provider has no partial-tool protocol.
    assert routing.has_incomplete_tool_calls(partial, model="claude-opus-4-5") is False
    # No model means the default transport, same as every other routed call.
    assert routing.has_incomplete_tool_calls(partial) is False


def test_build_routing_transport_shares_cached_clients_across_rebuilds():
    """Per-session routing rebuilds must not open a client per session."""
    made: list[str] = []

    def factory(provider_cfg: dict, api_format: str) -> Any:
        made.append(str(provider_cfg.get("api_key")))
        return object()

    cache: dict = {}

    def cfg_for(api_key: str) -> dict:
        return {
            "active_provider": "anthropic",
            "providers": {
                "anthropic": {"api_format": "anthropic", "models": ["m1"]},
                "openai": {
                    "api_format": "openai",
                    "api_key": api_key,
                    "models": ["g1"],
                },
            },
        }

    build_routing_transport(cfg_for("k1"), "anthropic", None, factory, client_cache=cache)
    build_routing_transport(cfg_for("k1"), "anthropic", None, factory, client_cache=cache)
    assert made == ["k1"], "a rebuild reused the cached client"

    # A changed credential must build a fresh client, not reuse the stale one.
    build_routing_transport(cfg_for("k2"), "anthropic", None, factory, client_cache=cache)
    assert made == ["k1", "k2"]


def test_agent_transport_is_injectable_and_defaults_to_the_client():
    """The transport is a construction dependency, not a post-hoc assignment.

    Omitting it must keep building from ``(api_format, client)`` — that is
    how every single-provider agent is made.
    """
    import agent as agent_module

    injected = _RecordingAnthropicTransport()
    agent = agent_module.BaseAgent(
        object(),
        agent_module.ToolRegistry(),
        model="m",
        api_format="anthropic",
        transport=injected,
    )
    assert agent._transport is injected

    defaulted = agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="m", api_format="openai"
    )
    assert isinstance(defaulted._transport, OpenAITransport)
    assert not isinstance(defaulted._transport, RoutingTransport)


def test_routing_table_is_the_one_definition_of_model_ownership():
    """The ids a caller may request and the ids the router can dispatch agree.

    Validation and dispatch used to derive their sets separately — one asked
    the config for a provider's models, the other built its own table — so an
    id could pass validation and still be handed to a provider that does not
    serve it.
    """
    cfg = {
        "active_provider": "deepseek",
        "model": "deepseek-flash",
        "providers": {
            "deepseek": {
                "api_format": "openai",
                "default_model": "deepseek-flash",
                "models": ["deepseek-flash", "deepseek-pro"],
            },
            "huoshan": {"api_format": "openai", "models": ["glm-5.3-flash"]},
        },
    }

    table = routing_table(cfg)
    assert table == {
        "deepseek-flash": "deepseek",
        "deepseek-pro": "deepseek",
        "glm-5.3-flash": "huoshan",
    }
    # The top-level `model` names the active provider's model, so it is the
    # only id a caller may request that is not itself a routing key.
    assert routable_model_ids(cfg) == set(table) | {"deepseek-flash"}

    routing = build_routing_transport(cfg, "openai", None, lambda *_a: object())
    assert set(routing.routes) == set(table)


def test_a_groups_default_model_is_routable_even_when_unlisted():
    """A group that names a default it does not also list still owns it.

    Omitting it left the id out of the routing table entirely, so a config
    value or a model selection naming it went to whichever provider happened
    to be active rather than the one that declares it.
    """
    cfg = {
        "active_provider": "deepseek",
        "providers": {
            "deepseek": {"api_format": "openai", "models": ["deepseek-flash"]},
            "qwen": {"api_format": "openai", "default_model": "qwen3.5-plus"},
        },
    }

    assert routing_table(cfg)["qwen3.5-plus"] == "qwen"
    assert "qwen3.5-plus" in routable_model_ids(cfg)

    routing = build_routing_transport(cfg, "openai", None, lambda *_a: object())
    assert "qwen3.5-plus" in routing.routes


def test_endpoint_for_pairs_a_model_with_the_client_that_owns_it():
    """Background consumers ask for a model's endpoint, not a client.

    Memory consolidation, the session-end flush and the evolution engine make
    their own LLM calls.  Each used to hold the active provider's client *and*
    a model id from config, so naming a model from another group posted it to
    the active provider.
    """
    import agent as agent_module

    active_client = object()
    foreign_client = object()
    made: list[str] = []

    def factory(provider_cfg: dict, api_format: str) -> Any:
        made.append(str(provider_cfg.get("api_key")))
        return foreign_client

    cfg = {
        "active_provider": "deepseek",
        "providers": {
            "deepseek": {
                "api_format": "openai",
                "api_key": "k-active",
                "models": ["deepseek-flash"],
            },
            "huoshan": {
                "api_format": "openai",
                "api_key": "k-foreign",
                "models": ["glm-5.3-flash"],
            },
        },
    }
    agent = agent_module.BaseAgent(
        active_client,
        agent_module.ToolRegistry(),
        model="deepseek-flash",
        api_format="openai",
        transport=build_routing_transport(cfg, "openai", active_client, factory),
    )

    foreign = agent.endpoint_for("glm-5.3-flash")
    assert foreign.client is foreign_client
    assert foreign.api_format == "openai"
    assert made == ["k-foreign"]

    # The active provider's own model keeps its own client...
    assert agent.endpoint_for("deepseek-flash").client is active_client
    # ...and an id no group owns falls back the way an unrouted turn does.
    assert agent.endpoint_for("no-such-model").client is active_client


def test_consolidation_endpoint_reads_the_configured_model():
    """The model a session consolidates with has exactly one definition.

    ``context.consolidation.model`` may name another group's model; when it is
    unset the session's own model is used.  The background worker and the
    session-end flush both read it from here, so a flush cannot run on a
    different model than the background passes did.
    """
    import agent as agent_module

    active_client = object()
    cfg = {
        "active_provider": "deepseek",
        "providers": {
            "deepseek": {
                "api_format": "openai",
                "api_key": "k-active",
                "models": ["deepseek-flash", "glm-5.3-flash"],
            },
        },
    }
    agent = agent_module.BaseAgent(
        active_client,
        agent_module.ToolRegistry(),
        model="deepseek-flash",
        api_format="openai",
        transport=build_routing_transport(
            cfg, "openai", active_client, lambda *_a: object()
        ),
    )

    assert agent.consolidation_model({}) == "deepseek-flash"
    assert agent.consolidation_model(
        {"context": {"consolidation": {"model": "glm-5.3-flash"}}}
    ) == "glm-5.3-flash"
    endpoint = agent.consolidation_endpoint(
        {"context": {"consolidation": {"model": "glm-5.3-flash"}}}
    )
    assert endpoint.client is active_client
    assert endpoint.api_format == "openai"


def test_sub_agent_dispatches_foreign_model_to_its_owning_provider():
    """A sub-agent must inherit the parent's routing table, not rebuild one.

    A sub-agent runs on the turn's effective model, and a per-turn override
    may name any configured provider's model — the composer dropdown offers
    them all.  When the sub-agent built its transport from the parent's
    *client* it got a bare active-provider transport, so the foreign model id
    was sent to the active provider's endpoint, which rejected it with a 400
    ("The supported API model names are ..., but you passed ...").
    """
    import agent as agent_module
    from agent.core.agent import _active_agent_context

    active = _RecordingAnthropicTransport()
    foreign = _RecordingOpenAITransport()
    routing = RoutingTransport(active, {"glm-5.3-flash": foreign})

    parent = agent_module.BaseAgent(
        object(),
        agent_module.ToolRegistry(),
        model="deepseek-flash",
        api_format="anthropic",
        transport=routing,
    )
    # The turn resolved a model owned by another provider.
    token = _active_agent_context.set(
        agent_module.AgentContext(
            system_prompt="system",
            metadata={"model_override": "glm-5.3-flash"},
        )
    )
    try:
        child = parent._create_sub_agent(agent_module.ToolRegistry())
        assert child._transport is routing
        # A sub-agent's own loop streams; the lightweight summariser chats.
        asyncio.run(
            child._transport.stream(
                model=child.model, max_tokens=16, system="s",
                messages=[], tools=[], callback=lambda _t: None,
            )
        )
        asyncio.run(child._call_llm("hi", system="s"))
    finally:
        _active_agent_context.reset(token)

    assert foreign.streamed == ["glm-5.3-flash"]
    assert foreign.chatted == ["glm-5.3-flash"]
    assert active.streamed == []
    assert active.chatted == []


def _serve_openai_provider() -> tuple[str, list[dict], ThreadingHTTPServer]:
    """A local OpenAI-shaped endpoint: returns (base_url, received_bodies, server)."""
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.loads(raw)
            received.append(body)
            payload = json.dumps(
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": body.get("model", "unknown"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ack"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/v1", received, server


def test_sub_agent_request_reaches_the_provider_that_owns_its_model():
    """The reported failure, end to end: two providers, real SDK clients.

    Two local servers stand in for two providers and real AsyncOpenAI clients
    talk to them over real sockets, so this exercises the whole chain a
    sub-agent's request travels — not just the transport object it holds.

    Spawned under a foreign-provider model, the sub-agent must send that id to
    the provider that owns it.  Before the fix it sent the foreign id to the
    active provider, which answered

        400 The supported API model names are ..., but you passed glm-5.3-flash.
    """
    import agent as agent_module
    from agent.core.agent import _active_agent_context

    openai = pytest.importorskip("openai")

    active_url, active_seen, active_srv = _serve_openai_provider()
    foreign_url, foreign_seen, foreign_srv = _serve_openai_provider()
    try:
        cfg = {
            "active_provider": "deepseek",
            "providers": {
                "deepseek": {
                    "api_format": "openai",
                    "base_url": active_url,
                    "api_key": "key-active",
                    "models": ["deepseek-flash"],
                },
                "huoshan": {
                    "api_format": "openai",
                    "base_url": foreign_url,
                    "api_key": "key-foreign",
                    "models": ["glm-5.3-flash"],
                },
            },
        }

        def factory(provider_cfg: dict, api_format: str) -> Any:
            return openai.AsyncOpenAI(
                base_url=provider_cfg["base_url"],
                api_key=provider_cfg["api_key"],
                max_retries=0,
            )

        active_client = factory(cfg["providers"]["deepseek"], "openai")
        parent = agent_module.BaseAgent(
            active_client,
            agent_module.ToolRegistry(),
            model="deepseek-flash",
            api_format="openai",
            transport=build_routing_transport(cfg, "openai", active_client, factory),
        )
        parent._base_system_prompt = "You are a probe."
        parent.sub_agent_timeout_seconds = 30
        parent.sub_agent_retries = 0

        token = _active_agent_context.set(
            agent_module.AgentContext(
                system_prompt="probe",
                metadata={"model_override": "glm-5.3-flash"},
            )
        )
        try:
            payload = asyncio.run(
                parent._execute_agent(
                    role="researcher", task="say hi", capability_profile="read_only"
                )
            )
        finally:
            _active_agent_context.reset(token)
    finally:
        active_srv.shutdown()
        active_srv.server_close()
        foreign_srv.shutdown()
        foreign_srv.server_close()

    assert payload.get("ok") is True, payload.get("error")
    assert [b.get("model") for b in foreign_seen] == ["glm-5.3-flash"]
    assert active_seen == [], "the active provider must never see a foreign model id"


def test_consolidation_request_reaches_the_provider_that_owns_its_model(tmp_path):
    """The reported failure for a background consumer, over real sockets.

    ``context.consolidation.model`` is meant to name a cheaper model, and the
    cheapest one often lives in another group.  The engine is handed the
    endpoint resolved from that model, so consolidation must land on the
    owning provider.  Before the fix the active provider saw the foreign id
    and answered

        400 The supported API model names are ..., but you passed glm-5.3-flash.
    """
    from agent import LTMStore, StagingBuffer
    from agent.memory.consolidation import ConsolidationEngine

    openai = pytest.importorskip("openai")
    import agent as agent_module

    active_url, active_seen, active_srv = _serve_openai_provider()
    foreign_url, foreign_seen, foreign_srv = _serve_openai_provider()
    try:
        cfg = {
            "active_provider": "deepseek",
            "providers": {
                "deepseek": {
                    "api_format": "openai",
                    "base_url": active_url,
                    "api_key": "key-active",
                    "models": ["deepseek-flash"],
                },
                "huoshan": {
                    "api_format": "openai",
                    "base_url": foreign_url,
                    "api_key": "key-foreign",
                    "models": ["glm-5.3-flash"],
                },
            },
        }

        def factory(provider_cfg: dict, api_format: str) -> Any:
            return openai.AsyncOpenAI(
                base_url=provider_cfg["base_url"],
                api_key=provider_cfg["api_key"],
                max_retries=0,
            )

        active_client = factory(cfg["providers"]["deepseek"], "openai")
        parent = agent_module.BaseAgent(
            active_client,
            agent_module.ToolRegistry(),
            model="deepseek-flash",
            api_format="openai",
            transport=build_routing_transport(cfg, "openai", active_client, factory),
        )

        staging = StagingBuffer(path=tmp_path / "staging.jsonl", session_id="s1")
        staging.append("user", "we decided to prefer concise responses")
        staging.append("assistant", "noted")

        endpoint = parent.endpoint_for("glm-5.3-flash")
        engine = ConsolidationEngine(
            store=LTMStore(
                context_dir=tmp_path / "context", memory_dir=tmp_path / "memory"
            )
        )
        asyncio.run(engine.consolidate([], endpoint, "glm-5.3-flash", staging=staging))
    finally:
        active_srv.shutdown()
        active_srv.server_close()
        foreign_srv.shutdown()
        foreign_srv.server_close()

    assert foreign_seen, "the owning provider received nothing at all"
    assert {b.get("model") for b in foreign_seen} == {"glm-5.3-flash"}
    assert active_seen == [], "the active provider must never see a foreign model id"
