"""Tests for the web channel HTTP/WebSocket API and SessionService."""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta, timezone
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


_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_DANGEROUS = "mkfs /dev/disk0"


def _shell_confirm_handler_factory(scope):
    """Mirror the shell tool's approval round trip for one dangerous command."""

    async def handler(msg, sink):
        from agent.security.shell import (
            shell_command_check,
            shell_command_confirm,
            shell_pending_reject,
        )

        check = shell_command_check(_DANGEROUS, set(), scope=scope, now=_T0)
        assert check.requires_confirmation
        approved = await sink.on_tool_confirmation(
            "shell",
            command=_DANGEROUS,
            risk_level=check.risk_level,
            reason=check.reason,
            confirmation_token=check.confirmation_token,
            scope=scope,
        )
        # Mirrors agent.tools.builtin_tools._try_interactive_confirmation:
        # a refusal retires the token, an approval redeems it.
        if approved:
            redeemed = shell_command_confirm(
                check.confirmation_token, scope=scope, now=_T0
            )
        else:
            shell_pending_reject(check.confirmation_token, scope=scope)
            redeemed = False
        sink.on_stream_chunk(f"approved={approved} redeemed={redeemed}")
        sink.on_turn_complete("done", [])
        return True

    return handler


def _run_shell_approval(decision_payload):
    """Drive one approval round trip and report whether it outlived the token."""
    from starlette.testclient import TestClient
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_session_allowlist_clear,
        shell_session_allowlist_contains,
    )

    scope = ShellAuthorizationScope("web-approval", "web", "")
    shell_session_allowlist_clear()
    channel = _channel()
    channel.bind_runtime({}, {})
    channel._handler = _shell_confirm_handler_factory(scope)
    try:
        with TestClient(channel.app) as client:
            sid = client.post("/api/sessions").json()["session_id"]
            with client.websocket_connect(f"/api/sessions/{sid}/stream") as ws:
                ws.send_json({"type": "message", "text": "run it"})
                req = ws.receive_json()
                assert req["type"] == "confirm_request"
                payload = {"type": "confirm_response", **decision_payload}
                payload["confirmation_token"] = req["confirmation_token"]
                ws.send_json(payload)
                events = []
                while True:
                    event = ws.receive_json()
                    events.append(event)
                    # Break on error too: a handler that raises otherwise shows
                    # up as a hung read instead of a diagnosable failure.
                    if event["type"] in ("turn_complete", "error"):
                        break
        assert events[-1]["type"] == "turn_complete", events
        decisions = [e["chunk"] for e in events if e["type"] == "stream_chunk"]
        # Two hours past the five-minute pending-token window: only a
        # session-scoped approval is still in force at this point.
        still_allowed = shell_session_allowlist_contains(
            _DANGEROUS, scope=scope, now=_T0 + timedelta(hours=2)
        )
        return req, (decisions[-1] if decisions else ""), still_allowed
    finally:
        shell_session_allowlist_clear()


def test_web_confirm_request_advertises_session_scope():
    req, _summary, _still = _run_shell_approval({"decision": "deny"})
    assert req["allow_session"] is True
    assert req["timeout_seconds"] > 0
    assert req["name"] == "shell"


def test_web_confirm_allow_session_outlives_the_token():
    _req, summary, still_allowed = _run_shell_approval({"decision": "allow_session"})
    assert summary == "approved=True redeemed=True"
    assert still_allowed is True


def test_web_confirm_allow_once_stays_bounded():
    _req, summary, still_allowed = _run_shell_approval({"decision": "allow_once"})
    assert summary == "approved=True redeemed=True"
    assert still_allowed is False


def test_web_confirm_legacy_approved_boolean_still_supported():
    _req, summary, still_allowed = _run_shell_approval({"approved": True})
    assert summary == "approved=True redeemed=True"
    assert still_allowed is False


def test_web_confirm_deny_is_not_redeemable():
    _req, summary, still_allowed = _run_shell_approval({"decision": "deny"})
    assert summary == "approved=False redeemed=False"
    assert still_allowed is False


def test_web_non_shell_prompt_rejects_session_scope():
    """A plugin install prompt must not offer, nor honour, "always allow"."""
    from starlette.testclient import TestClient

    async def handler(msg, sink):
        approved = await sink.on_tool_confirmation(
            "install_plugin",
            command="user plugin 'demo'",
            risk_level="high",
            reason="runs code",
            confirmation_token="",
            scope=None,
        )
        sink.on_stream_chunk("approved" if approved else "denied")
        sink.on_turn_complete("done", [])
        return True

    channel = _channel()
    channel.bind_runtime({}, {})
    channel._handler = handler
    with TestClient(channel.app) as client:
        sid = client.post("/api/sessions").json()["session_id"]
        with client.websocket_connect(f"/api/sessions/{sid}/stream") as ws:
            ws.send_json({"type": "message", "text": "install"})
            req = ws.receive_json()
            assert req["type"] == "confirm_request"
            assert req["allow_session"] is False
            # Even an over-eager client asking for it only ever gets one-off
            # consent, because there is no pending record to widen.
            ws.send_json({"type": "confirm_response", "decision": "allow_session"})
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "turn_complete":
                    break
    assert events[-1]["text"] == "done"
    assert any(
        e.get("chunk") == "approved" for e in events if e["type"] == "stream_chunk"
    )


