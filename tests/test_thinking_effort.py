"""Thinking effort: the setting, the wire parameter, and the display channel.

Three things have to agree, and each is tested where it is decided:
  * the vocabulary and its "no opinion" state (shared, config),
  * the translation into each provider's own parameter (the transports),
  * the channel that carries thinking to a sink without joining the answer.
"""

import asyncio
import types

import pytest

from agent import shared
from agent.config import _validate_config
from agent.core import output as output_module
from agent.core.output import CliOutputSink, OutputSink
from agent.core.transport import (
    AnthropicTransport,
    OpenAITransport,
    RoutingTransport,
    build_routing_transport,
    build_transport,
    model_thinking_overrides,
    provider_thinking_effort,
)


# ── The vocabulary ─────────────────────────────────────────────────────────


def test_effort_words_are_the_documented_four():
    assert shared.THINKING_EFFORTS == ("off", "low", "medium", "high")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("off", "off"),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        (" High ", "high"),
        ("HIGH", "high"),
        # No opinion: absent, a typo, or something that is not a word at all.
        (None, None),
        ("", None),
        ("highest", None),
        ("minimal", None),  # a real gateway level, but not one we offer
        (3, None),
        ({}, None),
    ],
)
def test_normalize_thinking_effort(raw, expected):
    assert shared.normalize_thinking_effort(raw) == expected


@pytest.mark.parametrize(
    "provider_cfg,expected",
    [
        ({}, None),
        ({"thinking": None}, None),
        ({"thinking": {}}, None),
        ({"thinking": {"effort": "high"}}, "high"),
        ({"thinking": {"effort": " HIGH "}}, "high"),
        ({"thinking": {"effort": "highest"}}, None),
        ({"thinking": "low"}, "low"),
        ({"thinking": 7}, None),
    ],
)
def test_provider_thinking_effort_reads_the_provider_block(provider_cfg, expected):
    assert provider_thinking_effort(provider_cfg) == expected


def test_config_validation_reports_a_bad_effort():
    def warnings(thinking):
        cfg = {
            "active_provider": "p",
            "providers": {
                "p": {
                    "api_format": "openai",
                    "api_key": "k",
                    "default_model": "m",
                    "thinking": thinking,
                }
            },
        }
        return [w for w in _validate_config(cfg) if "thinking" in w]

    assert warnings({"effort": "high"}) == []
    assert warnings({"effort": "off"}) == []
    assert warnings({}) == []          # no level chosen is a legitimate state
    assert warnings({"effort": "highest"}) == [
        "providers.p.thinking.effort: must be one of off, low, medium, high, "
        "got 'highest'"
    ]
    assert warnings("loud") == [
        "providers.p.thinking.effort: must be one of off, low, medium, high, "
        "got 'loud'"
    ]
    assert len(warnings(7)) == 1


# ── The OpenAI wire parameter ──────────────────────────────────────────────


def test_unconfigured_openai_provider_sends_no_reasoning_parameter():
    """The state every shipped config is in must not change the request."""
    kwargs = build_transport("openai", object())._create_kwargs(
        model="m", max_tokens=8, system="s", messages=[], tools=[],
    )
    assert "reasoning_effort" not in kwargs


@pytest.mark.parametrize(
    "effort,expected",
    [("off", "none"), ("low", "low"), ("medium", "medium"), ("high", "high")],
)
def test_configured_openai_effort_reaches_the_wire(effort, expected):
    kwargs = build_transport("openai", object(), effort)._create_kwargs(
        model="m", max_tokens=8, system="s", messages=[], tools=[],
    )
    assert kwargs["reasoning_effort"] == expected


def test_off_says_the_word_that_silences_rather_than_saying_nothing():
    """`off` and "no key" are different requests, and both are reachable."""
    silent = build_transport("openai", object(), "off")._reasoning_effort_kwarg()
    unset = build_transport("openai", object())._reasoning_effort_kwarg()
    assert silent == {"reasoning_effort": "none"}
    assert unset == {}


# ── The Anthropic wire parameter ───────────────────────────────────────────


