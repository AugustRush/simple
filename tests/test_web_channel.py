"""Tests for the web channel HTTP/WebSocket API and SessionService."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


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
    def __init__(self, turns=None, working_state=None):
        self._turns = turns or {}
        self._titles = {}
        self._working_state = working_state

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

    def load_session_working_state(self, session_id):
        if self._working_state is None:
            return None
        return SimpleNamespace(state=self._working_state)

    def save_session_working_state(self, session_id, state, updated_at=None):
        self._working_state = state
        return SimpleNamespace(state=state)


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


def test_session_service_exposes_task_guidance_and_queue_state():
    class Snapshot:
        state = {
            "task_id": "task-1",
            "active_goal": "finish the migration",
            "status": "in_progress",
            "progress": "database schema updated",
            "next_action": "run the verification suite",
            "last_error": "",
            "artifacts": ["schema.sql"],
        }

    class Store(_FakeStore):
        def load_session_working_state(self, session_id):
            assert session_id == "s-1"
            return Snapshot()

    live = SimpleNamespace(
        operation_state="active",
        pending_interjections=[{"text": "urgent"}],
        restart_queue=[{"text": "follow-up"}, {"text": "another"}],
    )
    state = SessionService(store=Store(), live_states={"s-1": live}).get_session_state("s-1")

    assert state["operation_state"] == "active"
    assert state["queue"] == {"pending": 3, "interjections": 1, "restarts": 2}
    assert state["task"]["active_goal"] == "finish the migration"


def test_session_service_dismisses_task_guidance_and_persists_state():
    store = _FakeStore(
        working_state={
            "task_id": "task-1",
            "active_goal": "finish the migration",
            "status": "cancelled",
            "next_action": "run verification",
            "tasks": [
                {
                    "task_id": "task-1",
                    "active_goal": "finish the migration",
                    "status": "cancelled",
                    "next_action": "run verification",
                }
            ],
        }
    )
    service = SessionService(store=store)

    assert service.dismiss_task_guidance("s-1", "task-1") is True
    assert store._working_state["status"] == "dismissed"
    assert store._working_state["next_action"] == ""
    assert store._working_state["tasks"][0]["status"] == "dismissed"


def test_session_service_dismisses_only_selected_legacy_task():
    store = _FakeStore(
        working_state={
            "active_goal": "second task",
            "status": "cancelled",
            "tasks": [
                {"active_goal": "first task", "status": "cancelled"},
                {"active_goal": "second task", "status": "cancelled"},
            ],
        }
    )

    assert SessionService(store=store).dismiss_task_guidance("s-1") is True
    assert store._working_state["tasks"][0]["status"] == "cancelled"
    assert store._working_state["tasks"][1]["status"] == "dismissed"


def test_web_task_guidance_dismiss_endpoint():
    from starlette.testclient import TestClient

    store = _FakeStore(
        working_state={
            "task_id": "task-1",
            "active_goal": "finish the migration",
            "status": "cancelled",
            "next_action": "run verification",
        }
    )
    channel = _channel()
    channel.bind_runtime({}, {"context_manager": SimpleNamespace(store=store)})

    with TestClient(channel.app) as client:
        response = client.post(
            "/api/sessions/s-1/task-guidance/dismiss",
            json={"task_id": "task-1"},
        )

    assert response.status_code == 200
    assert response.json()["dismissed"] is True
    assert store._working_state["status"] == "dismissed"


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


def test_web_output_sink_deduplicates_equivalent_attachment_paths(tmp_path):
    import asyncio

    from agent.channels.web import WebOutputSink

    recorded = []
    target = tmp_path / "images" / "result.png"
    sink = WebOutputSink(
        collect=True,
        on_attachment=lambda path, name: recorded.append((path, name)),
    )
    sink.mark_turn_start()
    sink.queue_attachment(target)
    sink.queue_attachment(target.parent / "." / target.name)

    asyncio.run(sink.flush_attachments())

    attachments = [event for event in sink.events if event["type"] == "attachment"]
    assert len(attachments) == 1
    assert recorded == [(str(target), "result.png")]


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


def test_web_session_home_uses_new_root_and_delete_cleans_it(tmp_path, monkeypatch):
    from pathlib import Path
    from agent import shared

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(shared, "AGENT_HOME", tmp_path / ".agent")
    home = shared.web_session_home("abc123")
    assert home == tmp_path / ".agent" / "web" / "sessions" / "abc123"
    home.mkdir(parents=True)
    (home / ".web-session").touch()
    (home / "output").mkdir()
    (home / "output" / "artifact.txt").write_text("x")

    service = SessionService(store=_FakeStore(), live_states={})
    assert service.delete_session("abc123") is True
    assert not home.exists()


def test_web_session_registry_keeps_empty_sessions(tmp_path, monkeypatch):
    from pathlib import Path
    from agent import shared
    from starlette.testclient import TestClient

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(shared, "AGENT_HOME", tmp_path / ".agent")
    channel = _channel()
    channel.bind_runtime(
        {},
        {
            "context_manager": SimpleNamespace(store=_FakeStore()),
            "session_store_factory": lambda sid: _FakeStore(),
        },
    )
    with TestClient(channel.app) as client:
        sid = client.post("/api/sessions").json()["session_id"]
        sessions = client.get("/api/sessions").json()["sessions"]
        assert any(item["session_id"] == sid for item in sessions)


def test_web_session_registry_keeps_registry_title(tmp_path, monkeypatch):
    from pathlib import Path
    from agent import shared
    from starlette.testclient import TestClient

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(shared, "AGENT_HOME", tmp_path / ".agent")
    channel = _channel()
    channel.bind_runtime(
        {},
        {
            "context_manager": SimpleNamespace(store=_FakeStore()),
            "session_store_factory": lambda sid: _FakeStore(),
        },
    )
    with TestClient(channel.app) as client:
        sid = client.post("/api/sessions").json()["session_id"]
        renamed = client.patch(
            f"/api/sessions/{sid}",
            json={"title": "保留的空会话标题"},
        )
        assert renamed.status_code == 200
        sessions = client.get("/api/sessions").json()["sessions"]

    assert next(item for item in sessions if item["session_id"] == sid)["title"] == (
        "保留的空会话标题"
    )


def test_web_delete_awaits_runtime_cleanup_before_removing_home(
    tmp_path, monkeypatch
):
    from pathlib import Path
    from agent import shared
    from starlette.testclient import TestClient

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(shared, "AGENT_HOME", tmp_path / ".agent")
    session_id = "abc123"
    home = shared.web_session_home(session_id)
    home.mkdir(parents=True)
    (home / ".web-session").touch()
    cleanup_observations = []

    async def cleanup(sid):
        cleanup_observations.append((sid, home.exists()))
        await asyncio.sleep(0)
        cleanup_observations.append((sid, home.exists()))

    channel = _channel()
    channel.bind_runtime(
        {session_id: SimpleNamespace(turn_count=0)},
        {
            "context_manager": SimpleNamespace(store=_FakeStore()),
            "session_store_factory": lambda sid: _FakeStore(),
            "session_runtime_cleanup": cleanup,
        },
    )
    with TestClient(channel.app) as client:
        response = client.delete(f"/api/sessions/{session_id}")

    assert response.status_code == 200
    assert cleanup_observations == [(session_id, True), (session_id, True)]
    assert not home.exists()


def test_web_delete_rejects_unsafe_session_id(tmp_path, monkeypatch):
    from pathlib import Path
    from agent import shared

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(shared, "AGENT_HOME", tmp_path / ".agent")
    outside = tmp_path / ".agent" / "web" / "victim"
    outside.mkdir(parents=True)
    (outside / ".web-session").touch()

    service = SessionService(store=_FakeStore(), live_states={})
    assert service.delete_session("../victim") is False
    assert outside.exists()


def test_web_model_override_uses_global_config(tmp_path, monkeypatch):
    from pathlib import Path
    from agent import shared
    import agent.config as config_module

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(shared, "AGENT_HOME", tmp_path / ".agent")
    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: (
            {
                "active_provider": "global-provider",
                "providers": {
                    "global-provider": {
                        "default_model": "global-model",
                        "models": ["global-model", "global-alt"],
                    }
                },
            },
            False,
        ),
    )

    assert WebChannel._resolve_model_override("global-alt", "abc123") == "global-alt"
    assert WebChannel._resolve_model_override("global-model", "abc123") == "global-model"


def test_web_session_runtime_uses_global_resources_and_session_output(tmp_path, monkeypatch):
    from pathlib import Path
    from agent import shared
    import agent.bootstrap as bootstrap

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(shared, "AGENT_HOME", tmp_path / ".agent")
    class _Registry:
        def __init__(self): self.context = {}
        def fork(self, context, **_kwargs):
            child = _Registry(); child.context.update(context); return child
        def set_context(self, key, value): self.context[key] = value

    class _Agent:
        api_format = "openai"
        supports_vision = False
        context_window = 10000
        max_parallel_agents = 2
        sub_agent_timeout_seconds = 30
        sub_agent_retries = 0
        max_agents_per_turn = 2
        max_tool_call_iterations = 8
        max_truncation_continuations = 1
        max_rendezvous_rounds = 2
        result_content_max_chars = 1000
        llm_max_retries = 1
        llm_retry_base_delay = 0.1
        content_filter = object()
        def __init__(self, *_args, **_kwargs): pass
        def register_spawn_capability(self, *_args, **_kwargs): pass

    monkeypatch.setattr(bootstrap, "BaseAgent", _Agent)
    monkeypatch.setattr(bootstrap, "_compose_system_prompt", lambda *_a, **_k: "session prompt")
    global_components = {
        "registry": _Registry(), "agent": _Agent(), "client": object(),
        "model": "global-model", "max_tokens": 1024, "base_system_prompt": "base",
        "context_manager": object(), "workspace_root": tmp_path / "workspace",
        "skill_catalog": object(), "plugin_catalog": object(),
    }
    result = __import__("asyncio").run(
        bootstrap._build_web_session_components(
            "sid123", {"model": "global"}, global_components
        )
    )

    home = tmp_path / ".agent" / "web" / "sessions" / "sid123"
    assert result["client"] is global_components["client"]
    assert result["skill_catalog"] is global_components["skill_catalog"]
    assert result["plugin_catalog"] is global_components["plugin_catalog"]
    assert result["output_dir"] == (home / "output").resolve()
    assert result["_shares_global_runtime"] is True
    assert not (home / "config.json").exists()


def test_web_session_runtime_restores_selected_workspace_write_grant(tmp_path, monkeypatch):
    from pathlib import Path
    from agent import shared
    import agent.bootstrap as bootstrap

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(shared, "AGENT_HOME", tmp_path / ".agent")
    home = tmp_path / ".agent" / "web" / "sessions" / "sid123"
    home.mkdir(parents=True)
    workspace = tmp_path / "project"
    workspace.mkdir()
    (home / ".session.json").write_text(
        __import__("json").dumps({
            "workspace_root": str(workspace),
            "workspace_read": True,
            "workspace_write": True,
        }),
        encoding="utf-8",
    )
    class _Registry:
        def __init__(self): self.context = {}
        def fork(self, context, **_kwargs):
            child = _Registry(); child.context.update(context); return child
        def set_context(self, key, value): self.context[key] = value

    class _Agent:
        api_format = "openai"; supports_vision = False; context_window = 10000
        max_parallel_agents = 2; sub_agent_timeout_seconds = 30; sub_agent_retries = 0
        max_agents_per_turn = 2; max_tool_call_iterations = 8
        max_truncation_continuations = 1; max_rendezvous_rounds = 2
        result_content_max_chars = 1000; llm_max_retries = 1; llm_retry_base_delay = 0.1
        content_filter = object()
        def __init__(self, *_args, **_kwargs): pass
        def register_spawn_capability(self, *_args, **_kwargs): pass

    monkeypatch.setattr(bootstrap, "BaseAgent", _Agent)
    monkeypatch.setattr(bootstrap, "_compose_system_prompt", lambda *_a, **_k: "session prompt")
    global_components = {
        "registry": _Registry(), "agent": _Agent(), "client": object(),
        "model": "global-model", "max_tokens": 1024, "base_system_prompt": "base",
        "context_manager": object(), "workspace_root": tmp_path / "default",
        "skill_catalog": object(), "plugin_catalog": object(),
    }
    result = __import__("asyncio").run(
        bootstrap._build_web_session_components(
            "sid123",
            {"file_access": {"workspace": {"read": True, "write": False}}},
            global_components,
        )
    )

    assert result["workspace_root"] == workspace.resolve()
    assert result["file_access_policy"].workspace_read is True
    assert result["file_access_policy"].workspace_write is True


def test_session_service_recovers_legacy_events_by_turn_id(tmp_path):
    from agent.memory.store import LTMStore

    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")
    store.append_conversation_turn(
        session_id="web-session",
        role="user",
        content="hello",
        channel="web",
        message_id="turn-legacy",
    )
    store.append_conversation_turn(
        session_id="web-session",
        role="assistant",
        content="done",
        channel="web",
        message_id="turn-legacy:completion:1",
        reply_to_id="turn-legacy",
    )
    store.append_agent_event(
        session_id="random-factory-session",
        turn_id="turn-legacy",
        event_type="tool_started",
        payload={"operation_id": "tool-1", "tool_name": "search"},
    )
    store.append_agent_event(
        session_id="random-factory-session",
        turn_id="turn-legacy",
        event_type="tool_completed",
        payload={"operation_id": "tool-1", "tool_name": "search", "ok": True},
    )

    messages = SessionService(store=store).get_messages("web-session")

    assert [item["tool"] for item in messages if item.get("role") == "tool"] == [
        "search"
    ]
    assert messages[1]["role"] == "tool"


def test_session_service_restores_attachments_on_the_user_message(tmp_path):
    from agent.memory.store import LTMStore

    attachment = tmp_path / "uploads" / "brief.pdf"
    attachment.parent.mkdir()
    attachment.write_bytes(b"%PDF-test")
    metadata = {
        "attachments": [
            {
                "id": "upload-1",
                "filename": "brief.pdf",
                "mime_type": "application/pdf",
                "kind": "document",
                "path": str(attachment),
                "size_bytes": attachment.stat().st_size,
            }
        ]
    }
    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")
    result = store.write_conversation_exchange(
        session_id="web-session",
        user_content="",
        channel="web",
        message_id="attachment-turn",
        metadata=metadata,
    )
    assert result.user_created is True
    # Simulate the event written by the previous implementation. It must not
    # create a second standalone attachment after a refresh.
    store.append_agent_event(
        session_id="web-session",
        turn_id="attachment-turn",
        event_type="attachment",
        payload={"name": "brief.pdf", "path": str(attachment)},
    )

    messages = SessionService(store=store).get_messages("web-session")

    assert messages == [
        {
            "role": "user",
            "content": "",
            "attachments": metadata["attachments"],
        }
    ]


def test_session_service_deduplicates_output_attachment_events_per_turn(tmp_path):
    from agent.memory.store import LTMStore

    attachment = tmp_path / "output" / "result.png"
    attachment.parent.mkdir()
    attachment.write_bytes(b"image")
    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")
    store.write_conversation_exchange(
        session_id="web-session",
        user_content="生成一张图片",
        assistant_content="图片已生成。",
        channel="web",
        message_id="image-turn",
    )
    for _ in range(2):
        store.append_agent_event(
            session_id="web-session",
            turn_id="image-turn",
            event_type="attachment",
            payload={"name": "result.png", "path": str(attachment)},
        )

    messages = SessionService(store=store).get_messages("web-session")

    attachments = [item for item in messages if item.get("tool") == "attachment"]
    assert len(attachments) == 1


def test_session_service_merges_continuations_without_moving_trace_or_attachment(
    tmp_path,
):
    from agent.memory.store import LTMStore

    attachment = tmp_path / "uploads" / "reference.png"
    attachment.parent.mkdir()
    attachment.write_bytes(b"image")
    metadata = {
        "attachments": [
            {
                "id": "upload-1",
                "filename": "reference.png",
                "mime_type": "image/png",
                "kind": "image",
                "path": str(attachment),
                "size_bytes": attachment.stat().st_size,
            }
        ]
    }
    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")
    store.write_conversation_exchange(
        session_id="web-session",
        user_content="分析这个附件",
        channel="web",
        message_id="attachment-turn",
        metadata=metadata,
    )
    store.write_conversation_exchange(
        session_id="web-session",
        user_content="分析这个附件",
        assistant_content="我先检查文件。",
        channel="web",
        message_id="attachment-turn",
        assistant_message_id="attachment-turn:completion:1",
    )
    store.append_agent_event(
        session_id="web-session",
        turn_id="attachment-turn",
        event_type="tool_started",
        payload={"operation_id": "tool-1", "tool_name": "inspect_image"},
    )
    store.append_agent_event(
        session_id="web-session",
        turn_id="attachment-turn",
        event_type="tool_completed",
        payload={
            "operation_id": "tool-1",
            "tool_name": "inspect_image",
            "ok": True,
        },
    )
    store.write_conversation_exchange(
        session_id="web-session",
        user_content="继续分析",
        assistant_content="检查完成，这是最终结论。",
        channel="web",
        message_id="attachment-turn",
        assistant_message_id="attachment-turn:completion:2",
    )

    messages = SessionService(store=store).get_messages("web-session")

    assert [item["role"] for item in messages] == ["user", "tool", "assistant"]
    assert messages[0]["attachments"] == metadata["attachments"]
    assert messages[1]["tool"] == "inspect_image"
    assert messages[1]["toolState"] == "done"
    assert messages[2]["content"] == "我先检查文件。\n\n检查完成，这是最终结论。"


def test_web_create_agent_schedule_with_structured_time(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")
    channel = _channel()
    channel.bind_runtime({}, {})
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

    with TestClient(channel.app) as client:
        created = client.post(
            "/api/schedules",
            json={
                "name": "daily review",
                "action_type": "agent_task",
                "prompt": "Review the repository and summarize open risks.",
                "trigger_type": "once",
                "at": future,
                "timezone_name": "Asia/Shanghai",
            },
        )
        assert created.status_code == 200
        assert created.json()["task"]["kind"] == "agent_prompt"
        tasks = client.get("/api/schedules").json()["tasks"]
        assert tasks[0]["payload"]["prompt"].startswith("Review the repository")

        invalid = client.post(
            "/api/schedules",
            json={
                "name": "past task",
                "action_type": "agent_task",
                "prompt": "Do something",
                "trigger_type": "once",
                "at": "2020-01-01T00:00:00+00:00",
                "timezone_name": "UTC",
            },
        )
        assert invalid.status_code == 400
        assert "晚于当前时间" in invalid.json()["error"]


def test_web_schedule_validates_selected_skills_and_permission(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    class SkillCatalog:
        def get(self, skill_id):
            if skill_id == "review":
                return SimpleNamespace(id="review", user_invocable=True)
            return None

    channel = _channel()
    channel.bind_runtime({}, {"skill_catalog": SkillCatalog()})
    body = {
        "name": "review",
        "action_type": "agent_task",
        "prompt": "Review the repository.",
        "trigger_type": "once",
        "at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        "timezone_name": "UTC",
        "selected_skills": ["review"],
        "permission_profile": "read_only",
    }

    with TestClient(channel.app) as client:
        created = client.post("/api/schedules", json=body)
        assert created.status_code == 200
        assert created.json()["task"]["selected_skills"] == ["review"]
        assert created.json()["task"]["permission_profile"] == "read_only"

        unavailable = client.post(
            "/api/schedules",
            json={**body, "name": "missing", "selected_skills": ["missing"]},
        )
        assert unavailable.status_code == 400
        assert "技能不可用" in unavailable.json()["error"]

        unsafe = client.post(
            "/api/schedules",
            json={**body, "name": "unsafe", "permission_profile": "full"},
        )
        assert unsafe.status_code == 400
        assert "权限策略" in unsafe.json()["error"]


def test_web_schedule_run_history_and_output(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import DeliveryTarget, NewScheduledTask, SchedulerStore, TriggerSpec

    agent_home = tmp_path / ".agent"
    db_path = agent_home / "tasks" / "scheduler.db"
    monkeypatch.setattr(shared, "AGENT_HOME", agent_home)
    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", db_path)
    store = SchedulerStore(db_path=db_path)
    scheduled_for = datetime(2026, 9, 7, 2, 0, tzinfo=timezone.utc)
    task = store.create_task(
        NewScheduledTask(
            name="daily report",
            kind="agent_prompt",
            trigger=TriggerSpec.once(scheduled_for, "Asia/Shanghai"),
            payload={"prompt": "Summarize today's work"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        ),
        now=scheduled_for - timedelta(hours=1),
    )
    claimed = store.claim_due_tasks(
        now=scheduled_for + timedelta(seconds=2),
        lease_seconds=300,
    )[0]
    output_path = agent_home / "output" / "scheduler" / task.id / f"{claimed.run.id}.md"
    output_path.parent.mkdir(parents=True)
    output_path.write_text("# Daily report\n\nEverything completed.", encoding="utf-8")
    artifact_path = output_path.parent / claimed.run.id / "artifacts" / "report.txt"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("artifact contents", encoding="utf-8")
    store.complete_run(
        task.id,
        claimed.run.id,
        finished_at=scheduled_for + timedelta(seconds=7),
        status="succeeded",
        summary="Daily report completed",
        output_path=str(output_path),
        delivery_status="stored",
    )
    store.close()

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        listed = client.get("/api/schedules")
        assert listed.status_code == 200
        listed_task = listed.json()["tasks"][0]
        assert listed_task["latest_run"]["status"] == "succeeded"
        assert listed_task["latest_run"]["duration_ms"] == 5000
        assert listed_task["latest_run"]["output_available"] is True

        history = client.get(f"/api/schedules/{task.id}/runs")
        assert history.status_code == 200
        run = history.json()["runs"][0]
        assert run["id"] == claimed.run.id
        assert run["summary"] == "Daily report completed"
        assert run["delivery_status"] == "stored"

        output = client.get(
            f"/api/schedules/{task.id}/runs/{claimed.run.id}/output"
        )
        assert output.status_code == 200
        assert output.json()["available"] is True
        assert output.json()["content"].startswith("# Daily report")
        assert output.json()["truncated"] is False

        artifacts = client.get(
            f"/api/schedules/{task.id}/runs/{claimed.run.id}/artifacts"
        )
        assert artifacts.status_code == 200
        assert artifacts.json()["artifacts"][0]["name"] == "report.txt"
        artifact = client.get(artifacts.json()["artifacts"][0]["url"])
        assert artifact.status_code == 200
        assert artifact.text == "artifact contents"

        missing = client.get(f"/api/schedules/{task.id}/runs/missing/output")
        assert missing.status_code == 404


def test_web_schedule_controls_delegate_to_live_scheduler(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import TaskRun

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    class Scheduler:
        def __init__(self):
            self.calls = []

        def health(self):
            return {"status": "online", "active_runs": 0}

        async def run_task_now(self, task_id):
            self.calls.append(("run", task_id))
            return SimpleNamespace(
                run=TaskRun(
                    id="run-now",
                    task_id=task_id,
                    scheduled_for=datetime.now(timezone.utc),
                    started_at=datetime.now(timezone.utc),
                    finished_at=None,
                    status="running",
                )
            )

        async def retry_run(self, task_id, run_id, *, use_latest=False):
            self.calls.append(("retry", task_id, run_id, use_latest))
            return SimpleNamespace(
                run=TaskRun(
                    id="retry-now",
                    task_id=task_id,
                    scheduled_for=datetime.now(timezone.utc),
                    started_at=datetime.now(timezone.utc),
                    finished_at=None,
                    status="running",
                    trigger_source="retry_latest" if use_latest else "retry_snapshot",
                )
            )

        async def cancel_run(self, task_id, run_id):
            self.calls.append(("cancel", task_id, run_id))
            return True

    scheduler = Scheduler()
    channel = _channel()
    channel.bind_runtime({}, {"scheduler_service": scheduler})
    with TestClient(channel.app) as client:
        assert client.get("/api/scheduler/health").json()["status"] == "online"
        assert client.post("/api/schedules/task-1/run").status_code == 202
        retried = client.post(
            "/api/schedules/task-1/runs/run-1/retry", json={"use_latest": True}
        )
        assert retried.status_code == 202
        assert retried.json()["run"]["trigger_source"] == "retry_latest"
        assert client.post("/api/schedules/task-1/runs/run-1/cancel").status_code == 202

    assert scheduler.calls == [
        ("run", "task-1"),
        ("retry", "task-1", "run-1", True),
        ("cancel", "task-1", "run-1"),
    ]


def test_web_bulk_schedule_management_skips_running_tasks(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import DeliveryTarget, NewScheduledTask, SchedulerStore, TriggerSpec

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")
    store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    ids = []
    for name in ("first", "second"):
        ids.append(store.create_task(NewScheduledTask(
            name=name,
            kind="message",
            trigger=TriggerSpec.once(future, "UTC"),
            payload={"message_text": name},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        )).id)
    running = store.claim_task_now(ids[1], lease_seconds=300)
    assert running is not None
    store.close()

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        paused = client.patch(
            "/api/schedules", json={"action": "disable", "task_ids": ids}
        )
        assert paused.status_code == 200
        assert set(paused.json()["completed"]) == set(ids)

        deleted = client.patch(
            "/api/schedules", json={"action": "delete", "task_ids": ids}
        )
        assert deleted.status_code == 200
        assert deleted.json()["completed"] == [ids[0]]
        assert deleted.json()["skipped"] == [{"id": ids[1], "reason": "running"}]


def test_web_bulk_delete_sessions():
    from starlette.testclient import TestClient

    store = _FakeStore({
        "abc123": [_turn("user", "hi")],
        "def456": [_turn("user", "hello")],
    })
    channel = _channel()
    channel.bind_runtime({}, {"context_manager": SimpleNamespace(store=store)})

    with TestClient(channel.app) as client:
        resp = client.request(
            "DELETE",
            "/api/sessions",
            json={"session_ids": ["abc123", "def456", "abc123"]},
        )
        assert resp.status_code == 200
        assert set(resp.json()["deleted"]) == {"abc123", "def456"}
        assert resp.json()["failed"] == []
        assert client.get("/api/sessions").json()["sessions"] == []


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


def test_web_file_rejects_legacy_session_home(tmp_path, monkeypatch):
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

    assert resp.status_code == 403


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