def test_web_session_approvals_can_be_revoked():
    from starlette.testclient import TestClient
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_session_allowlist_add_persistent,
        shell_session_allowlist_clear,
    )

    channel = _channel()
    channel.bind_runtime({}, {})
    try:
        with TestClient(channel.app) as client:
            listed = client.get("/api/sessions/web-revoke/permissions")
            assert listed.status_code == 200
            assert listed.json()["approved_commands"] == []

            shell_session_allowlist_add_persistent(
                _DANGEROUS, scope=ShellAuthorizationScope("web-revoke", "web", "")
            )
            listed = client.get("/api/sessions/web-revoke/permissions")
            assert listed.json()["approved_commands"] == [_DANGEROUS]

            removed = client.delete("/api/sessions/web-revoke/approvals")
            assert removed.status_code == 200
            assert removed.json()["removed"] == 1
            after = client.get("/api/sessions/web-revoke/permissions")
            assert after.json()["approved_commands"] == []
    finally:
        shell_session_allowlist_clear()


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


def test_web_files_endpoint_requires_an_owning_session(tmp_path):
    """A file link is a capability: without a named owner it must be refused.

    Even a real file inside the agent home is off limits when the request does
    not name the session (or scheduled run) that owns it.
    """
    from starlette.testclient import TestClient
    from agent import shared

    channel = _channel()
    channel.bind_runtime({}, {})

    stray = shared.AGENT_HOME / "web-test-note.txt"
    stray.write_text("hello from agent home", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    try:
        with TestClient(channel.app) as client:
            assert client.get(f"/api/files?path={stray}").status_code == 403
            assert client.get(f"/api/files?path={outside}").status_code == 403
            # An unknown session owns nothing, so it unlocks nothing.
            resp = client.get(f"/api/files?path={stray}&session_id=no-such-session")
            assert resp.status_code == 403
    finally:
        stray.unlink(missing_ok=True)


def test_web_files_endpoint_serves_session_workspace_files(tmp_path):
    """An attachment written into the project workspace must be renderable.

    Regression: ``send_file`` accepts anything inside the session workspace, but
    ``/api/files`` only allowed the agent home. The agent could therefore send an
    image that the browser was then refused with "forbidden path", so
    attachments from the workspace never displayed while output-dir ones did.

    The link must name the owning session, and another session's id must not
    unlock this workspace.
    """
    import json as _json

    from starlette.testclient import TestClient
    from agent import shared

    channel = _channel()
    channel.bind_runtime({}, {})

    session_id = "workspace-files"
    workspace = tmp_path / "project"
    workspace.mkdir()
    picture = workspace / "chart.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n")

    other_id = "other-session"
    other_workspace = tmp_path / "other-project"
    other_workspace.mkdir()

    home = shared.web_session_home(session_id)
    other_home = shared.web_session_home(other_id)
    for sid, root in ((session_id, workspace), (other_id, other_workspace)):
        target = shared.web_session_home(sid)
        target.mkdir(parents=True, exist_ok=True)
        (target / ".session.json").write_text(
            _json.dumps({"session_id": sid, "workspace_root": str(root)}),
            encoding="utf-8",
        )

    try:
        with TestClient(channel.app) as client:
            resp = client.get(f"/api/files?path={picture}&session_id={session_id}")
            assert resp.status_code == 200
            assert resp.content == b"\x89PNG\r\n\x1a\n"

            # No owner named -> refused even though the file is real.
            assert client.get(f"/api/files?path={picture}").status_code == 403

            # A different session's id does not unlock this workspace.
            cross = client.get(f"/api/files?path={picture}&session_id={other_id}")
            assert cross.status_code == 403

            # A directory that no session ever selected stays off limits.
            stranger = tmp_path / "stranger"
            stranger.mkdir()
            (stranger / "secret.png").write_bytes(b"nope")
            denied = client.get(
                f"/api/files?path={stranger / 'secret.png'}&session_id={session_id}"
            )
            assert denied.status_code == 403
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(other_home, ignore_errors=True)


