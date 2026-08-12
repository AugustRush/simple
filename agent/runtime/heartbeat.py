from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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


class TurnHeartbeat:
    """Drives one turn's liveness signal: a file write plus a UI tick.

    Extracted from ``BaseAgent.send_message``, where it was a mutable dict, a
    nested writer closure, a nested ticker coroutine and six assignment sites
    interleaved with the tool loop.  None of that shares state with the rest
    of the turn — it only needs to be told which phase the turn is in — so it
    reads far better as an object with an explicit vocabulary
    (``operation()``) than as bookkeeping spread through a 500-line function.

    Used as an async context manager: the ticker starts on entry and is
    always stopped and awaited on exit, including on error.
    """

    def __init__(
        self,
        *,
        writer: "HeartbeatWriter | None",
        interval_seconds: float,
        turn_id: str = "",
        pending_messages: Callable[[], int] = lambda: 0,
        sink_provider: Callable[[], Any] = lambda: None,
    ) -> None:
        self._writer = writer
        interval = float(interval_seconds or 0.0)
        self._interval = interval if interval > 0 else 5.0
        self._turn_id = str(turn_id or "")
        self._pending_messages = pending_messages
        self._sink_provider = sink_provider
        self._op = "starting"
        self._detail = ""
        self._current_tool: str | None = None
        self._started_at = time.monotonic()
        self._active = True
        self._task: asyncio.Task | None = None

    # ── Phase reporting ──────────────────────────────────────────────────

    def operation(
        self, op: str, detail: str = "", *, current_tool: str | None = None
    ) -> None:
        """Declare the phase the turn has just entered and emit immediately."""
        self._op = op
        self._detail = detail
        self._current_tool = current_tool
        self._started_at = time.monotonic()
        if self._writer is not None:
            self._writer.mark_progress()
        self.write()

    def write(self, *, status: str = "running", active: bool = True) -> None:
        if self._writer is None:
            return
        with shared._suppress_with_log("heartbeat writer failed"):
            self._writer.write(
                state=self._op,
                detail=self._detail,
                current_tool=self._current_tool,
                turn_id=self._turn_id,
                pending_messages=self._pending_messages(),
                active=active,
                status=status,
            )

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def __aenter__(self) -> "TurnHeartbeat":
        self._task = asyncio.create_task(self._tick())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    async def stop(self, *, status: str = "finished", detail: str = "") -> None:
        self._op = "finished"
        self._detail = detail
        self._current_tool = None
        self.write(status=status, active=False)
        self._active = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _tick(self) -> None:
        self.write()
        while self._active:
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return
            if not self._active:
                return
            self.write()
            sink = self._sink_provider()
            if sink is None:
                continue
            fn = getattr(sink, "on_heartbeat", None)
            if not callable(fn):
                continue
            elapsed = max(0.0, time.monotonic() - self._started_at)
            # Don't tick for super-fast ops — most LLM calls finish in <5s
            # and the first tick would land right as we're handing back.
            if elapsed < self._interval - 0.5:
                continue
            with shared._suppress_with_log("sink.on_heartbeat raised"):
                fn(
                    elapsed_seconds=elapsed,
                    current_op=self._op,
                    op_detail=self._detail,
                    pending_messages=self._pending_messages(),
                )


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
    """Delegates to the one durable-write primitive.

    This used to be a second, independent copy of write-tmp-then-rename.  Two
    implementations of one guarantee means hardening either leaves the other
    behind — exactly what happened: neither had ``fsync``, and fixing one would
    have silently skipped heartbeats.
    """
    shared._atomic_write_json(path, payload)
