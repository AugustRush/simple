from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent import shared


class HeartbeatWriter:
    """Side-channel health writer for one agent session.

    Heartbeats are runtime telemetry only: they are never appended to
    conversation messages, memory staging, or model prompts.
    """

    def __init__(
        self,
        *,
        session_id: str,
        agent_id: str,
        path: Path | None = None,
        process_token: str | None = None,
    ) -> None:
        self.session_id = str(session_id or "default")
        self.agent_id = str(agent_id or "")
        self.process_token = process_token or uuid.uuid4().hex
        self.pid = os.getpid()
        self.path = (
            path
            if path is not None
            else heartbeat_path_for_session(self.session_id)
        )
        self.path = Path(self.path).expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.started_at = _now_iso()
        self.seq = 0
        self.last_progress_at = self.started_at

    def mark_progress(self) -> None:
        self.last_progress_at = _now_iso()

    def write(
        self,
        *,
        state: str,
        detail: str = "",
        current_tool: str | None = None,
        turn_id: str = "",
        pending_messages: int = 0,
        active: bool = True,
        status: str = "running",
    ) -> dict[str, Any]:
        self.seq += 1
        payload: dict[str, Any] = {
            "pid": self.pid,
            "process_token": self.process_token,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "turn_id": str(turn_id or ""),
            "heartbeat_seq": self.seq,
            "state": str(state or ""),
            "detail": str(detail or ""),
            "current_tool": current_tool,
            "pending_messages": int(pending_messages),
            "started_at": self.started_at,
            "last_seen_at": _now_iso(),
            "last_progress_at": self.last_progress_at,
            "active": bool(active),
            "status": str(status or "running"),
        }
        _atomic_write_json(self.path, payload)
        return payload


def heartbeat_path_for_session(session_id: str) -> Path:
    filename = _safe_heartbeat_filename(session_id)
    return (
        shared.DEFAULT_OUTPUT_DIR
        / "runtime"
        / "health"
        / filename
    )


def _safe_heartbeat_filename(session_id: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(session_id or "default")).strip("._")
    return f"{stem or 'default'}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)