def test_web_files_endpoint_serves_session_home_files():
    """Files inside a session's own home (output/, uploads/) are servable."""
    from starlette.testclient import TestClient
    from agent import shared

    channel = _channel()
    channel.bind_runtime({}, {})

    session_id = "home-files"
    home = shared.web_session_home(session_id)
    out = home / "output" / "note.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("session output", encoding="utf-8")

    try:
        with TestClient(channel.app) as client:
            resp = client.get(f"/api/files?path={out}&session_id={session_id}")
            assert resp.status_code == 200
            assert resp.text == "session output"
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_web_files_endpoint_serves_live_session_workspace(tmp_path):
    """A live session's workspace is honoured without a manifest on disk."""
    from starlette.testclient import TestClient

    workspace = tmp_path / "live-project"
    workspace.mkdir()
    (workspace / "figure.png").write_bytes(b"live")

    class _Ctx:
        metadata = {"workspace_root": str(workspace)}

    class _State:
        ctx = _Ctx()

    channel = _channel()
    channel.bind_runtime({"live-session": _State()}, {})

    with TestClient(channel.app) as client:
        resp = client.get(
            f"/api/files?path={workspace / 'figure.png'}&session_id=live-session"
        )
        assert resp.status_code == 200
        assert resp.content == b"live"

        # Without naming the session there is no entitlement to check.
        assert (
            client.get(f"/api/files?path={workspace / 'figure.png'}").status_code == 403
        )


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


def test_web_output_sink_serialises_subagent_progress_fields():
    """Sub-agent updates must carry the fields the event actually holds.

    Regression: the sink read ``agent`` / ``event`` / ``detail``, none of which
    exist on ``SubAgentProgressEvent`` (it carries ``kind`` / ``role`` /
    ``message`` / ``completed`` / ``total``). Every lookup returned an empty
    string, so the browser received unlabelled rows and the transcript filled
    with identical "子代理 · 状态更新" placeholders.
    """
    from agent.channels.web import WebOutputSink
    from agent.core import SubAgentProgressEvent

    sink = WebOutputSink(collect=True)
    sink.on_subagent_event(
        SubAgentProgressEvent(
            kind="batch_progress",
            role="researcher",
            message="Sub-agents running: 2/4 completed",
            completed=2,
            total=4,
        )
    )

    event = sink.events[-1]
    assert event["type"] == "subagent_event"
    assert event["kind"] == "batch_progress"
    assert event["role"] == "researcher"
    assert event["message"] == "Sub-agents running: 2/4 completed"
    assert event["completed"] == 2
    assert event["total"] == 4


def test_web_output_sink_subagent_event_tolerates_sparse_payload():
    """A minimal event must still serialise, with zeroed counters."""
    from agent.channels.web import WebOutputSink
    from agent.core import SubAgentProgressEvent

    sink = WebOutputSink(collect=True)
    sink.on_subagent_event(SubAgentProgressEvent(kind="agent_started"))

    event = sink.events[-1]
    assert event["kind"] == "agent_started"
    assert event["role"] == ""
    assert event["message"] == ""
    assert event["completed"] == 0
    assert event["total"] == 0


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

    assert WebChannel._resolve_model_override("global-alt") == "global-alt"
    assert WebChannel._resolve_model_override("global-model") == "global-model"


def test_web_model_override_accepts_other_providers_models(tmp_path, monkeypatch):
    """The dropdown offers every provider's models and routing dispatches on
    the id, so a foreign provider's model is a valid per-turn override."""
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
                "active_provider": "anthropic",
                "providers": {
                    "anthropic": {
                        "default_model": "claude-x",
                        "models": ["claude-x"],
                    },
                    "openai": {
                        "default_model": "gpt-y",
                        "models": ["gpt-y"],
                    },
                },
            },
            False,
        ),
    )

    assert WebChannel._resolve_model_override("gpt-y") == "gpt-y"
    assert WebChannel._resolve_model_override("claude-x") == "claude-x"
    # Unknown ids are still rejected, and no override resolves to None.
    try:
        WebChannel._resolve_model_override("not-a-model")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown model must be rejected")
    assert WebChannel._resolve_model_override(None) is None
    assert WebChannel._resolve_model_override("  ") is None


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

        # The output link names its owning run, and only that entitlement
        # unlocks it — the bare path, or a forged foreign path, is refused.
        output_url = output.json()["output_url"]
        assert "task_id=" in output_url and "run_id=" in output_url
        raw = client.get(output_url)
        assert raw.status_code == 200
        assert raw.text.startswith("# Daily report")
        bare = client.get(f"/api/files?path={output_path}")
        assert bare.status_code == 403
        # A real file outside the run's recorded output, addressed with the
        # run's ids, is still refused: the entitlement is checked against the
        # store, not against the caller's path.
        secret = agent_home / "secret.txt"
        secret.write_text("not for the browser", encoding="utf-8")
        forged = client.get(
            f"/api/files?path={secret}&task_id={task.id}&run_id={claimed.run.id}"
        )
        assert forged.status_code == 403

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


