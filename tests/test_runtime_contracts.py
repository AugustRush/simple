from __future__ import annotations

import pytest

from agent.runtime import RuntimeComponents, TurnInput, TurnResult


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
