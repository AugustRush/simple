"""A provider call's cost has to be recorded for the cache to be measurable.

The interactive path streams, and an OpenAI-compatible stream reports nothing
about what it cost unless the request asks.  So every streamed turn was missing
from `usage_events` while every non-streaming call — sub-agents, consolidation —
was recorded, and the only measurement of prompt-cache behaviour described the
traffic that does *not* stream.

These tests pin both halves of that: the request asks for usage, and the
usage-only final chunk — which carries an empty `choices` list and is exactly
what the streaming loop skips — is read rather than discarded.  The rest pin the
shape fields a recorded row carries, because those are what turn "the hit rate
is 55%" into "this call's head changed" or "this call's body was rewritten".
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

import agent as agent_module
from agent.core.payload_shape import (
    body_front_fingerprint,
    describe_payload,
    head_fingerprint,
)
from agent.core.transport import OpenAITransport, build_transport, provider_stream_usage
from agent.shared import _OAIChoice, _OAIMsg, _OAIResponse
from agent.usage import extract_provider_usage

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── The request asks for usage ─────────────────────────────────────────────


def test_a_streaming_request_asks_the_provider_for_usage():
    """Without this a streamed call reports nothing, so it cannot be recorded."""
    kwargs = build_transport("openai", object())._create_kwargs(
        model="m", max_tokens=8, system="s", messages=[], tools=[], stream=True,
    )
    assert kwargs["stream_options"] == {"include_usage": True}


def test_a_non_streaming_request_does_not_ask_for_usage():
    """The parameter is about streaming, and the response carries usage anyway."""
    kwargs = build_transport("openai", object())._create_kwargs(
        model="m", max_tokens=8, system="s", messages=[], tools=[],
    )
    assert "stream_options" not in kwargs


def test_a_gateway_that_rejects_the_parameter_can_be_configured_around():
    """The escape hatch: one line of provider config restores the old request."""
    kwargs = build_transport("openai", object(), None, False)._create_kwargs(
        model="m", max_tokens=8, system="s", messages=[], tools=[], stream=True,
    )
    assert kwargs["stream"] is True
    assert "stream_options" not in kwargs


@pytest.mark.parametrize(
    "configured,expected",
    [
        (None, True),
        (True, True),
        (False, False),
        ("false", False),
        ("off", False),
        ("0", False),
        (0, False),
        ("true", True),
        ("yes", True),
        # A typo must leave the measurement working rather than silently
        # switching it off, which is the failure that is hard to notice.
        ("flase", True),
        ({"nested": False}, True),
    ],
)
def test_stream_usage_defaults_on_and_only_an_explicit_no_turns_it_off(
    configured, expected
):
    provider_cfg = {} if configured is None else {"stream_usage": configured}
    assert provider_stream_usage(provider_cfg) is expected


def test_an_unconfigured_provider_keeps_the_measurement_on():
    """The default is on because off is the state that hid the bug."""
    assert provider_stream_usage({}) is True
    assert provider_stream_usage({"api_key": "x", "base_url": "y"}) is True


# ── The usage-only chunk is read ───────────────────────────────────────────


class _Delta:
    def __init__(self, content=None):
        self.content = content
        self.tool_calls = None
        self.model_extra = {}


class _Chunk:
    """One streamed chunk.  `choices` empty is how a usage-only chunk arrives."""

    def __init__(self, delta=None, usage=None):
        self.choices = (
            [] if delta is None
            else [types.SimpleNamespace(delta=delta, finish_reason=None)]
        )
        self.usage = usage


class _Usage:
    """DeepSeek's field names, which is the shape that has to survive."""

    def __init__(self, prompt=100, completion=20, hit=64):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.prompt_cache_hit_tokens = hit


class _StreamingClient:
    def __init__(self, chunks):
        self._chunks = chunks
        self.calls: list[dict] = []
        self.chat = types.SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)

        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()


def _run_stream(chunks):
    client = _StreamingClient(chunks)
    transport = OpenAITransport(client)
    answer: list[str] = []
    response, _ = asyncio.run(
        transport.stream(
            model="m", max_tokens=8, system="", messages=[], tools=[],
            callback=answer.append,
        )
    )
    return response, "".join(answer), client


def test_the_usage_chunk_is_read_even_though_it_has_no_choices():
    """The last chunk says what the call cost and is the one the loop skipped."""
    response, answer, _ = _run_stream(
        [
            _Chunk(_Delta("432")),
            _Chunk(usage=_Usage(prompt=1000, completion=20, hit=960)),
        ]
    )

    assert answer == "432"
    usage = extract_provider_usage(response)
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 20
    assert usage.cached_input_tokens == 960


