"""Tests for the web channel HTTP/WebSocket API and SessionService."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.channels.web import WebChannel, WebConfig
from agent.session_service import SessionService


def _channel(**kwargs) -> WebChannel:
    cfg = dict(
        enabled=True,
        host="127.0.0.1",
        port=8787,
        auth_token="",
        cors_origins=(),
    )
    cfg.update(kwargs)
    channel = WebChannel(WebConfig(**cfg))
    return channel


async def _echo_handler(msg, sink):
    sink.on_stream_chunk("hello")
    sink.on_stream_chunk(" world")
    sink.on_turn_complete("hello world", [])
    return True


class _FakeStore:
    def __init__(self, turns=None):
        self._turns = turns or {}
        self._titles = {}

    def list_session_ids(self, prefix="", limit=50):
        return [
            (sid, "2026-01-01T00:00:00", len(turns))
            for sid, turns in self._turns.items()
        ]

    def delete_conversation_session(self, session_id):
        self._turns.pop(session_id, None)
        self._titles.pop(session_id, None)

    def set_session_title(self, session_id, title):
        self._titles[session_id] = title

    def list_session_titles(self):
        return dict(self._titles)

    def recent_conversation_turns(self, session_id, limit=100):
        turns = self._turns.get(session_id, [])
        return turns[-limit:]


def _turn(role, content):
    return SimpleNamespace(role=role, content=content)


def test_session_service_lists_live_and_durable_sessions():
    store = _FakeStore({"abc123": [_turn("user", "hi")]})
    live = {"live-1": SimpleNamespace(turn_count=2)}
    service = SessionService(store=store, live_states=live)

    sessions = {item["session_id"]: item for item in service.list_sessions()}
    assert sessions["live-1"]["live"] is True
    assert sessions["live-1"]["turn_count"] == 2
    assert sessions["abc123"]["live"] is False

    assert service.get_messages("abc123") == [{"role": "user", "content": "hi"}]
    assert service.create_session()


def test_session_service_tracks_live_mapping_after_empty_bind():
    live = {}
    service = SessionService(live_states=live)
    live["later"] = SimpleNamespace(turn_count=1)

    sessions = service.list_sessions()
    assert [item["session_id"] for item in sessions] == ["later"]


def test_web_index_serves_ui():
    from starlette.testclient import TestClient

    channel = _channel()
    channel.bind_runtime({}, {})

    with TestClient(channel.app) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Simple Agent" in resp.text


def test_web_health_and_sessions_endpoints():
    from starlette.testclient import TestClient

    channel = _channel()
    live_state = SimpleNamespace(turn_count=1)
    channel.bind_runtime({"s-1": live_state}, {})
    channel._handler = _echo_handler

    with TestClient(channel.app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True

        sessions = client.get("/api/sessions")
        assert sessions.status_code == 200
        ids = [item["session_id"] for item in sessions.json()["sessions"]]
        assert "s-1" in ids

        created = client.post("/api/sessions")
        assert created.status_code == 200
        sid = created.json()["session_id"]
        assert sid
        messages = client.get(f"/api/sessions/{sid}/messages")
        assert messages.status_code == 200
        assert messages.json()["messages"] == []


def test_web_session_permissions_are_scoped_to_session():
    from starlette.testclient import TestClient
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_session_allowlist_clear,
        shell_session_permission_get,
    )

    channel = _channel()
    channel.bind_runtime({}, {})
    try:
        with TestClient(channel.app) as client:
            changed = client.patch(
                "/api/sessions/web-a/permissions",
                json={"level": "high", "sandbox": "read_all"},
            )
            assert changed.status_code == 200
            assert changed.json()["level"] == "high"
            assert changed.json()["sandbox"] == "read_all"

            other = client.get("/api/sessions/web-b/permissions")
            assert other.status_code == 200
            assert other.json()["session_level"] == ""
            assert shell_session_permission_get(
                ShellAuthorizationScope("web-a", "web", "")
            ) == "high"
    finally:
        shell_session_allowlist_clear()

def test_web_post_message_runs_handler():
    from starlette.testclient import TestClient

    channel = _channel()
    channel.bind_runtime({}, {})
    channel._handler = _echo_handler

    with TestClient(channel.app) as client:
        sid = client.post("/api/sessions").json()["session_id"]
        resp = client.post(f"/api/sessions/{sid}/messages", json={"text": "hi"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["text"] == "hello world"
        assert body["events"][0]["type"] == "stream_chunk"
        assert body["events"][-1]["type"] == "turn_complete"


async def _confirm_handler(msg, sink):
    approved = await sink.on_tool_confirmation(
        "demo",
        command="ls -la",
        risk_level="medium",
        reason="test confirmation",
        confirmation_token="token-1",
        scope=None,
    )
    sink.on_stream_chunk("approved" if approved else "denied")
    sink.on_turn_complete("approved" if approved else "denied", [])
    return True


def test_web_stream_tool_confirmation_roundtrip():
    from starlette.testclient import TestClient

    channel = _channel()
    channel.bind_runtime({}, {})
    channel._handler = _confirm_handler

    with TestClient(channel.app) as client:
        sid = client.post("/api/sessions").json()["session_id"]
        with client.websocket_connect(f"/api/sessions/{sid}/stream") as ws:
            ws.send_json({"type": "message", "text": "run tool"})
            req = ws.receive_json()
            assert req["type"] == "confirm_request"
            assert req["confirmation_token"] == "token-1"
            ws.send_json({"type": "confirm_response", "approved": True, "confirmation_token": "token-1"})
            received = []
            while True:
                event = ws.receive_json()
                received.append(event)
                if event["type"] == "turn_complete":
                    break
            assert any(e["type"] == "stream_chunk" for e in received)
            assert received[-1]["text"] == "approved"


def test_web_stream_emits_json_events():
    from starlette.testclient import TestClient

    channel = _channel()
    channel.bind_runtime({}, {})
    channel._handler = _echo_handler

    with TestClient(channel.app) as client:
        sid = client.post("/api/sessions").json()["session_id"]
        with client.websocket_connect(f"/api/sessions/{sid}/stream") as ws:
            ws.send_json({"type": "message", "text": "hi"})
            types = []
            while True:
                event = ws.receive_json()
                types.append(event["type"])
                if event["type"] == "turn_complete":
                    break
            assert "stream_chunk" in types
            assert "turn_complete" in types


def test_web_auth_token_required():
    from starlette.testclient import TestClient

    channel = _channel(auth_token="secret")
    channel.bind_runtime({}, {})

    with TestClient(channel.app) as client:
        assert client.get("/api/sessions").status_code == 401
        ok = client.get(
            "/api/sessions", headers={"Authorization": "Bearer secret"}
        )
        assert ok.status_code == 200


def test_web_post_message_requires_text():
    from starlette.testclient import TestClient

    channel = _channel()
    channel.bind_runtime({}, {})
    channel._handler = _echo_handler

    with TestClient(channel.app) as client:
        sid = client.post("/api/sessions").json()["session_id"]
        resp = client.post(f"/api/sessions/{sid}/messages", json={"text": ""})
        assert resp.status_code == 400


def test_web_commands_endpoint_lists_all_channel_commands():
    from starlette.testclient import TestClient

    channel = _channel()
    channel.bind_runtime({}, {})

    with TestClient(channel.app) as client:
        resp = client.get("/api/commands")
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()["commands"]]
        assert "help" in names
        assert "tools" in names
        assert "sessions" in names


def test_web_files_endpoint_serves_agent_home_files(tmp_path):
    from starlette.testclient import TestClient
    from agent import shared

    channel = _channel()
    channel.bind_runtime({}, {})
    monkeypatch_holder = None

    with TestClient(channel.app) as client:
        # Use the active agent home as the allowed root.
        allowed = shared.AGENT_HOME
        inside = allowed / "web-test-note.txt"
        inside.write_text("hello from agent home", encoding="utf-8")
        outside = tmp_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")

        resp = client.get(f"/api/files?path={inside}")
        assert resp.status_code == 200
        assert resp.text == "hello from agent home"

        denied = client.get(f"/api/files?path={outside}")
        assert denied.status_code == 403


def test_web_output_sink_emits_attachment_events():
    import asyncio

    from agent.channels.web import WebOutputSink

    sink = WebOutputSink(collect=True)
    sink.queue_attachment("/tmp/example.txt")
    asyncio.run(sink.flush_attachments())
    assert sink.events[-1]["type"] == "attachment"
    assert sink.events[-1]["name"] == "example.txt"


def test_web_management_endpoints_plugins_skills_context():
    from starlette.testclient import TestClient

    channel = _channel()
    channel.bind_runtime({}, {})

    with TestClient(channel.app) as client:
        plugins = client.get("/api/plugins")
        assert plugins.status_code == 200
        assert "plugins" in plugins.json()
        skills = client.get("/api/skills")
        assert skills.status_code == 200
        assert "skills" in skills.json()
        context = client.get("/api/context")
        assert context.status_code == 200
        assert "stats" in context.json()


def test_web_delete_session_removes_durable_turns():
    from starlette.testclient import TestClient

    store = _FakeStore({"abc123": [_turn("user", "hi")]})
    channel = _channel()
    channel.bind_runtime({}, {"context_manager": SimpleNamespace(store=store)})

    with TestClient(channel.app) as client:
        sessions = client.get("/api/sessions").json()["sessions"]
        assert any(s["session_id"] == "abc123" for s in sessions)
        resp = client.delete("/api/sessions/abc123")
        assert resp.status_code == 200
        sessions = client.get("/api/sessions").json()["sessions"]
        assert all(s["session_id"] != "abc123" for s in sessions)


def test_web_reveal_session_opens_session_snapshot(monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    context_dir = tmp_path / "context"
    context_dir.mkdir()
    store = _FakeStore({"abc123": [_turn("user", "hi")]})
    store.dir = context_dir
    channel = _channel()
    channel.bind_runtime({}, {"context_manager": SimpleNamespace(store=store)})

    calls = {}

    async def fake_launch(path, *, reveal):
        calls["path"] = path
        calls["reveal"] = reveal
        from agent.commands.models import CommandResult

        return CommandResult(response_text="ok")

    monkeypatch.setattr("agent.commands.builtin._launch_path", fake_launch)
    with TestClient(channel.app) as client:
        resp = client.post("/api/sessions/abc123/reveal")

    assert resp.status_code == 200
    target = calls["path"]
    assert resp.json()["path"] == str(target)
    assert target.parent == (context_dir / "sessions").resolve()
    assert target.is_file()
    assert "hi" in target.read_text(encoding="utf-8")
    assert calls["reveal"] is True


def test_web_reveal_session_rejects_unknown_session(tmp_path):
    from starlette.testclient import TestClient

    store = _FakeStore({"abc123": [_turn("user", "hi")]})
    store.dir = tmp_path / "context"
    store.dir.mkdir()
    channel = _channel()
    channel.bind_runtime({}, {"context_manager": SimpleNamespace(store=store)})

    with TestClient(channel.app) as client:
        resp = client.post("/api/sessions/unknown/reveal")

    assert resp.status_code == 404


def test_web_file_allows_marked_isolated_session_home(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from pathlib import Path

    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    session_home = fake_home / ".agent-web-1"
    (session_home / "output").mkdir(parents=True)
    (session_home / ".web-session").touch()
    artifact = session_home / "output" / "result.md"
    artifact.write_text("isolated", encoding="utf-8")
    channel = _channel()
    channel.bind_runtime({}, {})

    with TestClient(channel.app) as client:
        resp = client.get(f"/api/files?path={artifact}")

    assert resp.status_code == 200
    assert resp.text == "isolated"


def test_web_config_get_masks_api_keys():
    from starlette.testclient import TestClient
    import agent.config as config_module

    channel = _channel()
    channel.bind_runtime({}, {})

    original_load = config_module.load_config
    config_module.load_config = lambda: ({"active_provider": "openai", "providers": {"openai": {"api_key": "sk-secret"}}}, False)
    try:
        with TestClient(channel.app) as client:
            resp = client.get("/api/config")
            assert resp.status_code == 200
            cfg = resp.json()["config"]
            assert cfg["providers"]["openai"]["api_key"] == "******"
    finally:
        config_module.load_config = original_load


def test_web_rename_session_updates_title():
    from starlette.testclient import TestClient

    store = _FakeStore({"abc123": [_turn("user", "hi")]})
    channel = _channel()
    channel.bind_runtime({}, {"context_manager": SimpleNamespace(store=store)})

    with TestClient(channel.app) as client:
        resp = client.patch(
            "/api/sessions/abc123",
            json={"title": "税务规划"},
        )
        assert resp.status_code == 200
        sessions = client.get("/api/sessions").json()["sessions"]
        session = next(s for s in sessions if s["session_id"] == "abc123")
        assert session["title"] == "税务规划"


def test_web_plugin_toggle_saves_config_and_reloads():
    import asyncio
    from starlette.testclient import TestClient
    import agent.config as config_module

    calls = {}
    catalog = SimpleNamespace(
        _plugin_config={},
        reload=None,
    )
    async def fake_reload(components):
        calls["reloaded"] = True
        return {"ok": True}
    catalog.reload = fake_reload

    channel = _channel()
    channel.bind_runtime({}, {"plugin_catalog": catalog})

    original_load = config_module.load_config
    original_save = config_module.save_config
    config_module.load_config = lambda: ({"plugins": {}}, False)
    config_module.save_config = lambda cfg: calls.setdefault("saved_cfg", cfg)
    try:
        with TestClient(channel.app) as client:
            resp = client.post(
                "/api/plugins/evolution/toggle",
                json={"enabled": False},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["enabled"] is False
            assert calls.get("saved_cfg", {}).get("plugins", {}).get("evolution", {}).get("enabled") is False
            assert calls.get("reloaded") is True
    finally:
        config_module.load_config = original_load
        config_module.save_config = original_save


def test_web_channel_is_registered_by_gateway_builder():
    from agent.channels.base import _build_gateway_channels

    channels = _build_gateway_channels({"channels": {"web": {"enabled": True}}})
    assert any(isinstance(ch, WebChannel) for ch in channels)