@pytest.mark.parametrize(
    "effort,budget",
    [("low", 1024), ("medium", 4096), ("high", 7168)],
)
def test_anthropic_thinking_budget_per_effort(effort, budget):
    transport = build_transport("anthropic", object(), effort)
    assert transport._thinking_kwarg(8192) == {
        "thinking": {"type": "enabled", "budget_tokens": budget}
    }


def test_anthropic_budget_is_clamped_under_max_tokens():
    """The API rejects a budget that is not below max_tokens."""
    transport = build_transport("anthropic", object(), "high")
    for max_tokens in (2048, 4096, 16384):
        budget = transport._thinking_kwarg(max_tokens)["thinking"]["budget_tokens"]
        assert 1024 <= budget < max_tokens


def test_anthropic_without_room_for_a_budget_sends_no_thinking():
    """A budget must be >= 1024 and strictly < max_tokens.

    A call capped at 1024 tokens has no legal budget at all, and sending an
    impossible one is a guaranteed 400 -- so the effort is dropped for that
    call rather than refused.
    """
    transport = build_transport("anthropic", object(), "high")
    assert transport._thinking_kwarg(1024) == {}
    assert transport._thinking_kwarg(512) == {}
    # 1025 has exactly one legal budget: 1024.
    assert transport._thinking_kwarg(1025) == {
        "thinking": {"type": "enabled", "budget_tokens": 1024}
    }


@pytest.mark.parametrize("effort", [None, "off"])
def test_anthropic_without_effort_sends_no_thinking(effort):
    assert build_transport("anthropic", object(), effort)._thinking_kwarg(8192) == {}


# ── Streaming reasoning ────────────────────────────────────────────────────


class _Delta:
    def __init__(self, content=None, reasoning=None):
        self.content = content
        self.tool_calls = None
        self.model_extra = {} if reasoning is None else {"reasoning_content": reasoning}


class _Chunk:
    def __init__(self, delta):
        self.choices = [types.SimpleNamespace(delta=delta, finish_reason=None)]


class _StreamingClient:
    """Yields the deltas it was built with, exactly as given."""

    def __init__(self, deltas):
        self._deltas = deltas
        self.calls: list[dict] = []
        self.chat = types.SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)

        async def _gen():
            for delta in self._deltas:
                yield _Chunk(delta)

        return _gen()


def _run_stream(deltas, reasoning_callback):
    client = _StreamingClient(deltas)
    transport = OpenAITransport(client)
    answer: list[str] = []
    asyncio.run(transport.stream(
        model="m", max_tokens=8, system="", messages=[], tools=[],
        callback=answer.append, reasoning_callback=reasoning_callback,
    ))
    return "".join(answer), client


def test_reasoning_arrives_as_fragments_and_never_joins_the_answer():
    think: list[str] = []
    answer, _ = _run_stream(
        [
            _Delta(reasoning="先算"),
            _Delta(reasoning="一下"),
            _Delta(content="432"),
        ],
        think.append,
    )
    assert think == ["先算", "一下"]
    assert answer == "432"


def test_fragments_that_extend_or_repeat_their_predecessor_are_kept():
    """A token stream repeats and extends prefixes constantly."""
    think: list[str] = []
    _run_stream(
        [
            _Delta(reasoning="The"),
            _Delta(reasoning="The"),
            _Delta(reasoning="The answer"),
        ],
        think.append,
    )
    assert think == ["The", "The", "The answer"]


def test_a_gateway_that_sends_the_whole_thought_each_time_is_not_repeated():
    """Some gateways re-send everything so far instead of the next fragment."""
    think: list[str] = []
    _run_stream(
        [
            _Delta(reasoning="先算"),
            _Delta(reasoning="先算一下"),
            _Delta(reasoning="先算一下再答"),
        ],
        think.append,
    )
    assert "".join(think) == "先算一下再答"


def test_reasoning_is_skipped_entirely_without_a_callback():
    answer, client = _run_stream(
        [_Delta(reasoning="想"), _Delta(content="432")], None
    )
    assert answer == "432"
    assert all("reasoning_effort" not in call for call in client.calls)


# ── Anthropic streaming ────────────────────────────────────────────────────


class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for key, value in kw.items():
            setattr(self, key, value)


