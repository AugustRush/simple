"""Per-provider request headers: what a gateway needs beyond URL and key.

`providers.<name>.headers` exists because some gateways want a client
identifier, an organisation tag, or a routing/session header on every request.
The last kind is the one with teeth: a header whose value is "one stable id per
conversation" cannot live on the SDK client, because the client is built once
per provider configuration and shared by every conversation -- a value captured
there could only be a process-wide constant. So the header travels per request
and the conversation is read at request time.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent import shared
from agent.core.transport import (
    AnthropicTransport,
    OpenAITransport,
    build_routing_transport,
    provider_client_cache_key,
)


# ── Resolution ────────────────────────────────────────────────────────────


def test_a_provider_without_headers_resolves_to_nothing():
    assert shared.resolve_provider_headers({}) == {}
    assert shared.resolve_provider_headers({"headers": {}}) == {}
    assert shared.resolve_provider_headers({"headers": None}) == {}
    assert shared.resolve_provider_headers("not a dict") == {}


def test_env_values_are_read_and_the_session_placeholder_is_left_alone(monkeypatch):
    monkeypatch.setenv("HEADER_TOKEN", "tok-123")
    resolved = shared.resolve_provider_headers(
        {
            "headers": {
                "Authorization": "$HEADER_TOKEN",
                "User-Agent": "zcode/1.0",
                "x-opencode-session": "{session}",
            }
        }
    )
    assert resolved == {
        "Authorization": "tok-123",
        "User-Agent": "zcode/1.0",
        # Left for the request to substitute: one client serves every
        # conversation, so this cannot be decided here.
        "x-opencode-session": "{session}",
    }


def test_a_missing_env_var_is_named_rather_than_sent_empty(monkeypatch):
    monkeypatch.delenv("HEADER_TOKEN_MISSING", raising=False)
    with pytest.raises(RuntimeError, match="HEADER_TOKEN_MISSING"):
        shared.resolve_provider_headers(
            {"headers": {"Authorization": "$HEADER_TOKEN_MISSING"}},
            provider_name="opencode-go",
        )


def test_the_process_has_one_stable_id_to_fall_back_to():
    assert shared.instance_id() == shared.instance_id()
    assert shared.current_session_id() == shared.instance_id()


def test_the_current_session_is_read_from_the_context(monkeypatch):
    token = shared._active_session_id.set("web:abc")
    try:
        assert shared.current_session_id() == "web:abc"
    finally:
        shared._active_session_id.reset(token)
    assert shared.current_session_id() == shared.instance_id()


# ── The request itself ────────────────────────────────────────────────────


class _RecordingCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        raise RuntimeError("stop here; the kwargs are the assertion")


class _RecordingClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": _RecordingCompletions()})()


def test_a_provider_without_headers_sends_no_extra_headers():
    """The regression pin: an unconfigured provider's request is unchanged.

    `extra_headers` must not appear at all -- not as an empty dict, not as
    None -- because "the config never mentioned headers" has to mean the bytes
    this transport sent before headers existed.
    """
    client = _RecordingClient()
    transport = OpenAITransport(client, None, True)

    with pytest.raises(RuntimeError):
        asyncio.run(
            transport.create(model="m", max_tokens=8, system="s", messages=[], tools=[])
        )
    assert "extra_headers" not in client.chat.completions.calls[0]


def test_one_client_serves_every_conversation_with_its_own_id():
    """The requirement itself: same client, two sessions, two header values.

    This is why the value is not on the client.  A gateway that asks for one
    stable id per conversation must not be told that every conversation is the
    same one -- and it must not be given a client per conversation either,
    which is what putting the value on the client would cost.
    """
    client = _RecordingClient()
    transport = OpenAITransport(
        client,
        None,
        True,
        headers={"User-Agent": "zcode/1.0", "x-opencode-session": "{session}"},
    )

    for session in ("web:one", "web:two"):
        token = shared._active_session_id.set(session)
        try:
            with pytest.raises(RuntimeError):
                asyncio.run(
                    transport.create(
                        model="m", max_tokens=8, system="s", messages=[], tools=[]
                    )
                )
        finally:
            shared._active_session_id.reset(token)

    calls = client.chat.completions.calls
    assert len(calls) == 2
    assert calls[0]["extra_headers"] == {
        "User-Agent": "zcode/1.0",
        "x-opencode-session": "web:one",
    }
    assert calls[1]["extra_headers"]["x-opencode-session"] == "web:two"
    # Everything else about the two requests is identical: the header is a
    # property of the request, not a reason to rebuild anything.
    assert calls[0]["model"] == calls[1]["model"]


def test_streaming_and_simple_chat_carry_the_headers_too():
    """Every way this transport can reach a provider, not just `create`."""
    seen: list[dict[str, Any]] = []

    async def fake_create(**kwargs: Any) -> Any:
        seen.append(kwargs)
        raise RuntimeError("stop here; the kwargs are the assertion")

    client = type(
        "C",
        (),
        {
            "chat": type(
                "Chat",
                (),
                {"completions": type("Comp", (), {"create": staticmethod(fake_create)})()},
            )()
        },
    )()
    transport = OpenAITransport(client, None, True, headers={"X-K": "v"})

    token = shared._active_session_id.set("s")
    try:
        # `simple_chat` swallows provider errors by design and returns None, so
        # it is called without expecting a raise.
        asyncio.run(
            transport.simple_chat(model="m", max_tokens=1, system="s", prompt="p")
        )
        with pytest.raises(RuntimeError):
            asyncio.run(
                transport.stream(
                    model="m", max_tokens=1, system="s", messages=[], tools=[],
                    callback=lambda _chunk: None,
                )
            )
    finally:
        shared._active_session_id.reset(token)

    assert len(seen) == 2, "both paths must reach the client"
    for call in seen:
        assert call["extra_headers"] == {"X-K": "v"}


def test_an_anthropic_format_provider_carries_them_as_well():
    """The need belongs to the gateway, not to the wire format."""

    class _Messages:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def create(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            raise RuntimeError("stop")

    class _Client:
        def __init__(self) -> None:
            self.messages = _Messages()

    client = _Client()
    transport = AnthropicTransport(client, None, headers={"X-K": "v"})
    with pytest.raises(RuntimeError):
        asyncio.run(
            transport.create(model="m", max_tokens=8, system="s", messages=[], tools=[])
        )
    assert client.messages.calls[0]["extra_headers"] == {"X-K": "v"}


# ── Wiring: routed providers and the wire itself ──────────────────────────


def test_a_routed_provider_gets_its_own_headers_not_the_active_providers():
    """Sub-agent and model-override calls route to another group's transport."""
    made: list[tuple[str, dict]] = []

    def factory(provider_cfg: dict, api_format: str) -> Any:
        return object()

    cfg = {
        "active_provider": "anthropic",
        "providers": {
            "anthropic": {"api_format": "anthropic", "models": ["m1"]},
            "opencode-go": {
                "api_format": "openai",
                "api_key": "k",
                "models": ["kimi-k2.7-code"],
                "headers": {"x-opencode-session": "{session}"},
            },
        },
    }
    routing = build_routing_transport(cfg, "anthropic", object(), factory, client_cache={})
    routed = routing._for("kimi-k2.7-code")
    assert routed is not routing.default
    assert routed.headers == {"x-opencode-session": "{session}"}


