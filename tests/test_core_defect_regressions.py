"""Regression tests for the four defects found by the first-principles audit.

Each test here was written **before** its fix and was red on the tree that
produced it.  They are grouped in one file because they share a root cause
rather than a module: `ctx.messages` is written by `transport` and read by
`memory`, and nothing enforced that both agree on what a message is.

The audit is recorded in `docs/superpowers/plans/2026-09-23-core-defect-fixes.md`.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest


def _block_types(message: dict) -> list[str]:
    """Type of every content block, for either wire form.

    Deliberately total: a block that is not a dict is reported by its own type
    name, because *that* is the defect these tests exist to catch.  A helper
    that only understood dicts would report an empty list and the assertion
    would pass for the wrong reason.
    """
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [
        block.get("type") if isinstance(block, dict) else type(block).__name__
        for block in content
    ]


def _anthropic_response():
    from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

    return Message(
        id="msg_1",
        content=[
            TextBlock(text="let me look", type="text"),
            ToolUseBlock(
                id="toolu_1", input={"cmd": "ls"}, name="shell", type="tool_use"
            ),
        ],
        model="claude-x",
        role="assistant",
        stop_reason="tool_use",
        stop_sequence=None,
        type="message",
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def _anthropic_history():
    """A complete provider protocol unit, built the way `send_message` builds it."""
    from agent.core.transport import AnthropicTransport

    transport = AnthropicTransport(None)
    assistant = transport.build_assistant_message(_anthropic_response(), "let me look")
    return [
        {"role": "user", "content": "please list the files"},
        assistant,
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt"}
            ],
        },
    ]


def _compact(messages, budget=100_000, **kwargs):
    from agent.memory.context import ContextManager
    from agent.memory.consolidation import estimate_message_tokens

    return ContextManager.fit_to_budget(
        messages,
        input_token_budget=budget,
        estimate_tokens=estimate_message_tokens,
        **kwargs,
    )


# ── 1. The transport's own output must survive the memory layer ──────────────


def test_an_anthropic_tool_result_survives_compaction():
    """A `tool_use` and its `tool_result` are one unit and must stay together.

    `_repair_tool_history` drops a `tool_result` it cannot pair, and it pairs by
    reading `tool_use` ids off the assistant message.  The Anthropic transport
    stored the SDK's own block objects, so the ids were invisible, the assistant
    message looked tool-free, and its results were discarded as orphans -- on
    every turn boundary, writing the loss back into `ctx.messages`.  The next
    request then carried a `tool_use` with no `tool_result`, which the provider
    rejects outright.
    """
    compacted = _compact(_anthropic_history())

    uses = sum(types.count("tool_use") for types in map(_block_types, compacted))
    results = sum(types.count("tool_result") for types in map(_block_types, compacted))
    assert uses == 1, f"the tool_use must survive; blocks were {[ _block_types(m) for m in compacted ]}"
    assert results == 1, (
        "the tool_result must survive alongside its tool_use, not be dropped as "
        f"an orphan; blocks were {[ _block_types(m) for m in compacted ]}"
    )


def test_a_stored_anthropic_message_is_json_native():
    """Every message in `ctx.messages` is JSON-serializable.

    That is the contract the whole memory layer is written against, and it is
    what the previous test's helper would hide.  Stated separately so a fix that
    merely teaches `_repair_tool_history` to understand SDK objects still fails.
    """
    for message in _anthropic_history():
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            assert isinstance(block, dict), (
                f"{type(block).__name__} reached ctx.messages; a message is "
                "serialized to SQLite and restored by another process, so its "
                "content must be plain dicts"
            )


def test_the_checkpoint_round_trip_preserves_anthropic_blocks():
    """What is persisted must reload as what was stored.

    `store.save_provider_checkpoint` serializes with `default=str`, which is a
    safety net, not a licence to store objects: an SDK block becomes the string
    `"ToolUseBlock(id='toolu_1', ...)"`, and a restart replays that string to the
    provider as literal text.
    """
    stored = json.loads(json.dumps(_anthropic_history(), default=str))
    assistant = stored[1]["content"]

    assert isinstance(assistant, list)
    assert all(isinstance(block, dict) for block in assistant), (
        f"a block survived serialization as {assistant!r}"
    )
    assert assistant[1]["type"] == "tool_use"
    assert assistant[1]["id"] == "toolu_1"


def test_a_non_dict_content_block_is_reported_rather_than_skipped():
    """The reader must not quietly re-derive the type either.

    The defect was silent.  `_repair_tool_history` pairs a `tool_use` with its
    `tool_result` by looking for **dicts**, so a block it could not read made the
    result look like an orphan, and the pair was dropped -- with nothing
    anywhere recording that a tool the model had already run went missing.

    The writer now guarantees JSON-native blocks, so a non-dict here can only
    mean a writer regressed or a checkpoint came back corrupted.  Both are
    things this layer must say out loud: `ContextLimitError` is what
    `_format_agent_error` turns into a visible error, and the alternative is a
    conversation quietly missing its tool results.

    The tolerance that remains is deliberate and narrow: the two text-extraction
    paths (`_checkpoint_summary`, the staged-turn visibility check) still skip an
    unreadable block, because there the cost is a line of summary text rather
    than a deleted tool result.
    """
    from anthropic.types import ToolUseBlock

    from agent.memory.system import ContextLimitError

    messages = [
        {"role": "user", "content": "please list the files"},
        {
            "role": "assistant",
            "content": [
                ToolUseBlock(
                    id="toolu_1", input={"cmd": "ls"}, name="shell", type="tool_use"
                )
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt"}
            ],
        },
    ]

    with pytest.raises(ContextLimitError, match="not a dict"):
        _compact(messages)

    # Control: the same history with the block in its wire form compacts, so the
    # raise above is about the block's type and not about the history's shape.
    assert _block_types(_compact(_anthropic_history())[1]) == ["text", "tool_use"]


# ── 2. A cut inside a turn must not remove that turn's reason for existing ───


def test_the_turn_request_survives_an_interjection():
    """The protected request is the turn's, not "the newest user message".

    An interjection arrives mid-turn and is appended as a real user message, so
    the newest-user-message proxy names the interjection and frees the original
    request for eviction.  The model then answers an eviction notice instead of
    the task -- the same failure the eviction notice's own placement was
    measured against, arriving through a different door.
    """
    request = {"role": "user", "content": "ORIGINAL-TURN-REQUEST " + "x" * 400}
    interjection = {
        "role": "user",
        "content": "<user_interjection>stop</user_interjection>",
    }

    compacted = _compact([request, interjection], budget=120, protected=[request])

    assert any(message is request for message in compacted), (
        "the turn lost its own request"
    )
    assert compacted[-1] is request, (
        "the request must be the message the provider reads as the turn to "
        f"answer; order was {[message['content'][:24] for message in compacted]}"
    )


def test_compaction_still_protects_the_newest_request_without_a_protected_set():
    """The identity pin is an addition, not a replacement.

    Runs with no context manager -- the scheduler's `stateless` policy -- where
    the caller has no turn object to hand over.
    """
    request = {"role": "user", "content": "LATEST " + "y" * 400}
    older = {"role": "user", "content": "OLDER " + "z" * 400}

    compacted = _compact([older, request], budget=120)

    assert compacted[-1] is request
    assert all(message is not older for message in compacted)


# ── 3. A budget is room in a window other things already occupy ─────────────


def test_retrieval_is_sized_against_the_message_it_rides_in():
    """The allocation must charge for every block the same message carries.

    Retrieval was sized from the raw user message, while what is actually sent
    is ``turn_context + "\\n\\n" + user_message``.  The checkpoint, the skills,
    the orchestration policy and the time riding in that same message were
    invisible to the allocation, so retrieval was handed room that had already
    been spent -- and the turn-boundary cut had to take it back out of older
    history to make the request fit.
    """
    import agent as agent_module

    def budget_for(checkpoint: str) -> int:
        seen: dict[str, int] = {}

        class _ContextManager:
            def retrieve_implicit_context(self, *_args, **kwargs):
                seen["token_budget"] = kwargs["token_budget"]
                return ""

        agent = agent_module.BaseAgent(
            object(),
            agent_module.ToolRegistry(),
            model="fake-model",
            api_format="openai",
        )
        agent.context_manager = _ContextManager()
        ctx = agent_module.AgentContext(system_prompt="system")
        if checkpoint:
            ctx.metadata["_checkpoint_summary"] = checkpoint
        agent._prepare_turn(ctx, "question", ())
        return seen["token_budget"]

    with_checkpoint = budget_for("x" * 40_000)
    without_checkpoint = budget_for("")

    assert with_checkpoint < without_checkpoint, (
        "a checkpoint that rides in the same message must shrink the retrieval "
        f"budget; got {with_checkpoint} with one and {without_checkpoint} without"
    )


# ── 5. One column, one meaning, whichever provider filled it ────────────────


def test_usage_means_the_whole_prompt_for_both_providers():
    """``input_tokens`` is ``|P_n|`` whichever wire format reported it.

    Anthropic's ``input_tokens`` excludes the cached part and splits the rest
    across two further fields; OpenAI's ``prompt_tokens`` is already the total.
    Reading the Anthropic field as though it were the total under-reports the
    prompt by the whole cached prefix, and the estimator -- which is calibrated
    against exactly this number -- learns from a figure that shrinks as caching
    improves.
    """
    from agent.usage import extract_provider_usage

    anthropic = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=10,
            cache_read_input_tokens=1000,
            cache_creation_input_tokens=500,
            output_tokens=20,
        )
    )
    openai = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=1510,
            completion_tokens=20,
            prompt_cache_hit_tokens=1000,
        )
    )

    from_anthropic = extract_provider_usage(anthropic)
    from_openai = extract_provider_usage(openai)

    assert from_anthropic.input_tokens == 1510
    assert from_anthropic.input_tokens == from_openai.input_tokens
    assert from_anthropic.cached_input_tokens == from_openai.cached_input_tokens == 1000
    assert from_anthropic.uncached_input_tokens == 510


def test_a_fully_cached_call_is_not_reported_as_free():
    """The cheapest input is not the absence of input.

    A call served entirely from cache reported ``input_tokens = 0``, which the
    row writer then read as "nothing worth recording" and the estimator as "the
    payload was empty".
    """
    from agent.usage import extract_provider_usage

    usage = extract_provider_usage(
        SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=0, cache_read_input_tokens=4000, output_tokens=12
            )
        )
    )

    assert usage.input_tokens == 4000
    assert usage.total_tokens == 4012


# ── 6. A stream that never terminates is not a finished answer ───────────────


def _chunk(text: str, finish_reason):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=text, tool_calls=None, reasoning_content=None
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=None,
    )


class _ScriptedStreamClient:
    """An OpenAI-compatible client whose stream is exactly the chunks given."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **_kwargs):
        chunks = self._chunks

        async def iterate():
            for chunk in chunks:
                yield chunk

        return iterate()