class _AnthropicStream:
    def __init__(self, texts, content):
        self._texts = texts
        self._content = content

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    @property
    def text_stream(self):
        async def _gen():
            for text in self._texts:
                yield text

        return _gen()

    async def get_final_message(self):
        return types.SimpleNamespace(content=self._content, stop_reason="end_turn")


class _AnthropicClient:
    def __init__(self, texts, content):
        self._texts = texts
        self._content = content
        self.calls: list[dict] = []
        self.messages = types.SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return _AnthropicStream(self._texts, self._content)


def test_anthropic_stream_sends_thinking_and_reports_the_blocks():
    client = _AnthropicClient(
        ["432"],
        [
            _Block("thinking", thinking="先算"),
            _Block("thinking", thinking="再答"),
            _Block("text", text="432"),
        ],
    )
    think: list[str] = []
    answer: list[str] = []
    asyncio.run(build_transport("anthropic", client, "medium").stream(
        model="m", max_tokens=8192, system="", messages=[], tools=[],
        callback=answer.append, reasoning_callback=think.append,
    ))
    assert client.calls[0]["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    assert answer == ["432"]
    assert "".join(think) == "先算再答"


# ── Routing ────────────────────────────────────────────────────────────────


def test_routing_gives_each_provider_its_own_effort():
    cfg = {
        "active_provider": "a",
        "providers": {
            "a": {
                "api_format": "openai", "api_key": "k", "default_model": "ma",
                "thinking": {"effort": "high"},
            },
            "b": {
                "api_format": "openai", "api_key": "k", "default_model": "mb",
            },
        },
    }
    client = object()
    routing = build_routing_transport(
        cfg, "openai", client, client_factory=lambda *_: client, client_cache={}
    )
    assert routing._for("ma").thinking_effort == "high"
    assert routing._for("mb").thinking_effort is None
    assert routing._for("unknown-model").thinking_effort == "high"


# ── Per-model overrides ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "provider_cfg,expected",
    [
        ({}, {}),
        ({"thinking": None}, {}),
        ({"thinking": {}}, {}),
        ({"thinking": {"models": None}}, {}),
        ({"thinking": {"models": []}}, {}),
        ({"thinking": {"models": {"m1": "high"}}}, {"m1": "high"}),
        ({"thinking": {"models": {"m1": " HIGH "}}}, {"m1": "high"}),
        ({"thinking": {"models": {"m1": "highest"}}}, {}),   # a typo is absent
        ({"thinking": {"models": {"m1": None}}}, {}),        # so is "no opinion"
        ({"thinking": {"models": {"m1": "high", "m2": "off"}}}, {"m1": "high", "m2": "off"}),
    ],
)
def test_model_thinking_overrides_reads_the_models_block(provider_cfg, expected):
    assert model_thinking_overrides(provider_cfg) == expected


def test_a_model_override_wins_over_its_provider_for_that_model_only():
    """A mixed group is the case the override exists for.

    The provider's reasoning model asks for effort; the plain model beside it
    would answer the same parameter with a 400.  The routing table already
    knows which model is which, so the override is keyed by it.
    """
    cfg = {
        "active_provider": "a",
        "providers": {
            "a": {
                "api_format": "openai", "api_key": "k", "default_model": "plain",
                "models": ["plain", "reasoner"],
                "thinking": {
                    "effort": "high",
                    "models": {"reasoner": "low", "plain": "off"},
                },
            },
        },
    }
    client = object()
    routing = build_routing_transport(
        cfg, "openai", client, client_factory=lambda *_: client, client_cache={}
    )
    assert routing.thinking_overrides == {"reasoner": "low", "plain": "off"}
    # The owning transport keeps its provider effort for every model it has.
    assert routing._for("reasoner").thinking_effort == "high"
    # And the calls carry the override instead, without mutating the shared
    # transport -- its other model keeps the provider's level.
    assert routing._effort_override("reasoner") == "low"
    assert routing._effort_override("plain") == "off"
    assert routing._effort_override("unknown-model") is None
    assert routing._effort_override(None) is None