def _seed_failed_run(db_path, name: str, scheduled_for):
    """Create one task with one failed run already finished."""
    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        SchedulerStore,
        TriggerSpec,
    )

    store = SchedulerStore(db_path=db_path)
    task = store.create_task(
        NewScheduledTask(
            name=name,
            kind="agent_prompt",
            trigger=TriggerSpec.once(scheduled_for, "Asia/Shanghai"),
            payload={"prompt": f"run {name}"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
            permission_profile="read_only",
        ),
        now=scheduled_for - timedelta(hours=1),
    )
    claimed = store.claim_due_tasks(now=scheduled_for + timedelta(seconds=2), lease_seconds=300)[0]
    store.complete_run(
        task.id,
        claimed.run.id,
        finished_at=scheduled_for + timedelta(seconds=5),
        status="failed",
        summary="",
        error="boom",
    )
    store.close()
    return task.id, claimed.run.id


def test_web_schedule_failure_is_announced_and_can_be_acknowledged(tmp_path, monkeypatch):
    """A failure nobody watched has to reach the user, then be dismissable.

    The badge is the whole point of this endpoint pair: the scheduler runs
    when no client is connected, so the count has to be readable from any view
    and the acknowledgement has to stick, otherwise the same failure would
    reappear on every poll.
    """
    from starlette.testclient import TestClient
    from agent import shared

    agent_home = tmp_path / ".agent"
    db_path = agent_home / "tasks" / "scheduler.db"
    monkeypatch.setattr(shared, "AGENT_HOME", agent_home)
    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", db_path)
    task_id, run_id = _seed_failed_run(
        db_path, "nightly", datetime(2026, 9, 7, 2, 0, tzinfo=timezone.utc)
    )

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        listed = client.get("/api/schedules")
        assert listed.status_code == 200
        assert listed.json()["unseen_attention"] == 1
        assert listed.json()["tasks"][0]["unseen_attention"] == 1

        # Readable without loading every task, which is what lets the badge be
        # polled from a view that is not the schedules page.
        assert client.get("/api/schedules/attention").json() == {"unseen_attention": 1}

        run = client.get(f"/api/schedules/{task_id}/runs").json()["runs"][0]
        assert run["id"] == run_id
        assert run["needs_attention"] is True
        assert run["acknowledged_at"] is None

        acked = client.post(f"/api/schedules/{task_id}/runs/{run_id}/acknowledge")
        assert acked.status_code == 200
        assert acked.json() == {
            "ok": True,
            "acknowledged": True,
            "unseen_attention": 0,
        }

        # Clicking again is honest rather than pretending to do work.
        repeat = client.post(f"/api/schedules/{task_id}/runs/{run_id}/acknowledge")
        assert repeat.json()["acknowledged"] is False

        assert client.get("/api/schedules/attention").json() == {"unseen_attention": 0}
        assert client.get("/api/schedules").json()["unseen_attention"] == 0
        after = client.get(f"/api/schedules/{task_id}/runs").json()["runs"][0]
        assert after["needs_attention"] is False
        assert after["acknowledged_at"] is not None


def test_web_schedule_attention_clears_one_task_or_all(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from agent import shared

    agent_home = tmp_path / ".agent"
    db_path = agent_home / "tasks" / "scheduler.db"
    monkeypatch.setattr(shared, "AGENT_HOME", agent_home)
    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", db_path)
    first, _ = _seed_failed_run(
        db_path, "first", datetime(2026, 9, 7, 2, 0, tzinfo=timezone.utc)
    )
    _seed_failed_run(db_path, "second", datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc))

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        assert client.get("/api/schedules/attention").json() == {"unseen_attention": 2}

        scoped = client.post("/api/schedules/attention", json={"task_id": first})
        assert scoped.status_code == 200
        assert scoped.json() == {"ok": True, "cleared": 1, "unseen_attention": 1}

        everything = client.post("/api/schedules/attention", json={})
        assert everything.json() == {"ok": True, "cleared": 1, "unseen_attention": 0}

        # Nothing left to clear, and it says so instead of claiming work.
        assert client.post("/api/schedules/attention").json()["cleared"] == 0


def test_web_schedule_attention_rejects_a_non_object_body(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from agent import shared

    agent_home = tmp_path / ".agent"
    monkeypatch.setattr(shared, "AGENT_HOME", agent_home)
    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", agent_home / "tasks" / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        bad = client.post("/api/schedules/attention", json=["not", "an", "object"])
        assert bad.status_code == 400


def _seed_skipped_run(db_path, name: str, first_due):
    """Create one daily task whose first run resumes a three-day gap."""
    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        SchedulerStore,
        TriggerSpec,
    )

    store = SchedulerStore(db_path=db_path)
    task = store.create_task(
        NewScheduledTask(
            name=name,
            kind="agent_prompt",
            trigger=TriggerSpec.daily("09:00", "UTC"),
            payload={"prompt": f"run {name}"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
            permission_profile="read_only",
            workspace_root="/tmp",
        ),
        now=first_due - timedelta(hours=1),
    )
    claimed = store.claim_due_tasks(
        now=first_due + timedelta(days=3, hours=3), lease_seconds=300
    )[0]
    store.complete_run(
        task.id,
        claimed.run.id,
        finished_at=first_due + timedelta(days=3, hours=3),
        status="succeeded",
        summary="日报已生成",
    )
    store.close()
    return task.id, claimed.run.id


def test_web_reports_a_run_that_succeeded_over_a_skipped_schedule(tmp_path, monkeypatch):
    """A gap has to reach the user even though every run succeeded.

    This is the case the interface used to be blind to: the schedule silently
    caught up, the run reports success, and nothing anywhere says the daily
    report did not happen for three days.  The count and the marker have to
    survive the API round trip, because that is where the badge reads them.
    """
    from starlette.testclient import TestClient
    from agent import shared

    agent_home = tmp_path / ".agent"
    db_path = agent_home / "tasks" / "scheduler.db"
    monkeypatch.setattr(shared, "AGENT_HOME", agent_home)
    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", db_path)
    task_id, run_id = _seed_skipped_run(
        db_path, "daily-report", datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)
    )

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        run = client.get(f"/api/schedules/{task_id}/runs").json()["runs"][0]
        assert run["id"] == run_id
        assert run["status"] == "succeeded"
        assert run["missed_count"] == 3
        assert run["needs_attention"] is True
        # The wording is composed where the run finishes, not here; this test
        # owns the wire, and the note itself is pinned end to end in
        # tests/test_scheduler_missed.py.

        # The task badge has to agree with the run it is pointing at.
        assert client.get("/api/schedules").json()["tasks"][0]["unseen_attention"] == 1

        acked = client.post(f"/api/schedules/{task_id}/runs/{run_id}/acknowledge")
        assert acked.json()["acknowledged"] is True
        assert client.get("/api/schedules/attention").json() == {"unseen_attention": 0}


def test_web_create_signal_schedule(tmp_path, monkeypatch):
    """A signal trigger is creatable from the interface, and stored as one."""
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        SchedulerStore,
        TriggerSpec,
        task_signal_name,
    )

    db_path = tmp_path / "scheduler.db"
    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", db_path)
    channel = _channel()
    channel.bind_runtime({}, {})

    store = SchedulerStore(db_path=db_path)
    try:
        emitter = store.create_task(
            NewScheduledTask(
                name="emitter",
                kind="agent_prompt",
                trigger=TriggerSpec.daily("09:00", "UTC"),
                payload={"prompt": "先跑这个"},
                delivery_mode="standalone",
                delivery_target=DeliveryTarget.standalone(),
            )
        )
        signal = task_signal_name(emitter.id, "succeeded")
    finally:
        store.close()

    with TestClient(channel.app) as client:
        created = client.post(
            "/api/schedules",
            json={
                "name": "follower",
                "action_type": "agent_task",
                "prompt": "再跑这个",
                "trigger_type": "signal",
                "signal_name": signal,
                "timezone_name": "UTC",
            },
        )
        assert created.status_code == 200, created.json()
        task = created.json()["task"]
        assert task["trigger_type"] == "signal"
        assert task["trigger"]["name"] == signal
        # No calendar, so the interface has to be told that "no next run" here
        # means "waiting", not "finished".
        assert task["next_run_at"] is None