def _run_stream(chunks):
    from agent.core.transport import OpenAITransport

    transport = OpenAITransport(_ScriptedStreamClient(chunks), None, True)
    return transport, asyncio.run(
        transport.stream(
            model="m",
            max_tokens=8,
            system="s",
            messages=[],
            tools=[],
            callback=lambda _chunk: None,
        )
    )


def test_a_stream_without_a_finish_reason_is_not_a_clean_end_turn():
    """Silence about why a stream ended is not the same as ending normally.

    `finish_reason` was initialised to `"stop"` and only overwritten when a
    chunk carried one, so a gateway that omits the terminator reported a
    half-finished answer as a complete one -- committed to history as the
    answer, with no error anywhere.
    """
    transport, (response, _text) = _run_stream([_chunk("half an ans", None)])

    assert transport.completion_error(response) is not None, (
        "a stream that ended without a terminator must be reported as incomplete"
    )


def test_a_stream_that_does_terminate_is_clean():
    """The control: the fix must not turn every stream into an error."""
    transport, (response, _text) = _run_stream(
        [_chunk("a whole ans", None), _chunk("", "stop")]
    )

    assert transport.completion_error(response) is None


def test_a_truncated_stream_is_still_reported_as_truncated():
    """`length` keeps meaning what it meant; the new case is layered on top."""
    transport, (response, _text) = _run_stream([_chunk("cut off", "length")])

    error = transport.completion_error(response)
    assert error is not None and "length" in error


