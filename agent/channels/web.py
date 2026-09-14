"""HTTP/WebSocket channel for a browser frontend.

The web frontend is just another channel: ``WebChannel`` implements the same
``Channel`` contract as Feishu, reuses the channel-runner message handler, and
bridges ``OutputSink`` events to JSON events over a WebSocket (or an HTTP
response for non-streaming calls).
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

from agent import shared
from agent.channels.base import Channel, IncomingMessage
from agent.core.attachments import MessageAttachment, attachment_kind_for_mime
from agent.core.output import OutputSink
from agent.pathing import path_contains
from agent.scheduler.models import parse_task_signal, run_needs_attention
from agent.session_service import SessionService

logger = logging.getLogger(__name__)

# How long a browser is given to answer an approval prompt before the pending
# action is declined.  Sent to the client as ``timeout_seconds`` so the UI can
# count down honestly instead of going quiet and letting the approval expire.
CONFIRMATION_TIMEOUT_SECONDS = 120

# Approval decisions a client may return.  ``allow_session`` widens the consent
# from "this one command" to "this command for the rest of the session" and is
# only offered when the pending record can actually carry that intent.
CONFIRM_DECISIONS = ("allow_once", "allow_session", "deny")

def _normalize_confirm_decision(payload: dict[str, Any]) -> str:
    """Map a client reply onto one of ``CONFIRM_DECISIONS``.

    Accepts both the rich ``{"decision": "allow_session"}`` form and the
    original ``{"approved": true}`` boolean, so an already-open browser tab
    keeps working across a backend upgrade.
    """
    raw = str(payload.get("decision") or "").strip().casefold()
    if raw == "allow_session":
        return "allow_session"
    if raw in ("allow_once", "allow", "approve"):
        return "allow_once"
    if raw in ("deny", "reject"):
        return "deny"
    return "allow_once" if payload.get("approved") else "deny"


async def _pick_workspace_directory() -> str | None:
    """Open the host OS folder picker and return the selected directory."""
    import sys

    if sys.platform == "darwin":
        script = (
            'set pickedFolder to choose folder with prompt "选择 Agent 项目文件夹"\n'
            'POSIX path of pickedFolder'
        )
        args = ["osascript", "-e", script]
    elif sys.platform.startswith("win"):
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d=New-Object System.Windows.Forms.FolderBrowserDialog; "
            "if($d.ShowDialog() -eq 'OK'){[Console]::Write($d.SelectedPath)}"
        )
        args = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    else:
        args = ["zenity", "--file-selection", "--directory", "--title=选择 Agent 项目文件夹"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except (FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    selected = stdout.decode("utf-8", errors="replace").strip()
    return selected or None


def _web_session_output_dir(session_id: str) -> Path:
    """Return the isolated output directory for a Web session."""
    try:
        return shared.web_session_home(str(session_id)) / "output"
    except ValueError:
        return shared.AGENT_HOME / "web" / "sessions" / "unknown" / "output"


def _web_session_upload_dir(session_id: str) -> Path:
    try:
        return shared.web_session_home(str(session_id)) / "uploads"
    except ValueError:
        return shared.AGENT_HOME / "web" / "sessions" / "unknown" / "uploads"


def _scheduler_run_payload(run: Any) -> dict[str, Any]:
    output_path = str(getattr(run, "output_path", "") or "").strip()
    output_available = False
    if output_path:
        try:
            output_available = Path(output_path).expanduser().is_file()
        except OSError:
            pass
    started_at = getattr(run, "started_at", None)
    finished_at = getattr(run, "finished_at", None)
    duration_ms = None
    if started_at is not None and finished_at is not None:
        duration_ms = max(0, round((finished_at - started_at).total_seconds() * 1000))
    return {
        "id": str(getattr(run, "id", "") or ""),
        "task_id": str(getattr(run, "task_id", "") or ""),
        "status": str(getattr(run, "status", "") or ""),
        "scheduled_for": (
            run.scheduled_for.isoformat() if getattr(run, "scheduled_for", None) else None
        ),
        "started_at": started_at.isoformat() if started_at else None,
        "finished_at": finished_at.isoformat() if finished_at else None,
        "duration_ms": duration_ms,
        "summary": str(getattr(run, "summary", "") or ""),
        "error": str(getattr(run, "error", "") or ""),
        "delivery_status": str(getattr(run, "delivery_status", "") or ""),
        "trigger_source": str(getattr(run, "trigger_source", "schedule") or "schedule"),
        "attempt": int(getattr(run, "attempt", 1) or 1),
        "cancel_requested_at": (
            run.cancel_requested_at.isoformat()
            if getattr(run, "cancel_requested_at", None)
            else None
        ),
        "retry_of_run_id": str(getattr(run, "retry_of_run_id", "") or ""),
        "missed_count": int(getattr(run, "missed_count", 0) or 0),
        "acknowledged_at": (
            run.acknowledged_at.isoformat()
            if getattr(run, "acknowledged_at", None)
            else None
        ),
        "needs_attention": run_needs_attention(run),
        "config_snapshot": dict(getattr(run, "config_snapshot", {}) or {}),
        "output_available": output_available,
        "output_url": (
            _scheduler_output_url(
                str(getattr(run, "task_id", "") or ""),
                str(getattr(run, "id", "") or ""),
                output_path,
            )
            if output_available
            else ""
        ),
    }


def _scheduler_output_url(task_id: str, run_id: str, output_path: str) -> str:
    """Build the ``/api/files`` link for a scheduled run's stored output.

    The link names the run it belongs to so ``GET /api/files`` can verify the
    entitlement against the scheduler store instead of trusting the path.
    """
    return (
        "/api/files?path="
        + quote(str(output_path), safe="")
        + "&task_id="
        + quote(str(task_id), safe="")
        + "&run_id="
        + quote(str(run_id), safe="")
    )


def _scheduler_task_payload(
    task: Any, latest_run: Any = None, unseen_attention: int = 0
) -> dict[str, Any]:
    return {
        "id": task.id,
        "name": task.name,
        "kind": task.kind,
        "enabled": task.enabled,
        "trigger_type": task.trigger.trigger_type,
        "trigger": task.trigger.payload,
        "payload": task.payload,
        "delivery_mode": task.delivery_mode,
        "delivery_target": {
            "target_type": task.delivery_target.target_type,
            "payload": task.delivery_target.payload,
        },
        "model_override": task.model_override,
        "workspace_root": task.workspace_root,
        "context_policy": task.context_policy,
        "timeout_seconds": task.timeout_seconds,
        "retry_policy": task.retry_policy,
        "selected_skills": task.selected_skills,
        "permission_profile": task.permission_profile,
        # Empty for a standalone task, which most are.  Carried on the task
        # rather than looked up from the graph so a task row in the list can
        # say where it belongs without the client holding the whole graph.
        "workflow_id": str(getattr(task, "workflow_id", "") or ""),
        "step_key": str(getattr(task, "step_key", "") or ""),
        "unseen_attention": int(unseen_attention or 0),
        "active_run_id": task.active_run_id,
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "last_run_at": task.last_run_at.isoformat() if task.last_run_at else None,
        "last_success_at": (
            task.last_success_at.isoformat() if task.last_success_at else None
        ),
        "latest_run": _scheduler_run_payload(latest_run) if latest_run else None,
    }


def _workflow_payload(
    workflow: Any,
    tasks_by_step: dict[str, Any],
    unseen_attention: dict[str, int],
    latest_runs: dict[str, Any],
) -> dict[str, Any]:
    """One workflow, with each step's task and its latest state.

    The graph is sent as the client needs it to draw: every step names its
    upstreams by key, so the client never has to parse a signal name to work
    out what depends on what.  The task ids travel alongside because the run
    history, the cancel button and the output links are all keyed by them.
    """
    steps: list[dict[str, Any]] = []
    attention = 0
    for step in workflow.steps:
        key = str(step.key).strip()
        task = tasks_by_step.get(key)
        task_id = task.id if task is not None else ""
        unseen = int(unseen_attention.get(task_id, 0)) if task_id else 0
        latest = latest_runs.get(task_id) if task_id else None
        attention += unseen
        steps.append(
            {
                "key": key,
                "name": step.name,
                "kind": step.kind,
                "payload": step.payload,
                "depends_on": list(step.depends_on),
                "trigger_type": (
                    step.trigger.trigger_type if step.trigger is not None else "signal"
                ),
                "trigger": step.trigger.payload if step.trigger is not None else {},
                "workspace_root": step.workspace_root,
                "permission_profile": step.permission_profile,
                "timeout_seconds": int(step.timeout_seconds),
                "task_id": task_id,
                "enabled": bool(task.enabled) if task is not None else False,
                "unseen_attention": unseen,
                "latest_run": _scheduler_run_payload(latest) if latest else None,
            }
        )
    return {
        "id": workflow.id,
        "name": workflow.name,
        "description": workflow.description,
        "enabled": bool(workflow.enabled),
        "created_at": (
            workflow.created_at.isoformat() if workflow.created_at else None
        ),
        "updated_at": (
            workflow.updated_at.isoformat() if workflow.updated_at else None
        ),
        "steps": steps,
        "unseen_attention": attention,
    }


def _scheduler_output_path(task_id: str, run: Any) -> Path | None:
    raw = str(getattr(run, "output_path", "") or "").strip()
    if not raw:
        return None
    try:
        resolved = Path(raw).expanduser().resolve()
    except OSError:
        return None
    # Standalone scheduler output is always <output-root>/<task-id>/<run-id>.md.
    # Validate that shape so an unexpected database value cannot expose an
    # unrelated local file through the result endpoint.
    if resolved.parent.name != task_id or resolved.name != f"{run.id}.md":
        return None
    return resolved

# File extensions treated as attachable media (image/audio/video).
_MEDIA_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tif", ".tiff",
    ".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac",
    ".mp4", ".mov", ".webm", ".mkv", ".avi",
}

_RASTER_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}


def _svg_has_raster_sibling(path: Path) -> bool:
    """True when ``path`` is an SVG source with a raster image sibling (X.svg.png)."""
    if path.suffix.lower() != ".svg":
        return False
    try:
        # The skill writes both ``X.svg`` and a raster sibling named after the
        # full source file (``X.svg.png``), not after the bare stem (``X.png``),
        # so build the candidate from ``path.name``.
        for rs in _RASTER_EXTS:
            if (path.parent / (path.name + rs)).is_file():
                return True
    except OSError:
        return False
    return False


@dataclass
class WebConfig:
    """Configuration for the web channel.

    ``cors_origins`` accepts a list (from JSON config) or tuple; the channel
    normalises it to a tuple internally.
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8787
    auth_token: str = ""
    cors_origins: tuple[Any, ...] = field(default_factory=tuple)


