from __future__ import annotations

import asyncio

import pytest

from agent.runtime import (
    AgentCore,
    RuntimeComponents,
    RuntimeSessionState,
    RuntimeEvent,
    TurnInput,
    TurnResult,
    TurnRunner,
)


def test_turn_input_from_text_normalizes_channel_message():
    turn = TurnInput.from_text(
        "hello",
        session_id="session-1",
        channel_name="feishu",
        metadata={"message_id": "msg-1"},
    )

    assert turn.text == "hello"
    assert turn.session_id == "session-1"
    assert turn.channel_name == "feishu"
    assert turn.metadata == {"message_id": "msg-1"}


def test_turn_result_record_tool_use_returns_new_result():
    result = TurnResult(text="done")

    updated = result.record_tool_use("bash")

    assert result.tool_calls == ()
    assert updated.tool_calls == ("bash",)
    assert updated.text == "done"


def test_runtime_event_copies_fields_and_metadata():
    fields = {"tool_calls": 1}
    metadata = {"message_id": "msg-1"}

    event = RuntimeEvent(
        name="agent_result_ready",
        session_id="session-1",
        channel_name="feishu",
        fields=fields,
        metadata=metadata,
    )
    fields["tool_calls"] = 2
    metadata["message_id"] = "msg-2"

    assert event.name == "agent_result_ready"
    assert event.session_id == "session-1"
    assert event.channel_name == "feishu"
    assert event.fields == {"tool_calls": 1}
    assert event.metadata == {"message_id": "msg-1"}


def test_runtime_components_require_returns_dependency():
    components = RuntimeComponents({"agent": object()})

    assert components.require("agent") is components.values["agent"]


def test_runtime_components_keeps_live_mapping_updates():
    values = {"system_prompt": "old"}
    components = RuntimeComponents(values)

    values["system_prompt"] = "new"

    assert components.require("system_prompt") == "new"


def test_runtime_components_require_raises_clear_error_for_missing_dependency():
    components = RuntimeComponents({"agent": object()})

    with pytest.raises(KeyError, match="missing runtime component: memory_worker"):
        components.require("memory_worker")


def test_core_lazy_exports_runtime_contracts():
    from agent.core import RuntimeComponents as CoreRuntimeComponents
    from agent.core import RuntimeEvent as CoreRuntimeEvent
    from agent.core import TurnInput as CoreTurnInput
    from agent.core import TurnResult as CoreTurnResult

    assert CoreRuntimeComponents is RuntimeComponents
    assert CoreRuntimeEvent is RuntimeEvent
    assert CoreTurnInput is TurnInput
    assert CoreTurnResult is TurnResult


def test_turn_runner_delegates_to_agent_and_normalizes_result():
    class _FakeAgentResult:
        agent_id = "agent-1"
        content = "reply"
        tool_calls_made = ["bash"]
        error = None

    class _FakeAgent:
        def __init__(self):
            self.calls = []

        async def send_message(self, ctx, user_message, stream_callback=None):
            self.calls.append((ctx, user_message, stream_callback))
            return _FakeAgentResult()

    agent = _FakeAgent()
    components = RuntimeComponents({"agent": agent})
    runner = TurnRunner(components)
    stream_callback = object()
    ctx = object()

    result = asyncio.run(
        runner.run(
            TurnInput.from_text("hello"),
            ctx,
            stream_callback=stream_callback,  # type: ignore[arg-type]
        )
    )

    assert agent.calls == [(ctx, "hello", stream_callback)]
    assert result == TurnResult(
        text="reply",
        tool_calls=("bash",),
        agent_id="agent-1",
    )


def test_runtime_session_state_records_task_context_once_and_truncates():
    state = RuntimeSessionState(ctx=object())

    state.ensure_task_context("x" * 350)
    state.ensure_task_context("new task")

    assert state.task_context == "x" * 300


def test_runtime_session_state_records_turns_and_tools():
    state = RuntimeSessionState(ctx=object())

    state.record_turn(["bash", "search"])

    assert state.turn_count == 1
    assert state.tools_used == ["bash", "search"]


