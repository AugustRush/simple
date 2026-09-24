"""Small session-management service shared by web and future channels.

This is intentionally a thin layer over the durable ``conversation_turns``
journal and the live ``RuntimeSessionState`` dictionary a channel runner keeps.
It gives HTTP endpoints and CLI commands one place to list sessions, create a
session id, and read messages without touching SQL directly.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote


class _WebSessionRegistry:
    """Small metadata index for isolated Web session homes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=2.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS web_sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'idle',
                    model TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    deleted_at TEXT
                )
                """
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_web_sessions_updated
                ON web_sessions(updated_at DESC)"""
            )

    def upsert(
        self,
        session_id: str,
        *,
        title: str = "",
        model: str = "",
        provider: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path, timeout=2.0) as conn:
            conn.execute(
                """
                INSERT INTO web_sessions
                    (session_id, title, created_at, updated_at, status, model, provider, deleted_at)
                VALUES (?, ?, ?, ?, 'idle', ?, ?, NULL)
                ON CONFLICT(session_id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE web_sessions.title END,
                    model = CASE WHEN excluded.model <> '' THEN excluded.model ELSE web_sessions.model END,
                    provider = CASE WHEN excluded.provider <> '' THEN excluded.provider ELSE web_sessions.provider END,
                    deleted_at = NULL
                """,
                (session_id, title, now, now, model, provider),
            )

    def touch(self, session_id: str, *, status: str = "active") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path, timeout=2.0) as conn:
            conn.execute(
                "UPDATE web_sessions SET updated_at = ?, status = ?, deleted_at = NULL WHERE session_id = ?",
                (now, status, session_id),
            )

    def delete(self, session_id: str) -> None:
        with sqlite3.connect(self.path, timeout=2.0) as conn:
            conn.execute("DELETE FROM web_sessions WHERE session_id = ?", (session_id,))

    def list(self, limit: int = 500) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path, timeout=2.0) as conn:
            rows = conn.execute(
                """
                SELECT session_id, title, created_at, updated_at, status, model, provider
                FROM web_sessions WHERE deleted_at IS NULL
                ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "session_id": row[0],
                "title": row[1],
                "created_at": row[2],
                "last_activity": row[3],
                "status": row[4],
                "model": row[5],
                "provider": row[6],
            }
            for row in rows
        ]


def _activity_iso(value: Any) -> str:
    """One clock format for every ``last_activity`` the session list reports.

    The list merges three sources that each write their own format: the
    registry and live states use ``isoformat()`` (``2026-09-24T09:00:00+00:00``)
    while the turn journal writes ``2026-09-24 09:00:00.000000 UTC``. Sorting
    those as strings compares the separator before the time -- ``' '`` sorts
    below ``'T'`` -- so on the same day every journaled session sank beneath
    every registry one whatever the clock said. Unparseable values pass
    through unchanged rather than being dropped.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    candidate = text[:-4] + "+00:00" if text.endswith(" UTC") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


_BUSY_SESSION_STATUSES = frozenset({"active", "cancelling", "queued"})


def _session_order(item: dict[str, Any]) -> tuple[int, int, float]:
    """Working sessions first, then the ones still held live, then history;
    most recent first inside each group.

    Recency alone put a session whose turn has been running for ten minutes
    under one that was merely opened a minute ago -- the one place the list
    is asked "what is my agent doing" answered it at the bottom.
    """
    busy = str(item.get("status") or "idle") in _BUSY_SESSION_STATUSES
    live = bool(item.get("live"))
    try:
        stamp = datetime.fromisoformat(str(item.get("last_activity") or "")).timestamp()
    except ValueError:
        stamp = 0.0
    return (0 if busy else 1, 0 if live else 1, -stamp)


def _turn_ids(turns: Any) -> set[str]:
    """The message ids of ``turns``, used to re-attach legacy events."""
    return {
        str(getattr(turn, "message_id", "") or "").strip()
        for turn in turns or ()
        if str(getattr(turn, "message_id", "") or "").strip()
    }


def _messages_from_turns(
    turns: Any,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Build the conversational rows from durable turns.

    Returns the rows plus the input-attachment paths that were seen, because
    the event merge below uses them to suppress legacy ``attachment`` events
    that would otherwise render the same file a second time as an output.
    """
    messages: list[dict[str, Any]] = []
    assistants_by_reply: dict[str, dict[str, Any]] = {}
    persisted_attachment_paths: set[str] = set()
    for turn in turns or ():
        role = str(getattr(turn, "role", "") or "")
        content = str(getattr(turn, "content", "") or "").strip()
        reply_to_id = str(getattr(turn, "reply_to_id", "") or "").strip()
        metadata = getattr(turn, "metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        attachments: list[dict[str, Any]] = []
        if role == "user" and isinstance(metadata.get("attachments"), list):
            for raw in metadata["attachments"][:12]:
                if not isinstance(raw, dict):
                    continue
                path = str(raw.get("path") or "").strip()
                if not path:
                    continue
                persisted_attachment_paths.add(path)
                attachments.append(
                    {
                        "id": str(raw.get("id") or Path(path).name),
                        "filename": str(raw.get("filename") or Path(path).name),
                        "mime_type": str(raw.get("mime_type") or "application/octet-stream"),
                        "kind": str(raw.get("kind") or "unknown"),
                        "path": path,
                        "size_bytes": raw.get("size_bytes"),
                    }
                )
        if role in ("user", "assistant") and (content or attachments):
            if role == "assistant" and reply_to_id in assistants_by_reply:
                previous = assistants_by_reply[reply_to_id]
                previous_content = str(previous.get("content") or "").strip()
                if content and content != previous_content:
                    if content.startswith(previous_content):
                        previous["content"] = content
                    elif not previous_content.startswith(content):
                        previous["content"] = f"{previous_content}\n\n{content}"
                continue
            item = {
                "role": role,
                "content": content,
                "created_at": str(getattr(turn, "created_at", "") or ""),
                "message_id": str(getattr(turn, "message_id", "") or ""),
                "reply_to_id": reply_to_id,
            }
            if attachments:
                item["attachments"] = attachments
            messages.append(item)
            if role == "assistant" and reply_to_id:
                assistants_by_reply[reply_to_id] = item
    return messages, persisted_attachment_paths


def _append_durable_event_rows(
    messages: list[dict[str, Any]],
    *,
    target_store: Any,
    session_id: str,
    limit: int,
    turn_ids: set[str],
    persisted_attachment_paths: set[str],
    is_live: bool,
) -> None:
    """Rehydrate the durable tool/attachment rows into ``messages``.

    Tool output is emitted as a live event, not as a conversation turn, so the
    transcript alone cannot rebuild the trace.  One compact row is appended per
    operation (and per output attachment), in event order.
    """
    get_events = getattr(target_store, "recent_agent_events", None)
    if not callable(get_events):
        return
    try:
        events = get_events(session_id=session_id, limit=max(100, limit * 8))
    except Exception:
        events = []
    # Recover events written by pre-fix Web runtimes under a factory
    # staging session id. Turn ids are globally unique and still tie
    # those events to the correct conversation.
    get_events_for_turns = getattr(
        target_store, "recent_agent_events_for_turns", None
    )
    if callable(get_events_for_turns) and turn_ids:
        try:
            legacy_events = get_events_for_turns(
                turn_ids=turn_ids,
                limit=max(100, limit * 8),
            )
            seen_event_ids = {
                int(getattr(event, "id", 0) or 0) for event in events
            }
            events.extend(
                event
                for event in legacy_events
                if int(getattr(event, "id", 0) or 0) not in seen_event_ids
            )
            events.sort(key=lambda event: int(getattr(event, "id", 0) or 0))
        except Exception:
            pass
    tools: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    terminal_turns: set[str] = set()
    seen_output_attachments: set[tuple[str, str]] = set()
    for event in events or ():
        event_type = str(getattr(event, "event_type", "") or "")
        event_turn_id = str(getattr(event, "turn_id", "") or "")
        if event_type in {
            "turn_response_delivered",
            "turn_failed",
            "turn_error_reported",
            "turn_interrupted",
        } and event_turn_id:
            terminal_turns.add(event_turn_id)
        payload = getattr(event, "payload", {})
        if not isinstance(payload, dict):
            payload = {}
        if event_type == "attachment":
            attachment_path = str(payload.get("path") or "")
            # Input attachments now live on the durable user turn.
            # Suppress equivalent legacy events so a refresh does not
            # render the same file again as an output attachment.
            if attachment_path in persisted_attachment_paths:
                continue
            attachment_key = (event_turn_id, attachment_path)
            if attachment_key in seen_output_attachments:
                continue
            seen_output_attachments.add(attachment_key)
            messages.append(
                {
                    "role": "tool",
                    "content": str(payload.get("name") or payload.get("path") or ""),
                    "tool": "attachment",
                    "toolState": "done",
                    "created_at": str(getattr(event, "created_at", "") or ""),
                    "turn_id": str(getattr(event, "turn_id", "") or ""),
                    "link": "/api/files?path="
                    + quote(attachment_path, safe=""),
                }
            )
            continue
        if event_type not in {"tool_started", "tool_progress", "tool_completed", "tool_failed"}:
            continue
        operation_id = str(payload.get("operation_id") or "")
        if not operation_id:
            operation_id = f"event-{getattr(event, 'id', len(order))}"
        item = tools.get(operation_id)
        if item is None:
            item = {
                "role": "tool",
                "content": "",
                "tool": str(payload.get("tool_name") or "tool"),
                "toolState": "running",
                "created_at": str(getattr(event, "created_at", "") or ""),
                "turn_id": str(getattr(event, "turn_id", "") or ""),
            }
            tools[operation_id] = item
            order.append(operation_id)
        if event_type == "tool_progress":
            detail = payload.get("detail") or payload.get("message") or payload.get("progress")
            if detail is not None:
                item["content"] = str(detail)[:220]
        elif event_type == "tool_completed":
            item["toolState"] = "done" if payload.get("ok", True) else "blocked"
            item["content"] = str(payload.get("result_preview") or item.get("content") or "")[:220]
        elif event_type == "tool_failed":
            item["toolState"] = "blocked"
            item["content"] = str(payload.get("result_preview") or item.get("content") or "执行失败")[:220]
    # A process restart can leave the last tool_started event without
    # a terminal event. It is unsafe to present that operation as
    # completed (or to replay it automatically), so expose an
    # explicit recoverable state to the UI. Live sessions keep the
    # running state because their worker may still be active.
    if not is_live:
        for item in tools.values():
            if item.get("toolState") == "running":
                item["toolState"] = "interrupted"
    else:
        for item in tools.values():
            if (
                item.get("toolState") == "running"
                and str(item.get("turn_id") or "") in terminal_turns
            ):
                item["toolState"] = "interrupted"
    messages.extend(tools[key] for key in order)


def _order_display_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Interleave tool rows into the transcript, then drop bookkeeping fields.

    Older runtime/attachment events may not have recorded a ``turn_id``.  They
    are still durable, but previously all of them were appended at the very end
    of the transcript after a restart.  Recover a sensible placement from the
    event timestamp by attaching each orphan to the most recent user turn that
    had already started.
    """
    turns_only = [item for item in messages if item.get("role") in ("user", "assistant")]
    tools_only = [item for item in messages if item.get("role") == "tool"]

    user_turns = [
        item
        for item in turns_only
        if item.get("role") == "user" and item.get("message_id")
    ]
    for tool in tools_only:
        if str(tool.get("turn_id") or ""):
            continue
        tool_created = str(tool.get("created_at") or "")
        candidate = ""
        for user in user_turns:
            user_created = str(user.get("created_at") or "")
            if not tool_created or not user_created or user_created <= tool_created:
                candidate = str(user.get("message_id") or "")
            else:
                break
        if not candidate and user_turns:
            candidate = str(user_turns[-1].get("message_id") or "")
        if candidate:
            tool["turn_id"] = candidate

    tools_by_turn: dict[str, list[dict[str, Any]]] = {}
    for item in tools_only:
        tools_by_turn.setdefault(str(item.get("turn_id") or ""), []).append(item)
    ordered: list[dict[str, Any]] = []
    for item in turns_only:
        ordered.append(item)
        if item.get("role") == "user":
            message_id = str(item.get("message_id") or "")
            if message_id:
                ordered.extend(tools_by_turn.pop(message_id, []))
    # Older events may not have a turn id; retain them at the end rather
    # than dropping durable trace history.
    for leftovers in tools_by_turn.values():
        ordered.extend(leftovers)
    for item in ordered:
        item.pop("created_at", None)
        item.pop("message_id", None)
        item.pop("reply_to_id", None)
        item.pop("turn_id", None)
    return ordered


class SessionService:
    """Read/query sessions for one agent home.

    ``store`` is any object with ``list_session_ids`` and
    ``recent_conversation_turns`` (today: ``LTMStore``).  ``live_states`` is
    the channel runner's ``dict[session_id, RuntimeSessionState]``; live
    sessions are reported alongside durable history so the frontend can show
    conversations that have not produced journaled turns yet.
    """

    def __init__(
        self,
        store: Any = None,
        live_states: Optional[dict[str, Any]] = None,
        store_factory: Any = None,
        runtime_cleanup: Any = None,
    ) -> None:
        self._store = store
        # Preserve an intentionally empty mapping: ChannelRunner populates the
        # shared dict after WebChannel.bind_runtime() returns.
        self._live_states = live_states if live_states is not None else {}
        self._store_factory = store_factory
        self._session_runtime_cleanup = runtime_cleanup
        self._registry: _WebSessionRegistry | None = None
        if store_factory is not None:
            try:
                from agent import shared

                self._registry = _WebSessionRegistry(
                    shared.AGENT_HOME / "web" / "sessions" / "index.db"
                )
            except (OSError, sqlite3.Error):
                self._registry = None

    def _store_for_session(self, session_id: str) -> Any:
        if self._store_factory is None:
            return self._store
        try:
            from agent import shared

            home = shared.web_session_home(str(session_id).strip())
            if not (home / ".web-session").is_file():
                return self._store
        except (OSError, ValueError):
            return self._store
        try:
            return self._store_factory(session_id)
        except Exception:
            return self._store

    def record_attachment(
        self,
        session_id: str,
        path: str,
        name: str = "",
        *,
        turn_id: str = "",
    ) -> None:
        """Journal an attachment (image/audio/video/file) for a session.

        Attachments are emitted as live events; persisting them keeps history
        reloads able to re-render the media inline instead of showing a path.
        """
        clean = str(session_id or "").strip()
        if not clean or not path:
            return
        store = self._store_for_session(clean)
        append_event = getattr(store, "append_agent_event", None)
        if not callable(append_event):
            return
        try:
            append_event(
                session_id=clean,
                event_type="attachment",
                payload={"name": str(name or Path(path).name), "path": str(path)},
                turn_id=str(turn_id or ""),
            )
        except Exception:
            pass

    def create_session(self) -> str:
        """Return a fresh session id (the live state is created on first use)."""
        session_id = uuid.uuid4().hex
        if self._registry is not None:
            try:
                self._registry.upsert(session_id)
            except (OSError, sqlite3.Error):
                pass
        return session_id

    def touch_session(self, session_id: str, *, status: str = "active") -> None:
        clean = str(session_id or "").strip()
        if not clean or self._registry is None:
            return
        try:
            metadata = self._session_manifest_metadata(clean)
            self._registry.upsert(
                clean,
                model=metadata.get("model", ""),
                provider=metadata.get("provider", ""),
            )
            self._registry.touch(clean, status=status)
        except (OSError, sqlite3.Error):
            pass

    @staticmethod
    def _session_manifest_metadata(session_id: str) -> dict[str, str]:
        """Read the non-secret provider/model summary for an isolated session."""
        try:
            from agent import shared

            raw = json.loads(
                (shared.web_session_home(session_id) / ".session.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            key: str(raw.get(key) or "").strip()
            for key in ("model", "provider")
        }

    def list_sessions(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return sessions ordered as durable history followed by live-only."""
        durable: dict[str, dict[str, Any]] = {}
        titles: dict[str, str] = {}
        if self._registry is not None:
            try:
                for item in self._registry.list(limit=limit):
                    durable[item["session_id"]] = {
                        "last_activity": _activity_iso(item.get("last_activity", "")),
                        "turn_count": 0,
                        "created_at": item.get("created_at", ""),
                        "status": item.get("status", "idle"),
                    }
                    titles[item["session_id"]] = item.get("title", "")
            except (OSError, sqlite3.Error):
                pass
        if self._store is not None:
            list_ids = getattr(self._store, "list_session_ids", None)
            if callable(list_ids):
                try:
                    for item in list_ids(limit=limit):
                        sid = str(item[0])
                        last_activity = _activity_iso(item[1]) if len(item) > 1 else ""
                        turn_count = int(item[2] or 0) if len(item) > 2 else 0
                        current = durable.get(sid, {})
                        durable[sid] = {
                            # The registry is touched when a turn ends, the
                            # journal when a turn is written; whichever is
                            # later is when the session was last active.
                            "last_activity": max(
                                last_activity, current.get("last_activity", "")
                            ),
                            "turn_count": turn_count,
                            "created_at": current.get("created_at", ""),
                            "status": current.get("status", "idle"),
                        }
                except Exception:
                    durable = {}
            list_titles = getattr(self._store, "list_session_titles", None)
            if callable(list_titles):
                try:
                    titles.update(
                        {str(k): str(v) for k, v in list_titles().items()}
                    )
                except Exception:
                    pass

        # Web sessions are backed by named agent homes. Discover only homes
        # carrying the marker created by the web session factory, so regular
        # CLI ``--name`` instances are not mixed into this list.
        try:
            from agent import shared

            web_root = shared.web_session_root()
            homes = list(web_root.iterdir()) if web_root.is_dir() else []
            for home in homes:
                marker = home / ".web-session"
                if not marker.is_file():
                    continue
                sid = home.name
                if sid and sid not in durable:
                    isolated_store = self._store_for_session(sid)
                    isolated_ids = getattr(isolated_store, "list_session_ids", None)
                    if callable(isolated_ids):
                        rows = isolated_ids(limit=1)
                        if rows:
                            row = rows[0]
                            durable[sid] = {
                                "last_activity": _activity_iso(row[1]),
                                "turn_count": int(row[2] or 0),
                            }
                    titles.update(
                        {
                            str(k): str(v)
                            for k, v in getattr(isolated_store, "list_session_titles", lambda: {})().items()
                        }
                    )
        except Exception:
            pass

        sessions: list[dict[str, Any]] = []
        live_seen: set[str] = set()
        for session_id, state in self._live_states.items():
            live_seen.add(session_id)
            turn_count = int(getattr(state, "turn_count", 0) or 0)
            activity = float(getattr(state, "last_activity", 0.0) or 0.0)
            last_activity = (
                datetime.fromtimestamp(activity, timezone.utc).isoformat()
                if activity > 0
                else ""
            )
            # ``operation_state`` only says whether a turn is executing, so a
            # session holding queued work in its restart queue reads as idle.
            # The queue is exactly what the list is asked about -- a badge
            # saying "排队中" is what tells the visitor their second message
            # was taken rather than lost -- so both facts go into the status.
            status = str(getattr(state, "operation_state", "idle") or "idle")
            restarts = getattr(state, "restart_queue", None)
            if status == "idle" and restarts:
                status = "queued"
            sessions.append(
                {
                    "session_id": session_id,
                    "last_activity": last_activity,
                    "turn_count": turn_count,
                    "live": True,
                    "title": titles.get(session_id, ""),
                    "status": status,
                }
            )

        for session_id, info in durable.items():
            if session_id in live_seen:
                continue
            # Only a live session can be running a turn. The registry's
            # ``status`` is written at turn start and reset in a ``finally``,
            # so a process killed mid-turn leaves it at "active" for good --
            # and the list then says "运行中" (and ranks it with the working
            # sessions) for an agent that has been gone since the restart.
            sessions.append(
                {
                    "session_id": session_id,
                    "last_activity": info["last_activity"],
                    "turn_count": info["turn_count"],
                    "live": False,
                    "title": titles.get(session_id, ""),
                    "created_at": info.get("created_at", ""),
                    "status": "idle",
                }
            )

        sessions.sort(key=_session_order)
        return sessions

    def get_session_state(self, session_id: str) -> dict[str, Any]:
        """Return durable task guidance and live queue information.

        The runtime already owns the authoritative mailboxes and the memory
        layer owns the durable ``session_working_state`` projection.  Expose a
        small, read-only view for Web clients instead of duplicating either
        queue in the browser or reconstructing task state from transcript
        text.  Missing/invalid sessions intentionally return an empty state so
        the endpoint is safe to poll while a session is being created.
        """
        clean = str(session_id or "").strip()
        if not clean:
            return {
                "session_id": "",
                "live": False,
                "operation_state": "idle",
                "queue": {
                    "pending": 0,
                    "interjections": 0,
                    "restarts": 0,
                    "items": [],
                },
                "task": None,
                "workspace_root": "",
                "workspace_status": "unset",
                "workspace_exists": False,
            }

        live = self._live_states.get(clean)
        from agent.runtime.contracts import describe_queued_messages

        live_ctx = getattr(live, "ctx", None) if live is not None else None
        live_metadata = getattr(live_ctx, "metadata", {}) if live_ctx is not None else {}
        persisted_workspace_meta: dict[str, Any] = {}
        workspace_root = (
            str(live_metadata.get("workspace_root") or "")
            if isinstance(live_metadata, dict)
            else ""
        )
        if not workspace_root:
            try:
                from agent import shared
                manifest = shared.web_session_home(clean) / ".session.json"
                if manifest.is_file():
                    payload = json.loads(manifest.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        persisted_workspace_meta = payload
                        workspace_root = str(payload.get("workspace_root") or "")
            except (OSError, ValueError, TypeError):
                workspace_root = ""
        workspace_path = Path(workspace_root).expanduser() if workspace_root else None
        workspace_exists = bool(workspace_path and workspace_path.is_dir())
        workspace_status = "ready" if workspace_exists else ("missing" if workspace_root else "unset")
        if isinstance(live_metadata, dict):
            recorded_status = str(live_metadata.get("workspace_status") or "").strip()
            if recorded_status:
                workspace_status = recorded_status
        interjections = getattr(live, "pending_interjections", []) if live else []
        restarts = getattr(live, "restart_queue", []) if live else []
        task: dict[str, Any] | None = None
        store = self._store_for_session(clean)
        load_state = getattr(store, "load_session_working_state", None)
        if callable(load_state):
            try:
                snapshot = load_state(clean)
                raw = getattr(snapshot, "state", None) if snapshot is not None else None
                if isinstance(raw, dict) and raw:
                    # Keep the payload intentionally bounded; recent turns and
                    # artifacts are enough to guide a continuation without
                    # shipping the whole memory database to the browser.
                    task = {
                        "task_id": str(raw.get("task_id") or ""),
                        "active_goal": str(raw.get("active_goal") or ""),
                        "status": str(raw.get("status") or ""),
                        "progress": str(raw.get("progress") or ""),
                        "next_action": str(raw.get("next_action") or ""),
                        "last_error": str(raw.get("last_error") or ""),
                        "artifacts": [
                            str(item)
                            for item in (raw.get("artifacts") or [])[:8]
                            if str(item).strip()
                        ],
                    }
            except Exception:
                task = None
        usage: dict[str, Any] = {}
        usage_summary = getattr(store, "usage_summary", None)
        if callable(usage_summary):
            try:
                usage = usage_summary(clean)
            except Exception:
                usage = {}

        return {
            "session_id": clean,
            "live": live is not None,
            "operation_state": str(getattr(live, "operation_state", "idle")),
            "queue": {
                "pending": len(interjections) + len(restarts),
                "interjections": len(interjections),
                "restarts": len(restarts),
                # The entries themselves, not just how many there are: a
                # queued message can be taken back only while the client can
                # name it, and the count alone cannot tell the client whether
                # the message it is showing is still really waiting.
                "items": describe_queued_messages(interjections, restarts),
            },
            "task": task,
            "usage": usage,
            "workspace_root": workspace_root,
            "workspace_status": workspace_status,
            "workspace_exists": workspace_exists,
            "workspace_read": bool(
                live_metadata.get(
                    "workspace_read", persisted_workspace_meta.get("workspace_read", True)
                )
            ) if isinstance(live_metadata, dict) else bool(persisted_workspace_meta.get("workspace_read", True)),
            "workspace_write": bool(
                live_metadata.get(
                    "workspace_write", persisted_workspace_meta.get("workspace_write", False)
                )
            ) if isinstance(live_metadata, dict) else bool(persisted_workspace_meta.get("workspace_write", False)),
        }

    def dismiss_task_guidance(self, session_id: str, task_id: str = "") -> bool:
        """Persist that a task guidance card was explicitly dismissed.

        Working state can be stored as either the current task object or a
        newer ``tasks`` collection.  Update both representations so clients
        using either shape observe the same terminal state after a refresh.
        """
        clean = str(session_id or "").strip()
        if not clean:
            return False
        store = self._store_for_session(clean)
        load_state = getattr(store, "load_session_working_state", None)
        save_state = getattr(store, "save_session_working_state", None)
        if not callable(load_state) or not callable(save_state):
            return False
        try:
            snapshot = load_state(clean)
            raw = getattr(snapshot, "state", None) if snapshot is not None else None
            if not isinstance(raw, dict) or not raw:
                return False
            state = dict(raw)
            requested_id = str(task_id or "").strip()
            candidates = self._working_state_candidates_for_dismiss(state)
            target_index: int | None = None
            if requested_id:
                target_index = next(
                    (
                        index
                        for index, item in enumerate(candidates)
                        if str(item.get("task_id") or "") == requested_id
                    ),
                    None,
                )
            if target_index is None:
                terminal_statuses = {
                    "completed",
                    "success",
                    "done",
                    "updated",
                    "dismissed",
                }
                target_index = next(
                    (
                        index
                        for index in range(len(candidates) - 1, -1, -1)
                        if str(candidates[index].get("status") or "").lower()
                        not in terminal_statuses
                    ),
                    None,
                )
            if target_index is None:
                return False
            target = dict(candidates[target_index])
            target_id = str(target.get("task_id") or "")
            target["status"] = "dismissed"
            target["next_action"] = ""
            target["updated_at"] = datetime.now(timezone.utc).isoformat()
            if isinstance(state.get("tasks"), list):
                tasks = [item for item in state["tasks"] if isinstance(item, dict)]
                tasks[target_index] = target
                state["tasks"] = tasks
            # Keep the legacy/top-level projection in sync when it identifies
            # the dismissed task, or when no task list is present.
            if (
                not isinstance(state.get("tasks"), list)
                or str(state.get("task_id") or "") == target_id
            ):
                state.update(target)
            save_state(
                clean,
                state,
                updated_at=str(target.get("updated_at") or ""),
            )
            return True
        except Exception:
            return False

    @staticmethod
    def _working_state_candidates_for_dismiss(
        state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        tasks = state.get("tasks")
        if isinstance(tasks, list):
            candidates = [item for item in tasks if isinstance(item, dict)]
            if candidates:
                return candidates
        return [state]

    def rename_session(self, session_id: str, title: str) -> bool:
        clean = str(session_id or "").strip()
        if not clean:
            return False
        target_store = self._store_for_session(clean)
        if target_store is not None:
            set_title = getattr(target_store, "set_session_title", None)
            if callable(set_title):
                try:
                    set_title(clean, title)
                except Exception:
                    return False
        live = self._live_states.get(clean)
        if live is not None:
            try:
                live.title = str(title or "").strip()[:120]
            except Exception:
                pass
        if self._registry is not None:
            try:
                self._registry.upsert(clean, title=str(title or "").strip()[:120])
            except (OSError, sqlite3.Error):
                pass
        return True

    @staticmethod
    def _close_live_state(live: Any) -> None:
        """Stop per-session helpers not owned by the component close hook."""
        if live is None:
            return
        worker = getattr(live, "memory_worker", None)
        if worker is not None:
            stop = getattr(worker, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    pass
        manager = getattr(live, "context_manager", None)
        if manager is not None:
            staging = getattr(manager, "staging", None)
            close = getattr(staging, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    @classmethod
    async def _close_live_state_async(cls, live: Any) -> None:
        """Stop a live state's worker and wait until it releases its files."""
        if live is None:
            return
        token = getattr(live, "cancel_token", None)
        cancel = getattr(token, "cancel", None)
        if callable(cancel):
            try:
                cancel("force")
            except Exception:
                pass
        worker = getattr(live, "memory_worker", None)
        stop = getattr(worker, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass
        wait = getattr(worker, "wait", None)
        if callable(wait):
            try:
                result = wait()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass
        cls._close_live_state(live)

    async def delete_session_async(self, session_id: str) -> bool:
        """Close a live runtime, then remove all durable session data."""
        clean = str(session_id or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", clean) is None:
            return False

        live = self._live_states.pop(clean, None)
        await self._close_live_state_async(live)
        # Component shutdown may flush/close files inside the isolated home.
        # Await it before deleting that directory so cleanup cannot race with
        # shutil.rmtree (or recreate files after the delete response).
        cleanup = self._registry_cleanup_callback()
        if cleanup is not None:
            try:
                result = cleanup(clean)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass
        return self._delete_session_data(clean)

    def delete_session(self, session_id: str) -> bool:
        """Synchronously remove durable data after runtime cleanup, if any."""
        clean = str(session_id or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", clean) is None:
            return False
        live = self._live_states.pop(clean, None)
        self._close_live_state(live)
        return self._delete_session_data(clean)

    def _delete_session_data(self, clean: str) -> bool:
        """Remove store records and the exact validated isolated home."""
        target_store = self._store_for_session(clean)
        if target_store is not None:
            delete = getattr(target_store, "delete_conversation_session", None)
            if callable(delete):
                try:
                    delete(clean)
                except Exception:
                    return False
        # Remove only the exact isolated Web session home. The store may be a
        # shared fake or a custom backend, so directory cleanup is best-effort
        # and never masks a successful database deletion.
        try:
            from agent import shared

            current_home = shared.web_session_home(clean)
            candidates = {
                current_home,
                shared.web_session_root() / clean,
            }
            for home in candidates:
                if (home / ".web-session").is_file() and home.is_dir():
                    shutil.rmtree(home)
        except (OSError, ValueError):
            pass
        if self._registry is not None:
            try:
                self._registry.delete(clean)
            except (OSError, sqlite3.Error):
                pass
        return True

    def _registry_cleanup_callback(self) -> Any:
        """Resolve the optional ChannelRunner cleanup hook lazily."""
        callback = getattr(self, "_session_runtime_cleanup", None)
        return callback if callable(callback) else None

    async def delete_sessions_async(self, session_ids: Any) -> dict[str, Any]:
        """Asynchronously clean and delete multiple isolated sessions."""
        if not isinstance(session_ids, (list, tuple, set)):
            return {"deleted": [], "failed": []}
        deleted: list[str] = []
        failed: list[str] = []
        seen: set[str] = set()
        for raw in session_ids:
            clean = str(raw or "").strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            if await self.delete_session_async(clean):
                deleted.append(clean)
            else:
                failed.append(clean)
        return {"deleted": deleted, "failed": failed}

    def get_messages(
        self,
        session_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return recent display messages and durable traces for ``session_id``."""
        target_store = self._store_for_session(str(session_id or "").strip())
        if target_store is None:
            return []
        get_turns = getattr(target_store, "recent_conversation_turns", None)
        if not callable(get_turns):
            return []
        try:
            turns = get_turns(session_id=session_id, limit=limit)
        except Exception:
            return []
        messages, persisted_attachment_paths = _messages_from_turns(turns)
        _append_durable_event_rows(
            messages,
            target_store=target_store,
            session_id=session_id,
            limit=limit,
            turn_ids=_turn_ids(turns),
            persisted_attachment_paths=persisted_attachment_paths,
            is_live=session_id in self._live_states,
        )
        return _order_display_messages(messages)

    def session_data_path(self, session_id: str) -> Optional[Path]:
        """Materialize and return a Finder-friendly file for one session.

        The durable store is shared SQLite, so revealing ``context/`` does not
        identify the selected conversation.  A small Markdown projection gives
        every session its own stable file while keeping the source database
        private and untouched.
        """
        clean = str(session_id or "").strip()
        if not clean:
            return None
        known = any(
            item.get("session_id") == clean for item in self.list_sessions(limit=500)
        )
        if not known or self._store is None:
            return None

        directory = getattr(self._store_for_session(clean), "dir", None)
        if not directory:
            return None
        temp: Path | None = None
        try:
            context_dir = Path(directory).expanduser().resolve(strict=False)
            if not context_dir.is_dir():
                return None
            export_dir = context_dir / "sessions"
            export_dir.mkdir(parents=True, exist_ok=True)

            title = next(
                (
                    str(item.get("title") or "")
                    for item in self.list_sessions(limit=500)
                    if item.get("session_id") == clean
                ),
                "",
            )
            digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:12]
            filename = f"session-{digest}.md"
            path = export_dir / filename

            messages = self.get_messages(clean, limit=500)
            live = self._live_states.get(clean)
            if not messages and live is not None:
                raw_messages = getattr(getattr(live, "ctx", None), "messages", ())
                for message in raw_messages or ():
                    role = str(message.get("role", "") if isinstance(message, dict) else "")
                    content = str(message.get("content", "") if isinstance(message, dict) else "").strip()
                    if role in ("user", "assistant") and content:
                        messages.append({"role": role, "content": content})

            lines = [
                f"# {title or '未命名会话'}",
                "",
                f"- Session ID: `{clean}`",
                f"- Messages: {len(messages)}",
                "",
            ]
            for item in messages:
                lines.extend(
                    (
                        f"## {'You' if item['role'] == 'user' else 'Simple Agent'}",
                        "",
                        item["content"],
                        "",
                    )
                )
            temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            temp.write_text("\n".join(lines), encoding="utf-8")
            os.replace(temp, path)
            return path
        except (OSError, RuntimeError, TypeError, ValueError):
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
            return None
