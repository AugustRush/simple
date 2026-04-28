from __future__ import annotations

import asyncio

import pytest

from agent.runtime import (
    RuntimeComponents,
    RuntimeSessionState,
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
    from agent.core import TurnInput as CoreTurnInput
    from agent.core import TurnResult as CoreTurnResult

    assert CoreRuntimeComponents is RuntimeComponents
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
        }
    ]


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
