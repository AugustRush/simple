"""Small session-management service shared by web and future channels.

This is intentionally a thin layer over the durable ``conversation_turns``
journal and the live ``RuntimeSessionState`` dictionary a channel runner keeps.
It gives HTTP endpoints and CLI commands one place to list sessions, create a
session id, and read messages without touching SQL directly.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any, Optional


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
    ) -> None:
        self._store = store
        # Preserve an intentionally empty mapping: ChannelRunner populates the
        # shared dict after WebChannel.bind_runtime() returns.
        self._live_states = live_states if live_states is not None else {}
        self._store_factory = store_factory

    def _store_for_session(self, session_id: str) -> Any:
        if self._store_factory is None:
            return self._store
        try:
            home = Path.home() / f".agent-{str(session_id).strip()}"
            if not (home / ".web-session").is_file():
                return self._store
        except OSError:
            return self._store
        try:
            return self._store_factory(session_id)
        except Exception:
            return self._store

    def create_session(self) -> str:
        """Return a fresh session id (the live state is created on first use)."""
        return uuid.uuid4().hex

    def list_sessions(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return sessions ordered as durable history followed by live-only."""
        durable: dict[str, dict[str, Any]] = {}
        titles: dict[str, str] = {}
        if self._store is not None:
            list_ids = getattr(self._store, "list_session_ids", None)
            if callable(list_ids):
                try:
                    for item in list_ids(limit=limit):
                        sid = str(item[0])
                        last_activity = str(item[1] or "") if len(item) > 1 else ""
                        turn_count = int(item[2] or 0) if len(item) > 2 else 0
                        durable[sid] = {
                            "last_activity": last_activity,
                            "turn_count": turn_count,
                        }
                except Exception:
                    durable = {}
            list_titles = getattr(self._store, "list_session_titles", None)
            if callable(list_titles):
                try:
                    titles = {
                        str(k): str(v) for k, v in list_titles().items()
                    }
                except Exception:
                    titles = {}

        # Web sessions are backed by named agent homes. Discover only homes
        # carrying the marker created by the web session factory, so regular
        # CLI ``--name`` instances are not mixed into this list.
        try:
            for home in Path.home().glob(".agent-*"):
                marker = home / ".web-session"
                if not marker.is_file():
                    continue
                sid = home.name[len(".agent-"):]
                if sid and sid not in durable:
                    isolated_store = self._store_for_session(sid)
                    isolated_ids = getattr(isolated_store, "list_session_ids", None)
                    if callable(isolated_ids):
                        rows = isolated_ids(limit=1)
                        if rows:
                            row = rows[0]
                            durable[sid] = {
                                "last_activity": str(row[1] or ""),
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
            sessions.append(
                {
                    "session_id": session_id,
                    "last_activity": "",
                    "turn_count": turn_count,
                    "live": True,
                    "title": titles.get(session_id, ""),
                }
            )

        for session_id, info in durable.items():
            if session_id in live_seen:
                continue
            sessions.append(
                {
                    "session_id": session_id,
                    "last_activity": info["last_activity"],
                    "turn_count": info["turn_count"],
                    "live": False,
                    "title": titles.get(session_id, ""),
                }
            )

        sessions.sort(key=lambda item: item["last_activity"], reverse=True)
        return sessions

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
        return True

    def delete_session(self, session_id: str) -> bool:
        """Delete a durable session and, if live, stop and remove it."""
        clean = str(session_id or "").strip()
        if not clean:
            return False
        live = self._live_states.pop(clean, None)
        if live is not None:
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
        target_store = self._store_for_session(clean)
        if target_store is not None:
            delete = getattr(target_store, "delete_conversation_session", None)
            if callable(delete):
                try:
                    delete(clean)
                except Exception:
                    return False
        return True

    def get_messages(
        self,
        session_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return recent plain-text turns for ``session_id``."""
        target_store = self._store_for_session(str(session_id or "").strip())
        if target_store is None:
            return []
        get_turns = getattr(target_store, "recent_conversation_turns", None)
        if not callable(get_turns):
            return []
        temp: Optional[Path] = None
        try:
            turns = get_turns(session_id=session_id, limit=limit)
        except Exception:
            return []
        messages: list[dict[str, Any]] = []
        for turn in turns or ():
            role = str(getattr(turn, "role", "") or "")
            content = str(getattr(turn, "content", "") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append(
                    {
                        "role": role,
                        "content": content,
                        "created_at": str(getattr(turn, "created_at", "") or ""),
                        "message_id": str(getattr(turn, "message_id", "") or ""),
                    }
                )

        # Tool output is emitted as a live event, not as a conversation turn.
        # Rehydrate one compact tool row per operation so the UI can rebuild
        # the trace without changing the conversational transcript itself.
        get_events = getattr(target_store, "recent_agent_events", None)
        if callable(get_events):
            try:
                events = get_events(session_id=session_id, limit=max(100, limit * 8))
            except Exception:
                events = []
            tools: dict[str, dict[str, Any]] = {}
            order: list[str] = []
            for event in events or ():
                event_type = str(getattr(event, "event_type", "") or "")
                payload = getattr(event, "payload", {})
                if not isinstance(payload, dict):
                    payload = {}
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
            messages.extend(tools[key] for key in order)

        turns_only = [item for item in messages if item.get("role") in ("user", "assistant")]
        tools_only = [item for item in messages if item.get("role") == "tool"]
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
            item.pop("turn_id", None)
        return ordered

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
