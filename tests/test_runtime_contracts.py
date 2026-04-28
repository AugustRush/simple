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