def test_a_tool_call_cut_mid_arguments_is_incomplete():
    """Partial argument JSON must be retried, not executed.

    `_parse_tool_arguments` reports unparseable arguments as
    `{"_malformed_arguments": <raw>}` -- and nothing anywhere consumed that
    marker, so a call cut mid-flight was executed with a dict of the wrong shape
    instead of being retried for a whole one.
    """
    truncated = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        _tool_call_delta(None, "call_a", "shell", '{"cmd": "l')
                    ],
                    reasoning_content=None,
                ),
                finish_reason="tool_calls",
            )
        ],
        usage=None,
    )

    from agent.core.transport import OpenAITransport

    transport = OpenAITransport(_ScriptedStreamClient([truncated]), None, True)
    response, _text = asyncio.run(
        transport.stream(
            model="m",
            max_tokens=8,
            system="s",
            messages=[],
            tools=[],
            callback=lambda _chunk: None,
        )
    )

    assert transport.has_incomplete_tool_calls(response) is True


def test_a_complete_tool_call_is_not_reported_as_incomplete():
    """The control: valid arguments with a terminator are a finished protocol."""
    complete = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        _tool_call_delta(None, "call_a", "shell", '{"cmd": "ls"}')
                    ],
                    reasoning_content=None,
                ),
                finish_reason="tool_calls",
            )
        ],
        usage=None,
    )

    from agent.core.transport import OpenAITransport

    transport = OpenAITransport(_ScriptedStreamClient([complete]), None, True)
    response, _text = asyncio.run(
        transport.stream(
            model="m",
            max_tokens=8,
            system="s",
            messages=[],
            tools=[],
            callback=lambda _chunk: None,
        )
    )

    assert transport.has_incomplete_tool_calls(response) is False


