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


def test_session_service_reports_what_a_live_session_is_doing():
    """The list is where a busy session says so, so the status follows the
    runtime's own words: a turn running, one being stopped, and -- the case
    ``operation_state`` alone cannot say -- a session whose turn is over but
    which is still holding messages in its restart queue.

    That last one is not idle: the visitor's second message was accepted and
    will run, and a badge saying "排队中" is what tells them it was taken
    rather than lost.
    """
    live = {
        "running": SimpleNamespace(
            turn_count=1, operation_state="active", restart_queue=[]
        ),
        "stopping": SimpleNamespace(
            turn_count=1, operation_state="cancelling", restart_queue=[]
        ),
        "waiting": SimpleNamespace(
            turn_count=1, operation_state="idle", restart_queue=[object()]
        ),
        "idle": SimpleNamespace(
            turn_count=1, operation_state="idle", restart_queue=[]
        ),
    }
    service = SessionService(live_states=live)

    sessions = {item["session_id"]: item for item in service.list_sessions()}

    assert sessions["running"]["status"] == "active"
    assert sessions["stopping"]["status"] == "cancelling"
    assert sessions["waiting"]["status"] == "queued"
    # The unremarkable case stays unremarkable: an idle session with nothing
    # queued reads as idle, and no badge is drawn for it.
    assert sessions["idle"]["status"] == "idle"


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
        pending_interjections=[{"text": "urgent", "message_id": "msg-1"}],
        restart_queue=[
            {"text": "follow-up", "message_id": "msg-2"},
            {"text": "another", "message_id": "msg-3"},
        ],
    )
    state = SessionService(store=Store(), live_states={"s-1": live}).get_session_state("s-1")

    assert state["operation_state"] == "active"
    assert state["queue"]["pending"] == 3
    assert state["queue"]["interjections"] == 1
    assert state["queue"]["restarts"] == 2
    assert [item["id"] for item in state["queue"]["items"]] == [
        "msg-1",
        "msg-2",
        "msg-3",
    ]
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
        # The reason is the refusal itself, in the words the single-task
        # endpoint would have used: one rule, so no second wording to drift.
        skipped = deleted.json()["skipped"]
        assert [item["id"] for item in skipped] == [ids[1]]
        assert "正在运行" in skipped[0]["reason"]


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
        # polled from a view that is not the schedules page -- and the rows come
        # with it, because a count with nothing to click on is a count nobody
        # can check.
        polled = client.get("/api/schedules/attention").json()
        assert polled["unseen_attention"] == 1
        assert [item["run_id"] for item in polled["attention_runs"]] == [run_id]

        run = client.get(f"/api/schedules/{task_id}/runs").json()["runs"][0]
        assert run["id"] == run_id
        assert run["needs_attention"] is True
        assert run["acknowledged_at"] is None

        acked = client.post(f"/api/schedules/{task_id}/runs/{run_id}/acknowledge")
        assert acked.status_code == 200
        assert acked.json()["acknowledged"] is True
        # The list the page is showing is corrected in the same answer, so the
        # row disappears without a second round trip.
        assert acked.json()["unseen_attention"] == 0
        assert acked.json()["attention_runs"] == []

        # Clicking again is honest rather than pretending to do work.
        repeat = client.post(f"/api/schedules/{task_id}/runs/{run_id}/acknowledge")
        assert repeat.json()["acknowledged"] is False

        assert client.get("/api/schedules/attention").json()["unseen_attention"] == 0
        assert client.get("/api/schedules/attention").json()["attention_runs"] == []
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
        attention = client.get("/api/schedules/attention").json()
        assert attention["unseen_attention"] == 2
        # One row per run, so clearing one task leaves exactly one behind --
        # the number and the list move together.
        assert len(attention["attention_runs"]) == 2

        scoped = client.post("/api/schedules/attention", json={"task_id": first})
        assert scoped.status_code == 200
        remaining = [
            item for item in attention["attention_runs"] if item["task_id"] != first
        ]
        assert scoped.json() == {
            "ok": True,
            "cleared": 1,
            "unseen_attention": 1,
            "attention_runs": remaining,
            # The map the cards click through, from the same snapshot: it
            # points at the one run still waiting, and no longer names the
            # task that was just cleared.
            "latest_run_by_task": {remaining[0]["task_id"]: remaining[0]["run_id"]},
        }

        everything = client.post("/api/schedules/attention", json={})
        assert everything.json() == {
            "ok": True,
            "cleared": 1,
            "unseen_attention": 0,
            "attention_runs": [],
            "latest_run_by_task": {},
        }

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
        listed = client.get("/api/schedules").json()
        assert listed["tasks"][0]["unseen_attention"] == 1
        # And the row behind the badge has to say why, since the run succeeded
        # and nothing else on the screen mentions the gap.
        assert listed["attention_runs"][0]["missed_count"] == 3
        assert listed["attention_runs"][0]["status"] == "succeeded"

        acked = client.post(f"/api/schedules/{task_id}/runs/{run_id}/acknowledge")
        assert acked.json()["acknowledged"] is True
        assert client.get("/api/schedules/attention").json() == {
            "unseen_attention": 0,
            "attention_runs": [],
            "latest_run_by_task": {},
        }


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


def _a_routable_model() -> str:
    """A step model the workflow endpoint will accept.

    A step's model is validated against the configured provider groups, so
    the test asks the same config the endpoint asks rather than hardcoding an
    id that only exists on one machine.
    """
    from agent.config import load_config
    from agent.core.transport import routable_model_ids

    ids = sorted(routable_model_ids(load_config()[0]))
    assert ids, "no configured models to choose from"
    return ids[0]


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