class WebOutputSink(OutputSink):
    """Serialises agent output events to JSON for a web client.

    With ``websocket`` set, events are queued and sent as JSON objects by a
    background sender task; call ``flush()`` after each turn and ``close()``
    when the socket is done.  Without a websocket, events are simply collected
    for the non-streaming HTTP path.
    """

    def __init__(
        self,
        websocket: Any = None,
        *,
        collect: bool = False,
        on_attachment: Any = None,
        output_dir: Any = None,
        confirmation_handler: Any = None,
    ) -> None:
        self._websocket = websocket
        self._collect = collect or websocket is None
        self._events: list[dict[str, Any]] = []
        self._attachments: list[str] = []
        self._queued_attachment_paths: set[str] = set()
        self._turn_complete_emitted = False
        self._completion_event = asyncio.Event()
        self.on_attachment = on_attachment
        self._confirmation_handler = confirmation_handler
        # Session output directory scanned for media produced during a turn so
        # images/audio/video show inline even if the agent never called send_file.
        self._output_dir = output_dir
        self._turn_started_at = 0.0
        self._attached_paths: set[str] = set()
        self.full_text = ""
        self._turn_started = False
        self._queue: Optional[asyncio.Queue[dict[str, Any] | None]] = None
        self._sender_task: Optional[asyncio.Task[Any]] = None
        if not self._collect:
            self._queue = asyncio.Queue()
            self._sender_task = asyncio.create_task(self._sender())

    # -- OutputSink overrides -------------------------------------------------

    def on_stream_chunk(self, chunk: str) -> None:
        self.full_text += chunk
        self._emit({"type": "stream_chunk", "chunk": chunk})

    def on_turn_complete(self, full_text: str, tool_calls: list[str]) -> None:
        if full_text:
            self.full_text = full_text
        self._turn_complete_emitted = True
        self._completion_event.set()
        self._emit(
            {
                "type": "turn_complete",
                "text": self.full_text,
                "tool_calls": list(tool_calls),
            }
        )

    def mark_turn_start(self) -> None:
        """Reset the per-message completion marker used by WebSocket delivery."""
        self._turn_complete_emitted = False
        self._turn_started = True
        self._turn_started_at = time.time()
        # ``_attachments`` / ``_attached_paths`` are per-turn transient state.  A
        # sink is reused across turns on a WebSocket connection; without clearing
        # them here a media path attached in an earlier turn would suppress the
        # auto-scan when the file is regenerated later, so a fresh image/audio/
        # video would never show inline.
        self._attachments.clear()
        self._queued_attachment_paths.clear()
        self._attached_paths.clear()

    @property
    def turn_complete_emitted(self) -> bool:
        return self._turn_complete_emitted

    @property
    def streaming_snapshot(self) -> dict[str, Any] | None:
        """Return a lightweight snapshot used when a browser reconnects.

        A session can keep running while its browser tab is switched away.
        Rebinding the sink and sending this snapshot lets the new tab render
        the already-generated text immediately instead of waiting for the
        final turn event.
        """
        # Empty sinks are usually queued messages waiting behind the active
        # operation. Do not replay them as additional blank assistant bubbles
        # when a browser switches back to this session.
        if not self._turn_started or self._turn_complete_emitted or not self.full_text:
            return None
        return {"type": "stream_snapshot", "text": self.full_text}

    def set_websocket(self, websocket: Any) -> None:
        """Route subsequent events to the currently selected browser tab."""
        self._websocket = websocket

    def on_tool_start(self, name: str, inputs: dict) -> None:
        self._emit({"type": "tool_start", "name": name, "inputs": dict(inputs)})

    def on_tool_end(self, name: str, result: str) -> None:
        self._emit({"type": "tool_end", "name": name, "result": result})

    def on_tool_progress(self, name: str, progress: Any) -> None:
        self._emit(
            {
                "type": "tool_progress",
                "name": name,
                "progress": _jsonable(progress),
            }
        )

    def on_tool_blocked(self, name: str, reason: str) -> None:
        self._emit({"type": "tool_blocked", "name": name, "reason": reason})

    def on_status(self, text: str, *, level: str = "info") -> None:
        self._emit({"type": "status", "text": text, "level": level})

    def on_error(self, error: str) -> None:
        self._emit({"type": "error", "error": error})

    def on_info(self, content: Any) -> None:
        self._emit({"type": "info", "content": _jsonable(content)})

    def on_notification(self, title: str, body: str, *, level: str = "info") -> None:
        self._emit({"type": "notification", "title": title, "body": body, "level": level})

    def on_workspace_changed(
        self,
        workspace_root: str,
        *,
        workspace_read: bool = True,
        workspace_write: bool = False,
        status: str = "ready",
    ) -> None:
        self._emit(
            {
                "type": "workspace_changed",
                "workspace_root": workspace_root,
                "workspace_read": bool(workspace_read),
                "workspace_write": bool(workspace_write),
                "status": status,
            }
        )

    def on_subagent_event(self, event: Any) -> None:
        # ``SubAgentProgressEvent`` carries ``kind`` / ``role`` / ``message`` and
        # an optional ``completed``/``total`` pair — not the ``agent``/``event``
        # /``detail`` triple an earlier revision of this sink read.  Reading the
        # wrong attributes silently produced empty strings, so every sub-agent
        # update reached the browser as an unlabelled "状态更新" row.
        self._emit(
            {
                "type": "subagent_event",
                "kind": str(getattr(event, "kind", "") or ""),
                "role": str(getattr(event, "role", "") or ""),
                "message": str(getattr(event, "message", "") or ""),
                "completed": int(getattr(event, "completed", 0) or 0),
                "total": int(getattr(event, "total", 0) or 0),
            }
        )

    def on_heartbeat(
        self,
        *,
        elapsed_seconds: float,
        current_op: str,
        op_detail: str = "",
        pending_messages: int = 0,
    ) -> None:
        self._emit(
            {
                "type": "heartbeat",
                "elapsed_seconds": elapsed_seconds,
                "current_op": current_op,
                "op_detail": op_detail,
                "pending_messages": pending_messages,
            }
        )

    def queue_attachment(self, path: Any) -> object | None:
        """Queue an attachment path for the next attachment flush."""
        value = str(path)
        key = self._attachment_key(value)
        if key not in self._queued_attachment_paths and key not in self._attached_paths:
            self._attachments.append(value)
            self._queued_attachment_paths.add(key)
        return value

    @staticmethod
    def _attachment_key(path: Any) -> str:
        """Return a stable key for equivalent local attachment paths."""
        value = str(path or "").strip()
        if not value:
            return ""
        try:
            return str(Path(value).expanduser().resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            return value

    async def flush_attachments(self) -> None:
        """Emit queued attachment events, auto-attach session media, and clear."""
        for path in self._attachments:
            key = self._attachment_key(path)
            if key in self._attached_paths:
                continue
            name = Path(path).name
            self._emit(
                {"type": "attachment", "path": path, "name": name}
            )
            # Mark as already attached so the auto-scan below won't re-emit it.
            self._attached_paths.add(key)
            cb = getattr(self, "on_attachment", None)
            if callable(cb):
                try:
                    cb(path, name)
                except Exception:
                    logger.exception("failed to journal attachment: %s", path)
        self._attachments.clear()
        self._queued_attachment_paths.clear()
        self._auto_attach_media()
        await self.flush()

    async def wait_for_completion(self, timeout: float = 3600.0) -> bool:
        """Wait until the coordinator finishes this sink's queued turn."""
        if self._turn_complete_emitted:
            return True
        try:
            await asyncio.wait_for(self._completion_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return self._turn_complete_emitted

    def _auto_attach_media(self) -> None:
        """Attach media files the agent created in this session's output dir.

        The agent often writes an image/audio/video into the session home and
        narrates that it was sent without ever calling ``send_file``.  To keep
        the web UI consistent (media shows inline, live and in history), scan the
        session output dir for media created during this turn and emit+journal a
        normal ``attachment`` event for each one not already queued.
        """
        out = getattr(self, "_output_dir", None)
        if out is None:
            return
        try:
            out = Path(out)
        except Exception:
            return
        if not out.is_dir():
            return
        turn_started = float(getattr(self, "_turn_started_at", 0.0) or 0.0)
        if turn_started <= 0:
            return
        cb = getattr(self, "on_attachment", None)
        attached = getattr(self, "_attached_paths", set())
        now = time.time()
        try:
            for p in out.iterdir():
                if not p.is_file():
                    continue
                if p.suffix.lower() not in _MEDIA_EXTS:
                    continue
                # Skip an SVG source when a raster sibling (e.g. X.svg -> X.svg.png)
                # exists: the skill writes both, and rendering both is a duplicate.
                if p.suffix.lower() == ".svg":
                    if _svg_has_raster_sibling(p):
                        continue
                s = str(p)
                key = self._attachment_key(s)
                if key in attached:
                    continue
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                # Created during this turn (allow small clock skew); ignore
                # future-dated files to avoid weird clock errors.
                if mtime < turn_started - 1 or mtime > now + 60:
                    continue
                attached.add(key)
                name = p.name
                self._emit({"type": "attachment", "path": s, "name": name})
                if callable(cb):
                    try:
                        cb(s, name)
                    except Exception:
                        logger.exception("failed to journal attachment: %s", s)
        except Exception:
            logger.exception("auto-attach media scan failed")

    def defer_temporary_attachment_cleanup(self, receipt: object) -> bool:
        return True

    async def on_tool_confirmation(
        self,
        name: str,
        *,
        command: str,
        risk_level: str,
        reason: str,
        confirmation_token: str,
        scope: Any,
    ) -> bool:
        """Ask a connected web client for approval; deny when unavailable.

        The prompt is advisory to the browser only: whatever the human picks
        still has to be redeemed against the pending record by the caller, so a
        forged ``allow_session`` reply cannot widen consent for a token the
        server never minted.
        """
        if self._collect or self._websocket is None:
            return False
        allow_session = self._session_scope_supported(
            name, confirmation_token, scope
        )
        handler = self._confirmation_handler
        # Claim the answer slot *before* the prompt goes out.  Registering it
        # afterwards left a window in which a fast client's reply reached the
        # socket reader while nothing was waiting on the token yet; the reply
        # was dropped on the floor and the prompt then sat until it timed out.
        pending = handler(confirmation_token) if callable(handler) else None
        self._emit(
            {
                "type": "confirm_request",
                "name": name,
                "command": command,
                "risk_level": risk_level,
                "reason": reason,
                "confirmation_token": confirmation_token,
                "allow_session": allow_session,
                "timeout_seconds": CONFIRMATION_TIMEOUT_SECONDS,
            }
        )
        await self.flush()
        decision = "deny"
        if pending is not None:
            try:
                decision = _normalize_confirm_decision(
                    {"decision": await pending}
                )
            except Exception:
                return False
        else:
            # Collection/testing sinks have no shared WebSocket reader.
            try:
                data = await asyncio.wait_for(
                    self._websocket.receive_json(),
                    timeout=CONFIRMATION_TIMEOUT_SECONDS,
                )
            except Exception:
                return False
            if not isinstance(data, dict) or data.get("type") != "confirm_response":
                return False
            decision = _normalize_confirm_decision(data)
        if decision == "deny":
            return False
        if decision == "allow_session" and allow_session:
            # Hand the intent to the redeem step; it owns the pending record.
            from agent.security.shell import shell_pending_mark_session_scope

            try:
                shell_pending_mark_session_scope(
                    confirmation_token, scope=scope
                )
            except Exception:
                logger.debug("session-scope consent mark failed", exc_info=True)
        return True

    @staticmethod
    def _session_scope_supported(
        name: str, confirmation_token: str, scope: Any
    ) -> bool:
        """Whether "always allow this command" is meaningful for this prompt.

        Only shell approvals carry a redeemable pending record plus an
        authorization scope, which is exactly the pair a session-wide entry is
        keyed by.  Plugin installs and memory clears are one-shot by design and
        must never advertise the option.
        """
        if str(name or "") != "shell" or not str(confirmation_token or ""):
            return False
        from agent.security.shell import ShellAuthorizationScope

        return isinstance(scope, ShellAuthorizationScope)

    # -- collection helpers ----------------------------------------------------

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    async def flush(self, timeout: float = 30.0) -> None:
        if self._queue is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def close(self) -> None:
        if self._queue is not None and self._sender_task is not None:
            await self._queue.put(None)
            try:
                await asyncio.wait_for(self._sender_task, timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                self._sender_task.cancel()
        self._sender_task = None
        self._queue = None

    # -- internals ------------------------------------------------------------

    def _emit(self, event: dict[str, Any]) -> None:
        if self._collect:
            self._events.append(event)
            return
        if self._queue is not None:
            self._queue.put_nowait(event)

    async def _sender(self) -> None:
        assert self._queue is not None and self._websocket is not None
        while True:
            event = await self._queue.get()
            if event is None:
                self._queue.task_done()
                break
            try:
                await self._websocket.send_json(event)
            except Exception:
                pass
            finally:
                self._queue.task_done()


def _mask_api_keys(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``cfg`` with provider API keys masked for display."""
    import copy

    masked = copy.deepcopy(cfg)
    providers = masked.get("providers")
    if isinstance(providers, dict):
        for provider in providers.values():
            if isinstance(provider, dict) and provider.get("api_key"):
                provider["api_key"] = "******"
    return masked


def _restore_masked_api_keys(new_cfg: dict[str, Any], current_cfg: dict[str, Any]) -> None:
    """Keep existing API keys when the UI saved the masked placeholder back."""
    providers = new_cfg.get("providers")
    current_providers = current_cfg.get("providers")
    if not isinstance(providers, dict) or not isinstance(current_providers, dict):
        return
    for name, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        if provider.get("api_key") == "******":
            current = current_providers.get(name)
            if isinstance(current, dict) and current.get("api_key"):
                provider["api_key"] = current["api_key"]


def _jsonable(value: Any) -> Any:
    """Best-effort conversion for values that are not JSON-native."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except Exception:
            return str(value)
    return str(value)


class WebChannel(Channel):
    """Starlette-based channel for a browser frontend."""

    def __init__(self, config: WebConfig) -> None:
        self._config = config
        self._handler: Optional[
            Callable[[IncomingMessage, OutputSink], Any]
        ] = None
        self._sessions: dict[str, Any] = {}
        self._live_sinks: dict[str, set[WebOutputSink]] = {}
        self._components: dict[str, Any] = {}
        self._session_service: Optional[SessionService] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._server_task: Optional[asyncio.Task[Any]] = None
        self._app = self._build_app()

    @property
    def app(self) -> Any:
        return self._app

    @staticmethod
    def _web_dist_dir() -> Path:
        """Resolve the frontend bundle used by the gateway.

        The bundled copy is the canonical runtime asset and is checked into
        the package.  ``frontend/dist`` is intentionally ignored by git and
        may contain an older local build, so it must never silently override
        the bundled assets after a restart.  Set ``SIMPLE_WEB_USE_SOURCE_DIST``
        to opt into source-dist serving during local frontend development.
        """
        package_dist = Path(__file__).resolve().parent.parent / "_builtin" / "web" / "dist"
        source_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
        # Vite can leave ``index.html`` behind when a build is interrupted or
        # when only the source tree has been copied.  Treat that as an
        # incomplete bundle; StaticFiles raises during app construction if the
        # referenced assets directory is missing.  Falling back to the bundled
        # release keeps the gateway usable until the next frontend build.
        use_source = os.getenv("SIMPLE_WEB_USE_SOURCE_DIST", "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if use_source and (source_dist / "index.html").is_file() and (source_dist / "assets").is_dir():
            return source_dist
        return package_dist

    def bind_runtime(
        self,
        sessions: dict[str, Any],
        components: dict[str, Any],
    ) -> None:
        """Receive the channel runner's live-session dict and components."""
        self._sessions = sessions
        self._components = components
        base_ctx_mgr = components.get("context_manager")
        store = getattr(base_ctx_mgr, "store", None)
        self._session_service = SessionService(
            store=store,
            live_states=sessions,
            store_factory=components.get("session_store_factory"),
            runtime_cleanup=lambda sid: (
                self._components.get("session_runtime_cleanup")(sid)
                if callable(self._components.get("session_runtime_cleanup"))
                else None
            ),
        )

    def _service(self) -> SessionService:
        if self._session_service is None:
            self._session_service = SessionService(live_states=self._sessions)
        return self._session_service

    @staticmethod
    def _resolve_model_override(raw_model: Any) -> str | None:
        """Validate a model selected by the browser.

        The input control sends a model id, not a provider configuration.
        Keep this value scoped to the current turn and only accept ids the
        routing layer can dispatch — every configured provider's models, not
        just the active provider's (RoutingTransport owns the model -> client
        mapping, so a foreign provider's id is a valid per-turn override).
        """
        if raw_model is None:
            return None
        if not isinstance(raw_model, str):
            raise ValueError("model must be a string")
        model = raw_model.strip()
        if not model:
            return None

        cfg: dict[str, Any] = {}
        try:
            from agent.config import load_config
            from agent.core.transport import routable_model_ids

            cfg, _ = load_config()
            configured = routable_model_ids(cfg)
        except Exception:
            configured = set()
        if configured and model not in configured:
            raise ValueError("model is not available in the current configuration")
        if not configured:
            raise ValueError("no configured models are available")
        return model

    def _authorized(self, request: Any) -> bool:
        token = self._config.auth_token.strip()
        if not token:
            return True
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip() == token
        if request.headers.get("x-auth-token", "").strip() == token:
            return True
        query = getattr(request, "query_params", None)
        if query is not None:
            try:
                if str(query.get("token", "") or "").strip() == token:
                    return True
            except Exception:
                pass
        return False

    def _ensure_router(self) -> Any:
        router = self._components.get("command_router")
        if router is not None:
            return router
        from agent.commands import CommandRouter, register_builtin_commands

        router = CommandRouter(skill_catalog=self._components.get("skill_catalog"))
        register_builtin_commands(router)
        plugin_catalog = self._components.get("plugin_catalog")
        if plugin_catalog is not None and hasattr(
            plugin_catalog, "get_slash_commands"
        ):
            router.register_plugin_catalog(plugin_catalog)
        self._components["command_router"] = router
        return router

    # -- HTTP endpoints -------------------------------------------------------

    async def _index(self, request: Any) -> Any:
        from starlette.responses import HTMLResponse

        dist = self._web_dist_dir() / "index.html"
        if dist.is_file():
            return HTMLResponse(dist.read_text(encoding="utf-8"))
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h1>Simple Agent API</h1>"
            "<p>This is the API endpoint for web integrations.</p>"
            "</body></html>"
        )

    async def _health(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        return JSONResponse({"ok": True, "home": str(shared.AGENT_HOME)})

    async def _commands(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            router = self._ensure_router()
            descriptors = router.visible_descriptors("web")
            commands = [
                {
                    "name": d.name,
                    "aliases": list(d.aliases),
                    "usage": d.usage or f"/{d.name}",
                    "description": d.description,
                    "concurrency": d.concurrency,
                }
                for d in descriptors
            ]
            catalog = self._components.get("skill_catalog")
            if catalog is not None:
                commands.extend({
                    "name": bundle.id,
                    "aliases": [],
                    "usage": f"/{bundle.id}",
                    "description": bundle.description or "技能",
                    "concurrency": "queue",
                    "kind": "skill",
                } for bundle in catalog.list_skills() if getattr(bundle, "user_invocable", True))
        except Exception:
            commands = []
        return JSONResponse({"commands": commands})

    async def _config_get(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.config import load_config

        cfg, _ = load_config()
        masked = _mask_api_keys(cfg)
        return JSONResponse({"config": masked})

    async def _config_save(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        from agent.config import load_config, save_config

        current, _ = load_config()
        new_cfg = body.get("config", body)
        if not isinstance(new_cfg, dict):
            return JSONResponse({"error": "config must be a dict"}, status_code=400)
        _restore_masked_api_keys(new_cfg, current)
        try:
            save_config(new_cfg)
        except Exception as exc:
            return JSONResponse({"error": f"save failed: {exc}"}, status_code=500)
        # Existing turns continue with their captured components; the next
        # turn will rebuild each session runtime against the new global config.
        self._components["config_revision"] = int(self._components.get("config_revision", 0)) + 1
        return JSONResponse({"ok": True})

    async def _plugins(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        plugins: list[dict[str, Any]] = []
        catalog = self._components.get("plugin_catalog")
        if catalog is not None:
            list_plugins = getattr(catalog, "list_plugins", None)
            if callable(list_plugins):
                try:
                    for meta in list_plugins():
                        plugins.append(
                            {
                                "name": getattr(meta, "name", "unknown"),
                                "version": getattr(meta, "version", "") or "",
                                "description": getattr(meta, "description", "") or "",
                                "source": getattr(meta, "source", "") or "",
                                "enabled": bool(getattr(meta, "enabled", True)),
                            }
                        )
                except Exception:
                    plugins = []
        return JSONResponse({"plugins": plugins})

    async def _skills(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        skills: list[dict[str, Any]] = []
        catalog = self._components.get("skill_catalog")
        if catalog is not None:
            list_skills = getattr(catalog, "list_skills", None)
            if callable(list_skills):
                try:
                    for bundle in list_skills():
                        skills.append(
                            {
                                "id": getattr(bundle, "id", "unknown"),
                                "name": getattr(bundle, "name", "") or "",
                                "description": getattr(bundle, "description", "") or "",
                                "source": getattr(bundle, "source", "") or "",
                                "user_invocable": bool(
                                    getattr(bundle, "user_invocable", True)
                                ),
                                "disable_model_invocation": bool(
                                    getattr(bundle, "disable_model_invocation", False)
                                ),
                                "path": str(getattr(bundle, "path", "") or ""),
                            }
                        )
                except Exception:
                    skills = []
        return JSONResponse({"skills": skills})

    async def _upload_attachment(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = str(request.path_params["session_id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", session_id):
            return JSONResponse({"error": "invalid session id"}, status_code=400)
        try:
            form = await request.form()
            uploads = form.getlist("files") if hasattr(form, "getlist") else []
        except Exception as exc:
            return JSONResponse({"error": f"invalid multipart body: {exc}"}, status_code=400)
        if not uploads:
            return JSONResponse({"error": "files are required"}, status_code=400)
        if len(uploads) > 12:
            return JSONResponse({"error": "最多上传 12 个文件"}, status_code=400)
        root = _web_session_upload_dir(session_id).resolve()
        root.mkdir(parents=True, exist_ok=True)
        result: list[dict[str, Any]] = []
        max_size = 25 * 1024 * 1024
        for upload in uploads:
            filename = Path(str(getattr(upload, "filename", "") or "附件")).name
            if not filename or filename in {".", ".."}:
                return JSONResponse({"error": "invalid filename"}, status_code=400)
            content_type = str(getattr(upload, "content_type", "") or "application/octet-stream")
            target = (root / f"{uuid.uuid4().hex[:12]}-{filename}").resolve()
            if root not in target.parents:
                return JSONResponse({"error": "invalid upload path"}, status_code=400)
            size = 0
            try:
                with target.open("wb") as out:
                    while True:
                        chunk = await upload.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_size:
                            target.unlink(missing_ok=True)
                            return JSONResponse({"error": "单个文件不能超过 25 MB"}, status_code=413)
                        out.write(chunk)
            except Exception as exc:
                target.unlink(missing_ok=True)
                return JSONResponse({"error": f"upload failed: {exc}"}, status_code=500)
            result.append({
                "id": target.name,
                "filename": filename,
                "mime_type": content_type,
                "kind": attachment_kind_for_mime(content_type),
                "path": str(target),
                "size_bytes": size,
            })
        return JSONResponse({"attachments": result})

    def _trigger_from_body(self, body: dict[str, Any], existing: Any = None):
        """Turn the trigger fields of a request body into a ``TriggerSpec``.

        Shared by the schedule API and by a workflow's entry step.  A second
        copy of these rules would be free to disagree with this one about what
        "每周三 09:00" means, and the disagreement would show up as a task that
        fires on the wrong day rather than as an error.
        """
        from agent.scheduler import TriggerSpec

        trigger_type = str(
            body.get(
                "trigger_type",
                getattr(getattr(existing, "trigger", None), "trigger_type", "once"),
            )
        ).lower()
        timezone_name = str(body.get("timezone_name", "UTC")).strip() or "UTC"
        if trigger_type == "once":
            trigger = TriggerSpec.once(body["at"], timezone_name)
            if trigger.initial_run_at() <= datetime.now(timezone.utc):
                raise ValueError("执行时间必须晚于当前时间")
        elif trigger_type == "interval":
            every = int(body["every"])
            if every < 1:
                raise ValueError("重复间隔必须大于 0")
            trigger = TriggerSpec.interval(
                every, str(body["unit"]), body["anchor_at"], timezone_name
            )
        elif trigger_type == "daily":
            trigger = TriggerSpec.daily(str(body["time_of_day"]), timezone_name)
        elif trigger_type == "weekly":
            trigger = TriggerSpec.weekly(
                str(body["day_of_week"]), str(body["time_of_day"]), timezone_name
            )
        elif trigger_type == "weekdays":
            trigger = TriggerSpec.weekdays(str(body["time_of_day"]), timezone_name)
        elif trigger_type == "monthly":
            trigger = TriggerSpec.monthly(
                int(body["day_of_month"]), str(body["time_of_day"]), timezone_name
            )
        elif trigger_type == "signal":
            signal_name = str(body.get("signal_name", "")).strip()
            if not signal_name:
                raise ValueError("请选择或填写要等待的信号")
            # A short-lived connection of its own, because this function
            # deliberately knows nothing about the store: it turns a request
            # body into a spec, and the caller owns persistence.  The check
            # needs to read tasks, so it borrows a connection rather than
            # widening the function's contract for one validation.
            from agent.scheduler import SchedulerStore

            probe = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
            try:
                problem = probe.describe_signal_problem(signal_name)
            finally:
                probe.close()
            if problem:
                raise ValueError(f"信号「{signal_name}」无法生效：{problem}")
            trigger = TriggerSpec.signal(signal_name)
        else:
            raise ValueError("不支持的执行计划")
        trigger.instantiate().next_after(datetime.now(timezone.utc))
        return trigger

    def _schedule_from_body(
        self, body: dict[str, Any], existing: Any = None, *, keep_trigger: bool = False
    ):
        from agent.scheduler import DeliveryTarget, NewScheduledTask, TriggerSpec
        from agent.scheduler.profiles import PERMISSION_PROFILES

        name = str(body.get("name", getattr(existing, "name", ""))).strip()
        if not name:
            raise ValueError("任务名称不能为空")
        if len(name) > 80:
            raise ValueError("任务名称不能超过 80 个字符")

        if keep_trigger:
            # A step with upstreams does not own its trigger: the graph says it
            # waits for them, and the task's own signal trigger is that answer
            # written down.  Letting the task editor set a second one would
            # either replace the edge with a clock or leave one of the two
            # lying, and the task editor has no field that could express
            # "after A and B succeed" anyway.  Saying so beats ignoring it.
            asked = str(body.get("trigger_type", "")).strip().lower()
            if asked and asked != existing.trigger.trigger_type:
                raise ValueError(
                    "这一步的触发方式由它上游的步骤决定，不能在这里改成别的"
                )
            trigger = existing.trigger
        else:
            trigger = self._trigger_from_body(body, existing)

        existing_kind = str(getattr(existing, "kind", "agent_prompt"))
        default_action = "message" if existing_kind == "message" else "agent_task"
        action = str(body.get("action_type", default_action))
        if action == "agent_task":
            task_kind = "agent_prompt"
            payload = {"prompt": str(body.get("prompt", "")).strip()}
            max_content_length = 6000
        elif action == "message":
            task_kind = "message"
            payload = {"message_text": str(body.get("message_text", "")).strip()}
            max_content_length = 2000
        else:
            raise ValueError("不支持的任务类型")
        content = str(next(iter(payload.values()), ""))
        if not content:
            raise ValueError("任务内容不能为空")
        if len(content) > max_content_length:
            raise ValueError(f"任务内容不能超过 {max_content_length} 个字符")

        permission_profile = str(
            body.get(
                "permission_profile",
                getattr(existing, "permission_profile", "inherit"),
            )
        )
        if permission_profile not in PERMISSION_PROFILES:
            raise ValueError("不支持的权限策略")
        profile = PERMISSION_PROFILES[permission_profile]

        # Resolved before the fallback chain on purpose.  A profile that
        # grants writes needs a directory a *person* chose: falling back to
        # the gateway process's working directory would make the task write
        # somewhere nobody can predict from its definition.
        chosen_workspace = str(
            body.get("workspace_root")
            or getattr(existing, "workspace_root", "")
            or ""
        ).strip()
        if profile.requires_workspace_root and not chosen_workspace:
            raise ValueError(
                f"权限策略「{profile.label}」需要显式指定项目文件夹，"
                "不能回落到服务进程的当前目录"
            )
        workspace_value = chosen_workspace or str(
            self._components.get("workspace_root") or Path.cwd()
        )
        workspace = Path(workspace_value).expanduser().resolve(strict=False)
        if task_kind == "agent_prompt" and not workspace.is_dir():
            raise ValueError(f"项目文件夹不存在：{workspace}")

        context_policy = str(
            body.get("context_policy", getattr(existing, "context_policy", "stateless"))
        )
        if context_policy not in {"stateless", "task_history", "shared_memory"}:
            raise ValueError("不支持的上下文策略")
        timeout_seconds = int(
            body.get("timeout_seconds", getattr(existing, "timeout_seconds", 1800))
        )
        if timeout_seconds < 10 or timeout_seconds > 604800:
            raise ValueError("超时时间必须在 10 秒到 7 天之间")
        raw_retry = body.get("retry_policy", getattr(existing, "retry_policy", {}))
        retry = dict(raw_retry) if isinstance(raw_retry, dict) else {}
        max_attempts = int(retry.get("max_attempts", 1))
        backoff_seconds = int(retry.get("backoff_seconds", 30))
        if max_attempts < 1 or max_attempts > 5:
            raise ValueError("最大尝试次数必须在 1 到 5 之间")
        if backoff_seconds < 0 or backoff_seconds > 86400:
            raise ValueError("重试间隔必须在 0 到 86400 秒之间")

        delivery_mode = str(
            body.get("delivery_mode", getattr(existing, "delivery_mode", "standalone"))
        )
        delivery_target = getattr(existing, "delivery_target", None)
        if delivery_mode == "standalone" or delivery_target is None:
            delivery_target = DeliveryTarget.standalone()

        raw_model = (
            body.get("model_override")
            if "model_override" in body
            else getattr(existing, "model_override", None)
        )
        model_override = self._resolve_model_override(raw_model)
        raw_skills = body.get(
            "selected_skills", getattr(existing, "selected_skills", [])
        )
        if not isinstance(raw_skills, list):
            raise ValueError("selected_skills must be a list")
        selected_skills = list(dict.fromkeys(
            str(item).strip() for item in raw_skills if str(item).strip()
        ))
        catalog = self._components.get("skill_catalog")
        if catalog is not None:
            for skill_id in selected_skills:
                bundle = catalog.get(skill_id)
                if bundle is None or not getattr(bundle, "user_invocable", False):
                    raise ValueError(f"技能不可用：{skill_id}")
        return NewScheduledTask(
            name=name,
            kind=task_kind,
            trigger=trigger,
            payload=payload,
            delivery_mode=delivery_mode,
            delivery_target=delivery_target,
            model_override=model_override,
            enabled=bool(body.get("enabled", getattr(existing, "enabled", True))),
            workspace_root=str(workspace),
            context_policy=context_policy,
            timeout_seconds=timeout_seconds,
            retry_policy={
                "max_attempts": max_attempts,
                "backoff_seconds": backoff_seconds,
            },
            selected_skills=selected_skills,
            permission_profile=permission_profile,
            # Membership is inherited, never taken from the body.  Which
            # workflow a task is a step of is decided by materialising that
            # workflow, so a request that could set it could also detach a step
            # from the chain it is part of -- and a detached step keeps running
            # while the graph that explains it stops mentioning it.
            workflow_id=str(getattr(existing, "workflow_id", "") or ""),
            step_key=str(getattr(existing, "step_key", "") or ""),
        )

    def _workflow_step_from_body(
        self, raw: dict[str, Any], index: int, existing: Any = None
    ):
        """One step of a workflow, from its JSON object.

        A step with upstreams may not carry a trigger, and a step without them
        must.  Both are refused here with a sentence about the step, and again
        by the graph check -- the first so the message names the step, the
        second so no other caller can get past it.

        A field the body *omits* keeps the value the step already had, and only
        a field that is present is taken as an answer.  The alternative is that
        every edit of a graph has to resend everything the editor never shows
        -- the entry step's schedule, a step's model, its skill list -- because
        leaving one out would silently reset it.  A graph editor that edits the
        graph should not be able to erase a step's model by not mentioning it.
        Sending the field explicitly, empty string included, still means what it
        says.
        """
        from agent.scheduler import DeliveryTarget, WorkflowStep

        def answered(field_name: str, fallback: Any) -> Any:
            """The value for *field_name*: what the body said, else what was."""
            if field_name in raw:
                return raw.get(field_name)
            if existing is not None:
                return getattr(existing, field_name, fallback)
            return fallback

        key = str(raw.get("key", "")).strip()
        if not key:
            raise ValueError(f"第 {index + 1} 个步骤缺少 key")
        if len(key) > 40:
            raise ValueError(f"步骤 key「{key}」不能超过 40 个字符")
        if any(ch.isspace() for ch in key):
            raise ValueError(f"步骤 key「{key}」不能包含空格")
        given_name = str(answered("name", "") or "").strip()
        payload = answered("payload", None)
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValueError(f"步骤「{key}」的 payload 必须是对象")
        depends_on = [
            str(item).strip()
            for item in (answered("depends_on", None) or [])
            if str(item).strip()
        ]
        trigger = None
        given_trigger = str(raw.get("trigger_type", "")).strip()
        if depends_on:
            if given_trigger:
                raise ValueError(
                    f"步骤「{key}」有上游，不能另外再指定时间或信号触发"
                )
        elif given_trigger:
            trigger = self._trigger_from_body(raw)
        elif existing is not None and existing.trigger is not None:
            trigger = existing.trigger
        else:
            raise ValueError(
                f"步骤「{key}」没有上游，必须指定触发方式（trigger_type）"
            )
        delivery_mode = str(answered("delivery_mode", "standalone") or "standalone")
        # A target is only ever kept, never parsed from the body: nothing in
        # this interface can choose one yet, and inventing a parse for a shape
        # no client sends would be a promise with no way to test it.
        delivery_target = getattr(existing, "delivery_target", None)
        if delivery_mode == "standalone" or delivery_target is None:
            delivery_target = DeliveryTarget.standalone()
        skills = answered("selected_skills", None)
        if skills is not None and not isinstance(skills, list):
            raise ValueError(f"步骤「{key}」的 selected_skills 必须是数组")
        return WorkflowStep(
            key=key,
            name=given_name or key,
            kind=str(answered("kind", "agent_prompt") or "agent_prompt").strip(),
            payload=dict(payload),
            depends_on=depends_on,
            trigger=trigger,
            workspace_root=str(answered("workspace_root", "") or "").strip(),
            permission_profile=str(
                answered("permission_profile", "inherit") or "inherit"
            ),
            context_policy=str(answered("context_policy", "stateless") or "stateless"),
            model_override=answered("model_override", None) or None,
            timeout_seconds=int(answered("timeout_seconds", 1800) or 1800),
            selected_skills=[
                str(item).strip()
                for item in (skills or [])
                if str(item).strip()
            ],
            delivery_mode=delivery_mode,
            delivery_target=delivery_target,
        )

    def _workflow_from_body(self, body: dict[str, Any], existing: Any = None):
        from agent.scheduler import Workflow, validate_workflow_graph

        name = str(body.get("name", getattr(existing, "name", ""))).strip()
        if not name:
            raise ValueError("workflow 名称不能为空")
        if len(name) > 60:
            raise ValueError("workflow 名称不能超过 60 个字符")
        description = str(
            body.get("description", getattr(existing, "description", "")) or ""
        ).strip()
        raw_steps = body.get("steps")
        if raw_steps is None and existing is not None:
            raw_steps = [item.to_dict() for item in existing.steps]
        if not isinstance(raw_steps, list):
            raise ValueError("steps 必须是数组")
        if not raw_steps:
            raise ValueError("workflow 至少要有一个步骤")
        if len(raw_steps) > 20:
            raise ValueError("一个 workflow 最多 20 个步骤")
        steps = []
        existing_by_key = {
            str(item.key).strip(): item
            for item in (getattr(existing, "steps", None) or [])
        }
        for index, raw in enumerate(raw_steps):
            if not isinstance(raw, dict):
                raise ValueError(f"第 {index + 1} 个步骤必须是对象")
            steps.append(
                self._workflow_step_from_body(
                    raw,
                    index,
                    existing_by_key.get(str(raw.get("key", "")).strip()),
                )
            )
        # Checked here as well as in the store so a cycle comes back as a 400
        # carrying the ring, rather than as a failure from inside the write.
        validate_workflow_graph(steps)
        return Workflow(
            name=name,
            description=description,
            enabled=bool(body.get("enabled", getattr(existing, "enabled", True))),
            steps=steps,
        )

    def _workflow_response(self, store: Any, workflow: Any) -> Any:
        """A workflow plus the state of the tasks behind it."""
        from starlette.responses import JSONResponse

        tasks = store.step_tasks(workflow.id)
        latest = {task.id: store.latest_run(task.id) for task in tasks.values()}
        return JSONResponse(
            {
                "workflow": _workflow_payload(
                    workflow,
                    tasks,
                    store.unacknowledged_attention_counts(),
                    latest,
                )
            }
        )

    async def _workflows(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import SchedulerStore
        from agent.scheduler.profiles import profile_payloads

        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            unseen = store.unacknowledged_attention_counts()
            workflows = []
            for workflow in store.list_workflows():
                tasks = store.step_tasks(workflow.id)
                latest = {
                    task.id: store.latest_run(task.id) for task in tasks.values()
                }
                workflows.append(
                    _workflow_payload(workflow, tasks, unseen, latest)
                )
            return JSONResponse(
                {
                    "workflows": workflows,
                    # Travelled with the schedules list too, for the same
                    # reason: the interface renders what this backend accepts.
                    "permission_profiles": profile_payloads(),
                }
            )
        finally:
            store.close()

    async def _create_workflow(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            from agent.scheduler import SchedulerStore

            workflow = self._workflow_from_body(body)
            store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
            try:
                created = store.create_workflow(workflow)
                return self._workflow_response(store, created)
            finally:
                store.close()
        except (KeyError, ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def _update_workflow(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            from agent.scheduler import SchedulerStore

            workflow_id = str(request.path_params["workflow_id"])
            store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
            try:
                existing = store.get_workflow(workflow_id)
                if existing is None:
                    return JSONResponse(
                        {"error": "workflow not found"}, status_code=404
                    )
                workflow = self._workflow_from_body(body, existing)
                updated = store.update_workflow(workflow_id, workflow)
                return self._workflow_response(store, updated)
            finally:
                store.close()
        except (KeyError, ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def _delete_workflow(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import SchedulerStore

        workflow_id = str(request.path_params["workflow_id"])
        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            if store.get_workflow(workflow_id) is None:
                return JSONResponse({"error": "workflow not found"}, status_code=404)
            # Deleting a workflow stops it; it does not erase what it ran.
            # Refused while a step is mid-run for the same reason deleting a
            # task is: the run would be left with nothing to write home to.
            running = [
                task.id
                for task in store.step_tasks(workflow_id).values()
                if task.active_run_id
            ]
            if running:
                return JSONResponse(
                    {"error": "有步骤正在运行，请先取消运行"}, status_code=409
                )
            disabled = store.delete_workflow(workflow_id)
            return JSONResponse({"ok": True, "disabled_task_ids": disabled})
        finally:
            store.close()

    async def _schedules(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import SchedulerStore
        from agent.scheduler.profiles import profile_payloads

        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            unseen = store.unacknowledged_attention_counts()
            tasks = []
            for task in store.list_tasks():
                latest_run = store.latest_run(task.id)
                tasks.append(
                    _scheduler_task_payload(
                        task, latest_run, unseen.get(task.id, 0)
                    )
                )
            # The profile list travels with the data so the UI renders what
            # this backend accepts instead of keeping its own copy of the set
            # -- the two would drift the moment a profile is added.
            return JSONResponse(
                {
                    "tasks": tasks,
                    "permission_profiles": profile_payloads(),
                    # Failures that finished while nobody was watching.  The
                    # scheduler runs precisely when no client is connected, so
                    # this has to be part of the data rather than a push event
                    # that only reaches whoever happened to be looking.
                    "unseen_attention": sum(unseen.values()),
                }
            )
        finally:
            store.close()

    async def _schedule_runs(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import SchedulerStore

        task_id = str(request.path_params["task_id"])
        try:
            limit = max(1, min(int(request.query_params.get("limit", "50")), 100))
        except ValueError:
            limit = 50
        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            task = store.get_task(task_id)
            if task is None:
                return JSONResponse({"error": "task not found"}, status_code=404)
            runs = list(reversed(store.list_runs(task_id)))[:limit]
            return JSONResponse(
                {
                    "task": _scheduler_task_payload(
                        task,
                        store.latest_run(task.id),
                        store.unacknowledged_attention_counts().get(task_id, 0),
                    ),
                    "runs": [_scheduler_run_payload(run) for run in runs],
                }
            )
        finally:
            store.close()

    async def _schedule_run_output(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import SchedulerStore

        task_id = str(request.path_params["task_id"])
        run_id = str(request.path_params["run_id"])
        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            if store.get_task(task_id) is None:
                return JSONResponse({"error": "task not found"}, status_code=404)
            run = store.get_run(task_id, run_id)
            if run is None:
                return JSONResponse({"error": "run not found"}, status_code=404)
        finally:
            store.close()
        if not run.output_path:
            return JSONResponse(
                {"run_id": run.id, "available": False, "content": "", "truncated": False}
            )
        output_path = _scheduler_output_path(task_id, run)
        if output_path is None:
            return JSONResponse({"error": "invalid run output path"}, status_code=403)
        if not output_path.is_file():
            return JSONResponse(
                {"run_id": run.id, "available": False, "content": "", "truncated": False}
            )
        try:
            max_bytes = 2 * 1024 * 1024
            with output_path.open("rb") as handle:
                raw = handle.read(max_bytes + 1)
            truncated = len(raw) > max_bytes
            content = raw[:max_bytes].decode("utf-8", errors="replace")
        except OSError as exc:
            return JSONResponse({"error": f"unable to read run output: {exc}"}, status_code=500)
        return JSONResponse(
            {
                "run_id": run.id,
                "available": True,
                "content": content,
                "truncated": truncated,
                "output_url": _scheduler_output_url(
                    task_id, str(getattr(run, "id", "") or run_id), str(output_path)
                ),
            }
        )

    def _schedule_artifact_root(self, task_id: str, run: Any) -> Path:
        output_path = str(getattr(run, "output_path", "") or "").strip()
        if output_path:
            return Path(output_path).expanduser().resolve().parent / run.id / "artifacts"
        output_root = Path(
            self._components.get("output_dir") or shared.DEFAULT_OUTPUT_DIR
        ).expanduser().resolve()
        return output_root / "scheduler" / task_id / run.id / "artifacts"

    async def _schedule_run_artifacts(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import SchedulerStore

        task_id = str(request.path_params["task_id"])
        run_id = str(request.path_params["run_id"])
        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            run = store.get_run(task_id, run_id)
        finally:
            store.close()
        if run is None:
            return JSONResponse({"error": "run not found"}, status_code=404)
        root = self._schedule_artifact_root(task_id, run)
        artifacts = []
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(root).as_posix()
                mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                artifacts.append(
                    {
                        "path": relative,
                        "name": path.name,
                        "mime_type": mime_type,
                        "size_bytes": path.stat().st_size,
                        "url": (
                            f"/api/schedules/{quote(task_id, safe='')}/runs/"
                            f"{quote(run_id, safe='')}/artifacts/{quote(relative, safe='/')}"
                        ),
                    }
                )
                if len(artifacts) >= 500:
                    break
        return JSONResponse({"artifacts": artifacts})

    async def _schedule_run_artifact(self, request: Any) -> Any:
        from starlette.responses import FileResponse, JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import SchedulerStore

        task_id = str(request.path_params["task_id"])
        run_id = str(request.path_params["run_id"])
        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            run = store.get_run(task_id, run_id)
        finally:
            store.close()
        if run is None:
            return JSONResponse({"error": "run not found"}, status_code=404)
        root = self._schedule_artifact_root(task_id, run)
        try:
            target = (root / str(request.path_params["artifact_path"])).resolve()
            if root != target and root not in target.parents:
                return JSONResponse({"error": "invalid artifact path"}, status_code=403)
            if not target.is_file():
                return JSONResponse({"error": "artifact not found"}, status_code=404)
        except OSError:
            return JSONResponse({"error": "invalid artifact path"}, status_code=400)
        return FileResponse(target, filename=target.name)

    async def _create_schedule(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            from agent.scheduler import SchedulerStore

            task = self._schedule_from_body(body)
            store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
            try:
                created = store.create_task(task)
            finally:
                store.close()
            return JSONResponse({"task": _scheduler_task_payload(created)})
        except (KeyError, ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def _delete_schedule(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        if not self._authorized(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import SchedulerStore
        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            task_id = str(request.path_params["task_id"])
            task = store.get_task(task_id)
            if task is not None and task.active_run_id:
                return JSONResponse(
                    {"error": "任务正在运行，请先取消运行"}, status_code=409
                )
            if task is not None and task.workflow_id:
                # Removing a step on its own would leave the steps below it
                # subscribed to a signal nobody emits any more, and the next
                # save of the workflow would build a fresh task for the step --
                # so the run history the deletion was meant to tidy up would
                # reappear under a new id.  Which step a graph should have is
                # the graph's question to answer.
                return JSONResponse(
                    {
                        "error": (
                            f"「{task.name}」是流程中的步骤，"
                            "请到流程里删除该步骤，或删除整个流程"
                        )
                    },
                    status_code=409,
                )
            ok = task is not None
            store.delete_task(task_id)
        finally: store.close()
        return JSONResponse({"ok": bool(ok)})

    async def _patch_schedule(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        if not self._authorized(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        try: body = await request.json()
        except Exception: return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict) or "enabled" not in body: return JSONResponse({"error": "enabled is required"}, status_code=400)
        from agent.scheduler import SchedulerStore
        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            task_id = str(request.path_params["task_id"])
            task = store.get_task(task_id)
            if task is not None and task.workflow_id:
                # Saving a workflow writes every step's enabled flag from the
                # workflow's own, so a switch thrown on one step would be undone
                # by the next save.  Refusing says why; accepting would not.
                return JSONResponse(
                    {
                        "error": (
                            f"「{task.name}」是流程中的步骤，"
                            "请暂停整个流程，或到流程里删除该步骤"
                        )
                    },
                    status_code=409,
                )
            store.set_enabled(task_id, bool(body["enabled"]))
        finally: store.close()
        return JSONResponse({"ok": True, "enabled": bool(body["enabled"])})

    def _step_owns_no_trigger(self, store: Any, task: Any) -> bool:
        """True when *task* is a workflow step whose timing comes from upstreams.

        Answered from the graph, not from the task: a fan-in trigger and a
        hand-picked signal both report ``trigger_type`` "signal", and only the
        graph knows which steps have upstreams.
        """
        if not task.workflow_id or not task.step_key:
            return False
        workflow = store.get_workflow(task.workflow_id)
        if workflow is None:
            return False
        step = workflow.step(task.step_key)
        return step is not None and bool(step.depends_on)

    def _mirror_step_edit(self, store: Any, task: Any) -> None:
        """Copy an edited step task back into the graph it belongs to.

        A step is edited through the ordinary task endpoint -- it is an
        ordinary task, with its own run history and its own switches -- but
        what it is *stored* as is a step of a graph, and the next save of that
        workflow rebuilds the task from the graph.  Without this, changing a
        step's prompt in the task editor would survive exactly until somebody
        moved an edge.

        The graph is not re-materialised: the task in hand was written a
        moment ago and is the newer of the two, so rebuilding it from the step
        copied from it would be a round trip that can only lose something.
        """
        if not task.workflow_id or not task.step_key:
            return
        workflow = store.get_workflow(task.workflow_id)
        if workflow is None:
            return
        from agent.scheduler import Workflow, WorkflowStep

        steps = []
        matched = False
        for step in workflow.steps:
            if str(step.key).strip() != str(task.step_key).strip():
                steps.append(step)
                continue
            matched = True
            steps.append(
                WorkflowStep(
                    key=step.key,
                    name=task.name,
                    kind=task.kind,
                    payload=dict(task.payload),
                    # A task cannot express an edge, so it must not be able to
                    # break or invent one.
                    depends_on=list(step.depends_on),
                    # Nor a trigger, when it has upstreams: the upstreams *are*
                    # its trigger, and this is the one field where the task row
                    # and the graph would otherwise disagree.
                    trigger=None if step.depends_on else task.trigger,
                    workspace_root=task.workspace_root,
                    permission_profile=task.permission_profile,
                    context_policy=task.context_policy,
                    model_override=task.model_override,
                    timeout_seconds=int(task.timeout_seconds),
                    selected_skills=list(task.selected_skills),
                    delivery_mode=task.delivery_mode,
                    delivery_target=task.delivery_target,
                )
            )
        if not matched:
            return
        store.update_workflow(
            workflow.id,
            Workflow(
                name=workflow.name,
                steps=steps,
                id=workflow.id,
                description=workflow.description,
                enabled=workflow.enabled,
            ),
            materialize=False,
        )

    async def _update_schedule(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            from agent.scheduler import SchedulerStore

            store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
            try:
                task_id = str(request.path_params["task_id"])
                existing = store.get_task(task_id)
                if existing is None:
                    return JSONResponse({"error": "task not found"}, status_code=404)
                spec = self._schedule_from_body(
                    body, existing, keep_trigger=self._step_owns_no_trigger(store, existing)
                )
                updated = store.update_task(task_id, spec)
                if updated is not None:
                    self._mirror_step_edit(store, updated)
                unseen = store.unacknowledged_attention_counts().get(task_id, 0)
            finally:
                store.close()
            if updated is None:
                return JSONResponse({"error": "task not found"}, status_code=404)
            return JSONResponse(
                {"task": _scheduler_task_payload(updated, None, unseen)}
            )
        except (KeyError, ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def _bulk_schedules(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        ids = list(dict.fromkeys(
            str(item).strip() for item in body.get("task_ids", []) if str(item).strip()
        ))
        if not ids or len(ids) > 200:
            return JSONResponse({"error": "task_ids must contain 1 to 200 ids"}, status_code=400)
        action = str(body.get("action", "")).strip().lower()
        if action not in {"enable", "disable", "delete"}:
            return JSONResponse({"error": "unsupported bulk action"}, status_code=400)
        from agent.scheduler import SchedulerStore

        completed: list[str] = []
        skipped: list[dict[str, str]] = []
        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            for task_id in ids:
                task = store.get_task(task_id)
                if task is None:
                    skipped.append({"id": task_id, "reason": "not_found"})
                    continue
                if action == "delete":
                    if task.active_run_id:
                        skipped.append({"id": task_id, "reason": "running"})
                        continue
                    if task.workflow_id:
                        # Same reason as deleting one on its own: the graph,
                        # not the task list, decides which steps exist.
                        skipped.append({"id": task_id, "reason": "workflow_step"})
                        continue
                    store.delete_task(task_id)
                elif task.workflow_id:
                    # A step's own switch is rewritten from its workflow's every
                    # time that workflow is saved, so flipping it here would be
                    # a promise the next save breaks.  Pausing the workflow is
                    # the switch that holds.
                    skipped.append({"id": task_id, "reason": "workflow_step"})
                    continue
                else:
                    store.set_enabled(task_id, action == "enable")
                completed.append(task_id)
        finally:
            store.close()
        return JSONResponse({"completed": completed, "skipped": skipped})

    async def _schedule_preview(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            spec = self._schedule_from_body(body)
            trigger = spec.trigger
            current = trigger.initial_run_at(datetime.now(timezone.utc))
            occurrences: list[str] = []
            while current is not None and len(occurrences) < 5:
                occurrences.append(current.isoformat())
                current = trigger.advance_after_claim(current, current)
            return JSONResponse({"occurrences": occurrences})
        except (KeyError, ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def _run_schedule_now(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        service = self._components.get("scheduler_service")
        if service is None:
            return JSONResponse({"error": "scheduler is offline"}, status_code=503)
        claimed = await service.run_task_now(str(request.path_params["task_id"]))
        if claimed is None:
            return JSONResponse(
                {"error": "任务不存在或已有运行中的实例"}, status_code=409
            )
        return JSONResponse({"run": _scheduler_run_payload(claimed.run)}, status_code=202)

    async def _retry_schedule_run(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        service = self._components.get("scheduler_service")
        if service is None:
            return JSONResponse({"error": "scheduler is offline"}, status_code=503)
        claimed = await service.retry_run(
            str(request.path_params["task_id"]),
            str(request.path_params["run_id"]),
            use_latest=bool(body.get("use_latest", False)),
        )
        if claimed is None:
            return JSONResponse(
                {"error": "运行记录不存在、仍在运行，或任务正忙"}, status_code=409
            )
        return JSONResponse({"run": _scheduler_run_payload(claimed.run)}, status_code=202)

    async def _cancel_schedule_run(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        service = self._components.get("scheduler_service")
        if service is None:
            return JSONResponse({"error": "scheduler is offline"}, status_code=503)
        cancelled = await service.cancel_run(
            str(request.path_params["task_id"]),
            str(request.path_params["run_id"]),
        )
        if not cancelled:
            return JSONResponse({"error": "运行不存在或已经结束"}, status_code=409)
        return JSONResponse({"ok": True}, status_code=202)

    async def _acknowledge_schedule_run(self, request: Any) -> Any:
        """Record that a person has now seen one run's failure."""
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import SchedulerStore

        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            acknowledged = store.acknowledge_run(
                str(request.path_params["task_id"]),
                str(request.path_params["run_id"]),
            )
            # Returning the fresh total lets the caller correct its badge
            # without a second round trip, which is where a stale count would
            # come from.
            return JSONResponse(
                {
                    "ok": True,
                    "acknowledged": acknowledged,
                    "unseen_attention": sum(
                        store.unacknowledged_attention_counts().values()
                    ),
                }
            )
        finally:
            store.close()

    async def _schedule_attention(self, request: Any) -> Any:
        """Just the count, for a badge that is polled from every view.

        Separate from ``GET /api/schedules`` because the indicator has to work
        for someone who never opens the schedules page -- making them load
        every task and run just to find out whether anything failed would be
        the opposite of a notification.
        """
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import SchedulerStore

        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            return JSONResponse(
                {
                    "unseen_attention": sum(
                        store.unacknowledged_attention_counts().values()
                    )
                }
            )
        finally:
            store.close()

    async def _clear_schedule_attention(self, request: Any) -> Any:
        """Mark every unseen failure as seen; optionally one task's only."""
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        task_id = str(body.get("task_id") or "").strip()
        from agent.scheduler import SchedulerStore

        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            cleared = store.acknowledge_attention(task_id or None)
            return JSONResponse(
                {
                    "ok": True,
                    "cleared": cleared,
                    "unseen_attention": sum(
                        store.unacknowledged_attention_counts().values()
                    ),
                }
            )
        finally:
            store.close()

    async def _scheduler_health(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        service = self._components.get("scheduler_service")
        if service is None:
            return JSONResponse({"status": "offline", "active_runs": 0})
        return JSONResponse(service.health())

    async def _signals(self, request: Any) -> Any:
        """What can be waited for, so a subscription is chosen rather than typed.

        The interface offers these as options because a signal name is matched
        exactly: a near miss does not fire late or fire anyway, it never fires,
        and the only trace is an emission recorded as unmatched.  Listing the
        names that exist turns that failure into one nobody can make by
        accident.
        """
        from starlette.responses import JSONResponse
        from agent.scheduler import SchedulerStore

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            emissions = store.signal_names()
            subscribers: dict[str, int] = {}
            task_labels: dict[str, str] = {}
            for task in store.list_tasks():
                task_labels[task.id] = task.name
                if not task.enabled or task.trigger.trigger_type != "signal":
                    continue
                key = str(task.trigger.payload.get("name", "")).strip()
                subscribers[key] = subscribers.get(key, 0) + 1
        finally:
            store.close()
        items = []
        for entry in emissions:
            name = str(entry["name"])
            # A task signal reads as gibberish on its own (task:9f2c…:succeeded),
            # so the task it belongs to travels alongside.  The label itself is
            # left to the interface, which already owns the wording for run
            # statuses; duplicating it here would create a second place to keep
            # in step for no gain.
            parsed = parse_task_signal(name)
            items.append(
                {
                    "name": name,
                    "source": "task" if parsed else "custom",
                    "task_id": parsed[0] if parsed else "",
                    "task_name": task_labels.get(parsed[0], "") if parsed else "",
                    "status": parsed[1] if parsed else "",
                    "last_emitted_at": entry.get("last_at"),
                    "emission_count": int(entry.get("count") or 0),
                    "subscriber_count": subscribers.get(name, 0),
                }
            )
        return JSONResponse(
            {
                "signals": items,
                "waiting": [
                    {"name": key, "subscriber_count": value}
                    for key, value in sorted(subscribers.items())
                    if key and key not in {item["name"] for item in items}
                ],
            }
        )

    async def _delete_skill(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        if not self._authorized(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        catalog = self._components.get("skill_catalog")
        bundle = catalog.get(str(request.path_params["skill_id"])) if catalog is not None else None
        if bundle is None: return JSONResponse({"error": "skill not found"}, status_code=404)
        if getattr(bundle, "source", "") != "user": return JSONResponse({"error": "内置技能不能删除"}, status_code=403)
        try:
            import shutil
            path = Path(bundle.path).resolve()
            root = Path(getattr(catalog, "user_root", shared.SKILLS_DIR)).resolve()
            if root not in path.parents: return JSONResponse({"error": "invalid skill path"}, status_code=400)
            shutil.rmtree(path)
            catalog.reload()
            self._components["config_revision"] = int(self._components.get("config_revision", 0)) + 1
            return JSONResponse({"ok": True, "id": bundle.id})
        except Exception as exc: return JSONResponse({"error": str(exc)}, status_code=500)

    async def _delete_plugin(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        if not self._authorized(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        name = str(request.path_params["plugin_name"])
        catalog = self._components.get("plugin_catalog")
        meta = next((item for item in (catalog.list_plugins() if catalog else []) if getattr(item, "name", "") == name), None)
        if meta is None: return JSONResponse({"error": "plugin not found"}, status_code=404)
        if getattr(meta, "source", "") != "user": return JSONResponse({"error": "内置插件不能删除"}, status_code=403)
        try:
            import shutil
            root = Path(shared.USER_PLUGINS_DIR).resolve(); path = Path(meta.path).resolve()
            if root not in path.parents: return JSONResponse({"error": "invalid plugin path"}, status_code=400)
            shutil.rmtree(path)
            reload_fn = getattr(catalog, "reload", None)
            if callable(reload_fn): await reload_fn(self._components)
            self._components["config_revision"] = int(self._components.get("config_revision", 0)) + 1
            return JSONResponse({"ok": True, "name": name})
        except Exception as exc: return JSONResponse({"error": str(exc)}, status_code=500)

    async def _context_stats(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        base_ctx_mgr = self._components.get("context_manager")
        stats = {}
        if base_ctx_mgr is not None:
            stats_method = getattr(base_ctx_mgr, "stats", None)
            if callable(stats_method):
                try:
                    stats = stats_method()
                except Exception:
                    stats = {}
        return JSONResponse({"stats": stats})

    async def _rename_session(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        title = str(body.get("title", "") or "").strip()
        if not title:
            return JSONResponse({"error": "title is required"}, status_code=400)
        session_id = request.path_params["session_id"]
        ok = self._service().rename_session(session_id, title)
        if not ok:
            return JSONResponse({"error": "rename failed"}, status_code=500)
        return JSONResponse({"ok": True, "title": title})

    async def _toggle_plugin(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        plugin_name = request.path_params["plugin_name"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict) or "enabled" not in body:
            return JSONResponse({"error": "enabled is required"}, status_code=400)
        desired = bool(body["enabled"])

        from agent.config import load_config, save_config

        cfg, _ = load_config()
        plugins_cfg = cfg.get("plugins")
        if not isinstance(plugins_cfg, dict):
            plugins_cfg = {}
            cfg["plugins"] = plugins_cfg
        plugin_entry = plugins_cfg.get(plugin_name)
        if not isinstance(plugin_entry, dict):
            plugin_entry = {}
            plugins_cfg[plugin_name] = plugin_entry
        plugin_entry["enabled"] = desired
        try:
            save_config(cfg)
        except Exception as exc:
            return JSONResponse({"error": f"save failed: {exc}"}, status_code=500)

        catalog = self._components.get("plugin_catalog")
        if catalog is not None:
            try:
                catalog._plugin_config = plugins_cfg  # noqa: SLF001
            except Exception:
                pass
            reload = getattr(catalog, "reload", None)
            if callable(reload):
                try:
                    self._ensure_router()
                    await reload(self._components)
                except Exception as exc:
                    return JSONResponse(
                        {"error": f"reload failed: {exc}", "saved": True},
                        status_code=500,
                    )
        self._components["config_revision"] = int(self._components.get("config_revision", 0)) + 1
        return JSONResponse({"ok": True, "name": plugin_name, "enabled": desired})

    async def _delete_session(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = request.path_params["session_id"]
        ok = await self._service().delete_session_async(session_id)
        if not ok:
            return JSONResponse({"error": "delete failed"}, status_code=500)
        return JSONResponse({"ok": True})

    async def _delete_sessions(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict) or not isinstance(body.get("session_ids"), list):
            return JSONResponse({"error": "session_ids must be an array"}, status_code=400)
        session_ids = body["session_ids"]
        if len(session_ids) > 200:
            return JSONResponse({"error": "too many sessions"}, status_code=400)
        if not all(isinstance(item, str) and item.strip() for item in session_ids):
            return JSONResponse({"error": "session_ids must contain non-empty strings"}, status_code=400)
        result = await self._service().delete_sessions_async(session_ids)
        return JSONResponse({"ok": not result["failed"], **result})

    async def _reveal_session(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        session_id = request.path_params["session_id"]
        target = self._service().session_data_path(session_id)
        if target is None:
            return JSONResponse(
                {"error": "session data is not available"}, status_code=404
            )

        # Reuse the same platform-aware launcher as the CLI /reveal command.
        # It receives an argv list, so session ids can never become shell code.
        from agent.commands.builtin import _launch_path

        result = await _launch_path(target, reveal=True)
        response_text = str(getattr(result, "response_text", "") or "")
        if getattr(result, "level", "info") == "error":
            return JSONResponse({"error": response_text or "unable to reveal session"}, status_code=500)
        return JSONResponse({"ok": True, "path": str(target)})

    def _file_roots_for(self, request: Any) -> list[Path]:
        """Directories ``GET /api/files`` may serve for *this* request.

        Serving a file is granting a capability to read one path, so the
        gateway authorises each request against the owner of the file instead
        of keeping one global allowlist of every directory it has ever seen.
        The owner is named explicitly by the caller and there are exactly two
        kinds:

        * ``session_id`` — the Web session that produced the file. Its sandbox
          is its own home (``output/``, ``uploads/``) plus the workspace folder
          the user picked for it, which is exactly what the agent's
          ``send_file`` tool was already allowed to write to.
        * ``task_id`` + ``run_id`` — a scheduled run whose output the gateway
          itself recorded.

        A request that names neither owner gets nothing, which is the point:
        without an owner there is no way to tell whether the caller is entitled
        to the file.
        """
        session_id = str(request.query_params.get("session_id") or "").strip()
        if session_id:
            return self._session_file_roots(session_id)
        task_id = str(request.query_params.get("task_id") or "").strip()
        run_id = str(request.query_params.get("run_id") or "").strip()
        if task_id and run_id:
            return self._schedule_file_roots(task_id, run_id)
        return []

    def _session_file_roots(self, session_id: str) -> list[Path]:
        """The sandbox one Web session is allowed to read attachments from."""
        roots: list[Path] = []

        def add(value: Any) -> None:
            if not value:
                return
            try:
                candidate = Path(str(value)).expanduser().resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                return
            if candidate.is_dir() and candidate not in roots:
                roots.append(candidate)

        # A session id that does not match a real home resolves to a directory
        # that is not there, so ``add`` drops it and the request is refused.
        try:
            add(shared.web_session_home(session_id))
        except (OSError, ValueError):
            pass
        add(self._session_workspace_root(session_id))
        return roots

    def _session_workspace_root(self, session_id: str) -> str:
        """The workspace folder recorded for one session, live or restored.

        A live session keeps the value in context metadata; a session restored
        from disk keeps it in its own ``.session.json`` manifest. Reading the
        manifest only for the session being asked about keeps this per-request
        lookup from ever widening into a scan of other sessions' folders.
        """
        state = self._sessions.get(session_id)
        metadata = getattr(getattr(state, "ctx", None), "metadata", None)
        if isinstance(metadata, dict):
            value = str(metadata.get("workspace_root") or "").strip()
            if value:
                return value
        try:
            manifest = shared.web_session_home(session_id) / ".session.json"
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return ""
        if isinstance(payload, dict):
            return str(payload.get("workspace_root") or "").strip()
        return ""

    def _schedule_file_roots(self, task_id: str, run_id: str) -> list[Path]:
        """The recorded output of one scheduled run, verified against the store.

        The path is never taken from the query string; it is re-read from the
        run so a forged ``path`` cannot point at an unrelated local file.
        """
        from agent.scheduler import SchedulerStore

        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            run = store.get_run(task_id, run_id)
        except Exception:
            return []
        finally:
            store.close()
        if run is None:
            return []
        output = _scheduler_output_path(task_id, run)
        if output is None or not output.is_file():
            return []
        return [output]

    async def _file(self, request: Any) -> Any:
        from starlette.responses import FileResponse, JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        raw_path = request.query_params.get("path", "")
        if not raw_path:
            return JSONResponse({"error": "path is required"}, status_code=400)
        try:
            resolved = Path(raw_path).expanduser().resolve()
        except OSError:
            return JSONResponse({"error": "invalid path"}, status_code=400)
        roots = self._file_roots_for(request)
        if not roots or not any(path_contains(root, resolved) for root in roots):
            return JSONResponse({"error": "forbidden path"}, status_code=403)
        if not resolved.is_file():
            return JSONResponse({"error": "file not found"}, status_code=404)
        return FileResponse(resolved)

    async def _list_sessions(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"sessions": self._service().list_sessions()})

    async def _create_session(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = self._service().create_session()
        return JSONResponse({"session_id": session_id})

    async def _get_messages(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = request.path_params["session_id"]
        try:
            limit = max(1, min(int(request.query_params.get("limit", "100")), 500))
        except ValueError:
            limit = 100
        return JSONResponse(
            {
                "session_id": session_id,
                "messages": self._service().get_messages(session_id, limit=limit),
            }
        )

    async def _get_session_state(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = str(request.path_params["session_id"])
        return JSONResponse(self._service().get_session_state(session_id))

    async def _dismiss_task_guidance(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if body is None:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        session_id = str(request.path_params["session_id"])
        task_id = str(body.get("task_id") or "").strip()
        ok = self._service().dismiss_task_guidance(session_id, task_id)
        if not ok:
            return JSONResponse({"ok": False, "dismissed": False, "error": "task not found"}, status_code=404)
        return JSONResponse({"ok": True, "dismissed": True, "task_id": task_id})

    async def _pick_workspace(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if self._handler is None:
            return JSONResponse({"error": "channel not started"}, status_code=503)
        session_id = str(request.path_params["session_id"])
        live_state = self._sessions.get(session_id)
        if live_state is not None and str(getattr(live_state, "operation_state", "idle")) != "idle":
            return JSONResponse(
                {
                    "ok": False,
                    "cancelled": False,
                    "error": "当前会话仍在执行任务，请等待任务结束或先停止任务后再切换项目文件夹",
                    "busy": True,
                },
                status_code=409,
            )
        selected = await _pick_workspace_directory()
        if not selected:
            return JSONResponse({"cancelled": True, "workspace_root": ""})
        # Reuse the transport-neutral command so filesystem policy, prompt,
        # and the session manifest are updated exactly as in CLI/Web commands.
        sink = WebOutputSink(collect=True)
        await self._handle_text(
            session_id,
            f"/workspace {selected}",
            sink,
            message_id=uuid.uuid4().hex,
        )
        errors = [
            str(event.get("error") or "项目文件夹切换失败")
            for event in sink.events
            if isinstance(event, dict) and event.get("type") == "error"
        ]
        errors.extend(
            str(event.get("text") or "项目文件夹切换失败")
            for event in sink.events
            if isinstance(event, dict)
            and event.get("type") == "status"
            and str(event.get("level") or "").casefold() == "error"
        )
        if errors:
            return JSONResponse(
                {
                    "ok": False,
                    "cancelled": False,
                    "error": errors[-1],
                    "events": sink.events,
                },
                status_code=409,
            )
        state = self._service().get_session_state(session_id)
        actual_root = str(state.get("workspace_root") or "")
        if actual_root != str(Path(selected).expanduser().resolve()):
            return JSONResponse(
                {
                    "ok": False,
                    "cancelled": False,
                    "error": "项目文件夹切换未生效",
                    "workspace_root": actual_root,
                    "events": sink.events,
                },
                status_code=409,
            )
        workspace_state = self._service().get_session_state(session_id)
        for live_sink in tuple(self._live_sinks.get(session_id, set())):
            try:
                live_sink.on_workspace_changed(
                    actual_root,
                    workspace_read=bool(workspace_state.get("workspace_read", True)),
                    workspace_write=bool(workspace_state.get("workspace_write", False)),
                    status=str(workspace_state.get("workspace_status") or "ready"),
                )
            except Exception:
                logger.debug("failed to broadcast workspace change", exc_info=True)
        return JSONResponse({
            "ok": True,
            "cancelled": False,
            "workspace_root": actual_root,
            "text": sink.full_text,
            "events": sink.events,
        })

    async def _get_session_permissions(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        from agent.security.shell import (
            PERMISSION_LEVELS,
            ShellAuthorizationScope,
            shell_session_allowlist_commands,
            shell_session_permission_get,
            shell_session_sandbox_get,
        )
        from agent.security.filesystem_sandbox import SANDBOX_MODES, effective_sandbox_mode
        from agent.config import load_config

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = str(request.path_params["session_id"])
        scope = ShellAuthorizationScope(session_id, "web", "")
        cfg, _ = load_config()
        permissions = cfg.get("permissions") if isinstance(cfg, dict) else {}
        permissions = permissions if isinstance(permissions, dict) else {}
        configured_level = str(permissions.get("shell_level", "ask") or "ask")
        configured_sandbox = str(permissions.get("shell_sandbox", "read_all") or "read_all")
        session_level = shell_session_permission_get(scope)
        session_sandbox = shell_session_sandbox_get(scope)
        level = session_level or configured_level
        sandbox = effective_sandbox_mode(session_sandbox or configured_sandbox, level)
        return JSONResponse({
            "session_id": session_id,
            "level": level,
            "sandbox": sandbox,
            "session_level": session_level,
            "session_sandbox": session_sandbox,
            "levels": list(PERMISSION_LEVELS),
            "sandbox_modes": list(SANDBOX_MODES),
            # Commands the human approved for this session, so "always allow"
            # answers are auditable and revocable rather than invisible.
            "approved_commands": shell_session_allowlist_commands(scope=scope),
        })

    async def _delete_session_approvals(self, request: Any) -> Any:
        """Revoke every session-scoped command approval for one session."""
        from starlette.responses import JSONResponse
        from agent.security.shell import (
            ShellAuthorizationScope,
            shell_session_allowlist_clear_scope,
        )

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = str(request.path_params["session_id"])
        scope = ShellAuthorizationScope(session_id, "web", "")
        removed = shell_session_allowlist_clear_scope(scope)
        return JSONResponse({
            "ok": True,
            "session_id": session_id,
            "removed": removed,
            "approved_commands": [],
        })

    async def _patch_session_permissions(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        from agent.security.shell import (
            PERMISSION_LEVELS,
            ShellAuthorizationScope,
            shell_session_permission_set,
            shell_session_sandbox_set,
        )
        from agent.security.filesystem_sandbox import SANDBOX_MODES

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        level = str(body.get("level", "") or "").strip().casefold()
        sandbox = str(body.get("sandbox", "") or "").strip().casefold()
        if level and level not in PERMISSION_LEVELS:
            return JSONResponse({"error": "invalid permission level"}, status_code=400)
        if sandbox and sandbox not in SANDBOX_MODES:
            return JSONResponse({"error": "invalid sandbox mode"}, status_code=400)
        if sandbox == "none" and level not in ("", "full"):
            return JSONResponse({"error": "sandbox none requires full permission"}, status_code=400)
        session_id = str(request.path_params["session_id"])
        scope = ShellAuthorizationScope(session_id, "web", "")
        if level:
            shell_session_permission_set(scope, level)
        if sandbox:
            shell_session_sandbox_set(scope, sandbox)
        return await self._get_session_permissions(request)

    async def _post_message(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if self._handler is None:
            return JSONResponse({"error": "channel not started"}, status_code=503)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)

        text = str(body.get("text", "") or "").strip()
        if not text and not body.get("attachments"):
            return JSONResponse({"error": "text is required"}, status_code=400)

        session_id = request.path_params["session_id"]
        message_id = str(body.get("message_id") or uuid.uuid4().hex)
        clean_sid = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id or ""))[:32]
        sink = WebOutputSink(
            collect=True,
            on_attachment=lambda path, name: self._record_attachment(
                session_id, path, name, turn_id=message_id
            ),
            output_dir=_web_session_output_dir(clean_sid),
        )
        sink.mark_turn_start()
        try:
            model = self._resolve_model_override(body.get("model"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        await self._handle_text(
            session_id,
            text,
            sink,
            message_id=message_id,
            model_override=model,
            attachments=self._parse_uploaded_attachments(session_id, body.get("attachments")),
        )
        await sink.flush_attachments()
        return JSONResponse(
            {
                "session_id": session_id,
                "message_id": message_id,
                "text": sink.full_text,
                "events": sink.events,
            }
        )

    async def _cancel_session(self, request: Any) -> Any:
        """Cancel the actively running turn for a session.

        Used by the frontend "stop" button.  Cancelling the session's
        ``cancel_token`` cooperatively stops the running agent turn (the
        registered cleanup aborts an in-flight model request) and the
        existing stream flow emits ``turn_complete`` so the client resets its
        streaming state.
        """
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = request.path_params["session_id"]
        state = self._sessions.get(session_id)
        token = getattr(state, "cancel_token", None) if state is not None else None
        if token is None:
            return JSONResponse({"ok": True, "cancelled": False})
        token.cancel("force")
        return JSONResponse({"ok": True, "cancelled": True})

    async def _stream(self, websocket: Any) -> None:
        await websocket.accept()
        if not self._authorized(websocket):
            await websocket.close(code=4401)
            return
        if self._handler is None:
            await websocket.send_json({"type": "error", "error": "channel not started"})
            await websocket.close(code=1011)
            return

        session_id = websocket.path_params["session_id"]
        clean_sid = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id or ""))[:32]
        # A previous tab may have been switched away while this session was
        # still generating. Rebind its sinks to the new socket and publish the
        # text generated so far before receiving new input.
        live_sinks = self._live_sinks.get(session_id, set())
        for live_sink in tuple(live_sinks):
            live_sink.set_websocket(websocket)
            snapshot = live_sink.streaming_snapshot
            if snapshot is not None:
                try:
                    await websocket.send_json(snapshot)
                except Exception:
                    pass
        active_tasks: set[asyncio.Task[Any]] = set()
        confirmation_waiters: dict[str, asyncio.Future[str]] = {}

        def wait_for_confirmation(token: str) -> asyncio.Future[str]:
            """Resolve to an ``allow_once`` / ``allow_session`` / ``deny`` string.

            A bare bool could not express "always allow", so the waiter carries
            the human's decision verbatim and lets the sink decide what it
            means for the pending record it is about to redeem.
            """
            loop = asyncio.get_running_loop()
            future: asyncio.Future[str] = loop.create_future()
            confirmation_waiters[token] = future
            async def guarded() -> str:
                try:
                    return str(await asyncio.wait_for(
                        future, timeout=CONFIRMATION_TIMEOUT_SECONDS
                    ))
                except asyncio.TimeoutError:
                    return "deny"
                finally:
                    confirmation_waiters.pop(token, None)
            return asyncio.ensure_future(guarded())

        async def process_message(
            text: str,
            message_id: str,
            model: str | None,
            attachment_payload: Any = None,
        ) -> None:
            # Each incoming message gets its own sink.  The coordinator owns
            # per-session serialization and can therefore queue a message
            # while an earlier turn is running; sharing one sink here would
            # reset its turn markers and make queued messages look completed.
            sink = WebOutputSink(
                websocket=websocket,
                output_dir=_web_session_output_dir(clean_sid),
                confirmation_handler=wait_for_confirmation,
            )
            sink.on_attachment = lambda path, name: self._record_attachment(
                session_id, path, name, turn_id=message_id
            )
            sink.mark_turn_start()
            self._live_sinks.setdefault(session_id, set()).add(sink)
            try:
                await self._handle_text(
                    session_id,
                    text,
                    sink,
                    message_id=message_id,
                    model_override=model,
                    attachments=self._parse_uploaded_attachments(session_id, attachment_payload),
                )
                await sink.flush_attachments()
                # A queued message intentionally has no turn_complete event;
                # its eventual execution gets its own sink and completion.
                # Commands that produce no output still receive a completion
                # marker so the client can clear its transient state.
                if not sink.turn_complete_emitted:
                    state = self._sessions.get(session_id)
                    operation_state = str(getattr(state, "operation_state", "idle"))
                    if operation_state != "idle":
                        # The coordinator accepted this request into its
                        # restart queue. Keep this sink alive until that
                        # queued operation is actually executed; closing it
                        # here would silently drop all later stream events.
                        await sink.wait_for_completion()
                    final_state = self._sessions.get(session_id)
                    final_operation_state = str(
                        getattr(final_state, "operation_state", "idle")
                    )
                    if not sink.turn_complete_emitted and final_operation_state == "idle":
                        sink.on_turn_complete("", [])
                        await sink.flush()
            finally:
                self._live_sinks.get(session_id, set()).discard(sink)
                await sink.close()

        try:
            while True:
                data = await websocket.receive_json()
                if not isinstance(data, dict):
                    continue
                if data.get("type") == "confirm_response":
                    token = str(data.get("confirmation_token") or "")
                    waiter = confirmation_waiters.get(token)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(_normalize_confirm_decision(data))
                    continue
                if data.get("type") != "message":
                    continue
                text = str(data.get("text", "") or "").strip()
                if not text and not data.get("attachments"):
                    await websocket.send_json(
                        {"type": "error", "error": "text is required"}
                    )
                    continue
                message_id = str(data.get("message_id") or uuid.uuid4().hex)
                try:
                    model = self._resolve_model_override(data.get("model"))
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "error": str(exc)})
                    continue
                task = asyncio.create_task(
                    process_message(text, message_id, model, data.get("attachments")),
                    name=f"web-message-{message_id}",
                )
                active_tasks.add(task)
                task.add_done_callback(active_tasks.discard)
        except Exception:
            pass
        finally:
            # A tab switch closes the old socket, but must not cancel the
            # session operation. Its sinks remain registered and are rebound
            # by the next WebSocket connection, allowing the reply to
            # continue and the partial snapshot to be restored.
            active_tasks.clear()

    async def _handle_text(
        self,
        session_id: str,
        text: str,
        sink: OutputSink,
        *,
        message_id: str,
        model_override: str | None = None,
        attachments: tuple[MessageAttachment, ...] = (),
    ) -> None:
        assert self._handler is not None
        service = self._service()
        service.touch_session(session_id, status="active")
        attachment_metadata = [
            {
                "id": attachment.source_ref or attachment.local_path.name,
                "filename": attachment.filename or attachment.local_path.name,
                "mime_type": attachment.mime_type,
                "kind": attachment.kind,
                "path": str(attachment.local_path),
                "size_bytes": attachment.size_bytes,
            }
            for attachment in attachments
        ]
        msg = IncomingMessage(
            text=text,
            session_id=session_id,
            channel_name="web",
            metadata={
                "message_id": message_id,
                "model_override": model_override,
                "attachments": attachment_metadata,
            },
            attachments=attachments,
        )
        try:
            await self._handler(msg, sink)
        finally:
            service.touch_session(session_id, status="idle")

    def _parse_uploaded_attachments(self, session_id: str, payload: Any) -> tuple[MessageAttachment, ...]:
        if not isinstance(payload, list):
            return ()
        root = _web_session_upload_dir(session_id).resolve()
        out: list[MessageAttachment] = []
        for raw in payload[:12]:
            if not isinstance(raw, dict):
                continue
            try:
                path = Path(str(raw.get("path", ""))).resolve()
                if root not in path.parents or not path.is_file():
                    continue
                mime = str(raw.get("mime_type", "application/octet-stream"))
                out.append(MessageAttachment(
                    kind=str(raw.get("kind") or attachment_kind_for_mime(mime)),
                    mime_type=mime,
                    local_path=path,
                    filename=Path(str(raw.get("filename") or path.name)).name,
                    source="web",
                    source_ref=str(raw.get("id") or path.name),
                    size_bytes=path.stat().st_size,
                ))
            except (OSError, ValueError):
                continue
        return tuple(out)

    def _record_attachment(
        self,
        session_id: str,
        path: str,
        name: str,
        *,
        turn_id: str = "",
    ) -> None:
        try:
            self._service().record_attachment(session_id, path, name, turn_id=turn_id)
        except Exception:
            logger.exception("failed to journal attachment: %s", path)

    # -- Channel contract -------------------------------------------------------

    async def start(
        self,
        handler: Callable[[IncomingMessage, OutputSink], Any],
    ) -> None:
        """Start the HTTP/WebSocket server and block until ``stop`` is called."""
        self._handler = handler
        self._stop_event = asyncio.Event()

        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError(
                "uvicorn is not installed. Run: uv sync --extra web"
            ) from exc

        config = uvicorn.Config(
            self._app,
            host=self._config.host or "127.0.0.1",
            port=int(self._config.port or 8787),
            log_level="warning",
        )
        server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(server.serve())
        try:
            await self._stop_event.wait()
        finally:
            server.should_exit = True
            if self._server_task is not None and not self._server_task.done():
                await self._server_task
            self._server_task = None
            self._handler = None

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    def create_sink(self, msg: IncomingMessage) -> OutputSink:
        return WebOutputSink(collect=True)

    # -- app factory ------------------------------------------------------------

    def _build_app(self) -> Any:
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.middleware.cors import CORSMiddleware
        from starlette.routing import Mount, Route, WebSocketRoute
        from starlette.staticfiles import StaticFiles

        dist_dir = self._web_dist_dir()

        middleware: list[Middleware] = []
        cors_origins = list(self._config.cors_origins or ())
        if cors_origins:
            middleware.append(
                Middleware(
                    CORSMiddleware,
                    allow_origins=cors_origins,
                    allow_methods=["*"],
                    allow_headers=["Authorization", "X-Auth-Token", "Content-Type"],
                )
            )

        routes: list[Any] = [
            Route("/", self._index, methods=["GET"]),
            Route("/api/health", self._health, methods=["GET"]),
            Route("/api/commands", self._commands, methods=["GET"]),
            Route("/api/config", self._config_get, methods=["GET"]),
            Route("/api/config", self._config_save, methods=["POST"]),
            Route("/api/plugins", self._plugins, methods=["GET"]),
            Route("/api/skills", self._skills, methods=["GET"]),
            Route("/api/context", self._context_stats, methods=["GET"]),
            Route(
                "/api/sessions/{session_id}",
                self._rename_session,
                methods=["PATCH"],
            ),
            Route(
                "/api/sessions/{session_id}",
                self._delete_session,
                methods=["DELETE"],
            ),
            Route(
                "/api/sessions/{session_id}/reveal",
                self._reveal_session,
                methods=["POST"],
            ),
            Route(
                "/api/plugins/{plugin_name}/toggle",
                self._toggle_plugin,
                methods=["POST"],
            ),
            Route("/api/plugins/{plugin_name}", self._delete_plugin, methods=["DELETE"]),
            Route("/api/skills/{skill_id:path}", self._delete_skill, methods=["DELETE"]),
            Route("/api/schedules", self._schedules, methods=["GET"]),
            Route("/api/schedules", self._create_schedule, methods=["POST"]),
            Route("/api/schedules", self._bulk_schedules, methods=["PATCH"]),
            Route("/api/schedules/preview", self._schedule_preview, methods=["POST"]),
            # Registered before /api/schedules/{task_id}: a literal path that
            # lost the race would be read as a task id.
            Route(
                "/api/schedules/attention",
                self._schedule_attention,
                methods=["GET"],
            ),
            Route(
                "/api/schedules/attention",
                self._clear_schedule_attention,
                methods=["POST"],
            ),
            Route("/api/scheduler/health", self._scheduler_health, methods=["GET"]),
            Route("/api/signals", self._signals, methods=["GET"]),
            Route("/api/workflows", self._workflows, methods=["GET"]),
            Route("/api/workflows", self._create_workflow, methods=["POST"]),
            Route(
                "/api/workflows/{workflow_id}",
                self._update_workflow,
                methods=["PUT"],
            ),
            Route(
                "/api/workflows/{workflow_id}",
                self._delete_workflow,
                methods=["DELETE"],
            ),
            Route(
                "/api/schedules/{task_id}/run",
                self._run_schedule_now,
                methods=["POST"],
            ),
            Route(
                "/api/schedules/{task_id}/runs",
                self._schedule_runs,
                methods=["GET"],
            ),
            Route(
                "/api/schedules/{task_id}/runs/{run_id}/retry",
                self._retry_schedule_run,
                methods=["POST"],
            ),
            Route(
                "/api/schedules/{task_id}/runs/{run_id}/cancel",
                self._cancel_schedule_run,
                methods=["POST"],
            ),
            Route(
                "/api/schedules/{task_id}/runs/{run_id}/acknowledge",
                self._acknowledge_schedule_run,
                methods=["POST"],
            ),
            Route(
                "/api/schedules/{task_id}/runs/{run_id}/output",
                self._schedule_run_output,
                methods=["GET"],
            ),
            Route(
                "/api/schedules/{task_id}/runs/{run_id}/artifacts",
                self._schedule_run_artifacts,
                methods=["GET"],
            ),
            Route(
                "/api/schedules/{task_id}/runs/{run_id}/artifacts/{artifact_path:path}",
                self._schedule_run_artifact,
                methods=["GET"],
            ),
            Route("/api/schedules/{task_id}", self._delete_schedule, methods=["DELETE"]),
            Route("/api/schedules/{task_id}", self._patch_schedule, methods=["PATCH"]),
            Route("/api/schedules/{task_id}", self._update_schedule, methods=["PUT"]),
            Route("/api/sessions/{session_id}/attachments", self._upload_attachment, methods=["POST"]),
            Route("/api/files", self._file, methods=["GET"]),
            Route("/api/sessions", self._list_sessions, methods=["GET"]),
            Route("/api/sessions", self._create_session, methods=["POST"]),
            Route("/api/sessions", self._delete_sessions, methods=["DELETE"]),
            Route(
                "/api/sessions/{session_id}/messages",
                self._get_messages,
                methods=["GET"],
            ),
            Route(
                "/api/sessions/{session_id}/state",
                self._get_session_state,
                methods=["GET"],
            ),
            Route(
                "/api/sessions/{session_id}/task-guidance/dismiss",
                self._dismiss_task_guidance,
                methods=["POST"],
            ),
            Route(
                "/api/sessions/{session_id}/workspace/pick",
                self._pick_workspace,
                methods=["POST"],
            ),
            Route(
                "/api/sessions/{session_id}/messages",
                self._post_message,
                methods=["POST"],
            ),
            Route(
                "/api/sessions/{session_id}/cancel",
                self._cancel_session,
                methods=["POST"],
            ),
            Route(
                "/api/sessions/{session_id}/permissions",
                self._get_session_permissions,
                methods=["GET"],
            ),
            Route(
                "/api/sessions/{session_id}/permissions",
                self._patch_session_permissions,
                methods=["PATCH"],
            ),
            Route(
                "/api/sessions/{session_id}/approvals",
                self._delete_session_approvals,
                methods=["DELETE"],
            ),
            WebSocketRoute(
                "/api/sessions/{session_id}/stream",
                self._stream,
            ),
        ]
        if (dist_dir / "assets").is_dir():
            routes.append(Mount("/assets", StaticFiles(directory=dist_dir / "assets")))
        return Starlette(routes=routes, middleware=middleware)