def test_runtime_session_state_has_explicit_operation_defaults():
    state = RuntimeSessionState(ctx=object())

    assert state.operation_state == "idle"
    assert state.accepts_interjections is False
    assert state.model_override is None
    assert state.turn_in_progress is False


def test_runtime_session_state_owns_distinct_interjection_and_restart_queues():
    first = RuntimeSessionState(ctx=object())
    second = RuntimeSessionState(ctx=object())

    assert first.pending_interjections == []
    assert first.restart_queue == []
    assert first.pending_interjections is not first.restart_queue
    assert first.pending_interjections is not second.pending_interjections
    assert first.restart_queue is not second.restart_queue


def test_runtime_session_state_compatibility_aliases_share_no_duplicate_state():
    state = RuntimeSessionState(ctx=object())

    state.turn_in_progress = True
    state.pending_messages.append({"text": "follow up"})

    assert state.operation_state == "active"
    assert state.pending_messages is state.pending_interjections
    assert state.pending_messages is not state.restart_queue

    state.turn_in_progress = False
    assert state.operation_state == "idle"


def test_runtime_session_state_compatibility_alias_cannot_alias_restart_queue():
    state = RuntimeSessionState(ctx=object())
    state.restart_queue.append({"text": "restart"})

    state.pending_messages = state.restart_queue

    assert state.pending_messages == state.restart_queue
    assert state.pending_messages is not state.restart_queue


def test_runtime_session_state_preserves_legacy_full_positional_constructor():
    ctx = object()
    tools_used = ["bash"]
    context_manager = object()
    memory_worker = object()
    cancel_token = object()
    pending_messages = [{"text": "follow up"}]

    state = RuntimeSessionState(
        ctx,
        tools_used,
        4,
        "task",
        context_manager,
        memory_worker,
        cancel_token,
        pending_messages,
        True,
    )

    assert state.ctx is ctx
    assert state.tools_used is tools_used
    assert state.turn_count == 4
    assert state.task_context == "task"
    assert state.context_manager is context_manager
    assert state.memory_worker is memory_worker
    assert state.cancel_token is cancel_token
    assert state.pending_messages is pending_messages
    assert state.pending_messages is state.pending_interjections
    assert state.pending_messages is not state.restart_queue
    assert state.operation_state == "active"
    assert state.turn_in_progress is True


@pytest.mark.parametrize(
    ("turn_in_progress", "expected_state"),
    [(False, "idle"), (True, "active")],
)
def test_runtime_session_state_accepts_legacy_keywords(
    turn_in_progress: bool,
    expected_state: str,
) -> None:
    pending_messages = [{"text": "follow up"}]

    state = RuntimeSessionState(
        ctx=object(),
        pending_messages=pending_messages,
        turn_in_progress=turn_in_progress,
    )

    assert state.pending_messages is pending_messages
    assert state.pending_messages is state.pending_interjections
    assert state.operation_state == expected_state
    assert state.turn_in_progress is turn_in_progress


def test_turn_runner_complete_turn_records_state_and_maintenance():
    class _PluginCatalog:
        def __init__(self):
            self.events = []

        async def fire_turn_end(self, event):
            self.events.append(event)

    maintenance_calls = []
    plugin_catalog = _PluginCatalog()
    state = RuntimeSessionState(
        ctx=object(),
        task_context="original task",
        context_manager=object(),
        memory_worker=object(),
    )
    agent = object()
    components = RuntimeComponents(
        {
            "agent": agent,
            "plugin_catalog": plugin_catalog,
            "system_prompt": "system",
            "post_turn_maintenance": lambda **kwargs: maintenance_calls.append(kwargs),
        }
    )
    runner = TurnRunner(components)

    asyncio.run(
        runner.complete_turn(
            TurnInput.from_text(
                "hello",
                session_id="session-1",
                channel_name="feishu",
                metadata={"message_id": "msg-1"},
            ),
            state,
            TurnResult(text="reply", tool_calls=("bash",)),
        )
    )

    assert state.turn_count == 1
    assert state.tools_used == ["bash"]
    assert len(plugin_catalog.events) == 1
    assert plugin_catalog.events[0].user_input == "hello"
    assert plugin_catalog.events[0].agent_response == "reply"
    assert plugin_catalog.events[0].tool_calls == ["bash"]
    assert plugin_catalog.events[0].session_id == "session-1"
    assert plugin_catalog.events[0].turn_index == 1
    assert maintenance_calls == [
        {
            "ctx_mgr": state.context_manager,
            "agent": agent,
            "ctx": state.ctx,
            "user_content": "hello",
            "assistant_content": "reply",
            "channel": "feishu",
            "record_kwargs": {
                "message_id": "msg-1",
                "metadata": {"message_id": "msg-1"},
            },
            "memory_worker": state.memory_worker,
            "system_prompt": "system",
            "task_context": "original task",
            "error": "",
        }
    ]