def test_a_model_override_reaches_the_openai_wire():
    calls: list[dict] = []

    async def _create(**kwargs):
        calls.append(kwargs)

    class _Client:
        chat = types.SimpleNamespace(completions=types.SimpleNamespace(
            create=_create
        ))

    cfg = {
        "active_provider": "a",
        "providers": {
            "a": {
                "api_format": "openai", "api_key": "k", "default_model": "plain",
                "models": ["plain", "reasoner"],
                "thinking": {"effort": "high", "models": {"plain": "off"}},
            },
        },
    }
    routing = build_routing_transport(
        cfg, "openai", _Client(), client_factory=lambda *_: _Client(), client_cache={}
    )
    asyncio.run(routing.create(
        model="plain", max_tokens=8, system="", messages=[], tools=[],
    ))
    asyncio.run(routing.create(
        model="reasoner", max_tokens=8, system="", messages=[], tools=[],
    ))
    # The overridden model says "none"; its sibling still carries the
    # provider's word.
    assert calls[0]["reasoning_effort"] == "none"
    assert calls[1]["reasoning_effort"] == "high"


def test_config_validation_reports_a_bad_per_model_effort():
    cfg = {
        "active_provider": "p",
        "providers": {
            "p": {
                "api_format": "openai", "api_key": "k", "default_model": "m",
                "thinking": {"models": {"m": "high", "other": "louder"}},
            },
        },
    }
    warnings = [w for w in _validate_config(cfg) if "thinking" in w]
    assert warnings == [
        "providers.p.thinking.models.other: must be one of off, low, medium, "
        "high, got 'louder'"
    ]


def test_routing_forwards_the_reasoning_callback():
    seen: dict = {}

    class _Recorder(OpenAITransport):
        def __init__(self):
            super().__init__(object())

        async def stream(self, *, model, max_tokens, system, messages, tools,
                         callback, reasoning_callback=None, thinking_effort=None):
            seen["reasoning_callback"] = reasoning_callback
            return None, ""

    routing = RoutingTransport(_Recorder(), {})
    marker = lambda chunk: None  # noqa: E731
    asyncio.run(routing.stream(
        model="m", max_tokens=8, system="", messages=[], tools=[],
        callback=lambda c: None, reasoning_callback=marker,
    ))
    assert seen["reasoning_callback"] is marker


# ── The sink channel ───────────────────────────────────────────────────────


class _RecordingSink(OutputSink):
    def __init__(self):
        self.text: list[str] = []
        self.think: list[str] = []

    def on_stream_chunk(self, chunk: str) -> None:
        self.text.append(chunk)

    def on_reasoning_chunk(self, chunk: str) -> None:
        self.think.append(chunk)


def test_sync_reasoning_cb_reaches_the_sink_override():
    sink = _RecordingSink()
    sink.sync_stream_cb("答")
    sink.sync_reasoning_cb("想")
    assert (sink.text, sink.think) == (["答"], ["想"])


def test_a_sink_without_the_hook_ignores_thinking():
    sink = OutputSink()
    sink.sync_reasoning_cb("想")  # must not raise
    sink.on_reasoning_chunk("想")


def test_stream_response_forwards_the_reasoning_callback_it_is_given():
    """``_stream_response`` is a pass-through; the caller resolves the sink."""
    from agent.core.agent import AgentContext, BaseAgent, ToolRegistry

    class _Transport:
        def __init__(self):
            self.reasoning_callback = "NEVER SET"

        async def stream(self, *, model, max_tokens, system, messages, tools,
                         callback, reasoning_callback=None, thinking_effort=None):
            self.reasoning_callback = reasoning_callback
            return {"choices": [{"finish_reason": "stop"}]}, "answer"

    agent = BaseAgent(object(), ToolRegistry(), model="m", api_format="openai")
    transport = _Transport()
    agent._transport = transport
    ctx = AgentContext(system_prompt="s", metadata={})

    asyncio.run(agent._stream_response(ctx, [], lambda c: None))
    assert transport.reasoning_callback is None

    sink = _RecordingSink()
    asyncio.run(agent._stream_response(ctx, [], lambda c: None, sink.sync_reasoning_cb))
    assert callable(transport.reasoning_callback)
    transport.reasoning_callback("想")
    assert sink.think == ["想"]


