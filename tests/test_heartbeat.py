from __future__ import annotations

import json


def test_heartbeat_writer_atomically_writes_runtime_health_file(tmp_path):
    from agent.runtime.heartbeat import HeartbeatWriter

    path = tmp_path / "health" / "session-a.json"
    writer = HeartbeatWriter(
        session_id="session-a",
        agent_id="agent-a",
        path=path,
        process_token="token-a",
    )

    first = writer.write(
        state="LLM",
        detail="fake-model",
        turn_id="turn-a",
        pending_messages=2,
    )
    writer.mark_progress()
    second = writer.write(
        state="tools",
        detail="shell",
        current_tool="shell",
        turn_id="turn-a",
    )

    payload = json.loads(path.read_text(encoding="utf-8"))

    assert first["heartbeat_seq"] == 1
    assert second["heartbeat_seq"] == 2
    assert payload["pid"] == writer.pid
    assert payload["process_token"] == "token-a"
    assert payload["session_id"] == "session-a"
    assert payload["agent_id"] == "agent-a"
    assert payload["turn_id"] == "turn-a"
    assert payload["state"] == "tools"
    assert payload["current_tool"] == "shell"
    assert payload["last_seen_at"]
    assert payload["last_progress_at"]
    assert not list(path.parent.glob("*.tmp"))


def test_heartbeat_default_path_sanitizes_session_id(monkeypatch, tmp_path):
    from agent import shared
    from agent.runtime.heartbeat import HeartbeatWriter

    monkeypatch.setattr(shared, "DEFAULT_OUTPUT_DIR", tmp_path / "output")

    writer = HeartbeatWriter(
        session_id="chat/a:b c",
        agent_id="agent-a",
        process_token="token-a",
    )
    payload = writer.write(state="LLM")

    assert payload["session_id"] == "chat/a:b c"
    assert writer.path == tmp_path / "output" / "runtime" / "health" / "chat_a_b_c.json"
    assert writer.path.exists()