def test_turn_runner_uses_turn_id_as_the_journal_identity():
    maintenance_calls = []
    runner = TurnRunner(
        {
            "agent": object(),
            "system_prompt": "system",
            "post_turn_maintenance": lambda **kwargs: maintenance_calls.append(kwargs),
        }
    )
    state = RuntimeSessionState(ctx=object())

    asyncio.run(
        runner.complete_turn(
            TurnInput.from_text(
                "hello",
                metadata={
                    "message_id": "transport-message",
                    "turn_id": "logical-turn",
                    "completion_id": "logical-turn:completion:1",
                },
            ),
            state,
            TurnResult(text="reply"),
        )
    )

    assert maintenance_calls[0]["record_kwargs"]["message_id"] == "logical-turn"
    assert (
        maintenance_calls[0]["record_kwargs"]["assistant_message_id"]
        == "logical-turn:completion:1"
    )


def test_turn_runner_complete_turn_passes_error_to_maintenance():
    maintenance_calls = []
    runner = TurnRunner(
        {
            "agent": object(),
            "system_prompt": "system",
            "post_turn_maintenance": lambda **kwargs: maintenance_calls.append(kwargs),
        }
    )
    state = RuntimeSessionState(ctx=object())

    asyncio.run(
        runner.complete_turn(
            TurnInput.from_text("hello"),
            state,
            TurnResult(text="", error="model response truncated"),
        )
    )

    assert maintenance_calls[0]["error"] == "model response truncated"


def test_post_turn_maintenance_records_runtime_event_turn_id():
    from agent.core.agent import BaseAgent

    events = []

    class _CtxMgr:
        class _Staging:
            def append(self, role, content):
                pass

        staging = _Staging()

        def record_turn(self, **kwargs):
            pass

        def record_runtime_event(self, event_type, payload=None, *, turn_id=""):
            events.append(
                {
                    "event_type": event_type,
                    "payload": payload,
                    "turn_id": turn_id,
                }
            )

        def should_enqueue_consolidation(self):
            return False

        def should_compact_messages(self, messages, input_token_budget):
            return False

    class _Agent:
        max_tokens = 1000

    class _Ctx:
        messages = []

    BaseAgent._post_turn_maintenance(
        ctx_mgr=_CtxMgr(),
        agent=_Agent(),
        ctx=_Ctx(),
        user_content="hello",
        assistant_content="reply",
        channel="feishu",
        record_kwargs={"message_id": "msg-123", "metadata": {"message_id": "msg-123"}},
    )

    assert events[0]["event_type"] == "turn_finished"
    assert events[0]["turn_id"] == "msg-123"