def test_send_message_routes_the_sinks_reasoning_channel(monkeypatch):
    """``send_message`` owns the sink lookup, because it is what wraps the
    channel in the emission tracker."""
    import agent as agent_module
    from agent.core.agent import AgentContext, BaseAgent, ToolRegistry

    agent = BaseAgent(object(), ToolRegistry(), model="m", api_format="openai")
    agent.llm_max_retries = 0
    captured: dict = {}

    async def fake_stream(ctx, tools, callback, reasoning_callback=None):
        captured["reasoning"] = reasoning_callback
        return (
            agent_module._OAIResponse(
                [agent_module._OAIChoice("stop", agent_module._OAIMsg("答", None))]
            ),
            "答",
        )

    monkeypatch.setattr(agent, "_stream_response", fake_stream)

    previous = output_module._active_sink.set(None)
    try:
        asyncio.run(agent.send_message(
            AgentContext(system_prompt="s", metadata={}),
            "hi",
            stream_callback=lambda c: None,
        ))
    finally:
        output_module._active_sink.reset(previous)
    assert captured["reasoning"] is None

    sink = _RecordingSink()
    previous = output_module._active_sink.set(sink)
    try:
        asyncio.run(agent.send_message(
            AgentContext(system_prompt="s", metadata={}),
            "hi",
            stream_callback=lambda c: None,
        ))
    finally:
        output_module._active_sink.reset(previous)
    assert callable(captured["reasoning"])
    captured["reasoning"]("想")
    assert sink.think == ["想"]


def test_streaming_retry_does_not_replay_reasoning_either(monkeypatch):
    """Reasoning arrives ahead of the first answer token, so it is exactly
    the emission a retry decision has to see.  A thinking-heavy turn that
    died before its first answer token used to replay its whole thinking —
    the tracker wrapped only the answer callback."""
    import agent as agent_module
    from agent.core.agent import AgentContext, BaseAgent, ToolRegistry

    agent = BaseAgent(object(), ToolRegistry(), model="m", api_format="openai")
    agent.llm_max_retries = 3
    agent.llm_retry_base_delay = 0
    attempts = 0

    async def thinking_then_disconnect(ctx, tools, callback, reasoning_callback=None):
        nonlocal attempts
        attempts += 1
        reasoning_callback("先想清楚")
        raise ConnectionError("connection reset by peer")

    monkeypatch.setattr(agent, "_stream_response", thinking_then_disconnect)

    sink = _RecordingSink()
    previous = output_module._active_sink.set(sink)
    try:
        result = asyncio.run(agent.send_message(
            AgentContext(system_prompt="s", metadata={}),
            "hi",
            stream_callback=lambda c: None,
        ))
    finally:
        output_module._active_sink.reset(previous)

    # A replay would print the thinking a second time; sinks cannot unsay it.
    assert attempts == 1
    assert sink.think == ["先想清楚"]
    assert result.error


def test_cli_reasoning_stays_out_of_the_streamed_answer():
    """``_streamed`` is how on_turn_complete knows the answer was printed."""

    class _Console:
        def __init__(self):
            self.lines: list[str] = []

        def print(self, value="", **kwargs):
            self.lines.append(str(value))

    console = _Console()
    sink = CliOutputSink(console)
    sink.on_reasoning_chunk("先想")
    assert sink._streamed == []
    assert "先想" in "".join(console.lines)

    sink.on_stream_chunk("答案")
    assert sink._streamed == ["答案"]
    # The reasoning run ended on its own line before the answer started.
    assert console.lines[-2] == ""


def test_cli_reasoning_ends_its_line_when_the_turn_stops_mid_thought():
    class _Console:
        def __init__(self):
            self.lines: list[str] = []

        def print(self, value="", **kwargs):
            self.lines.append(str(value))

    console = _Console()
    sink = CliOutputSink(console)
    sink.on_reasoning_chunk("想了一半")
    sink.on_error("boom")
    assert console.lines == [
        "[dim]… 思考[/dim] ",      # the run opens with its own label
        "想了一半",                # and streams inline
        "",                        # the run is ended on its own line...
        "[red]Error[/red] [dim]boom[/dim]",   # ...before the error is printed
    ]