def test_the_headers_reach_the_wire():
    """End to end over a real socket: the gateway sees what the config said."""
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server's name
            length = int(self.headers.get("content-length") or 0)
            self.rfile.read(length)
            seen.append({k.lower(): v for k, v in self.headers.items()})
            body = _chat_completion_body("kimi-k2.7-code")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep the test output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import openai

        async def scenario():
            client = openai.AsyncOpenAI(
                api_key="k",
                base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            )
            transport = OpenAITransport(
                client,
                None,
                True,
                headers={
                    "User-Agent": "zcode/1.0",
                    "x-opencode-session": "{session}",
                },
            )
            token = shared._active_session_id.set("web:wire")
            try:
                await transport.create(
                    model="kimi-k2.7-code",
                    max_tokens=8,
                    system="s",
                    messages=[],
                    tools=[],
                )
            finally:
                shared._active_session_id.reset(token)

        asyncio.run(scenario())

        assert seen, "the request never arrived"
        assert seen[0]["x-opencode-session"] == "web:wire"
        # The configured value replaces the SDK's own User-Agent.
        assert seen[0]["user-agent"] == "zcode/1.0"
        assert seen[0]["authorization"] == "Bearer k"
    finally:
        server.shutdown()
        server.server_close()


def test_a_provider_without_headers_sends_the_sdk_default_user_agent():
    """The control for the test above: nothing is added when nothing is asked."""
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length") or 0)
            self.rfile.read(length)
            seen.append({k.lower(): v for k, v in self.headers.items()})
            body = _chat_completion_body("m")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import openai

        async def scenario():
            client = openai.AsyncOpenAI(
                api_key="k",
                base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            )
            transport = OpenAITransport(client, None, True)
            await transport.create(
                model="m", max_tokens=8, system="s", messages=[], tools=[]
            )

        asyncio.run(scenario())

        assert seen
        assert "x-opencode-session" not in seen[0]
        assert "openai" in seen[0]["user-agent"].lower()
    finally:
        server.shutdown()
        server.server_close()


def _chat_completion_body(model: str) -> bytes:
    return json.dumps(
        {
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    ).encode()


# ── Config validation ─────────────────────────────────────────────────────


def _warnings_for(headers: Any) -> list[str]:
    from agent.config import _validate_config

    cfg = {
        "providers": {
            "p": {
                "api_format": "openai",
                "api_key": "k",
                "default_model": "m",
                "headers": headers,
            }
        }
    }
    return [w for w in _validate_config(cfg) if "headers" in w]


def test_a_valid_headers_block_raises_no_warning():
    assert (
        _warnings_for(
            {"User-Agent": "zcode/1.0", "x-opencode-session": "{session}"}
        )
        == []
    )


def test_header_warnings_name_the_problem():
    assert any("must be a dict" in w for w in _warnings_for(["nope"]))
    assert any("not a valid header name" in w for w in _warnings_for({"Bad Name": "x"}))
    assert any("must be a string" in w for w in _warnings_for({"X-T": ["a"]}))
    # A placeholder that will never be substituted must not look like it works.
    unknown = _warnings_for({"X-T": "{conversation}"})
    assert any("{conversation}" in w and "{session}" in w for w in unknown)