def test_post_turn_maintenance_is_idempotent_for_replayed_message_id(tmp_path):
    from agent import (
        AgentContext,
        BaseAgent,
        ConsolidationEngine,
        ContextManager,
        LocalRetriever,
        LTMStore,
        StagingBuffer,
    )

    context_dir = tmp_path / "context"
    store = LTMStore(context_dir=context_dir, memory_dir=tmp_path / "memory")
    manager = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        staging=StagingBuffer(context_dir=context_dir, session_id="session-1"),
    )

    class _Agent:
        context_window = 8192
        max_tokens = 1024

    ctx = AgentContext(system_prompt="system")
    kwargs = {
        "ctx_mgr": manager,
        "agent": _Agent(),
        "ctx": ctx,
        "user_content": "hello",
        "assistant_content": "reply",
        "channel": "feishu",
        "record_kwargs": {
            "message_id": "msg-123",
            "metadata": {"message_id": "msg-123"},
        },
    }

    BaseAgent._post_turn_maintenance(**kwargs)
    BaseAgent._post_turn_maintenance(**kwargs)

    turns = store.recent_conversation_turns(session_id="session-1", limit=10)
    events = store.recent_agent_events(session_id="session-1", limit=10)
    assert [(turn.role, turn.content) for turn in turns] == [
        ("user", "hello"),
        ("assistant", "reply"),
    ]
    assert [event.event_type for event in events] == ["turn_finished"]
    assert manager.staging.count() == 2


def test_agent_core_journals_user_input_before_model_completion(tmp_path):
    from agent import (
        AgentContext,
        ConsolidationEngine,
        ContextManager,
        LocalRetriever,
        LTMStore,
        StagingBuffer,
    )

    context_dir = tmp_path / "context"
    store = LTMStore(context_dir=context_dir, memory_dir=tmp_path / "memory")
    manager = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        staging=StagingBuffer(context_dir=context_dir, session_id="cli"),
    )
    model_started = asyncio.Event()

    class _Agent:
        async def send_message(self, ctx, text, **kwargs):
            model_started.set()
            await asyncio.Event().wait()

    async def scenario():
        core = AgentCore(
            {
                "agent": _Agent(),
                "system_prompt": "system",
                "post_turn_maintenance": lambda **kwargs: None,
            }
        )
        state = RuntimeSessionState(
            ctx=AgentContext(system_prompt="system"),
            context_manager=manager,
        )
        pending = asyncio.create_task(
            core.handle_turn(
                TurnInput.from_text(
                    "用 three.js 画一个房子",
                    session_id="cli",
                    channel_name="cli",
                ),
                state,
            )
        )
        await model_started.wait()
        turns = store.recent_conversation_turns(session_id="cli", limit=10)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        return turns

    turns = asyncio.run(scenario())

    assert [(turn.role, turn.content) for turn in turns] == [
        ("user", "用 three.js 画一个房子")
    ]
    assert turns[0].message_id


def test_turn_runner_complete_turn_uses_live_component_updates():
    maintenance_calls = []
    values = {
        "agent": object(),
        "system_prompt": "old",
        "post_turn_maintenance": lambda **kwargs: maintenance_calls.append(kwargs),
    }
    runner = TurnRunner(values)
    state = RuntimeSessionState(ctx=object())

    values["system_prompt"] = "new"

    asyncio.run(
        runner.complete_turn(
            TurnInput.from_text("hello"),
            state,
            TurnResult(text="reply"),
        )
    )

    assert maintenance_calls[0]["system_prompt"] == "new"


def test_agent_core_uses_live_component_prompt_for_existing_session():
    observed = {}

    class _Ctx:
        system_prompt = "old"
        metadata = {}

    class _FakeTurnRunner:
        async def run(self, turn_input, ctx, stream_callback=None):
            observed["system_prompt"] = ctx.system_prompt
            return TurnResult(text="reply")

        async def complete_turn(self, turn_input, state, result):
            return []

    core = AgentCore(
        {
            "system_prompt": "new",
            "turn_runner": _FakeTurnRunner(),
        }
    )
    state = RuntimeSessionState(ctx=_Ctx())

    asyncio.run(core.handle_turn(TurnInput.from_text("hello"), state))

    assert observed["system_prompt"].startswith("new")
    assert "Current Task Context" in observed["system_prompt"]