def _chain_with_its_first_two_steps_exchanged() -> dict:
    """The editor's request after somebody swaps the first two steps.

    Nothing here mentions a time, a timezone or a signal: an editor that can
    only draw "每天 02:00" cannot send it back without guessing, so the swap
    names the step the schedule comes from instead.  `publish` is re-pointed
    onto `collect` because that is the step it now follows.
    """
    return {
        "name": "夜间报告",
        "description": "每天采集、分析、发布",
        "steps": [
            {
                "key": "analyze",
                "name": "分析",
                "kind": "agent_prompt",
                "payload": {"prompt": "分析数据"},
                "depends_on": [],
                "trigger_from": "collect",
            },
            {
                "key": "collect",
                "name": "采集",
                "kind": "agent_prompt",
                "payload": {"prompt": "采集数据"},
                "depends_on": ["analyze"],
            },
            {
                "key": "publish",
                "name": "发布",
                "kind": "message",
                "payload": {"message_text": "报告已生成"},
                "depends_on": ["collect"],
            },
        ],
    }


def test_web_swapping_two_steps_moves_the_schedule_without_rebuilding_tasks(
    tmp_path, monkeypatch
):
    """Reordering a chain moves the clock and keeps every task it already had.

    The schedule is the *chain's*: whoever is first is the task that fires, so
    changing which step is first has to move the clock to it.  The steps
    themselves keep their keys, and a step's key is what its task is found by,
    so no task is rebuilt and no run history is disturbed.
    """
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import SchedulerStore

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post("/api/workflows", json=_workflow_body()).json()["workflow"]
        before = {item["key"]: item for item in created["steps"]}
        assert before["collect"]["trigger"]["time_of_day"] == "02:00"

        updated = client.put(
            f"/api/workflows/{created['id']}",
            json=_chain_with_its_first_two_steps_exchanged(),
        )
        assert updated.status_code == 200, updated.text
        after = {
            item["key"]: item for item in updated.json()["workflow"]["steps"]
        }

        # The clock travelled, whole: the request never said 02:00 or which
        # timezone it was in, so this can only have come from the stored spec.
        assert after["analyze"]["trigger_type"] == "daily"
        assert after["analyze"]["trigger"]["time_of_day"] == "02:00"
        assert after["analyze"]["trigger"]["timezone_name"] == "Asia/Shanghai"
        # And the step that lost the entry role is driven by its upstream now.
        assert after["collect"]["depends_on"] == ["analyze"]
        assert after["collect"]["trigger_type"] == "signal"
        assert after["collect"]["trigger"] == {}

        # Nothing was rebuilt: same task ids, so the run history is still there.
        assert after["analyze"]["task_id"] == before["analyze"]["task_id"]
        assert after["collect"]["task_id"] == before["collect"]["task_id"]
        assert after["publish"]["task_id"] == before["publish"]["task_id"]

        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            assert len(store.step_tasks(created["id"])) == 3
            analyze_task = store.get_task(before["analyze"]["task_id"])
            assert analyze_task.trigger.trigger_type == "daily"
            assert analyze_task.trigger.payload["time_of_day"] == "02:00"
            for driven, waiting_for in (
                (before["collect"]["task_id"], before["analyze"]["task_id"]),
                (before["publish"]["task_id"], before["collect"]["task_id"]),
            ):
                waiting = store.get_task(driven).trigger
                assert waiting.trigger_type == "signal"
                # A step waits on its upstreams' *success* signals, one per
                # upstream, and it is namespaced by the task id.
                assert any(
                    waiting_for in item for item in waiting.payload["names"]
                )
        finally:
            store.close()


def test_web_a_borrowed_trigger_has_to_come_from_an_entry_step(
    tmp_path, monkeypatch
):
    """Only an entry step has a schedule to lend.

    A step with upstreams carries a subscription instead, and copying that onto
    an entry step would make the chain wait for a task that is already part of
    it.  The message says which step cannot lend, because "invalid trigger" is
    not something a reader can act on.
    """
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post("/api/workflows", json=_workflow_body()).json()["workflow"]
        body = _chain_with_its_first_two_steps_exchanged()
        body["steps"][0]["trigger_from"] = "publish"

        refused = client.put(f"/api/workflows/{created['id']}", json=body)
        assert refused.status_code == 400
        assert "不是入口步骤" in refused.json()["error"]
        assert "publish" in refused.json()["error"]


def test_web_a_step_with_upstreams_cannot_borrow_a_trigger(
    tmp_path, monkeypatch
):
    """The two rules about triggers do not get to contradict each other."""
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post("/api/workflows", json=_workflow_body()).json()["workflow"]
        body = _chain_with_its_first_two_steps_exchanged()
        # A real entry step lending to a step that already has upstreams: the
        # donor check passes and the "no trigger with upstreams" rule catches it.
        body["steps"][2]["trigger_from"] = "collect"

        refused = client.put(f"/api/workflows/{created['id']}", json=body)
        assert refused.status_code == 400
        assert "不能沿用别的步骤的触发方式" in refused.json()["error"]