# ── 4. A malformed tool-call delta must not merge every call into one ────────


def _tool_call_delta(index, call_id, name, arguments):
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=call_id, function=function)


def test_tool_calls_stay_separate_when_the_gateway_omits_the_index():
    """`index` is the accumulator key and is typed `Optional[int]`.

    A gateway that omits it puts every call under the same key: ids and names
    overwrite each other and the argument fragments concatenate, so one corrupt
    call is executed instead of two good ones.
    """
    first = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        _tool_call_delta(None, "call_a", "shell", '{"cmd": "ls"}')
                    ],
                    reasoning_content=None,
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )
    second = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        _tool_call_delta(None, "call_b", "read_file", '{"path": "a"}')
                    ],
                    reasoning_content=None,
                ),
                finish_reason="tool_calls",
            )
        ],
        usage=None,
    )

    from agent.core.transport import OpenAITransport

    transport = OpenAITransport(_ScriptedStreamClient([first, second]), None, True)
    response, _text = asyncio.run(
        transport.stream(
            model="m",
            max_tokens=8,
            system="s",
            messages=[],
            tools=[],
            callback=lambda _chunk: None,
        )
    )
    _stop, _text, tool_calls = transport.parse_response(response)

    assert [call["name"] for call in tool_calls] == ["shell", "read_file"]
    assert [call["id"] for call in tool_calls] == ["call_a", "call_b"]


@pytest.mark.parametrize("count,results", [(2, 2), (2, 1)])
def test_the_rollback_count_matches_what_was_appended(count, results):
    """A rollback deletes as many messages as the append created, no more.

    The count was the number of tool calls, while the append produced
    `len(zip(tool_calls, results))`.  When they disagree the rollback cuts into
    the turn's real history.
    """
    from agent.core.transport import OpenAITransport

    transport = OpenAITransport(None, None, True)
    tool_calls = [{"id": f"c{i}", "name": "n", "input": {}} for i in range(count)]
    appended = transport.build_tool_result_messages(
        tool_calls, ["r"] * results
    )

    assert transport.tool_result_rollback_count(
        tool_calls, ["r"] * results
    ) == len(appended)


# ── 7. "Already in front of the model" survives the framing ─────────────────


def test_a_staged_turn_is_not_re_injected_while_it_is_still_visible(tmp_path):
    """The visibility test must recognise a message that carries framing.

    Staging records the user's own words, and the message that was sent is
    ``turn_context + "\\n\\n" + user_message``.  Comparing the two by equality
    stopped matching as soon as per-turn context moved into the message, so
    every staged turn was re-injected on every turn -- new tail content, billed
    at full rate, pushing compaction earlier -- until a checkpoint happened to
    exist and the path was switched off wholesale.
    """
    from agent import (
        ConsolidationEngine,
        ContextManager,
        LocalRetriever,
        LTMStore,
        StagingBuffer,
    )

    context_dir = tmp_path / "context"
    store = LTMStore(context_dir=context_dir)
    manager = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        idle_seconds=300,
        min_messages=4,
        staging=StagingBuffer(context_dir=context_dir, session_id="s"),
    )
    manager.staging.append("user", "what is the capital of Peru")

    body = [
        {
            "role": "user",
            "content": (
                "Current UTC time: 2026-09-23 00:00 UTC. "
                "Use the current_time tool when the user asks about local time.\n\n"
                "what is the capital of Peru"
            ),
        }
    ]

    injected = manager.retrieve_implicit_context(
        "and of Chile?",
        current_messages=body,
        current_turn_id="t2",
        token_budget=4000,
        include_recent_session=True,
        include_working_state=False,
    )

    assert "capital of Peru" not in injected, (
        "a staged turn the model can already see must not be injected again"
    )


def test_a_staged_turn_that_is_no_longer_visible_is_injected(tmp_path):
    """The control: the fix must not disable the injection altogether.

    A turn that has been compacted away *should* come back -- that is the whole
    point of re-surfacing recent session turns.
    """
    from agent import (
        ConsolidationEngine,
        ContextManager,
        LocalRetriever,
        LTMStore,
        StagingBuffer,
    )

    context_dir = tmp_path / "context"
    store = LTMStore(context_dir=context_dir)
    manager = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        idle_seconds=300,
        min_messages=4,
        staging=StagingBuffer(context_dir=context_dir, session_id="s"),
    )
    manager.staging.append("user", "what is the capital of Peru")

    injected = manager.retrieve_implicit_context(
        "and of Chile?",
        current_messages=[{"role": "user", "content": "a later, unrelated turn"}],
        current_turn_id="t2",
        token_budget=4000,
        include_recent_session=True,
        include_working_state=False,
    )

    assert "capital of Peru" in injected