def test_agent_core_replaces_request_model_metadata_for_each_session_state():
    class _Ctx:
        metadata = {"model_override": "stale-model"}

    core = AgentCore({})
    ctx = _Ctx()

    core._publish_turn_runtime_metadata(
        TurnInput.from_text("first", session_id="session-1"),
        RuntimeSessionState(ctx=ctx, model_override="session-model"),
    )
    assert ctx.metadata["model_override"] == "session-model"

    core._publish_turn_runtime_metadata(
        TurnInput.from_text("second", session_id="session-2"),
        RuntimeSessionState(ctx=ctx),
    )
    assert "model_override" not in ctx.metadata


def test_agent_core_handles_prompt_hooks_turn_loop_and_plugin_continue():
    class _Ctx:
        agent_id = "ctx-1"

        def __init__(self):
            self.metadata = {}
            self.messages = []
            self.system_prompt = "system"

    class _PluginCatalog:
        def __init__(self):
            self.submitted = []

        async def fire_prompt_submit(self, text, metadata=None):
            import agent as agent_module

            self.submitted.append((text, metadata))
            return agent_module.HookResult(context="runtime context")

    class _SkillCatalog:
        def consume_dirty(self):
            return False

        def get(self, _skill_ref):
            return None

    class _FakeTurnRunner:
        def __init__(self):
            self.run_calls = []
            self.complete_calls = 0
            self.completion_ids = []

        async def run(self, turn_input, ctx, stream_callback=None):
            self.run_calls.append((turn_input, ctx, stream_callback))
            return TurnResult(
                text=f"reply:{turn_input.text}",
                tool_calls=("search",),
            )

        async def complete_turn(self, turn_input, state, result):
            self.complete_calls += 1
            self.completion_ids.append(turn_input.metadata.get("completion_id"))
            state.record_turn(list(result.tool_calls))
            if self.complete_calls == 1:
                return [type("Hook", (), {"action": "continue", "message": "follow up"})()]
            return []

    class _Sink:
        def __init__(self):
            self.completed = []
            self.errors = []
            self.drained = 0

        def sync_stream_cb(self, chunk):
            return None

        def on_turn_complete(self, text, tool_calls):
            self.completed.append((text, tool_calls))

        def on_error(self, error):
            self.errors.append(error)

        async def drain(self):
            self.drained += 1

    turn_runner = _FakeTurnRunner()
    plugin_catalog = _PluginCatalog()
    state = RuntimeSessionState(ctx=_Ctx())
    sink = _Sink()
    core = AgentCore(
        {
            "agent": object(),
            "system_prompt": "system",
            "base_system_prompt": "system",
            "registry": object(),
            "skill_catalog": _SkillCatalog(),
            "plugin_catalog": plugin_catalog,
            "turn_runner": turn_runner,
        }
    )

    execution = asyncio.run(
        core.handle_turn(
            TurnInput.from_text(
                "hello",
                session_id="session-1",
                channel_name="feishu",
                metadata={"message_id": "msg-1"},
            ),
            state,
            sink=sink,
        )
    )

    assert [call[0].text for call in turn_runner.run_calls] == [
        "[runtime context]\n\nhello",
        "follow up",
    ]
    assert [call[0].channel_name for call in turn_runner.run_calls] == [
        "feishu",
        "feishu",
    ]
    assert sink.completed == [
        ("reply:[runtime context]\n\nhello", ["search"]),
        ("reply:follow up", ["search"]),
    ]
    assert sink.errors == []
    assert sink.drained == 2
    assert state.turn_count == 2
    assert execution.result.text == "reply:follow up"
    assert execution.iterations == 2
    assert turn_runner.completion_ids == [
        "msg-1:completion:1",
        "msg-1:completion:2",
    ]
    assert not execution.blocked
    assert [event.name for event in execution.events] == [
        "turn_started",
        "agent_result_ready",
        "turn_response_delivered",
        "turn_continued",
        "agent_result_ready",
        "turn_response_delivered",
    ]
    assert execution.events[0].name == "turn_started"
    assert execution.events[1].session_id == "session-1"
    assert execution.events[1].channel_name == "feishu"
    assert execution.events[1].metadata["message_id"] == "msg-1"
    assert execution.events[1].fields["tool_calls"] == 1
    assert execution.events[1].fields["error"] is False
    assert execution.events[1].fields["content_len"] == len(
        "reply:[runtime context]\n\nhello"
    )
    assert execution.events[3].fields["next_prompt"] == "follow up"
    assert plugin_catalog.submitted == [
        (
            "hello",
            {
                "channel": "feishu",
                "session_id": "session-1",
                "message_id": "msg-1",
            },
        )
    ]


