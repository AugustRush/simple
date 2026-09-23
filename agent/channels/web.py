"""HTTP/WebSocket channel for a browser frontend.

The web frontend is just another channel: ``WebChannel`` implements the same
``Channel`` contract as Feishu, reuses the channel-runner message handler, and
bridges ``OutputSink`` events to JSON events over a WebSocket (or an HTTP
response for non-streaming calls).
"""

from __future__ import annotations

import asyncio
import contextlib
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
from agent.scheduler.models import (
    RUN_IN_FLIGHT_STATUSES,
    acceptance_payload,
    parse_task_signal,
    run_needs_attention,
)
from agent.session_service import SessionService
from agent.verification import verification_payload

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


def _task_in_flight(task: Any, latest_run: Any = None) -> bool:
    """Whether this task has a run that is going to happen, or is happening.

    Answered here rather than left to the client because the client uses it to
    decide how often to ask again, and a client that re-derives it from
    ``active_run_id`` gets it wrong for a signal-woken run: that run is written
    down as ``queued`` and has no ``active_run_id`` until a later tick claims
    it.  One definition, one place to be wrong.
    """
    if getattr(task, "active_run_id", None):
        return True
    return str(getattr(latest_run, "status", "") or "") in RUN_IN_FLIGHT_STATUSES


def _scheduler_run_payload(run: Any, *, with_snapshot: bool = True) -> dict[str, Any]:
    """One run, as the interface reads it.

    *with_snapshot* is off for the copies embedded in a list: the run's config
    snapshot is the largest field it has, it is read only by the run-detail
    drawer, and that drawer is fed by the run-history endpoint rather than by
    the list.  Sending it with every task on every poll is the difference
    between a list payload that is mostly answer and one that is mostly a
    field nobody looks at -- and it is the part that grows, because a step's
    snapshot now carries its upstreams' report previews.
    """
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
    payload = {
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
        # Two answers that are not the same question: ``status`` says whether
        # the run happened, ``verdict`` says whether what it produced met the
        # bar the task was given.  They travel separately because a run can
        # deliver a result that does not do the job, and a result that does the
        # job can fail to arrive -- and collapsing them is what made a useless
        # answer read as success.
        "verdict": str(getattr(run, "verdict", "") or ""),
        "verification": verification_payload(getattr(run, "verification", None)),
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
    if with_snapshot:
        payload["config_snapshot"] = dict(getattr(run, "config_snapshot", {}) or {})
    return payload


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
        # What this task's runs are judged by.  Sent so the interface can say
        # it, because a criterion the person never saw is indistinguishable
        # from a run that failed for no reason.
        "acceptance": acceptance_payload(getattr(task, "acceptance", None)),
        # Empty for a standalone task, which most are.  Carried on the task
        # rather than looked up from the graph so a task row in the list can
        # say where it belongs without the client holding the whole graph.
        "workflow_id": str(getattr(task, "workflow_id", "") or ""),
        "step_key": str(getattr(task, "step_key", "") or ""),
        # The words that asked for this task.  Sent for the same reason the
        # acceptance criterion above is: a task that appeared without anyone
        # asking for it is what this field exists to make impossible to hide,
        # and that only works if the answer is visible rather than only stored.
        "request_quote": str(getattr(task, "request_quote", "") or ""),
        "unseen_attention": int(unseen_attention or 0),
        "active_run_id": task.active_run_id,
        # The client uses this to choose how soon to ask again, so it travels
        # as an answer rather than as the two fields it is derived from.
        "in_flight": _task_in_flight(task, latest_run),
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "last_run_at": task.last_run_at.isoformat() if task.last_run_at else None,
        "last_success_at": (
            task.last_success_at.isoformat() if task.last_success_at else None
        ),
        "latest_run": (
            _scheduler_run_payload(latest_run, with_snapshot=False)
            if latest_run
            else None
        ),
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
                "acceptance": acceptance_payload(getattr(step, "acceptance", None)),
                # Sent back because a field that can be set and not read is a
                # field the editor has to remember for itself -- and the next
                # save, which resends what it was shown, would quietly reset it.
                "retry_policy": dict(getattr(step, "retry_policy", None) or {}),
                "timeout_seconds": int(step.timeout_seconds),
                "task_id": task_id,
                "enabled": bool(task.enabled) if task is not None else False,
                "unseen_attention": unseen,
                "in_flight": _task_in_flight(task, latest) if task is not None else False,
                "latest_run": (
                    _scheduler_run_payload(latest, with_snapshot=False)
                    if latest
                    else None
                ),
            }
        )
    return {
        "id": workflow.id,
        "name": workflow.name,
        "description": workflow.description,
        "enabled": bool(workflow.enabled),
        # The sentence that asked for the chain, which its steps inherit.
        # Sent here rather than repeated on every step because the chain is
        # what was asked for -- the steps are how it runs.
        "request_quote": str(getattr(workflow, "request_quote", "") or ""),
        "created_at": (
            workflow.created_at.isoformat() if workflow.created_at else None
        ),
        "updated_at": (
            workflow.updated_at.isoformat() if workflow.updated_at else None
        ),
        "steps": steps,
        "unseen_attention": attention,
    }