def test_web_a_borrowed_trigger_must_name_another_step_that_exists(
    tmp_path, monkeypatch
):
    """Including on creation, where there is nothing to borrow from."""
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        fresh = _chain_with_its_first_two_steps_exchanged()
        refused = client.post("/api/workflows", json=fresh)
        assert refused.status_code == 400
        assert "没有这一步" in refused.json()["error"]

        created = client.post("/api/workflows", json=_workflow_body()).json()["workflow"]
        body = _chain_with_its_first_two_steps_exchanged()
        body["steps"][0]["trigger_from"] = "analyze"
        refused = client.put(f"/api/workflows/{created['id']}", json=body)
        assert refused.status_code == 400
        assert "不能沿用自己" in refused.json()["error"]


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
        step_model = _a_routable_model()
        body = _workflow_body()
        body["steps"][1].update(
            {
                "model_override": step_model,
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
            assert task.model_override == step_model
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
        step_model = _a_routable_model()
        body = _workflow_body()
        body["steps"][1].update(
            {"model_override": step_model, "selected_skills": ["pdf"]}
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


def _step_edit_body(**overrides) -> dict:
    body = {
        "name": "步骤",
        "action_type": "agent_task",
        "prompt": "改了内容",
        "trigger_type": "signal",
        "signal_name": "",
    }
    body.update(overrides)
    return body


def test_web_editing_a_step_task_teaches_the_graph_about_it(tmp_path, monkeypatch):
    """A step is edited as a task, but stored as a step; the two have to agree.

    The next save of the workflow rebuilds each task from its step, so a prompt
    changed only on the task would survive exactly until somebody moved an
    edge.  The edit is copied back instead.
    """
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post("/api/workflows", json=_workflow_body()).json()["workflow"]
        analyze = next(item for item in created["steps"] if item["key"] == "analyze")

        edited = client.put(
            f"/api/schedules/{analyze['task_id']}",
            json=_step_edit_body(name="分析", prompt="只改内容"),
        )
        assert edited.status_code == 200, edited.text

        listed = client.get("/api/workflows").json()["workflows"][0]
        after = {item["key"]: item for item in listed["steps"]}
        assert after["analyze"]["payload"]["prompt"] == "只改内容"
        assert after["analyze"]["task_id"] == analyze["task_id"]

        # And it survives a later graph edit, which is the whole point.
        graph_only = {
            "name": "夜间报告",
            "steps": [
                {
                    "key": item["key"],
                    "name": item["name"],
                    "kind": item["kind"],
                    "payload": item["payload"],
                    "depends_on": item["depends_on"],
                }
                for item in listed["steps"]
            ],
        }
        resaved = client.put(f"/api/workflows/{listed['id']}", json=graph_only)
        assert resaved.status_code == 200, resaved.text
        steps = {item["key"]: item for item in resaved.json()["workflow"]["steps"]}
        assert steps["analyze"]["payload"]["prompt"] == "只改内容"
        # The entry step's clock, which the graph body never mentioned, is
        # still where the user put it.
        assert steps["collect"]["trigger"]["time_of_day"] == "02:00"


def test_web_a_dependent_step_keeps_the_trigger_its_upstreams_give_it(
    tmp_path, monkeypatch
):
    """Editing a middle step's content must not quietly detach it from the chain."""
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import SchedulerStore

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post("/api/workflows", json=_workflow_body()).json()["workflow"]
        by_key = {item["key"]: item for item in created["steps"]}

        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            before = store.get_task(by_key["analyze"]["task_id"])
            upstream_names = list(before.trigger.payload["names"])
        finally:
            store.close()
        assert upstream_names == [f"task:{by_key['collect']['task_id']}:succeeded"]

        edited = client.put(
            f"/api/schedules/{by_key['analyze']['task_id']}",
            json=_step_edit_body(name="分析", prompt="换了写法"),
        )
        assert edited.status_code == 200, edited.text
        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            after = store.get_task(by_key["analyze"]["task_id"])
            assert after.trigger.payload["names"] == upstream_names
            assert after.trigger.payload["mode"] == "all"
            assert after.payload["prompt"] == "换了写法"
        finally:
            store.close()


def test_web_a_dependent_step_refuses_a_trigger_of_its_own(tmp_path, monkeypatch):
    """Silently ignoring the answer would be worse than refusing to take it."""
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post("/api/workflows", json=_workflow_body()).json()["workflow"]
        analyze = next(item for item in created["steps"] if item["key"] == "analyze")

        refused = client.put(
            f"/api/schedules/{analyze['task_id']}",
            json=_step_edit_body(
                name="分析", trigger_type="daily", time_of_day="03:00"
            ),
        )
        assert refused.status_code == 400
        assert "由它上游的步骤决定" in refused.json()["error"]


def test_web_an_entry_steps_clock_is_edited_from_either_end(tmp_path, monkeypatch):
    """An entry step is an ordinary scheduled task, so its time is its own.

    Changing it from the task endpoint writes it back into the graph, otherwise
    the next save of the workflow would restore the old hour while the task list
    had been showing the new one in the meantime.
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

        moved = client.put(
            f"/api/schedules/{collect['task_id']}",
            json=_step_edit_body(
                name="采集",
                trigger_type="daily",
                time_of_day="05:30",
                timezone_name="Asia/Shanghai",
            ),
        )
        assert moved.status_code == 200, moved.text

        listed = client.get("/api/workflows").json()["workflows"][0]
        steps = {item["key"]: item for item in listed["steps"]}
        assert steps["collect"]["trigger"]["time_of_day"] == "05:30"

        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            task = store.get_task(collect["task_id"])
            assert task.trigger.payload["time_of_day"] == "05:30"
        finally:
            store.close()


def test_web_editing_a_step_task_does_not_detach_it_from_its_workflow(
    tmp_path, monkeypatch
):
    """Which workflow a step belongs to is not something a task edit can change.

    Membership is decided by materialising the workflow, so a request that
    could set it could also orphan a step: it would keep firing on its own
    while the graph that explains it stopped mentioning it.
    """
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post("/api/workflows", json=_workflow_body()).json()["workflow"]
        analyze = next(item for item in created["steps"] if item["key"] == "analyze")

        # A body that even tries to claim it is standalone.
        body = _step_edit_body(name="分析", prompt="改了内容")
        body["workflow_id"] = ""
        body["step_key"] = ""
        edited = client.put(f"/api/schedules/{analyze['task_id']}", json=body)
        assert edited.status_code == 200, edited.text

        placed = {
            item["id"]: item
            for item in client.get("/api/schedules").json()["tasks"]
            if item["id"] == analyze["task_id"]
        }
        assert placed[analyze["task_id"]]["workflow_id"] == created["id"]
        assert placed[analyze["task_id"]]["step_key"] == "analyze"


def test_web_a_steps_retry_policy_round_trips(tmp_path, monkeypatch):
    """Settable on a step, sent back on the step, and still there after both
    kinds of edit -- the graph's and the task's.

    A field that can be set but not read back is one the editor cannot show,
    and a field that a later save resets is one the editor lied about.
    """
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        body = _workflow_body()
        body["steps"][1]["retry_policy"] = {
            "max_attempts": 3,
            "backoff_seconds": 5,
        }
        created = client.post("/api/workflows", json=body)
        assert created.status_code == 200, created.text
        steps = {item["key"]: item for item in created.json()["workflow"]["steps"]}
        assert steps["analyze"]["retry_policy"] == {
            "max_attempts": 3,
            "backoff_seconds": 5,
        }
        # A step that said nothing gets the default, which is not to retry.
        assert steps["collect"]["retry_policy"]["max_attempts"] == 1

        # It reaches the task, which is the only place the retry path reads.
        listed = {
            item["id"]: item for item in client.get("/api/schedules").json()["tasks"]
        }
        assert listed[steps["analyze"]["task_id"]]["retry_policy"] == {
            "max_attempts": 3,
            "backoff_seconds": 5,
        }

        # A graph edit that never mentions it keeps it, like every other field
        # the editor does not show.
        graph_only = {
            "name": "夜间报告",
            "steps": [
                {
                    "key": item["key"],
                    "name": item["name"],
                    "kind": item["kind"],
                    "payload": item["payload"],
                    "depends_on": item["depends_on"],
                }
                for item in created.json()["workflow"]["steps"]
            ],
        }
        resaved = client.put(
            f"/api/workflows/{created.json()['workflow']['id']}", json=graph_only
        )
        assert resaved.status_code == 200, resaved.text
        after = {item["key"]: item for item in resaved.json()["workflow"]["steps"]}
        assert after["analyze"]["retry_policy"] == {
            "max_attempts": 3,
            "backoff_seconds": 5,
        }

        # And an edit of the step's *task*, which copies the task back over the
        # step.  A field missing from that copy is not merely uncopied -- the
        # step is rebuilt from the dataclass default, so the policy would reset
        # to "give up after one attempt" because somebody renamed the step.
        edited = client.put(
            f"/api/schedules/{after['analyze']['task_id']}",
            json=_step_edit_body(name="分析二", prompt="只改内容"),
        )
        assert edited.status_code == 200, edited.text
        final = {
            item["key"]: item
            for item in client.get("/api/workflows").json()["workflows"][0]["steps"]
        }
        assert final["analyze"]["retry_policy"] == {
            "max_attempts": 3,
            "backoff_seconds": 5,
        }


def test_web_a_steps_retry_policy_is_checked_like_a_tasks(tmp_path, monkeypatch):
    """Refused rather than clamped, and named by step.

    A number that comes back different from the one that was sent is a silent
    disagreement about what the step does when it fails, and a message that
    does not say which step leaves the editor to guess.
    """
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        for policy in ({"max_attempts": 99}, {"max_attempts": 0}):
            body = _workflow_body()
            body["steps"][1]["retry_policy"] = policy
            refused = client.post("/api/workflows", json=body)
            assert refused.status_code == 400, refused.text
            assert "analyze" in refused.json()["error"]

        body = _workflow_body()
        body["steps"][1]["retry_policy"] = {"backoff_seconds": 99999999}
        refused = client.post("/api/workflows", json=body)
        assert refused.status_code == 400, refused.text
        assert "analyze" in refused.json()["error"]

        body = _workflow_body()
        body["steps"][1]["retry_policy"] = "not an object"
        refused = client.post("/api/workflows", json=body)
        assert refused.status_code == 400, refused.text

        # Nothing was created by any of the refusals.
        assert client.get("/api/workflows").json()["workflows"] == []


def test_web_refuses_to_delete_or_pause_one_step_of_a_workflow(
    tmp_path, monkeypatch
):
    """Both would be undone, or would break the chain, so both are refused.

    Deleting a step leaves the steps below it subscribed to a signal nobody
    emits, and the next save of the workflow builds a fresh task for it -- so
    the history the deletion was about reappears under a new id.  Pausing one
    is rewritten from the workflow's own flag on the next save.  Either way the
    graph is where the answer is, and the message has to say so.
    """
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post("/api/workflows", json=_workflow_body()).json()["workflow"]
        analyze = next(item for item in created["steps"] if item["key"] == "analyze")

        removed = client.delete(f"/api/schedules/{analyze['task_id']}")
        assert removed.status_code == 409
        assert "流程中的步骤" in removed.json()["error"]

        paused = client.patch(
            f"/api/schedules/{analyze['task_id']}", json={"enabled": False}
        )
        assert paused.status_code == 409
        assert "请暂停整个流程" in paused.json()["error"]

        assert len(client.get("/api/schedules").json()["tasks"]) == 3

        bulk = client.patch(
            "/api/schedules",
            json={"action": "delete", "task_ids": [item["task_id"] for item in created["steps"]]},
        ).json()
        assert bulk["completed"] == []
        assert all("流程中的步骤" in item["reason"] for item in bulk["skipped"])
        assert len(client.get("/api/schedules").json()["tasks"]) == 3


def test_web_deleting_a_workflow_leaves_steps_that_can_still_be_deleted(
    tmp_path, monkeypatch
):
    """The steps a deleted workflow leaves behind are ordinary tasks.

    Deleting a workflow stops it and keeps its steps so their runs stay
    readable.  Those tasks still name a workflow that no longer exists, and
    every surface refused them for that reason alone -- pointing at a graph
    nobody can open, which left rows that could not be removed at all.
    """
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post("/api/workflows", json=_workflow_body()).json()["workflow"]
        analyze = next(item for item in created["steps"] if item["key"] == "analyze")

        deleted = client.delete(f"/api/workflows/{created['id']}")
        assert deleted.status_code == 200, deleted.text
        assert client.get("/api/workflows").json()["workflows"] == []

        # Still listed -- the record outlives the graph -- and now removable.
        listed = client.get("/api/schedules").json()["tasks"]
        assert any(item["id"] == analyze["task_id"] for item in listed)

        paused = client.patch(
            f"/api/schedules/{analyze['task_id']}", json={"enabled": True}
        )
        assert paused.status_code == 200, paused.text

        removed = client.delete(f"/api/schedules/{analyze['task_id']}")
        assert removed.status_code == 200, removed.text

        left = client.get("/api/schedules").json()["tasks"]
        assert analyze["task_id"] not in {item["id"] for item in left}

        # The bulk path answers the same way, rather than skipping them.
        bulk = client.patch(
            "/api/schedules",
            json={
                "action": "delete",
                "task_ids": [
                    item["task_id"] for item in created["steps"]
                    if item["task_id"] != analyze["task_id"]
                ],
            },
        ).json()
        assert len(bulk["completed"]) == 2
        assert bulk["skipped"] == []
        assert client.get("/api/schedules").json()["tasks"] == []


def test_web_pausing_a_whole_workflow_stops_all_of_its_steps(tmp_path, monkeypatch):
    """The switch that holds, because it is the one the save writes from."""
    from starlette.testclient import TestClient
    from agent import shared

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post("/api/workflows", json=_workflow_body()).json()["workflow"]

        paused = client.put(
            f"/api/workflows/{created['id']}", json={"enabled": False}
        )
        assert paused.status_code == 200, paused.text
        steps = paused.json()["workflow"]["steps"]
        assert all(item["enabled"] is False for item in steps)
        # And the graph is intact: pausing is not deleting.
        assert [item["key"] for item in steps] == ["collect", "analyze", "publish"]

        resumed = client.put(
            f"/api/workflows/{created['id']}", json={"enabled": True}
        )
        assert resumed.status_code == 200, resumed.text
        assert all(item["enabled"] is True for item in resumed.json()["workflow"]["steps"])


def _feishu_config(app_id: str = "app", app_secret: str = "secret") -> dict:
    return (
        {"channels": {"feishu": {"app_id": app_id, "app_secret": app_secret}}},
        False,
    )


def test_web_schedule_delivery_to_feishu_stores_the_chosen_chat(tmp_path, monkeypatch):
    """The form's 发到飞书 choice is a target the runtime can deliver to.

    Saving it writes the chat_id down with the receive_id_type that address
    needs; leaving it out of a later edit keeps what was saved, because the
    body that does not mention delivery is not an answer about delivery.
    """
    from datetime import datetime, timedelta, timezone
    from starlette.testclient import TestClient
    from agent import shared
    import agent.config as config_module

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")
    monkeypatch.setattr(config_module, "load_config", _feishu_config)

    channel = _channel()
    channel.bind_runtime({}, {})
    body = {
        "name": "晨报",
        "action_type": "message",
        "message_text": "早上好",
        "trigger_type": "once",
        "at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        "timezone_name": "Asia/Shanghai",
        "delivery_mode": "channel",
        "delivery_target": {
            "target_type": "feishu_chat",
            "payload": {"chat_id": "oc_morning", "chat_type": "group"},
        },
    }
    with TestClient(channel.app) as client:
        created = client.post("/api/schedules", json=body)
        assert created.status_code == 200, created.text
        task = created.json()["task"]
        assert task["delivery_mode"] == "channel"
        assert task["delivery_target"]["target_type"] == "feishu_chat"
        payload = task["delivery_target"]["payload"]
        assert payload["chat_id"] == "oc_morning"
        # Written down, not left to the chat_type heuristic, because the
        # heuristic predates the picker and would misread a p2p chat_id.
        assert payload["receive_id_type"] == "chat_id"

        renamed = client.put(
            f"/api/schedules/{task['id']}",
            json={**{key: value for key, value in body.items() if key != "delivery_target"},
                  "name": "晨报改", "delivery_mode": "channel"},
        )
        assert renamed.status_code == 200, renamed.text
        kept = renamed.json()["task"]["delivery_target"]["payload"]
        assert kept["chat_id"] == "oc_morning"

        back = client.put(
            f"/api/schedules/{task['id']}",
            json={**body, "delivery_mode": "standalone"},
        )
        assert back.status_code == 200, back.text
        assert back.json()["task"]["delivery_mode"] == "standalone"


def test_web_schedule_delivery_to_feishu_needs_config_and_a_chat(tmp_path, monkeypatch):
    """Both refusals happen at save time, where the user is, not at run time.

    A task whose every run fails with "app_id required" is a form that let
    the user write a promise the code cannot keep -- the same class of bug as
    the wording that promised sending while only storing.
    """
    from datetime import datetime, timedelta, timezone
    from starlette.testclient import TestClient
    from agent import shared
    import agent.config as config_module

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")
    monkeypatch.setattr(config_module, "load_config", lambda: ({"channels": {}}, False))

    channel = _channel()
    channel.bind_runtime({}, {})
    base = {
        "name": "提醒",
        "action_type": "message",
        "message_text": "喝水",
        "trigger_type": "once",
        "at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        "timezone_name": "UTC",
        "delivery_mode": "channel",
        "delivery_target": {
            "target_type": "feishu_chat",
            "payload": {"chat_id": "oc_x"},
        },
    }
    with TestClient(channel.app) as client:
        no_config = client.post("/api/schedules", json=base)
        assert no_config.status_code == 400
        assert "app_id" in no_config.json()["error"]

        monkeypatch.setattr(config_module, "load_config", _feishu_config)
        no_chat = client.post(
            "/api/schedules",
            json={**base, "delivery_target": {"target_type": "feishu_chat", "payload": {}}},
        )
        assert no_chat.status_code == 400
        assert "选择一个会话" in no_chat.json()["error"]

        foreign = client.post(
            "/api/schedules",
            json={**base, "delivery_target": {"target_type": "wecom", "payload": {"chat_id": "x"}}},
        )
        assert foreign.status_code == 400
        assert "暂不支持的投递渠道" in foreign.json()["error"]

        # And the refusal leaves nothing half-created behind.
        assert client.get("/api/schedules").json()["tasks"] == []


def test_web_feishu_chat_listing_and_test_message(tmp_path, monkeypatch):
    """The two endpoints the picker leans on, with Feishu itself faked.

    A missing config is answered in words the settings page can act on; an
    upstream failure keeps its own code and message, because "会话列表为空"
    and "应用没权限" must not read as the same thing.
    """
    from starlette.testclient import TestClient
    import agent.config as config_module

    monkeypatch.setattr(
        config_module, "load_config", lambda: ({"channels": {}}, False)
    )

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        missing = client.get("/api/feishu/chats")
        assert missing.status_code == 400
        assert "app_id" in missing.json()["error"]

        monkeypatch.setattr(config_module, "load_config", _feishu_config)
        monkeypatch.setattr(
            WebChannel,
            "_list_feishu_chats",
            staticmethod(
                lambda cfg: [
                    {"chat_id": "oc_1", "name": "日报群", "description": "", "external": False}
                ]
            ),
        )
        listed = client.get("/api/feishu/chats")
        assert listed.status_code == 200
        assert listed.json()["chats"] == [
            {"chat_id": "oc_1", "name": "日报群", "description": "", "external": False}
        ]

        sent = []

        def _fake_send(cfg, chat_id, text):
            sent.append((chat_id, text))

        monkeypatch.setattr(WebChannel, "_send_feishu_text", staticmethod(_fake_send))
        tested = client.post("/api/feishu/test", json={"chat_id": "oc_1"})
        assert tested.status_code == 200
        assert sent and sent[0][0] == "oc_1"

        def _failing_send(cfg, chat_id, text):
            raise RuntimeError("飞书消息发送失败：code=99991663 msg=no permission")

        monkeypatch.setattr(WebChannel, "_send_feishu_text", staticmethod(_failing_send))
        failed = client.post("/api/feishu/test", json={"chat_id": "oc_1"})
        assert failed.status_code == 502
        assert "99991663" in failed.json()["error"]

        empty = client.post("/api/feishu/test", json={})
        assert empty.status_code == 400


def test_web_pick_directory_returns_the_os_dialogs_answer(tmp_path, monkeypatch):
    """One endpoint, one dialog; cancelling it is an answer, not an error."""
    from starlette.testclient import TestClient
    import agent.channels.web as web_module

    channel = _channel()
    channel.bind_runtime({}, {})

    async def _picked():
        return "/Users/demo/projects/report"

    monkeypatch.setattr(web_module, "_pick_workspace_directory", _picked)
    with TestClient(channel.app) as client:
        picked = client.post("/api/fs/pick-directory")
        assert picked.status_code == 200
        assert picked.json() == {"cancelled": False, "workspace_root": "/Users/demo/projects/report"}

    async def _cancelled():
        return None

    monkeypatch.setattr(web_module, "_pick_workspace_directory", _cancelled)
    with TestClient(channel.app) as client:
        cancelled = client.post("/api/fs/pick-directory")
        assert cancelled.status_code == 200
        assert cancelled.json() == {"cancelled": True, "workspace_root": ""}


# ── What the automation page is told about liveness ─────────────────────────
#
# The page chooses how soon to ask again from `in_flight`, so these tests are
# about a request cadence, not about a colour.  The two halves that matter are
# the case where a run is on its way but not yet claimed, and the case where
# the list is polled often enough that its size is worth arguing about.


def _two_step_workflow():
    from agent.scheduler import TriggerSpec, Workflow, WorkflowStep

    def step(key, *, depends_on=(), trigger=None):
        return WorkflowStep(
            key=key,
            name=key,
            kind="agent_prompt",
            payload={"prompt": f"do {key}"},
            depends_on=list(depends_on),
            trigger=trigger,
            workspace_root="",
            timeout_seconds=600,
            delivery_mode="standalone",
        )

    return Workflow(
        name="nightly",
        steps=[
            step("collect", trigger=TriggerSpec.daily("09:00", "UTC")),
            step("analyze", depends_on=["collect"]),
        ],
    )


def test_web_schedule_list_calls_a_queued_run_in_flight(tmp_path, monkeypatch):
    """A run woken by a signal is `queued`, and has no `active_run_id` yet.

    Both fields the page could otherwise derive liveness from say "idle" in
    that window: the task has no active run, and its latest run is queued
    rather than running.  A client deriving it for itself therefore stops
    polling for the whole of a run that was started by the step above it --
    which is precisely the run somebody watching a workflow is looking at.
    The answer travels as its own field so the two sides cannot disagree.
    """
    from datetime import datetime, timezone
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import SchedulerStore, task_signal_name

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")
    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    now = datetime(2026, 9, 16, 0, 0, tzinfo=timezone.utc)
    workflow = store.create_workflow(_two_step_workflow(), now=now)
    tasks = store.step_tasks(workflow.id)
    upstream, subscriber = tasks["collect"], tasks["analyze"]

    # The upstream run finished and said so; the subscriber is woken and
    # queued, and nothing has claimed it yet.
    store.emit_signal(task_signal_name(upstream.id, "succeeded"), source="manual")
    store.deliver_signals(now=now)
    queued = store.latest_run(subscriber.id)
    assert queued is not None and queued.status == "queued"
    store.close()

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        listed = {
            item["id"]: item for item in client.get("/api/schedules").json()["tasks"]
        }

    waiting = listed[subscriber.id]
    # The two facts a client could derive this from, both saying "idle".
    assert waiting["active_run_id"] is None
    assert waiting["latest_run"]["status"] == "queued"
    assert waiting["in_flight"] is True
    # An entry step with no run at all is not in flight, so the field is not
    # simply "true for everything".
    assert listed[upstream.id]["in_flight"] is False


def test_web_workflow_steps_report_liveness_the_same_way_their_tasks_do(
    tmp_path, monkeypatch
):
    """The graph draws each step's liveness, and it draws it from here.

    A step whose task is queued has to read as queued in the graph too, or the
    picture and the list below it disagree about the same run.
    """
    from datetime import datetime, timezone
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import SchedulerStore, task_signal_name

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")
    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    now = datetime(2026, 9, 16, 0, 0, tzinfo=timezone.utc)
    workflow = store.create_workflow(_two_step_workflow(), now=now)
    tasks = store.step_tasks(workflow.id)
    store.emit_signal(task_signal_name(tasks["collect"].id, "succeeded"), source="manual")
    store.deliver_signals(now=now)
    store.close()

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        steps = {
            step["key"]: step
            for step in client.get("/api/workflows").json()["workflows"][0]["steps"]
        }

    assert steps["analyze"]["latest_run"]["status"] == "queued"
    assert steps["analyze"]["in_flight"] is True
    assert steps["collect"]["in_flight"] is False
    # The graph is polled at the same cadence as the list, so its embedded runs
    # are trimmed the same way. The queued run does carry a snapshot -- it
    # records the signal that woke it -- and it is still not sent here.
    assert "config_snapshot" not in steps["analyze"]["latest_run"]


def test_web_schedule_payload_carries_the_words_that_asked(tmp_path, monkeypatch):
    """The page can say why a task exists, from the task itself.

    Storing the sentence is half of the point; the other half is being able to
    see it.  A task nobody asked for can only be noticed if the words it claims
    as its authority are readable next to it.
    """
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        SchedulerStore,
        TriggerSpec,
        Workflow,
        WorkflowStep,
    )

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")
    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    task = store.create_task(
        NewScheduledTask(
            name="看盘",
            kind="message",
            trigger=TriggerSpec.daily("09:00", "UTC"),
            payload={"message_text": "看盘"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
            request_quote="每天早上九点提醒我看盘",
        )
    )
    workflow = store.create_workflow(
        Workflow(
            name="nightly",
            steps=[
                WorkflowStep(
                    key="collect",
                    name="collect",
                    kind="message",
                    payload={"message_text": "go"},
                    trigger=TriggerSpec.daily("09:00", "UTC"),
                    delivery_mode="standalone",
                    delivery_target=DeliveryTarget.standalone(),
                )
            ],
            request_quote="每天跑一次收集",
        ),
        now=datetime(2026, 9, 16, 0, 0, tzinfo=timezone.utc),
    )
    store.close()

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        listed = {
            item["id"]: item for item in client.get("/api/schedules").json()["tasks"]
        }
        workflows = client.get("/api/workflows").json()["workflows"]

    assert listed[task.id]["request_quote"] == "每天早上九点提醒我看盘"
    assert workflows[0]["id"] == workflow.id
    assert workflows[0]["request_quote"] == "每天跑一次收集"


def test_web_schedule_list_leaves_the_run_snapshot_to_the_run_history(
    tmp_path, monkeypatch
):
    """The list is polled every couple of seconds; the snapshot is not read.

    Measured on the real database, `config_snapshot` was 3373 of the 12123
    bytes of ``/api/schedules`` -- 28% of a payload fetched thirty times a
    minute, and read by nothing on that page: the two places that do read it,
    the cascade badge and the model line, are fed by the run history.  It is
    also the field that grows without bound, because a step's snapshot carries
    its upstreams' report previews.
    """
    from datetime import datetime, timedelta, timezone
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        SchedulerStore,
        TriggerSpec,
    )

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")
    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    scheduled_for = datetime(2026, 9, 16, 0, 0, tzinfo=timezone.utc)
    task = store.create_task(
        NewScheduledTask(
            name="daily report",
            kind="agent_prompt",
            trigger=TriggerSpec.once(scheduled_for, "UTC"),
            payload={"prompt": "Summarise today"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        ),
        now=scheduled_for - timedelta(hours=1),
    )
    claimed = store.claim_due_tasks(now=scheduled_for + timedelta(seconds=2))[0]
    store.complete_run(
        task.id,
        claimed.run.id,
        finished_at=scheduled_for + timedelta(seconds=5),
        status="succeeded",
        summary="done",
    )
    store.close()

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        listed = client.get("/api/schedules").json()["tasks"][0]
        history = client.get(f"/api/schedules/{task.id}/runs").json()["runs"][0]

    assert listed["latest_run"]["status"] == "succeeded"
    assert "config_snapshot" not in listed["latest_run"]
    # Everything else the page reads off a run is still there -- the field is
    # dropped, not the run.
    assert listed["latest_run"]["summary"] == "done"
    assert listed["latest_run"]["id"] == claimed.run.id
    # And the drawer, which is the one reader, is still served.
    assert history["config_snapshot"]["task_id"] == task.id


def _write_user_skill(root, skill_id: str) -> None:
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_id}\n"
        f"description: {skill_id} helper\n"
        "user-invocable: true\n"
        "---\n"
        "Instructions.\n",
        encoding="utf-8",
    )


def _skill_catalog_home(tmp_path, monkeypatch):
    """A catalog rooted in tmp_path, with config.json kept out of the real home."""
    from agent import shared
    from agent.skills.catalog import SkillCatalog

    monkeypatch.setattr(shared, "CONFIG_FILE", tmp_path / "config.json")
    root = tmp_path / "skills"
    root.mkdir(parents=True, exist_ok=True)
    catalog = SkillCatalog(user_root=root, builtin_root=tmp_path / "builtin")
    catalog.load_all()
    return catalog, root


def test_web_skill_toggle_persists_and_applies_without_a_restart(tmp_path, monkeypatch):
    """Switching a skill off is a config write the running process obeys.

    The listing the settings page reads has to keep showing the skill --
    it is the only place the switch can be found again -- while the catalog
    the model asks stops answering for it.  A nested id is used on purpose:
    the toggle route has to be reached through the greedy {skill_id:path}
    converter.
    """
    import json
    from starlette.testclient import TestClient

    catalog, root = _skill_catalog_home(tmp_path, monkeypatch)
    _write_user_skill(root, "group/review")

    channel = _channel()
    channel.bind_runtime({}, {"skill_catalog": catalog})

    with TestClient(channel.app) as client:
        listed = client.get("/api/skills").json()["skills"]
        assert [(s["id"], s["enabled"]) for s in listed] == [("group/review", True)]

        off = client.post(
            "/api/skills/group/review/toggle", json={"enabled": False}
        )
        assert off.status_code == 200
        assert off.json() == {"ok": True, "id": "group/review", "enabled": False}

        # Already true for the process that answered, and asking for the
        # prompt to be recomposed is how "already true" reaches the model.
        assert catalog.get("group/review") is None
        assert catalog.consume_dirty() is True

        listed = client.get("/api/skills").json()["skills"]
        assert [(s["id"], s["enabled"]) for s in listed] == [("group/review", False)]

        stored = json.loads((tmp_path / "config.json").read_text())
        assert stored["skills"]["group/review"]["enabled"] is False

        # A skill that is off is still a skill somebody can switch back on.
        on = client.post("/api/skills/group/review/toggle", json={"enabled": True})
        assert on.status_code == 200
        assert catalog.get("group/review") is not None


def test_web_skill_toggle_rejects_an_unknown_skill(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    catalog, _ = _skill_catalog_home(tmp_path, monkeypatch)
    channel = _channel()
    channel.bind_runtime({}, {"skill_catalog": catalog})

    with TestClient(channel.app) as client:
        missing = client.post("/api/skills/nope/toggle", json={"enabled": False})
        assert missing.status_code == 404
        body = client.post("/api/skills/nope/toggle", json={})
        assert body.status_code == 400


def test_web_deleting_a_switched_off_skill_takes_its_switch_with_it(tmp_path, monkeypatch):
    import json
    from starlette.testclient import TestClient

    catalog, root = _skill_catalog_home(tmp_path, monkeypatch)
    _write_user_skill(root, "review")
    catalog.set_enabled("review", False)

    channel = _channel()
    channel.bind_runtime({}, {"skill_catalog": catalog})

    with TestClient(channel.app) as client:
        assert client.post(
            "/api/skills/review/toggle", json={"enabled": False}
        ).status_code == 200
        assert client.delete("/api/skills/review").status_code == 200

    assert not (root / "review").exists()
    # Nothing left behind to meet the next skill that calls itself "review".
    stored = json.loads((tmp_path / "config.json").read_text())
    assert "review" not in (stored.get("skills") or {})


def test_web_a_schedule_with_no_timezone_is_read_in_the_machines_zone(
    tmp_path, monkeypatch
):
    """The REST default is the local zone too, not UTC.

    The browser always sends its own zone, so this decides the case where
    something else posts a wall-clock time and leaves the zone out -- which
    used to be stored as UTC and fire eight hours away from what was asked.
    """
    from starlette.testclient import TestClient
    from agent import shared
    from agent.scheduler import SchedulerStore

    monkeypatch.setattr(shared, "SCHEDULER_DB_FILE", tmp_path / "scheduler.db")
    monkeypatch.setenv("TZ", "Asia/Shanghai")

    channel = _channel()
    channel.bind_runtime({}, {})
    with TestClient(channel.app) as client:
        created = client.post(
            "/api/schedules",
            json={
                "name": "A股模拟盘每日结算",
                "trigger_type": "weekdays",
                "action_type": "agent_task",
                "prompt": "结算模拟盘",
                "time_of_day": "08:00",
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["task"]["id"]

    store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
    try:
        task = store.get_task(task_id)
    finally:
        store.close()

    assert task.trigger.payload["timezone_name"] == "Asia/Shanghai"


def _queued_state(*, message_id: str, text: str, sink=None):
    """A live session state holding one message still waiting in its queue."""
    from agent.runtime import RuntimeSessionState

    state = RuntimeSessionState(ctx=SimpleNamespace(metadata={}))
    entry = {
        "text": text,
        "message_id": message_id,
        "arrived_at": 1.0,
        "urgency": "normal",
    }
    if sink is not None:
        entry["sink"] = sink
    state.restart_queue.append(entry)
    return state


def test_web_withdrawing_a_queued_message_hands_the_text_back():
    from starlette.testclient import TestClient

    state = _queued_state(message_id="msg-7", text="结算模拟盘")
    channel = _channel()
    channel.bind_runtime({"s-1": state}, {})

    with TestClient(channel.app) as client:
        listed = client.get("/api/sessions/s-1/state").json()
        assert [item["id"] for item in listed["queue"]["items"]] == ["msg-7"]

        withdrawn = client.delete("/api/sessions/s-1/queue/msg-7")
        assert withdrawn.status_code == 200
        assert withdrawn.json() == {"ok": True, "withdrawn": True, "text": "结算模拟盘"}

        after = client.get("/api/sessions/s-1/state").json()

    assert state.restart_queue == []
    assert after["queue"]["pending"] == 0
    assert after["queue"]["items"] == []


def test_web_a_second_withdrawal_reports_the_message_is_already_gone():
    from starlette.testclient import TestClient

    state = _queued_state(message_id="msg-7", text="结算模拟盘")
    channel = _channel()
    channel.bind_runtime({"s-1": state}, {})

    with TestClient(channel.app) as client:
        assert client.delete("/api/sessions/s-1/queue/msg-7").json()["withdrawn"] is True
        again = client.delete("/api/sessions/s-1/queue/msg-7")

    assert again.status_code == 200
    # Saying "taken back" twice would tell the reader it is safe to resend a
    # message that is already on its way.
    assert again.json() == {"ok": True, "withdrawn": False, "text": ""}


def test_web_withdrawing_from_a_session_that_is_not_live_changes_nothing():
    from starlette.testclient import TestClient

    channel = _channel()
    channel.bind_runtime({}, {})

    with TestClient(channel.app) as client:
        response = client.delete("/api/sessions/nobody/queue/msg-7")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "withdrawn": False, "text": ""}


def test_web_a_withdrawn_message_does_not_end_the_running_turn():
    """Releasing the sink must not be mistaken for a finished turn.

    The sink of a message queued behind a running turn is kept alive on
    ``wait_for_completion``.  Waking it is what lets the withdrawal finish,
    but a ``turn_complete`` event here would make the browser reset the turn
    that is still running.
    """
    import asyncio

    from agent.channels.web import WebOutputSink

    sink = WebOutputSink(collect=True)
    sink.mark_turn_start()
    sink.retire_queued_message()

    finished = asyncio.run(sink.wait_for_completion(timeout=1.0))

    assert sink.retired is True
    assert finished is False
    assert [event for event in sink.events if event["type"] == "turn_complete"] == []