def test_agent_core_prompt_submit_metadata_uses_canonical_turn_identity():
    class _Ctx:
        agent_id = "ctx-1"

        def __init__(self):
            self.metadata = {}
            self.messages = []
            self.system_prompt = "system"

    class _PluginCatalog:
        def __init__(self):
            self.seen_metadata = []

        async def fire_prompt_submit(self, text, metadata=None):
            import agent as agent_module

            self.seen_metadata.append(metadata)
            return agent_module.HookResult()

    class _FakeTurnRunner:
        async def run(self, turn_input, ctx, stream_callback=None):
            return TurnResult(text="ok")

        async def complete_turn(self, turn_input, state, result):
            state.record_turn(list(result.tool_calls))
            return []

    plugin_catalog = _PluginCatalog()
    core = AgentCore(
        {
            "agent": object(),
            "system_prompt": "system",
            "plugin_catalog": plugin_catalog,
            "turn_runner": _FakeTurnRunner(),
        }
    )

    asyncio.run(
        core.handle_turn(
            TurnInput.from_text(
                "hello",
                session_id="session-1",
                channel_name="cli",
                metadata={"channel": "spoofed", "session_id": "spoofed"},
            ),
            RuntimeSessionState(ctx=_Ctx()),
        )
    )

    assert plugin_catalog.seen_metadata == [
        {
            "channel": "cli",
            "session_id": "session-1",
        }
    ]


def test_agent_core_publishes_runtime_heartbeat_identity(tmp_path):
    class _Ctx:
        agent_id = "ctx-1"

        def __init__(self):
            self.metadata = {}
            self.messages = []
            self.system_prompt = "system"

    class _FakeTurnRunner:
        def __init__(self):
            self.metadata_seen = None

        async def run(self, turn_input, ctx, stream_callback=None):
            self.metadata_seen = dict(ctx.metadata)
            return TurnResult(text="ok")

        async def complete_turn(self, turn_input, state, result):
            state.record_turn(list(result.tool_calls))
            return []

    runner = _FakeTurnRunner()
    core = AgentCore(
        {
            "agent": object(),
            "system_prompt": "system",
            "turn_runner": runner,
            "output_dir": tmp_path / "output",
        }
    )

    asyncio.run(
        core.handle_turn(
            TurnInput.from_text(
                "hello",
                session_id="chat/a",
                channel_name="feishu",
                metadata={"message_id": "msg-1", "heartbeat_interval": 0.25},
            ),
            RuntimeSessionState(ctx=_Ctx()),
        )
    )

    assert runner.metadata_seen["session_id"] == "chat/a"
    assert runner.metadata_seen["channel_name"] == "feishu"
    assert runner.metadata_seen["turn_id"] == "msg-1"
    assert runner.metadata_seen["heartbeat_interval"] == 0.25
    assert runner.metadata_seen["heartbeat_path"] == (
        tmp_path / "output" / "runtime" / "health" / "chat_a.json"
    )