def test_web_signal_schedule_needs_a_name_and_a_real_task(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")
    channel = _channel()
    channel.bind_runtime({}, {})

    with TestClient(channel.app) as client:
        missing = client.post(
            "/api/schedules",
            json={
                "name": "follower",
                "action_type": "agent_task",
                "prompt": "再跑这个",
                "trigger_type": "signal",
                "signal_name": "   ",
                "timezone_name": "UTC",
            },
        )
        assert missing.status_code == 400
        assert "信号" in missing.json()["error"]

        bogus = client.post(
            "/api/schedules",
            json={
                "name": "follower",
                "action_type": "agent_task",
                "prompt": "再跑这个",
                "trigger_type": "signal",
                "signal_name": "task:nope:succeeded",
                "timezone_name": "UTC",
            },
        )
        assert bogus.status_code == 400
        assert "nope" in bogus.json()["error"]


def test_web_signals_endpoint_offers_names_and_shows_who_is_waiting(
    tmp_path, monkeypatch
):
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import SchedulerStore

    db_path = tmp_path / "scheduler.db"
    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", db_path)
    channel = _channel()
    channel.bind_runtime({}, {})

    store = SchedulerStore(db_path=db_path)
    try:
        store.emit_signal("report.ready", source="agent")
    finally:
        store.close()

    with TestClient(channel.app) as client:
        client.post(
            "/api/schedules",
            json={
                "name": "follower",
                "action_type": "agent_task",
                "prompt": "等报告就绪",
                "trigger_type": "signal",
                "signal_name": "report.ready",
                "timezone_name": "UTC",
            },
        )

        payload = client.get("/api/signals").json()

    emitted = {item["name"]: item for item in payload["signals"]}
    assert emitted["report.ready"]["subscriber_count"] == 1
    assert emitted["report.ready"]["source"] == "custom"
    # Nothing emitted yet, but the subscription exists -- hiding it would make
    # the picker disagree with what the scheduler will actually do.
    assert payload["waiting"] == []


def _workflow_body() -> dict:
    return {
        "name": "夜间报告",
        "description": "每天采集、分析、发布",
        "steps": [
            {
                "key": "collect",
                "name": "采集",
                "kind": "agent_prompt",
                "payload": {"prompt": "采集数据"},
                "trigger_type": "daily",
                "time_of_day": "02:00",
                "timezone_name": "Asia/Shanghai",
            },
            {
                "key": "analyze",
                "name": "分析",
                "kind": "agent_prompt",
                "payload": {"prompt": "分析数据"},
                "depends_on": ["collect"],
            },
            {
                "key": "publish",
                "name": "发布",
                "kind": "message",
                "payload": {"message_text": "报告已生成"},
                "depends_on": ["analyze"],
            },
        ],
    }


def test_web_workflow_round_trip(tmp_path, monkeypatch):
    """Create, list, and see the tasks it built in the ordinary schedule list."""
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post("/api/workflows", json=_workflow_body())
        assert created.status_code == 200, created.text
        workflow = created.json()["workflow"]
        assert [item["key"] for item in workflow["steps"]] == [
            "collect",
            "analyze",
            "publish",
        ]
        # Each step names its upstreams by key and carries the task that will
        # actually run, because everything else in the interface -- history,
        # cancel, output -- is keyed by that id.
        assert workflow["steps"][1]["depends_on"] == ["collect"]
        assert all(item["task_id"] for item in workflow["steps"])
        assert workflow["steps"][0]["trigger_type"] == "daily"
        assert workflow["steps"][1]["trigger_type"] == "signal"

        listed = client.get("/api/workflows")
        assert listed.status_code == 200
        payload = listed.json()
        assert [item["id"] for item in payload["workflows"]] == [workflow["id"]]
        assert payload["permission_profiles"]

        schedules = client.get("/api/schedules").json()["tasks"]
        placed = {item["step_key"]: item for item in schedules if item["workflow_id"]}
        assert set(placed) == {"collect", "analyze", "publish"}
        assert all(
            item["workflow_id"] == workflow["id"] for item in placed.values()
        )
        # A step is a task like any other, so it appears in the list the page
        # already renders instead of only inside the workflow view.
        assert len(schedules) == 3


def test_web_workflow_reports_each_steps_latest_state(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import SchedulerStore

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post("/api/workflows", json=_workflow_body()).json()["workflow"]
        by_key = {item["key"]: item["task_id"] for item in created["steps"]}

        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            claimed = store.claim_task_now(by_key["collect"], lease_seconds=300)
            assert claimed is not None
            store.complete_run(
                by_key["collect"],
                claimed.run.id,
                finished_at=datetime.now(timezone.utc),
                status="succeeded",
                summary="采集完成",
            )
        finally:
            store.close()

        listed = client.get("/api/workflows").json()["workflows"][0]
        steps = {item["key"]: item for item in listed["steps"]}
        assert steps["collect"]["latest_run"]["status"] == "succeeded"
        assert steps["collect"]["latest_run"]["summary"] == "采集完成"
        assert steps["analyze"]["latest_run"] is None


def test_web_refuses_a_cyclic_workflow_and_says_which_steps(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        refused = client.post(
            "/api/workflows",
            json={
                "name": "环",
                "steps": [
                    {
                        "key": "a",
                        "kind": "agent_prompt",
                        "payload": {"prompt": "a"},
                        "depends_on": ["b"],
                    },
                    {
                        "key": "b",
                        "kind": "agent_prompt",
                        "payload": {"prompt": "b"},
                        "depends_on": ["a"],
                    },
                ],
            },
        )
        assert refused.status_code == 400
        assert "循环依赖" in refused.json()["error"]
        # Refused, not half-built: a rejected graph leaves nothing behind.
        assert client.get("/api/workflows").json()["workflows"] == []
        assert client.get("/api/schedules").json()["tasks"] == []


def test_web_refuses_the_two_ways_a_steps_timing_can_contradict_itself(
    tmp_path, monkeypatch
):
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        no_trigger = client.post(
            "/api/workflows",
            json={
                "name": "无触发",
                "steps": [
                    {"key": "solo", "kind": "agent_prompt", "payload": {"prompt": "x"}}
                ],
            },
        )
        assert no_trigger.status_code == 400
        assert "必须指定触发方式" in no_trigger.json()["error"]

        both = client.post(
            "/api/workflows",
            json={
                "name": "两个答案",
                "steps": [
                    {
                        "key": "collect",
                        "kind": "agent_prompt",
                        "payload": {"prompt": "x"},
                        "trigger_type": "daily",
                        "time_of_day": "02:00",
                    },
                    {
                        "key": "analyze",
                        "kind": "agent_prompt",
                        "payload": {"prompt": "y"},
                        "depends_on": ["collect"],
                        "trigger_type": "daily",
                        "time_of_day": "03:00",
                    },
                ],
            },
        )
        assert both.status_code == 400
        assert "不能另外再指定" in both.json()["error"]


def test_web_editing_a_workflow_keeps_the_task_ids(tmp_path, monkeypatch):
    """The ids are what the downstream subscriptions point at."""
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post("/api/workflows", json=_workflow_body()).json()["workflow"]
        before = {item["key"]: item["task_id"] for item in created["steps"]}

        edited = _workflow_body()
        edited["steps"].insert(
            2,
            {
                "key": "review",
                "name": "复核",
                "kind": "agent_prompt",
                "payload": {"prompt": "复核"},
                "depends_on": ["analyze"],
            },
        )
        edited["steps"][3]["depends_on"] = ["review"]
        updated = client.put(
            f"/api/workflows/{created['id']}", json=edited
        )
        assert updated.status_code == 200, updated.text
        after = {
            item["key"]: item["task_id"] for item in updated.json()["workflow"]["steps"]
        }
        assert after["collect"] == before["collect"]
        assert after["analyze"] == before["analyze"]
        assert after["publish"] == before["publish"]
        assert after["review"] and after["review"] not in before.values()


def test_web_deleting_a_workflow_disables_its_steps(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import SchedulerStore

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post("/api/workflows", json=_workflow_body()).json()["workflow"]
        task_ids = [item["task_id"] for item in created["steps"]]

        deleted = client.delete(f"/api/workflows/{created['id']}")
        assert deleted.status_code == 200
        assert sorted(deleted.json()["disabled_task_ids"]) == sorted(task_ids)
        assert client.get("/api/workflows").json()["workflows"] == []

        # The tasks stay, disabled: deleting a workflow stops it, it does not
        # erase the record of what it ran.
        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            assert len(store.list_tasks()) == 3
            assert all(not store.get_task(item).enabled for item in task_ids)
        finally:
            store.close()

        assert client.delete(f"/api/workflows/{created['id']}").status_code == 404


def test_web_refuses_to_delete_a_workflow_that_is_mid_run(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import SchedulerStore

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post("/api/workflows", json=_workflow_body()).json()["workflow"]
        first = created["steps"][0]["task_id"]

        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            assert store.claim_task_now(first, lease_seconds=300) is not None
        finally:
            store.close()

        refused = client.delete(f"/api/workflows/{created['id']}")
        assert refused.status_code == 409
        assert "正在运行" in refused.json()["error"]
        # Still there, because the refusal is the point.
        assert len(client.get("/api/workflows").json()["workflows"]) == 1


def test_web_editing_a_workflow_keeps_the_entry_steps_trigger(tmp_path, monkeypatch):
    """Omitting the trigger keeps it: editing the graph must not move the clock.

    The graph editor never shows the entry step's schedule, so it must not be
    able to overwrite one -- least of all one the user set in the task editor.
    """
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import SchedulerStore

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post("/api/workflows", json=_workflow_body()).json()["workflow"]
        collect = next(item for item in created["steps"] if item["key"] == "collect")
        assert collect["trigger"]["time_of_day"] == "02:00"

        edited = _workflow_body()
        for step in edited["steps"]:
            # No trigger fields at all on the entry step, exactly as the graph
            # editor sends it back.
            step.pop("trigger_type", None)
            step.pop("time_of_day", None)
            step.pop("timezone_name", None)
        edited["steps"][1]["payload"]["prompt"] = "分析这些数据"

        updated = client.put(f"/api/workflows/{created['id']}", json=edited)
        assert updated.status_code == 200, updated.text
        after = {
            item["key"]: item for item in updated.json()["workflow"]["steps"]
        }
        assert after["collect"]["trigger"]["time_of_day"] == "02:00"
        assert (
            after["collect"]["trigger"]["timezone_name"] == "Asia/Shanghai"
        )
        assert after["collect"]["task_id"] == collect["task_id"]

        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            task = store.get_task(collect["task_id"])
            assert task.trigger.payload["time_of_day"] == "02:00"
        finally:
            store.close()


def test_web_a_new_entry_step_without_a_trigger_is_still_refused(
    tmp_path, monkeypatch
):
    """Omitting is only allowed when there is something to keep."""
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post("/api/workflows", json=_workflow_body()).json()["workflow"]
        edited = _workflow_body()
        edited["steps"].append(
            {
                "key": "extra",
                "kind": "agent_prompt",
                "payload": {"prompt": "另一个起点"},
            }
        )
        refused = client.put(f"/api/workflows/{created['id']}", json=edited)
        assert refused.status_code == 400
        assert "必须指定触发方式" in refused.json()["error"]


def test_web_editing_a_workflow_keeps_the_fields_the_editor_never_shows(
    tmp_path, monkeypatch
):
    """A graph editor edits the graph; it cannot erase a model by not asking.

    The step body has more fields than the editor has controls -- a model, a
    skill list, a timeout.  If leaving one out meant "reset it", then the only
    way to change an edge would be to first reproduce every other setting, and
    anyone who forgot one would lose it without being told.
    """
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import SchedulerStore

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        body = _workflow_body()
        body["steps"][1].update(
            {
                "model_override": "deepseek-v4",
                "selected_skills": ["pdf"],
                "timeout_seconds": 900,
                "context_policy": "task_history",
                "permission_profile": "inherit",
            }
        )
        created = client.post("/api/workflows", json=body).json()["workflow"]

        # What the graph editor sends: the graph, and nothing else.
        edited = {
            "name": "夜间报告",
            "description": "每天采集、分析、发布",
            "steps": [
                {
                    "key": item["key"],
                    "name": item["name"],
                    "kind": item["kind"],
                    "payload": item["payload"],
                    "depends_on": item["depends_on"],
                }
                for item in created["steps"]
            ],
        }
        edited["steps"][0]["trigger_type"] = "daily"
        edited["steps"][0]["time_of_day"] = "02:00"
        edited["steps"][0]["timezone_name"] = "Asia/Shanghai"
        # The one field the editor does own, changed.
        edited["steps"][1]["name"] = "分析（改名不改设置）"

        updated = client.put(f"/api/workflows/{created['id']}", json=edited)
        assert updated.status_code == 200, updated.text
        after = {
            item["key"]: item for item in updated.json()["workflow"]["steps"]
        }
        assert after["analyze"]["name"] == "分析（改名不改设置）"
        assert after["analyze"]["permission_profile"] == "inherit"

        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            task = store.get_task(after["analyze"]["task_id"])
            assert task.model_override == "deepseek-v4"
            assert task.selected_skills == ["pdf"]
            assert task.timeout_seconds == 900
            assert task.context_policy == "task_history"
        finally:
            store.close()


def test_web_a_field_sent_empty_is_cleared_not_kept(tmp_path, monkeypatch):
    """The other half of the rule: present means what it says.

    "Omitted keeps" would be a trap if it also swallowed an explicit empty
    value -- there would then be no way to remove a step's model at all.
    """
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import SchedulerStore

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        body = _workflow_body()
        body["steps"][1].update(
            {"model_override": "deepseek-v4", "selected_skills": ["pdf"]}
        )
        created = client.post("/api/workflows", json=body).json()["workflow"]

        edited = {"name": "夜间报告", "steps": []}
        for item in created["steps"]:
            step = {
                "key": item["key"],
                "name": item["name"],
                "kind": item["kind"],
                "payload": item["payload"],
                "depends_on": item["depends_on"],
            }
            if item["key"] == "analyze":
                step["model_override"] = ""
                step["selected_skills"] = []
            edited["steps"].append(step)
        edited["steps"][0].update(
            {
                "trigger_type": "daily",
                "time_of_day": "02:00",
                "timezone_name": "Asia/Shanghai",
            }
        )

        updated = client.put(f"/api/workflows/{created['id']}", json=edited)
        assert updated.status_code == 200, updated.text
        after = {
            item["key"]: item for item in updated.json()["workflow"]["steps"]
        }
        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            task = store.get_task(after["analyze"]["task_id"])
            assert task.model_override in (None, "")
            assert task.selected_skills == []
        finally:
            store.close()