def test_a_stream_that_reports_nothing_is_not_recorded_as_zero():
    """The two states differ, and only one of them may be written down.

    A row of zeros reads as a request that cost nothing and drags down every
    aggregate it enters; an absent row is honest about not knowing.
    """
    response, _, _ = _run_stream([_Chunk(_Delta("hi"))])

    assert response.usage is None
    assert extract_provider_usage(response).total_tokens == 0


def test_a_zeroed_usage_object_is_read_as_reported_zero():
    """`is not None` rather than truthiness: a gateway may report real zeros."""
    response, _, _ = _run_stream(
        [_Chunk(_Delta("hi")), _Chunk(usage=_Usage(prompt=0, completion=0, hit=0))]
    )

    assert response.usage is not None


def test_the_answer_still_streams_while_usage_is_collected():
    """The usage chunk must not disturb the text path."""
    _, answer, _ = _run_stream(
        [
            _Chunk(_Delta("a")),
            _Chunk(_Delta("b")),
            _Chunk(usage=_Usage()),
        ]
    )
    assert answer == "ab"


# ── The recorded payload shape ─────────────────────────────────────────────


def test_head_fingerprint_covers_the_system_prompt_and_the_tools_together():
    """Both sit ahead of the messages, so either changing costs the prefix.

    A fingerprint covering only one would report "unchanged" for a request that
    had just lost its whole prefix to the other.
    """
    tools = [{"name": "shell", "description": "run"}]

    baseline = head_fingerprint("system", tools)
    assert head_fingerprint("system", tools) == baseline
    assert head_fingerprint("system changed", tools) != baseline
    assert head_fingerprint("system", tools + [{"name": "read_file"}]) != baseline
    assert head_fingerprint("system", [{"name": "shell", "description": "run!"}]) != baseline


def test_head_fingerprint_ignores_key_order_but_not_membership():
    """Re-serialising the same tokens in another key order cannot cost a hit."""
    assert head_fingerprint("s", [{"name": "a", "description": "b"}]) == head_fingerprint(
        "s", [{"description": "b", "name": "a"}]
    )


def test_body_front_fingerprint_notices_a_rewritten_front_and_ignores_the_tail():
    """A drop or an insert at the front is what breaks the prefix."""
    first = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]

    assert body_front_fingerprint(first) == body_front_fingerprint(
        [{"role": "user", "content": "one"}, {"role": "assistant", "content": "changed"}]
    )
    assert body_front_fingerprint(first) != body_front_fingerprint(
        [{"role": "user", "content": "[context-eviction] 3 dropped"}, *first]
    )
    assert body_front_fingerprint([]) == ""


def test_describe_payload_reports_the_shape_and_the_rewrite_flag():
    shape = describe_payload(
        "system",
        [{"name": "shell"}],
        [{"role": "user", "content": "hi"}],
        compacted=True,
    )

    assert shape["head_fingerprint"] == head_fingerprint("system", [{"name": "shell"}])
    assert shape["body_front_fingerprint"] == body_front_fingerprint(
        [{"role": "user", "content": "hi"}]
    )
    assert shape["body_messages"] == 1
    assert shape["body_compacted"] is True


def test_the_fingerprint_survives_a_process_restart():
    """It is compared against values written by an *earlier* process.

    `hash()` would pass every other test in this file and still be useless
    here: it is salted per process, so every restart would report that the head
    had changed.  Two subprocesses with different hash seeds is the check that
    distinguishes them.
    """
    code = (
        "from agent.core.payload_shape import head_fingerprint;"
        "print(head_fingerprint('system', [{'name': 'shell'}]))"
    )
    outputs = []
    for seed in ("1", "2"):
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        outputs.append(result.stdout.strip())

    assert outputs[0] == outputs[1]
    assert len(outputs[0]) == 16


# ── The row a foreground call writes ───────────────────────────────────────


def _ctx_manager(tmp_path):
    from agent import (
        LTMStore,
        ConsolidationEngine,
        LocalRetriever,
        ContextManager,
        StagingBuffer,
    )

    store = LTMStore(context_dir=tmp_path / "context")
    return ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        staging=StagingBuffer(
            context_dir=tmp_path / "context", session_id="test-session"
        ),
    )


def _agent_with_store(tmp_path, **kwargs):
    """A real agent over a real store, so the row is written for real.

    The transport is a real `OpenAITransport` so `observed_usage` is the
    production reader, not a stub that would agree with a wrong field name.
    """
    transport = OpenAITransport(object())
    agent = agent_module.BaseAgent(
        object(),
        agent_module.ToolRegistry(),
        model="deepseek-flash",
        api_format="openai",
        transport=transport,
        **kwargs,
    )
    agent.context_manager = _ctx_manager(tmp_path)
    return agent