def test_agent_core_returns_blocked_execution_with_reason_without_running_turn():
    class _Ctx:
        agent_id = "ctx-1"

        def __init__(self):
            self.metadata = {}
            self.messages = []
            self.system_prompt = "system"

    class _PluginCatalog:
        async def fire_prompt_submit(self, text, metadata=None):
            import agent as agent_module

            return agent_module.HookResult(action="block", message="policy")

    class _FakeTurnRunner:
        async def run(self, turn_input, ctx, stream_callback=None):
            raise AssertionError("blocked prompts must not reach the turn runner")

        async def complete_turn(self, turn_input, state, result):
            raise AssertionError("blocked prompts must not complete a turn")

    class _Sink:
        def __init__(self):
            self.statuses = []
            self.drained = 0

        def on_status(self, message, level="info"):
            self.statuses.append((message, level))

        async def drain(self):
            self.drained += 1

    sink = _Sink()
    core = AgentCore(
        {
            "agent": object(),
            "system_prompt": "system",
            "plugin_catalog": _PluginCatalog(),
            "turn_runner": _FakeTurnRunner(),
        }
    )

    execution = asyncio.run(
        core.handle_turn(
            TurnInput.from_text("hello", session_id="session-1"),
            RuntimeSessionState(ctx=_Ctx()),
            sink=sink,
        )
    )

    assert execution.blocked
    assert execution.block_reason == "policy"
    assert execution.iterations == 0
    assert execution.result.text == ""
    assert [event.name for event in execution.events] == ["prompt_blocked"]
    assert execution.events[0].session_id == "session-1"
    assert execution.events[0].channel_name == "cli"
    assert execution.events[0].fields["reason"] == "policy"
    assert sink.statuses == [("Message blocked: policy", "warning")]
    assert sink.drained == 1


def test_agent_core_records_error_reported_runtime_event():
    class _Ctx:
        agent_id = "ctx-1"

        def __init__(self):
            self.metadata = {}
            self.messages = []
            self.system_prompt = "system"

    class _FakeTurnRunner:
        async def run(self, turn_input, ctx, stream_callback=None):
            return TurnResult(text="", error="boom")

        async def complete_turn(self, turn_input, state, result):
            state.record_turn(list(result.tool_calls))
            return []

    class _Sink:
        def __init__(self):
            self.completed = []
            self.errors = []

        def on_turn_complete(self, text, tool_calls):
            self.completed.append((text, tool_calls))

        def on_error(self, error):
            self.errors.append(error)

    core = AgentCore(
        {
            "agent": object(),
            "system_prompt": "system",
            "turn_runner": _FakeTurnRunner(),
        }
    )

    execution = asyncio.run(
        core.handle_turn(
            TurnInput.from_text("hello", session_id="session-1"),
            RuntimeSessionState(ctx=_Ctx()),
            sink=_Sink(),
        )
    )

    assert [event.name for event in execution.events] == [
        "turn_started",
        "agent_result_ready",
        "turn_response_delivered",
        "turn_error_reported",
    ]
    assert execution.events[3].fields["error"] == "boom"


def test_agent_core_records_failed_runtime_event_when_runner_raises():
    completed = []

    class _Ctx:
        agent_id = "ctx-1"

        def __init__(self):
            self.metadata = {}
            self.messages = []
            self.system_prompt = "system"

    class _FakeTurnRunner:
        async def run(self, turn_input, ctx, stream_callback=None):
            raise RuntimeError("runner exploded")

        async def complete_turn(self, turn_input, state, result):
            completed.append(
                {
                    "text": turn_input.text,
                    "session_id": turn_input.session_id,
                    "error": result.error,
                }
            )
            state.record_turn(list(result.tool_calls))
            return []

    core = AgentCore(
        {
            "agent": object(),
            "system_prompt": "system",
            "turn_runner": _FakeTurnRunner(),
        }
    )

    execution = asyncio.run(
        core.handle_turn(
            TurnInput.from_text(
                "hello",
                session_id="session-1",
                channel_name="feishu",
                metadata={"message_id": "msg-1"},
            ),
            RuntimeSessionState(ctx=_Ctx()),
        )
    )

    assert execution.failed
    assert execution.result.error == "runner exploded"
    assert [event.name for event in execution.events] == [
        "turn_started",
        "turn_failed",
    ]
    assert execution.events[1].session_id == "session-1"
    assert execution.events[1].channel_name == "feishu"
    assert execution.events[1].fields["error"] == "runner exploded"
    assert execution.events[1].metadata["message_id"] == "msg-1"
    assert completed == [
        {
            "text": "hello",
            "session_id": "session-1",
            "error": "runner exploded",
        }
    ]