def _attention_rows(store: Any, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """The snapshot's runs as rows a page can recognise and reopen.

    Each row carries where the run came from -- the task, and the workflow
    and step when it has one -- because a failure that belongs to a step of a
    deleted workflow is otherwise a row with no home, which is how a count
    becomes impossible to find.  The reason a run is asking is left to the
    client's own wording: the sentence already exists there next to the
    status labels it quotes, and a second copy here would be the thing that
    starts saying something else.
    """
    tasks = {task.id: task for task in store.list_tasks()}
    workflows = {workflow.id: workflow for workflow in store.list_workflows()}
    items = []
    for run in snapshot["runs"]:
        task = tasks.get(run.task_id)
        workflow_id = str(getattr(task, "workflow_id", "") or "")
        workflow = workflows.get(workflow_id)
        items.append(
            {
                "run_id": run.id,
                "task_id": run.task_id,
                "task_name": getattr(task, "name", "") or run.task_id,
                "workflow_id": workflow_id,
                "workflow_name": workflow.name if workflow is not None else "",
                # A step whose workflow is gone is not a step of nothing: it is
                # a step of something nobody can open, and the list has to be
                # able to say so rather than show a blank.
                "workflow_deleted": bool(workflow_id) and workflow is None,
                "step_key": str(getattr(task, "step_key", "") or ""),
                "status": run.status,
                "missed_count": int(getattr(run, "missed_count", 0) or 0),
                "error": str(getattr(run, "error", "") or ""),
                "started_at": (
                    run.started_at.isoformat() if run.started_at else None
                ),
                "finished_at": (
                    run.finished_at.isoformat() if run.finished_at else None
                ),
            }
        )
    return items


def _attention_payload(store: Any) -> dict[str, Any]:
    """The number on the badge and the list behind it, from one snapshot.

    Sent together because they are the same fact at two sizes, and two facts
    is what a badge that disagrees with its own page is.  The number and the
    rows come out of the store's :meth:`attention_snapshot` — one query under
    one lock — so a run finishing between two reads cannot make the payload
    disagree with itself.

    ``latest_run_by_task`` answers "which run do I open for this task" for
    every task the count includes, including the ones whose rows fell out of
    the capped list -- where the list's own first row would be missing and
    the caller would be back to opening the newest run, the one that is
    usually fine.
    """
    snapshot = store.attention_snapshot()
    return {
        "unseen_attention": snapshot["total"],
        "attention_runs": _attention_rows(store, snapshot),
        "latest_run_by_task": snapshot["latest_by_task"],
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
        self._retired = False
        self._completion_event = asyncio.Event()
        self.on_attachment = on_attachment
        self._confirmation_handler = confirmation_handler
        # Session output directory scanned for media produced during a turn so
        # images/audio/video show inline even if the agent never called send_file.
        self._output_dir = output_dir
        self._turn_started_at = 0.0
        self._attached_paths: set[str] = set()
        self.full_text = ""
        self.reasoning_text = ""
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

    def on_reasoning_chunk(self, chunk: str) -> None:
        """Stream the model's thinking, kept out of ``full_text``.

        ``full_text`` is the message: it is what the reconnect snapshot
        replays and what a turn is reported to have said.  Thinking is not
        part of it, so it travels on its own event and its own accumulator.
        """
        if not chunk:
            return
        self.reasoning_text += chunk
        self._emit({"type": "reasoning_chunk", "chunk": chunk})

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
        # Per-turn like the text: a reused socket sink would otherwise replay
        # the previous message's thinking onto this one.
        self.reasoning_text = ""

    @property
    def turn_complete_emitted(self) -> bool:
        return self._turn_complete_emitted

    @property
    def retired(self) -> bool:
        """True once the queued message this sink carried was withdrawn."""
        return self._retired

    def retire_queued_message(self) -> None:
        """Wake this sink's waiter without reporting a finished turn.

        A message queued behind a running operation parks its own
        ``process_message`` task on ``wait_for_completion``.  Withdrawing the
        message has to release that task — but it must not emit
        ``turn_complete``, which the client would read as the *current* turn
        ending.  Marking the sink retired lets the delivery path tell "nothing
        to report" apart from "still waiting".
        """
        self._retired = True
        self._completion_event.set()

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
        snapshot = {"type": "stream_snapshot", "text": self.full_text}
        if self.reasoning_text:
            # Carried so a tab switch during a thinking-heavy turn brings the
            # note back with the text, rather than emptying it mid-sentence.
            snapshot["reasoning"] = self.reasoning_text
        return snapshot

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


#: Header names whose value is a credential.  A header is just a name, so the
#: only thing that can be said about one is whether its *name* reads like a
#: secret -- which is what a masking pass has to go on, and is why the list is
#: a pattern rather than an enumeration.
_SECRET_HEADER_RE = re.compile(r"authorization|cookie|(^|[-_])key$|key[-_]|token|secret")


def _mask_api_keys(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``cfg`` with provider credentials masked for display.

    ``api_key`` and any header whose name reads like a credential: the settings
    page round-trips the whole provider block, so a token that happens to be
    spelled as a header would otherwise be handed back to the browser in the
    clear.  The masking must be symmetric with :func:`_restore_masked_api_keys`
    or a save would write ``******`` over a working credential.
    """
    import copy

    masked = copy.deepcopy(cfg)
    providers = masked.get("providers")
    if isinstance(providers, dict):
        for provider in providers.values():
            if not isinstance(provider, dict):
                continue
            if provider.get("api_key"):
                provider["api_key"] = "******"
            headers = provider.get("headers")
            if isinstance(headers, dict):
                for name, value in headers.items():
                    if value and _SECRET_HEADER_RE.search(str(name).lower()):
                        headers[name] = "******"
    return masked


def _restore_masked_api_keys(new_cfg: dict[str, Any], current_cfg: dict[str, Any]) -> None:
    """Keep existing credentials when the UI saved the masked placeholder back."""
    providers = new_cfg.get("providers")
    current_providers = current_cfg.get("providers")
    if not isinstance(providers, dict) or not isinstance(current_providers, dict):
        return
    for name, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        current = current_providers.get(name)
        if provider.get("api_key") == "******":
            if isinstance(current, dict) and current.get("api_key"):
                provider["api_key"] = current["api_key"]
        headers = provider.get("headers")
        current_headers = current.get("headers") if isinstance(current, dict) else None
        if isinstance(headers, dict) and isinstance(current_headers, dict):
            for header_name, value in headers.items():
                if value == "******" and current_headers.get(header_name):
                    headers[header_name] = current_headers[header_name]


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


def _names_a_trigger_field(raw: dict[str, Any]) -> bool:
    """Whether a step body says anything about *when* it runs, other than type.

    Kept separate from the ``trigger_type`` test so the type's own field is not
    double-counted, and so a body that names only an hour is still read as an
    answer.  Emptiness is not an answer: an editor that sends every field it
    knows about, blank ones included, is not asking for anything.
    """
    from agent.scheduler.editing import TRIGGER_BODY_FIELDS

    return any(
        field_name != "trigger_type"
        and field_name != "trigger_from"
        and str(raw.get(field_name) or "").strip()
        for field_name in TRIGGER_BODY_FIELDS
    )


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
        from agent.config import load_config, provider_fields_payload
        from agent.shared import THINKING_EFFORTS

        cfg, _ = load_config()
        masked = _mask_api_keys(cfg)
        # The vocabulary the settings page renders from, sent for the same
        # reason `thinking_efforts` is: the page draws what this backend
        # accepts instead of keeping its own copy of the fields, so a provider
        # key added here appears in the form without a frontend change and one
        # removed here disappears from it.
        return JSONResponse({
            "config": masked,
            "thinking_efforts": list(THINKING_EFFORTS),
            "provider_fields": provider_fields_payload(),
        })

    def _bump_config_revision(self) -> None:
        """Make the next turn rebuild against the config just written."""
        self._components["config_revision"] = (
            int(self._components.get("config_revision", 0)) + 1
        )

    @staticmethod
    def _provider_write_error(provider_name: str, provider: dict[str, Any]) -> Optional[str]:
        """Why this provider block cannot be written, or None.

        Structural problems are refused here rather than warned about, because
        a write is an assertion about what the agent will do next: a key the
        loader never reads, a format that does not exist, or a missing required
        field would be saved and then quietly ignored at runtime.  Warnings
        remain the mode for *reading* a config -- the file may already hold
        something imperfect and refusing to start over it would be worse.
        """
        from agent.config import PROVIDER_FIELDS, provider_config_errors

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(provider_name or "")):
            return (
                "provider 名字只能包含字母、数字、点、下划线和连字符，"
                "且不能以下划线开头"
            )
        for field in PROVIDER_FIELDS:
            if field.required and not str(provider.get(field.key) or "").strip():
                return f"{field.label}（{field.key}）不能为空"
        # The same checks the config reader warns about, refusing instead of
        # warning: a write is an assertion about what the agent will do next.
        # Reading the *messages* would mean matching their text, which is how a
        # header name that is not an HTTP token could be saved by a form and
        # then sent verbatim (a strict gateway rejects it, and the reason is
        # nowhere near the config that caused it).
        errors = provider_config_errors(provider_name, provider)
        if errors:
            return errors[0]
        return None

    async def _provider_save(self, request: Any) -> Any:
        """Create or update one provider, leaving every other key alone."""
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        name = str(request.path_params.get("provider_name") or "").strip()
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        incoming = body.get("provider") if isinstance(body, dict) else None
        if not isinstance(incoming, dict):
            return JSONResponse({"error": "body must be {'provider': {...}}"}, status_code=400)
        from agent.config import PROVIDER_FIELDS, load_config, save_config

        cfg, _ = load_config()
        providers = cfg.get("providers")
        if not isinstance(providers, dict):
            providers = {}
            cfg["providers"] = providers
        current = providers.get(name)
        merged = dict(current) if isinstance(current, dict) else {}
        # Only declared fields are written: a patch that names a key the
        # loader never reads is a promise the agent would not keep, and the
        # `_readme` companions are documentation, not settings.
        known = {field.key for field in PROVIDER_FIELDS}
        unknown = [
            key for key in incoming if key not in known and not str(key).startswith("_")
        ]
        if unknown:
            return JSONResponse(
                {
                    "error": (
                        f"未知的 provider 字段：{'、'.join(sorted(unknown))}；"
                        f"可用：{'、'.join(sorted(known))}"
                    )
                },
                status_code=400,
            )
        for key, value in incoming.items():
            if key in known:
                merged[key] = value
        # A value the page was shown masked means "unchanged": it must be
        # resolved against what is on disk before anything is written, or a
        # save would store the asterisks over a working credential.
        _restore_masked_api_keys({"providers": {name: merged}}, cfg)
        error = self._provider_write_error(name, merged)
        if error:
            return JSONResponse({"error": error}, status_code=400)
        providers[name] = merged
        try:
            save_config(cfg)
        except Exception as exc:
            return JSONResponse({"error": f"save failed: {exc}"}, status_code=500)
        self._bump_config_revision()
        return JSONResponse({"ok": True, "provider": _mask_api_keys({"providers": {name: merged}})["providers"][name]})

    async def _provider_delete(self, request: Any) -> Any:
        """Remove a provider, refusing the two that would brick the config."""
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        name = str(request.path_params.get("provider_name") or "").strip()
        from agent.config import load_config, save_config

        cfg, _ = load_config()
        providers = cfg.get("providers")
        if not isinstance(providers, dict) or name not in providers:
            return JSONResponse({"error": f"找不到 provider '{name}'"}, status_code=404)
        if str(cfg.get("active_provider") or "") == name:
            return JSONResponse(
                {"error": "这是当前使用的 provider，请先切换到别的再删除"},
                status_code=400,
            )
        if len([k for k in providers if not str(k).startswith("_")]) <= 1:
            return JSONResponse({"error": "至少要保留一个 provider"}, status_code=400)
        providers.pop(name, None)
        try:
            save_config(cfg)
        except Exception as exc:
            return JSONResponse({"error": f"save failed: {exc}"}, status_code=500)
        self._bump_config_revision()
        return JSONResponse({"ok": True})

    async def _provider_activate(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        name = str(request.path_params.get("provider_name") or "").strip()
        from agent.config import load_config, save_config

        cfg, _ = load_config()
        providers = cfg.get("providers")
        if not isinstance(providers, dict) or name not in providers:
            return JSONResponse({"error": f"找不到 provider '{name}'"}, status_code=404)
        cfg["active_provider"] = name
        # The model has to follow the switch when the old id is not one this
        # group offers.  `routable_model_ids` would not catch that -- it spans
        # every provider, so another group's model id is still "routable" and
        # the session would keep sending it to the endpoint that does not own
        # it (the 400-naming-its-own-models failure the routing table exists
        # to prevent).  The question is group-scoped.
        from agent.core.transport import provider_model_ids

        provider_cfg = providers.get(name) or {}
        current_model = str(cfg.get("model") or "")
        if current_model and current_model not in provider_model_ids(provider_cfg):
            cfg["model"] = str(provider_cfg.get("default_model") or "")
        try:
            save_config(cfg)
        except Exception as exc:
            return JSONResponse({"error": f"save failed: {exc}"}, status_code=500)
        self._bump_config_revision()
        # The model the session will actually send with, not the raw `model`
        # key: that key may be absent, in which case the provider's
        # `default_model` is what a turn resolves to -- and the page has to show
        # the id it is about to put on the wire.  `active_model_and_tokens` is
        # the one place that answers "which model does this config name", so
        # this asks it instead of deciding again.
        from agent.config import ModelClientFactory

        effective_model, _tokens = ModelClientFactory.active_model_and_tokens(cfg)
        return JSONResponse(
            {"ok": True, "active_provider": name, "model": effective_model}
        )

    async def _provider_test(self, request: Any) -> Any:
        """Send one minimal request to a provider, through the real path.

        Deliberately not a hand-rolled HTTP call: the point of the button is to
        prove that *this* provider's configuration works -- its base_url, its
        wire format, its auth, and its headers, including a per-conversation
        header (which falls back to the process id here, there being no
        conversation).  A check that builds its own request would pass while
        the agent's own requests failed.
        """
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        name = str(request.path_params.get("provider_name") or "").strip()
        from agent.config import load_config
        from agent.bootstrap import _provider_client_factory
        from agent.core.transport import build_transport, provider_stream_usage
        from agent.shared import provider_headers

        cfg, _ = load_config()
        providers = cfg.get("providers")
        if not isinstance(providers, dict) or name not in providers:
            return JSONResponse({"error": f"找不到 provider '{name}'"}, status_code=404)
        provider_cfg = providers.get(name) or {}
        if not isinstance(provider_cfg, dict):
            return JSONResponse({"error": f"provider '{name}' 不是一个对象"}, status_code=400)
        api_format = str(provider_cfg.get("api_format") or "openai")
        model = str(provider_cfg.get("default_model") or "")
        if not model:
            return JSONResponse({"error": "这个 provider 没有 default_model"}, status_code=400)

        started = time.perf_counter()
        try:
            client = _provider_client_factory(provider_cfg, api_format)
            transport = build_transport(
                api_format,
                client,
                None,
                provider_stream_usage(provider_cfg),
                headers=provider_headers(provider_cfg),
                provider_name=name,
            )
            # Small but not degenerate: a gateway is entitled to reject a
            # max_tokens of 1, and that rejection would say nothing about the
            # configuration being tested.
            await transport.create(
                model=model,
                max_tokens=16,
                system="You are a connectivity check. Reply with one word.",
                messages=[{"role": "user", "content": "ping"}],
                tools=[],
            )
        except Exception as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "provider": name,
                    "model": model,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "error": f"{type(exc).__name__}: {exc}",
                },
                status_code=200,
            )
        finally:
            # This endpoint builds its own client (the routing transport's
            # cached one belongs to the running components), so it owns closing
            # it: an SDK client holds a connection pool, and a button that
            # leaves one behind per press leaks sockets.
            with contextlib.suppress(Exception):
                await client.close()
        return JSONResponse(
            {
                "ok": True,
                "provider": name,
                "model": model,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "error": "",
            },
            status_code=200,
        )

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
        self._bump_config_revision()
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
            # The listing that manages skills, not the one the model reads:
            # a switched-off skill has to stay visible, otherwise the switch
            # that turned it off could never be found again.
            list_skills = getattr(catalog, "list_all_skills", None)
            is_enabled = getattr(catalog, "is_enabled", None)
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
                                "enabled": (
                                    bool(is_enabled(bundle)) if callable(is_enabled) else True
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

    def _feishu_ready(self) -> bool:
        feishu = self._feishu_channel_config()
        return bool(feishu.get("app_id") and feishu.get("app_secret"))

    def _signal_problem(self, name: str) -> Optional[str]:
        """Why *name* can never fire, or None when it can.

        A short-lived connection of its own, because the builder deliberately
        knows nothing about persistence: it turns a body into a spec, and the
        caller owns the write.  The check needs to read tasks, so it borrows a
        connection rather than widening the builder's contract for one
        validation.
        """
        from agent.scheduler import SchedulerStore

        probe = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            return probe.describe_signal_problem(name)
        finally:
            probe.close()

    def _edit_context(self):
        """What building a task definition needs from this process.

        Named in one place because two builders ask for it -- a standalone task
        and a workflow's step -- and a step that resolved the folder, the skill
        catalogue or the model list differently from a task would be the same
        definition accepted through one door and refused through another.
        """
        from agent.scheduler import EditContext

        chosen = self._components.get("workspace_root")
        return EditContext(
            chosen_workspace_root=Path(chosen) if chosen else None,
            fallback_workspace_root=Path.cwd().resolve(),
            skill_catalog=self._components.get("skill_catalog"),
            model_validator=self._resolve_model_override,
            feishu_ready=self._feishu_ready,
            signal_problem=self._signal_problem,
        )

    def _trigger_from_body(self, body: dict[str, Any], existing: Any = None):
        """A ``TriggerSpec`` from a body, filling gaps from what was stored."""
        from agent.scheduler import trigger_from_body

        return trigger_from_body(body, existing, signal_problem=self._signal_problem)

    def _delivery_from_body(self, body: dict[str, Any], existing: Any = None):
        """Delivery mode and target, from the body or from what the task had."""
        from agent.scheduler import delivery_from_body

        return delivery_from_body(body, existing, feishu_ready=self._feishu_ready)

    def _feishu_channel_config(self) -> dict[str, Any]:
        """The global Feishu channel config, as delivery reads it at run time.

        Delivery resolves credentials from the global config when a run
        finishes, so this is the same source -- asking here what the runtime
        will see then, not a copy that can drift.
        """
        from agent.config import load_config

        cfg, _ = load_config()
        feishu = cfg.get("channels", {}).get("feishu", {})
        return feishu if isinstance(feishu, dict) else {}

    def _schedule_from_body(
        self, body: dict[str, Any], existing: Any = None, *, keep_trigger: bool = False
    ):
        """The whole task a body leaves behind, given the task it edits.

        A field the body does not mention keeps the value the stored task had.
        That is the convention the graph editor has always used -- written down
        at ``_workflow_step_from_body``, where the reason is spelled out -- and
        it is the one place the task editor did not follow it.  Because
        ``update_task`` rewrites every column from the spec it is handed, a
        field the builder left out was not *skipped*: it was written back as
        the dataclass default.  Saving a task's name through the interface
        cleared the files it had declared it would produce, and the run went on
        failing against a promise its own row no longer contained.
        """
        from agent.scheduler import task_from_body

        return task_from_body(
            body,
            existing,
            context=self._edit_context(),
            keep_trigger=keep_trigger,
        )

    def _borrowed_trigger(
        self, raw: dict[str, Any], key: str, existing_by_key: dict[str, Any]
    ) -> Any:
        """The trigger a step asks to take over from another step, if it asks.

        A schedule belongs to the *chain*, not to the step that happens to hold
        it: "每天早上九点" is when the workflow runs, and the entry step's task
        is only where the clock lives, because that is the task that fires.
        So when an edit moves the entry role from one step to another -- which
        is what reordering a chain does -- the schedule has to move with it.

        ``trigger_from`` is how the editor says that without ever holding the
        trigger itself.  It names the step whose schedule moves, and the spec is
        copied from what is stored, so a reorder cannot round-trip somebody's
        weekly clock through a form that only knows how to render it.

        The donor must be a step of the *stored* graph, and the answer is read
        before anything is written, so it does not matter what order the body
        lists the steps in.
        """
        donor_key = str(raw.get("trigger_from", "") or "").strip()
        if not donor_key:
            return None
        if donor_key == key:
            raise ValueError(f"步骤「{key}」不能沿用自己现有的触发方式")
        donor = existing_by_key.get(donor_key)
        if donor is None:
            raise ValueError(
                f"步骤「{key}」想沿用「{donor_key}」的触发方式，"
                "但这个流程里没有这一步"
            )
        if getattr(donor, "trigger", None) is None:
            raise ValueError(
                f"步骤「{key}」想沿用「{donor_key}」的触发方式，"
                f"但「{donor_key}」不是入口步骤，它由上游驱动，没有自己的触发方式"
            )
        return donor.trigger

    def _workflow_step_from_body(
        self,
        raw: dict[str, Any],
        index: int,
        existing: Any = None,
        borrowed_trigger: Any = None,
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

        Where a step's own schedule is concerned, an explicit ``trigger_type``
        is an answer, an inherited one from ``borrowed_trigger`` is the next
        best thing, and only then does the step keep what it had.
        """
        from agent.scheduler import (
            MAX_RETRY_ATTEMPTS,
            MAX_RETRY_BACKOFF_SECONDS,
            WorkflowStep,
        )

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
            if borrowed_trigger is not None:
                raise ValueError(
                    f"步骤「{key}」有上游，不能沿用别的步骤的触发方式；"
                    "触发方式跟着入口步骤走"
                )
        elif given_trigger:
            trigger = self._trigger_from_body(raw, existing)
        elif _names_a_trigger_field(raw):
            # A body that names no ``trigger_type`` but does name, say, the hour
            # is still an answer about when this runs; the stored trigger's own
            # type is what it is answering about.  Read as "unset" it was
            # silently ignored, and a step whose schedule was never moved
            # looked exactly like one that was.
            trigger = self._trigger_from_body(raw, existing)
        elif borrowed_trigger is not None:
            trigger = borrowed_trigger
        elif existing is not None and existing.trigger is not None:
            trigger = existing.trigger
        else:
            raise ValueError(
                f"步骤「{key}」没有上游，必须指定触发方式（trigger_type）"
            )
        delivery_mode, delivery_target = self._delivery_from_body(raw, existing)
        skills = answered("selected_skills", None)
        if skills is not None and not isinstance(skills, list):
            raise ValueError(f"步骤「{key}」的 selected_skills 必须是数组")
        # Checked against the same bounds a task's is, and refused rather than
        # clamped: a number that comes back different from the one that was
        # sent is a silent disagreement about what this step will do when it
        # fails.  The store clamps instead, because a graph written straight to
        # it has nobody left to tell.
        raw_retry = answered("retry_policy", None)
        if raw_retry is not None and not isinstance(raw_retry, dict):
            raise ValueError(f"步骤「{key}」的 retry_policy 必须是对象")
        retry = dict(raw_retry or {})
        try:
            # No ``or`` fallbacks here: ``0 or 1`` is ``1``, and a
            # max_attempts of 0 sent by the editor would slip through the
            # range check below as the no-retry default instead of being
            # refused.  The task-level check reads its fields the same way.
            max_attempts = int(retry.get("max_attempts", 1))
            backoff_seconds = int(retry.get("backoff_seconds", 30))
        except (TypeError, ValueError):
            raise ValueError(f"步骤「{key}」的 retry_policy 必须是整数")
        if max_attempts < 1 or max_attempts > MAX_RETRY_ATTEMPTS:
            raise ValueError(
                f"步骤「{key}」的最大尝试次数必须在 1 到 {MAX_RETRY_ATTEMPTS} 之间"
            )
        if backoff_seconds < 0 or backoff_seconds > MAX_RETRY_BACKOFF_SECONDS:
            raise ValueError(
                f"步骤「{key}」的重试间隔必须在 0 到 {MAX_RETRY_BACKOFF_SECONDS} 秒之间"
            )
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
            # Validated exactly like a task's: a step is a task, and an id no
            # provider group owns can only reach the wrong endpoint at run
            # time -- where nothing re-checks it.  Accepting it here would put
            # the failure a scheduled run away from the request that caused it.
            model_override=self._resolve_model_override(
                answered("model_override", None)
            ),
            timeout_seconds=int(answered("timeout_seconds", 1800) or 1800),
            selected_skills=[
                str(item).strip()
                for item in (skills or [])
                if str(item).strip()
            ],
            retry_policy={
                "max_attempts": max_attempts,
                "backoff_seconds": backoff_seconds,
            },
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
            key = str(raw.get("key", "")).strip()
            steps.append(
                self._workflow_step_from_body(
                    raw,
                    index,
                    existing_by_key.get(key),
                    # Resolved against the stored graph rather than against the
                    # body, so a step may name a donor that this same request is
                    # about to give upstreams to: what moves is the schedule
                    # that is there now.
                    borrowed_trigger=self._borrowed_trigger(
                        raw, key, existing_by_key
                    ),
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
            # One snapshot for the whole response: the per-task counts on the
            # cards, the badge number, and the rows behind it all read from
            # the same query, so the page can add up its own rows and get the
            # number the navigation is showing.
            snapshot = store.attention_snapshot()
            unseen = snapshot["counts"]
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
                    # that only reaches whoever happened to be looking.  The
                    # runs come along so the page can point at them: a count
                    # with nothing to click is the question this answers.
                    "unseen_attention": snapshot["total"],
                    "attention_runs": _attention_rows(store, snapshot),
                    "latest_run_by_task": snapshot["latest_by_task"],
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
            ok = task is not None
            try:
                store.delete_task(task_id)
            except ValueError as exc:
                # A running task, or a step of a workflow that still exists:
                # both are the store's call, and both are conflicts rather
                # than bad requests -- the id is right, the moment is not.
                return JSONResponse({"error": str(exc)}, status_code=409)
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
            try:
                store.set_enabled(task_id, bool(body["enabled"]))
            except ValueError as exc:
                # A step of a workflow that still exists: its switch is the
                # workflow's to write, so the refusal says where to go.  The
                # store decides, because the same question is asked by the
                # bulk endpoint and by the agent's tools, and one rule should
                # not have three copies to drift.
                return JSONResponse({"error": str(exc)}, status_code=409)
        finally: store.close()
        return JSONResponse({"ok": True, "enabled": bool(body["enabled"])})

    async def _update_schedule(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            from agent.scheduler import SchedulerStore, mirror_step_edit, step_owns_no_trigger

            store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
            try:
                task_id = str(request.path_params["task_id"])
                existing = store.get_task(task_id)
                if existing is None:
                    return JSONResponse({"error": "task not found"}, status_code=404)
                spec = self._schedule_from_body(
                    body,
                    existing,
                    # A step that waits on upstreams does not own its trigger:
                    # the graph says it waits for them, and the task's own
                    # signal trigger is that answer written down.  Letting the
                    # task editor set a second one would either replace the
                    # edge with a clock or leave one of the two lying, and the
                    # editor has no field that could express "after A and B
                    # succeed" anyway.  The store decides, so the two doors
                    # cannot disagree.
                    keep_trigger=step_owns_no_trigger(store, existing),
                )
                updated = store.update_task(task_id, spec)
                if updated is not None:
                    # The task row is what was written; the graph is what the
                    # next save of the workflow rebuilds it from.  Without
                    # this the edit survives until somebody moves an edge.
                    mirror_step_edit(store, updated)
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
        # One convention for both kinds of skip: the sentence that says why.
        # A code would need a table on the other side to become a sentence, and
        # the only reader here counts them -- while the reasons worth having
        # are the ones the store writes, which are already sentences.
        skipped: list[dict[str, str]] = []
        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            for task_id in ids:
                task = store.get_task(task_id)
                if task is None:
                    skipped.append({"id": task_id, "reason": "任务不存在"})
                    continue
                try:
                    if action == "delete":
                        store.delete_task(task_id)
                    else:
                        store.set_enabled(task_id, action == "enable")
                except ValueError as exc:
                    # The store's own refusal -- a running task, or a step
                    # whose workflow still exists.
                    skipped.append({"id": task_id, "reason": str(exc)})
                    continue
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
                    # Same payload as the poll, so the page that just cleared
                    # one run shows the same list it would have fetched.
                    **_attention_payload(store),
                }
            )
        finally:
            store.close()

    async def _schedule_attention(self, request: Any) -> Any:
        """The count for a badge polled from every view, and the runs behind it.

        Separate from ``GET /api/schedules`` because the indicator has to work
        for someone who never opens the schedules page -- making them load
        every task and run just to find out whether anything failed would be
        the opposite of a notification.

        The runs ride along rather than arriving on a second request, because
        the two numbers that used to disagree were a count from here and a
        list assembled somewhere else.  One payload cannot disagree with
        itself, and at this poll rate the extra rows cost nothing.
        """
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from agent.scheduler import SchedulerStore

        store = SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)
        try:
            return JSONResponse(_attention_payload(store))
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
                    **_attention_payload(store),
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

    async def _toggle_skill(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict) or "enabled" not in body:
            return JSONResponse({"error": "enabled is required"}, status_code=400)
        desired = bool(body["enabled"])

        catalog = self._components.get("skill_catalog")
        if catalog is None:
            return JSONResponse({"error": "skill catalog is not available"}, status_code=503)
        skill_id = str(request.path_params["skill_id"])
        # The switch is thrown on a skill that may well be off already, so the
        # lookup has to see the ones that are off -- otherwise the only
        # position a switch could not be moved from is "off".
        bundle = catalog.find_any(skill_id)
        if bundle is None:
            return JSONResponse({"error": "skill not found"}, status_code=404)

        from agent.config import load_config, save_config

        cfg, _ = load_config()
        skills_cfg = cfg.get("skills")
        if not isinstance(skills_cfg, dict):
            skills_cfg = {}
            cfg["skills"] = skills_cfg
        entry = skills_cfg.get(bundle.id)
        if not isinstance(entry, dict):
            entry = {}
            skills_cfg[bundle.id] = entry
        entry["enabled"] = desired
        try:
            save_config(cfg)
        except Exception as exc:
            return JSONResponse({"error": f"save failed: {exc}"}, status_code=500)

        # Applied in memory too, and that is the whole of it. Unlike a plugin,
        # whose tools and MCP servers have to be rebuilt, a skill switch only
        # changes what the prompt says is available: marking the catalog dirty
        # recomposes the prompt before the next turn, for sessions that are
        # already open, without rebuilding their runtime underneath them.
        catalog.set_enabled(bundle.id, desired)
        return JSONResponse({"ok": True, "id": bundle.id, "enabled": desired})

    async def _delete_skill(self, request: Any) -> Any:
        from starlette.responses import JSONResponse
        if not self._authorized(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        catalog = self._components.get("skill_catalog")
        # Management, so switched-off skills count: deleting one is how a
        # person gets rid of a skill they turned off and no longer want.
        bundle = catalog.find_any(str(request.path_params["skill_id"])) if catalog is not None else None
        if bundle is None: return JSONResponse({"error": "skill not found"}, status_code=404)
        if getattr(bundle, "source", "") != "user": return JSONResponse({"error": "内置技能不能删除"}, status_code=403)
        try:
            import shutil
            path = Path(bundle.path).resolve()
            root = Path(getattr(catalog, "user_root", shared.SKILLS_DIR)).resolve()
            if root not in path.parents: return JSONResponse({"error": "invalid skill path"}, status_code=400)
            shutil.rmtree(path)
            catalog.forget(bundle.id)
            self._forget_skill_config(bundle.id)
            catalog.reload()
            self._components["config_revision"] = int(self._components.get("config_revision", 0)) + 1
            return JSONResponse({"ok": True, "id": bundle.id})
        except Exception as exc: return JSONResponse({"error": str(exc)}, status_code=500)

    @staticmethod
    def _forget_skill_config(skill_id: str) -> None:
        """Drop a deleted skill's switch from config.json.

        The id can be reused by a skill created later, and a stale "off" entry
        would meet it with a state nobody chose and nothing on screen to
        explain. Best-effort: the directory is already gone, and failing to
        tidy the config must not turn a completed deletion into an error.
        """
        from agent.config import load_config, save_config

        try:
            cfg, _ = load_config()
            skills_cfg = cfg.get("skills")
            if isinstance(skills_cfg, dict) and skill_id in skills_cfg:
                del skills_cfg[skill_id]
                save_config(cfg)
        except Exception:
            pass

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

    async def _pick_directory(self, request: Any) -> Any:
        """The OS folder picker, for a form field instead of a session.

        Session picking goes through ``/api/sessions/{id}/workspace/pick``
        because it has to reroute the session afterwards; a form just wants
        the path back.  Same native dialog either way, so both places see the
        same folders the user sees in Finder/Explorer.
        """
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        selected = await _pick_workspace_directory()
        if not selected:
            return JSONResponse({"cancelled": True, "workspace_root": ""})
        return JSONResponse({"cancelled": False, "workspace_root": selected})

    @staticmethod
    def _list_feishu_chats(feishu_cfg: dict[str, Any]) -> list[dict[str, Any]]:
        """The chats the bot sits in, page by page.

        The lark client is synchronous; callers run this in a thread so the
        event loop is never blocked, the same rule FeishuOutputSink follows.
        """
        from agent.channels.feishu import FeishuConfig, build_feishu_client
        from lark_oapi.api.im.v1 import ListChatRequestBuilder  # type: ignore[import]

        client = build_feishu_client(FeishuConfig(**feishu_cfg))
        chats: list[dict[str, Any]] = []
        page_token = ""
        for _ in range(10):  # 10 pages x 100 is far past any real bot
            builder = ListChatRequestBuilder().page_size(100)
            if page_token:
                builder = builder.page_token(page_token)
            resp = client.im.v1.chat.list(builder.build())
            if not resp.success():
                raise RuntimeError(
                    f"飞书会话列表获取失败：code={resp.code} msg={resp.msg}"
                )
            data = resp.data
            for item in getattr(data, "items", None) or []:
                chat_id = str(getattr(item, "chat_id", "") or "")
                if not chat_id:
                    continue
                chats.append(
                    {
                        "chat_id": chat_id,
                        "name": str(getattr(item, "name", "") or ""),
                        "description": str(getattr(item, "description", "") or ""),
                        "external": bool(getattr(item, "external", False)),
                    }
                )
            if not getattr(data, "has_more", False):
                break
            page_token = str(getattr(data, "page_token", "") or "")
            if not page_token:
                break
        return chats

    async def _feishu_chats(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        feishu = self._feishu_channel_config()
        if not feishu.get("app_id") or not feishu.get("app_secret"):
            return JSONResponse(
                {"error": "还没有配置飞书应用（app_id / app_secret），请先在设置里填写"},
                status_code=400,
            )
        try:
            chats = await asyncio.to_thread(self._list_feishu_chats, feishu)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"chats": chats})

    @staticmethod
    def _send_feishu_text(feishu_cfg: dict[str, Any], chat_id: str, text: str) -> None:
        """One plain text message, sent the same way a run's delivery sends."""
        from agent.channels.feishu import FeishuConfig, build_feishu_client
        from lark_oapi.api.im.v1 import (  # type: ignore[import]
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        client = build_feishu_client(FeishuConfig(**feishu_cfg))
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = client.im.v1.message.create(req)
        if not resp.success():
            raise RuntimeError(
                f"飞书消息发送失败：code={resp.code} msg={resp.msg}"
            )

    async def _feishu_test(self, request: Any) -> Any:
        """A short test message to one chat, before the user trusts the form.

        Whether the app credentials carry the send permission is not knowable
        from the config alone, and finding out at 3am from a failed run is
        the worst answer.  Finding out now, from a button, is the best one.
        """
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid json body"}, status_code=400)
        chat_id = str(body.get("chat_id", "")).strip()
        if not chat_id:
            return JSONResponse({"error": "缺少 chat_id"}, status_code=400)
        feishu = self._feishu_channel_config()
        if not feishu.get("app_id") or not feishu.get("app_secret"):
            return JSONResponse(
                {"error": "还没有配置飞书应用（app_id / app_secret），请先在设置里填写"},
                status_code=400,
            )
        try:
            await asyncio.to_thread(
                self._send_feishu_text,
                feishu,
                chat_id,
                "测试消息：定时任务的飞书投递配置正确，这条由设置页发出。",
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"ok": True})

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

    async def _withdraw_queued_message(self, request: Any) -> Any:
        """Take back one message that is still waiting in a session's queue.

        The text is returned so the client can put it back in the composer
        and send an edited version.  A message that was already folded into
        the running turn is no longer in either queue, so it reports
        ``withdrawn: false`` rather than pretending it was taken back — the
        model has read it.
        """
        from starlette.responses import JSONResponse

        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_id = request.path_params["session_id"]
        message_id = request.path_params["message_id"]
        state = self._sessions.get(session_id)
        if state is None:
            return JSONResponse({"ok": True, "withdrawn": False, "text": ""})
        text = state.withdraw_queued(message_id)
        return JSONResponse(
            {
                "ok": True,
                "withdrawn": text is not None,
                "text": text or "",
            }
        )

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
                    # A retired sink means the queued message was withdrawn:
                    # there is no turn to report, and the idle session we now
                    # see is the *other* turn finishing, not this one.
                    if (
                        not sink.turn_complete_emitted
                        and not sink.retired
                        and final_operation_state == "idle"
                    ):
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
            # Provider-level edits.  Separate from /api/config because the
            # settings page should not have to send a whole config back to
            # change one endpoint, and because "the page sent what it was
            # shown" is how a field nobody looked at gets overwritten.
            Route(
                "/api/providers/{provider_name}",
                self._provider_save,
                methods=["POST"],
            ),
            Route(
                "/api/providers/{provider_name}",
                self._provider_delete,
                methods=["DELETE"],
            ),
            Route(
                "/api/providers/{provider_name}/activate",
                self._provider_activate,
                methods=["POST"],
            ),
            Route(
                "/api/providers/{provider_name}/test",
                self._provider_test,
                methods=["POST"],
            ),
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
            # Before /api/skills/{skill_id:path}: the path converter is greedy,
            # so the toggle has to be offered the path first or a skill named
            # "x/toggle" would swallow it.
            Route(
                "/api/skills/{skill_id:path}/toggle",
                self._toggle_skill,
                methods=["POST"],
            ),
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
            Route("/api/fs/pick-directory", self._pick_directory, methods=["POST"]),
            Route("/api/feishu/chats", self._feishu_chats, methods=["GET"]),
            Route("/api/feishu/test", self._feishu_test, methods=["POST"]),
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
                "/api/sessions/{session_id}/queue/{message_id}",
                self._withdraw_queued_message,
                methods=["DELETE"],
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
