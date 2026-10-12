"""The web socket sink: agent output, serialised for a browser tab.

Why this module exists
----------------------
``WebOutputSink`` is the web channel's whole outbound half: it turns
``OutputSink`` callbacks into the JSON events a browser receives, carries the
approval handshake, and queues attachments.  It lived in
``agent/channels/web.py`` beside the HTTP routing, although it never references
``WebChannel`` -- so it could only be read or exercised by importing the router
that happened to contain it.

The helpers it needs came with it, because it was their only reader:
``_turn_tag`` / ``_operation_tag`` (which turn and which tool call an event
belongs to, omitted rather than sent empty), ``_jsonable`` (progress payloads
are not always JSON-native), ``_svg_has_raster_sibling`` (a skill that writes
both ``X.svg`` and ``X.svg.png`` must not have both attached), and the media
extension sets.

Two names stay shared, and stay single-homed:

- ``CONFIRMATION_TIMEOUT_SECONDS`` is read here and by
  ``WebChannel.wait_for_confirmation``.  ``web.py`` reads it *through this
  module* rather than importing a copy, so the number has one home and a test
  that shortens the clock shortens both readers.
- ``_normalize_confirm_decision`` is used by the sink and by the socket
  handler.

Behaviour is preserved exactly, including one pair that looks duplicated and was
left alone on purpose: ``_turn_tag`` / ``_operation_tag`` each carry a docstring
explaining a *different* failure they prevent.  That is recorded in the survey
rather than tidied inside a move.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from agent.core.output import OutputSink, recorded_text

logger = logging.getLogger(__name__)


# How long a browser is given to answer an approval prompt before the pending
# action is declined.  Sent to the client as ``timeout_seconds`` so the UI can
# count down honestly instead of going quiet and letting the approval expire.
CONFIRMATION_TIMEOUT_SECONDS = 300


# Approval decisions a client may return.  ``allow_session`` widens the consent
# from "this one command" to "this command for the rest of the session" and is
# only offered when the pending record can actually carry that intent.
CONFIRM_DECISIONS = ("allow_once", "allow_session", "deny")


def _normalize_confirm_decision(payload: dict[str, Any]) -> str:
    """Map a client reply onto one of ``CONFIRM_DECISIONS``.

    Accepts both the rich ``{"decision": "allow_session"}`` form and the
    original ``{"approved": true}`` boolean, so an already-open browser tab
    keeps working across a backend upgrade.

    This is the one door client input comes through, so the vocabulary is
    *closed* here.  It matters because of how the answer is read downstream: the
    caller tests for ``deny`` and for ``allow_session`` and treats everything
    else as approval, so a value outside the vocabulary would fail **open**.  A
    reply nobody can classify is a denial.
    """
    raw = str(payload.get("decision") or "").strip().casefold()
    if raw == "allow_session":
        decision = "allow_session"
    elif raw in ("allow_once", "allow", "approve"):
        decision = "allow_once"
    elif raw in ("deny", "reject"):
        decision = "deny"
    else:
        decision = "allow_once" if payload.get("approved") else "deny"
    return decision if decision in CONFIRM_DECISIONS else "deny"


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


def _identity_tag(key: str, value: object) -> dict[str, str]:
    """One-key event tag, *omitted* when the identity is unknown.

    The omission is the rule, not an accident of formatting: a client that sees
    no ``turn_id`` knows it cannot attribute the event, whereas an empty string
    reads as a turn whose id is "".  The two wrappers below keep each wire key
    spelled exactly once, so a call site cannot misspell it into a field the
    client silently ignores.
    """
    clean = str(value or "").strip()
    return {key: clean} if clean else {}


def _turn_tag(turn_id: str) -> dict[str, str]:
    """The turn a socket event belongs to.

    A client that cannot tell which turn produced an event has to assume every
    event belongs to the one it is watching, which is how a reply delivered for
    a withdrawn or restarted message ends up appended to the wrong turn.
    """
    return _identity_tag("turn_id", turn_id)


def _operation_tag(operation_id: str) -> dict[str, str]:
    """The tool call an event belongs to.

    Without it a client can only match a tool's result to its start by name, so
    two concurrent calls to the same tool overwrite each other's rows.
    """
    return _identity_tag("operation_id", operation_id)


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
        turn_id: str = "",
    ) -> None:
        self._websocket = websocket
        # The user message this sink narrates. Carried on every event so the
        # client can keep a late event out of the turn it does not belong to.
        self._turn_id = str(turn_id or "").strip()
        self._collect = collect or websocket is None
        self._events: list[dict[str, Any]] = []
        self._attachments: list[str] = []
        self._queued_attachment_paths: set[str] = set()
        self._turn_complete_emitted = False
        self._retired = False
        self._completion_event = asyncio.Event()
        self.on_attachment = on_attachment
        self._confirmation_handler = confirmation_handler
        # The approval prompt currently awaiting an answer, as
        # ``(payload, deadline)`` with the deadline on loop time. Kept so a
        # reconnecting browser can be re-offered the decision -- the waiter
        # survives the socket the prompt was sent to, but the prompt itself
        # was only ever sent to that socket (see
        # ``pending_confirmation_resend``).
        self._pending_confirmation: Optional[tuple[dict[str, Any], float]] = None
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
        self._emit({"type": "stream_chunk", "chunk": chunk, **_turn_tag(self._turn_id)})

    def on_reasoning_chunk(self, chunk: str) -> None:
        """Stream the model's thinking, kept out of ``full_text``.

        ``full_text`` is the message: it is what the reconnect snapshot
        replays and what a turn is reported to have said.  Thinking is not
        part of it, so it travels on its own event and its own accumulator.
        """
        if not chunk:
            return
        self.reasoning_text += chunk
        self._emit({"type": "reasoning_chunk", "chunk": chunk, **_turn_tag(self._turn_id)})

    def on_command_result(self, text: str, *, level: str = "info") -> None:
        """A command's answer: its own event, so the client can print it."""
        self._emit({"type": "command_result", "text": text, "level": level})

    @property
    def displayed_reasoning(self) -> str:
        """What this sink showed as thinking (``reasoning_text``)."""
        return self.reasoning_text

    @property
    def displayed_text(self) -> str:
        """What this sink showed for the turn: the text it streamed.

        It is what the client has already rendered, what the reconnect snapshot
        replays, and -- through ``OutputSink.displayed_text`` -- what the durable
        record is asked to hold.  The runtime's own text is the turn's *final*
        message, so it is only a fallback (see ``on_turn_complete``).
        """
        return self.full_text

    def on_turn_complete(self, full_text: str, tool_calls: list[str]) -> None:
        # Never shrink.  The frame sent below replaces the client's streaming
        # message, so adopting a shorter text here is exactly how a turn that
        # narrated before calling a tool lost that narration on completion.
        # The runtime's ``full_text`` is the last message of the turn; it is the
        # whole answer only when this sink streamed nothing.
        self.full_text = recorded_text(self.full_text, full_text)
        self._turn_complete_emitted = True
        self._completion_event.set()
        self._emit(
            {
                "type": "turn_complete",
                "text": self.full_text,
                "tool_calls": list(tool_calls),
                **_turn_tag(self._turn_id),
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
        snapshot = {"type": "stream_snapshot", "text": self.full_text, **_turn_tag(self._turn_id)}
        if self.reasoning_text:
            # Carried so a tab switch during a thinking-heavy turn brings the
            # note back with the text, rather than emptying it mid-sentence.
            snapshot["reasoning"] = self.reasoning_text
        return snapshot

    def set_websocket(self, websocket: Any) -> None:
        """Route subsequent events to the currently selected browser tab."""
        self._websocket = websocket

    def pending_confirmation_resend(self) -> dict[str, Any] | None:
        """The pending approval prompt, re-addressed to a newly connected tab.

        The waiter survives the socket the prompt was sent to, but the prompt
        itself does not: a browser that switched tabs (or reloaded) mid-
        approval would otherwise render a session that looks idle while the
        decision ran out its clock and failed closed. The re-offer carries
        the seconds that actually remain, so the receiving countdown stays
        honest about the server's own deadline.
        """
        if self._pending_confirmation is None:
            return None
        prompt, deadline = self._pending_confirmation
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return None
        resent = dict(prompt)
        # Truncated, never rounded up: the client treats a non-positive
        # timeout_seconds as "server sent nothing" and falls back to a full
        # window, which would be a lie. Losing the last sub-second is the
        # safe direction -- the bar converges a moment before the server does.
        resent["timeout_seconds"] = max(1, int(remaining))
        return resent

    def on_tool_start(
        self,
        name: str,
        inputs: dict,
        *,
        operation_id: str = "",
    ) -> None:
        self._emit(
            {
                "type": "tool_start",
                "name": name,
                "inputs": dict(inputs),
                **_operation_tag(operation_id),
                **_turn_tag(self._turn_id),
            }
        )

    def on_tool_end(
        self,
        name: str,
        result: str,
        *,
        operation_id: str = "",
    ) -> None:
        self._emit(
            {
                "type": "tool_end",
                "name": name,
                "result": result,
                **_operation_tag(operation_id),
                **_turn_tag(self._turn_id),
            }
        )

    def on_tool_progress(self, name: str, progress: Any) -> None:
        payload = progress if isinstance(progress, dict) else {}
        self._emit(
            {
                "type": "tool_progress",
                "name": name,
                "progress": _jsonable(progress),
                **_operation_tag(str(payload.get("operation_id") or "")),
                **_turn_tag(self._turn_id),
            }
        )

    def on_tool_blocked(
        self,
        name: str,
        reason: str,
        *,
        operation_id: str = "",
    ) -> None:
        self._emit(
            {
                "type": "tool_blocked",
                "name": name,
                "reason": reason,
                **_operation_tag(operation_id),
                **_turn_tag(self._turn_id),
            }
        )

    def on_status(self, text: str, *, level: str = "info") -> None:
        self._emit({"type": "status", "text": text, "level": level})

    def on_message_queued(
        self,
        text: str,
        *,
        kind: str,
        message_id: str = "",
        urgency: str = "normal",
    ) -> None:
        """A message accepted mid-turn, as data rather than as a sentence.

        Deliberately not an ``on_status`` line.  This client shows the message
        in its queue strip and then renders it in the turn once the record has
        it, so what it needs is the kind and the id -- not prose to parse, and
        not a transcript row for a transient acknowledgement.  The English
        fallback stays on the base sink, for the channels that only print.
        """
        self._emit(
            {
                "type": "message_queued",
                "text": text,
                "kind": kind,
                "message_id": message_id,
                "urgency": urgency,
            }
        )

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
        """Own the attachment only when the session already stores it.

        The web channel hands the browser a URL, so the file has to outlive the
        turn -- which is why this used to answer ``True`` for everything.  But a
        sink that says "I own this" has to have a plan for the file, and this
        sink's only plan is session storage: the file belongs inside the
        session's own output directory, which is deleted with the session
        (``SessionService.delete_session_async`` removes the validated home) and
        can be purged with ``clean_output``.

        Anything else is a file this sink has no way to clean up, so the honest
        answer is ``False`` and the coordinator's cleanup runs.  The warning is
        the point: declining is a real outcome, not a silent drop, because the
        browser has already been given the URL.
        """
        if not self._output_dir:
            return False
        try:
            output_dir = (
                Path(str(self._output_dir)).expanduser().resolve(strict=False)
            )
            path = Path(str(receipt or "")).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return False
        if path == output_dir or output_dir in path.parents:
            return True
        logger.warning(
            "web sink cannot own a temporary attachment outside the session's "
            "output directory: %s",
            receipt,
        )
        return False

    @property
    def interactive_confirmation(self) -> bool:
        """A connected browser is a person who can be asked, right now.

        The base sink answers ``False`` -- "no dialog here, leave a pending
        record and ask by chat reply instead" -- and that answer is what the
        consent callers branch on.  Reporting it here was wrong in both
        directions:

        * ``memory_clear`` refuses outright unless a person can be asked, so
          clearing memory from the browser failed with "requires interactive
          human approval" while the bar it needs was one call away;
        * ``install_plugin`` and user-tool activation skipped the prompt and
          left a pending record for a later 「同意」 reply;
        * and the coordinator's phrase matching runs *only* where no menu
          exists, because a bare "yes" is ambiguous and the weakest consent
          channel must not become the operative one.  With this sink claiming
          no menu, a message that normalizes to ``ok`` / ``yes`` / ``y`` could
          redeem the sole pending high-risk shell command without the card
          being read at all.

        A tab that is not connected is still handled the other way:
        ``on_tool_confirmation`` answers ``None`` -- "nobody could be asked" --
        which is not a refusal, so the pending record survives for the
        reconnect, when the prompt is re-offered, or for a later ``/confirm``.
        """
        return not self._collect

    async def on_tool_confirmation(
        self,
        name: str,
        *,
        command: str,
        risk_level: str,
        reason: str,
        confirmation_token: str,
        scope: Any,
    ) -> Optional[bool]:
        """Ask a connected web client for approval; ``None`` when there is no client.

        ``None`` rather than ``False`` on the no-socket path: a browser that is not connected
        is a human who could not be asked, and the pending record has to survive so the
        answer can arrive later -- as a ``/confirm <token>`` in the conversation, or on the
        next socket.  A client that *was* asked and declined still gets ``False``.

        The prompt is advisory to the browser only: whatever the human picks
        still has to be redeemed against the pending record by the caller, so a
        forged ``allow_session`` reply cannot widen consent for a token the
        server never minted.
        """
        if self._collect or self._websocket is None:
            return None
        allow_session = self._session_scope_supported(
            name, confirmation_token, scope
        )
        handler = self._confirmation_handler
        # Claim the answer slot *before* the prompt goes out.  Registering it
        # afterwards left a window in which a fast client's reply reached the
        # socket reader while nothing was waiting on the token yet; the reply
        # was dropped on the floor and the prompt then sat until it timed out.
        pending = handler(confirmation_token) if callable(handler) else None
        prompt = {
            "type": "confirm_request",
            "name": name,
            "command": command,
            "risk_level": risk_level,
            "reason": reason,
            "confirmation_token": confirmation_token,
            "allow_session": allow_session,
            "timeout_seconds": CONFIRMATION_TIMEOUT_SECONDS,
        }
        # Remember the prompt while it is out. A browser that reconnects
        # mid-approval must be re-offered the decision, with the seconds
        # that actually remain, on the socket it now reads -- otherwise the
        # new tab shows a session that looks idle while the decision runs
        # out its clock. Cleared when the answer (or the timeout) lands.
        self._pending_confirmation = (
            prompt,
            asyncio.get_running_loop().time() + CONFIRMATION_TIMEOUT_SECONDS,
        )
        self._emit(prompt)
        await self.flush()
        try:
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
        finally:
            self._pending_confirmation = None

    @staticmethod
    def _session_scope_supported(
        name: str, confirmation_token: str, scope: Any
    ) -> bool:
        """Whether "always allow this command" is meaningful for this prompt.

        Only shell approvals carry a redeemable pending record plus an
        authorization scope, which is exactly the pair a session-wide entry is
        keyed by.  Plugin installs and memory clears are one-shot by design and
        must never advertise the option.  The answer lives in
        :func:`agent.security.shell.shell_session_scope_supported` because the
        terminal asks the same question and the two must not diverge.
        """
        from agent.security.shell import shell_session_scope_supported

        return shell_session_scope_supported(name, confirmation_token, scope)

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