def test_agent_core_does_not_complete_turn_twice_when_completion_raises():
    completed = []

    class _Ctx:
        agent_id = "ctx-1"

        def __init__(self):
            self.metadata = {}
            self.messages = []
            self.system_prompt = "system"

    class _FakeTurnRunner:
        async def run(self, turn_input, ctx, stream_callback=None):
            return TurnResult(text="reply", tool_calls=("bash",))

        async def complete_turn(self, turn_input, state, result):
            completed.append(
                {
                    "text": turn_input.text,
                    "error": result.error,
                    "tool_calls": list(result.tool_calls),
                }
            )
            state.record_turn(list(result.tool_calls))
            raise RuntimeError("maintenance failed")

    core = AgentCore(
        {
            "agent": object(),
            "system_prompt": "system",
            "base_system_prompt": "system",
            "registry": object(),
            "turn_runner": _FakeTurnRunner(),
        }
    )

    execution = asyncio.run(
        core.handle_turn(
            TurnInput.from_text(
                "hello",
                session_id="session-1",
                channel_name="feishu",
                metadata={"message_id": "msg-1"},
            ),
            RuntimeSessionState(ctx=_Ctx()),
        )
    )

    assert execution.result.error == "maintenance failed"
    assert completed == [
        {
            "text": "hello",
            "error": None,
            "tool_calls": ["bash"],
        }
    ]


def test_agent_core_handles_minimal_context_without_metadata_when_no_skill_catalog():
    class _FakeTurnRunner:
        async def run(self, turn_input, ctx, stream_callback=None):
            return TurnResult(text=f"reply:{turn_input.text}")

        async def complete_turn(self, turn_input, state, result):
            state.record_turn(list(result.tool_calls))
            return []

    core = AgentCore(
        {
            "agent": object(),
            "system_prompt": "system",
            "turn_runner": _FakeTurnRunner(),
        }
    )
    state = RuntimeSessionState(ctx=object())

    execution = asyncio.run(
        core.handle_turn(
            TurnInput.from_text("hello", session_id="session-1"),
            state,
        )
    )

    assert execution.result.text == "reply:hello"
    assert state.turn_count == 1


def test_agent_core_normalizes_explicit_skill_requests_before_prompt_hooks():
    class _Ctx:
        agent_id = "ctx-1"

        def __init__(self):
            self.metadata = {}
            self.messages = []
            self.system_prompt = "system"

    class _Bundle:
        id = "quality/review"
        user_invocable = True

    class _SkillCatalog:
        def consume_dirty(self):
            return False

        def get(self, skill_ref):
            return _Bundle() if skill_ref == "quality/review" else None

    class _PluginCatalog:
        def __init__(self):
            self.seen = []

        async def fire_prompt_submit(self, text, metadata=None):
            import agent as agent_module

            self.seen.append(text)
            return agent_module.HookResult()

    class _FakeTurnRunner:
        def __init__(self):
            self.run_calls = []

        async def run(self, turn_input, ctx, stream_callback=None):
            self.run_calls.append((turn_input, dict(ctx.metadata)))
            return TurnResult(text="ok")

        async def complete_turn(self, turn_input, state, result):
            state.record_turn(list(result.tool_calls))
            return []

    turn_runner = _FakeTurnRunner()
    plugin_catalog = _PluginCatalog()
    core = AgentCore(
        {
            "agent": object(),
            "system_prompt": "system",
            "base_system_prompt": "system",
            "registry": object(),
            "skill_catalog": _SkillCatalog(),
            "plugin_catalog": plugin_catalog,
            "turn_runner": turn_runner,
        }
    )

    asyncio.run(
        core.handle_turn(
            TurnInput.from_text("/skill quality/review tighten this"),
            RuntimeSessionState(ctx=_Ctx()),
        )
    )

    assert plugin_catalog.seen == ["tighten this"]
    turn_input, metadata = turn_runner.run_calls[0]
    assert turn_input.text == "tighten this"
    assert metadata["required_skills"] == ["quality/review"]