def _response(usage):
    return _OAIResponse([_OAIChoice("stop", _OAIMsg("done", None, None))], usage=usage)


def test_a_foreground_call_records_its_cost_and_the_payload_shape(tmp_path):
    """The regression: streamed turns used to record nothing at all."""
    agent = _agent_with_store(tmp_path)
    ctx = agent_module.AgentContext(
        system_prompt="system prompt",
        messages=[{"role": "user", "content": "hello"}],
        metadata={"session_id": "s-1", "turn_id": "t-1"},
    )

    agent._prepare_provider_context(ctx, [{"name": "shell"}])
    agent._observe_provider_usage(ctx, _response(_Usage(prompt=1000, completion=20, hit=960)))

    events = agent.context_manager.store.usage_summary("s-1")
    assert events["by_phase"]["foreground"]["calls"] == 1
    assert events["by_phase"]["foreground"]["input_tokens"] == 1000
    assert events["by_phase"]["foreground"]["cached_input_tokens"] == 960


def test_the_recorded_row_carries_what_explains_a_miss(tmp_path):
    """A hit rate alone cannot say which part of the request changed."""
    agent = _agent_with_store(tmp_path)
    ctx = agent_module.AgentContext(
        system_prompt="system prompt",
        messages=[{"role": "user", "content": "hello"}],
        metadata={"session_id": "s-2", "turn_id": "t-2"},
    )

    agent._prepare_provider_context(ctx, [{"name": "shell"}])
    agent._observe_provider_usage(ctx, _response(_Usage()))

    row = _latest_metadata(agent, "s-2")
    assert row["head_fingerprint"] == head_fingerprint("system prompt", [{"name": "shell"}])
    assert row["body_front_fingerprint"] == body_front_fingerprint(
        [{"role": "user", "content": "hello"}]
    )
    assert row["body_messages"] == 1
    assert row["body_compacted"] is False
    assert row["step"] == 1
    assert row["input_budget"] > 0


def test_a_call_with_no_reported_usage_writes_no_row_and_says_so_once(tmp_path, capsys):
    """Silence was the bug: the row vanished and nothing said why."""
    agent_module.BaseAgent._missing_usage_warned = False
    agent = _agent_with_store(tmp_path)
    ctx = agent_module.AgentContext(
        system_prompt="system prompt",
        messages=[{"role": "user", "content": "hello"}],
        metadata={"session_id": "s-3", "turn_id": "t-3"},
    )

    agent._prepare_provider_context(ctx, [])
    agent._observe_provider_usage(ctx, _response(None))
    agent._observe_provider_usage(ctx, _response(None))

    assert agent.context_manager.store.usage_summary("s-3")["calls"] == 0
    assert capsys.readouterr().out.count("stream_options") == 1


def test_a_compaction_that_rewrites_the_body_is_recorded_as_such(tmp_path):
    """The flag is the one fact the fingerprints cannot recover themselves.

    A body can be rewritten to something whose front happens to fingerprint the
    same, and that call still paid full price for its history.
    """
    # A window small enough that the older turns cannot fit beside the newest.
    # The reserve is named explicitly because this window is far smaller than a
    # real one, so the default reserve would leave it no input budget at all.
    agent = _agent_with_store(
        tmp_path, max_tokens=512, context_window=3000
    )
    ctx = agent_module.AgentContext(
        system_prompt="system prompt",
        messages=[
            {"role": "user", "content": "old " * 2000},
            {"role": "assistant", "content": "old " * 2000},
            {"role": "user", "content": "old " * 2000},
            {"role": "assistant", "content": "old " * 2000},
            {"role": "user", "content": "old " * 2000},
            {"role": "assistant", "content": "old " * 2000},
            {"role": "user", "content": "the newest request"},
        ],
        metadata={"session_id": "s-4", "turn_id": "t-4"},
    )

    agent._prepare_provider_context(ctx, [])
    agent._observe_provider_usage(ctx, _response(_Usage()))

    row = _latest_metadata(agent, "s-4")
    assert row["body_compacted"] is True
    assert row["body_messages"] < 7


def _latest_metadata(agent, session_id):
    """The metadata of the newest recorded call, as the row stored it."""
    import json

    store = agent.context_manager.store
    with store._connect() as conn:
        row = conn.execute(
            "SELECT metadata_json FROM usage_events WHERE session_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    assert row is not None, "no usage row was recorded"
    return json.loads(row["metadata_json"])
