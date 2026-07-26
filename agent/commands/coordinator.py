from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
from pathlib import Path
import time
from typing import Any, Callable, Mapping, NotRequired, TypedDict

from agent.core.output import (
    OutputSink,
    RuntimeEvent,
    _active_event_collector,
)
from agent.runtime.contracts import AgentCore, RuntimeSessionState, TurnInput
from agent.shared import CancelToken

from .models import CommandAction, CommandContext, CommandResult
from .router import CommandClassification, CommandRouter

logger = logging.getLogger(__name__)

_BUSY_MESSAGE = "Session is busy; wait for the current operation to finish."
_FAILED_MESSAGE = "Command handling failed."


class _QueueEntry(TypedDict):
    text: str
    from_user: str
    arrived_at: float
    urgency: str
    turn_input: TurnInput
    sink: OutputSink
    ready: NotRequired[asyncio.Event]


class CommandCoordinator:
    """Coordinate one session operation without owning session storage."""

    def __init__(
        self,
        agent_core: AgentCore,
        router: CommandRouter,
        *,
        components: Mapping[str, Any] | None = None,
        config: Mapping[str, Any] | None = None,
        cancel_token_factory: Callable[[], Any] | None = None,
        event_hook: Callable[[RuntimeEvent], None] | None = None,
    ) -> None:
        self._agent_core = agent_core
        self._router = router
        self._components = components if components is not None else {}
        self._config = config if config is not None else {}
        self._cancel_token_factory = cancel_token_factory or CancelToken
        self._event_hook = event_hook

    async def handle(
        self,
        turn_input: TurnInput,
        state: RuntimeSessionState,
        sink: OutputSink,
    ) -> CommandAction | None:
        sink_ready = asyncio.Event()
        action: CommandAction | None = None
        try:
            classification = self._router.classify(
                turn_input.text,
                channel_name=turn_input.channel_name,
                session_id=turn_input.session_id,
                metadata=turn_input.metadata,
            )
            action = await self._route(
                classification,
                turn_input,
                state,
                sink,
                sink_ready,
            )
        except Exception:
            logger.exception(
                "command coordination failed: session_id=%s channel=%s",
                turn_input.session_id,
                turn_input.channel_name,
            )
            self._emit("command_failed", turn_input, outcome="internal_error")
            self._safe_error(sink, _FAILED_MESSAGE)
        finally:
            try:
                await self._drain_if_supported(sink)
            finally:
                sink_ready.set()
        await self._run_restarts_if_idle(state, sink)
        return action

    async def _route(
        self,
        classification: CommandClassification,
        turn_input: TurnInput,
        state: RuntimeSessionState,
        sink: OutputSink,
        sink_ready: asyncio.Event,
    ) -> CommandAction | None:
        if classification.kind == "unknown_slash":
            self._emit_received(classification, turn_input)
            result = await self._router.execute(
                classification,
                self._command_context(turn_input, state, sink),
            )
            await self._render_result(result, sink)
            self._emit(
                "command_rejected",
                turn_input,
                command=self._command_name(classification),
                reason="unknown",
            )
            return result.action

        if classification.kind == "skill":
            self._emit_received(classification, turn_input)
            if state.operation_state != "idle":
                await self._reject_busy(classification, turn_input, sink)
                return None
            return await self._run_operation(
                turn_input,
                state,
                sink,
                classification=None,
                forward_text=classification.text,
                accepts_interjections=True,
                forward_target="skill",
                forward_command=classification.skill_id or "skill",
            )

        if classification.kind == "text":
            if state.operation_state == "cancelling":
                self._queue_restart(turn_input, sink, state, ready=sink_ready)
                self._safe_status(
                    sink, "Message queued for the next turn.", level="info"
                )
                return None
            if state.operation_state == "active":
                if state.accepts_interjections:
                    self._queue_interjection(turn_input, sink, state, ready=sink_ready)
                    self._safe_status(sink, "Interjection queued.", level="info")
                else:
                    self._queue_restart(turn_input, sink, state, ready=sink_ready)
                    self._safe_status(
                        sink, "Message queued for the next turn.", level="info"
                    )
                return None
            return await self._run_operation(
                turn_input,
                state,
                sink,
                classification=None,
                forward_text=classification.text,
                accepts_interjections=True,
            )

        self._emit_received(classification, turn_input)
        descriptor = classification.descriptor
        if descriptor is None:
            raise RuntimeError("classified command has no descriptor")
        if descriptor.name == "cancel":
            return await self._handle_cancel(
                classification, turn_input, state, sink, sink_ready
            )
        if descriptor.name == "now":
            return await self._handle_now(
                classification, turn_input, state, sink, sink_ready
            )

        if state.operation_state != "idle":
            if descriptor.concurrency in ("anytime", "interrupt"):
                return await self._execute_command(
                    classification,
                    turn_input,
                    state,
                    sink,
                    queue_forward=True,
                    forward_ready=sink_ready,
                )
            await self._reject_busy(classification, turn_input, sink)
            return None

        return await self._run_operation(
            turn_input,
            state,
            sink,
            classification=classification,
            forward_text=None,
            accepts_interjections=descriptor.accepts_interjections,
        )

    async def _run_operation(
        self,
        turn_input: TurnInput,
        state: RuntimeSessionState,
        sink: OutputSink,
        *,
        classification: CommandClassification | None,
        forward_text: str | None,
        accepts_interjections: bool,
        forward_target: str = "model",
        forward_command: str = "",
        initial_ready: asyncio.Event | None = None,
    ) -> CommandAction | None:
        current_input = turn_input
        current_sink = sink
        current_classification = classification
        current_forward = forward_text
        current_accepts = accepts_interjections
        current_forward_target = forward_target
        current_forward_command = forward_command
        current_ready = initial_ready
        first_action: CommandAction | None = None
        first = True

        while True:
            token = self._cancel_token_factory()
            state.cancel_token = token
            state.accepts_interjections = current_accepts
            state.operation_state = "active"
            try:
                if current_ready is not None:
                    await current_ready.wait()
                if current_classification is not None:
                    action = await self._execute_command(
                        current_classification,
                        current_input,
                        state,
                        current_sink,
                    )
                else:
                    self._emit(
                        "command_forwarded",
                        current_input,
                        command=current_forward_command,
                        target=current_forward_target,
                    )
                    forwarded_input = replace(
                        current_input,
                        text=current_forward
                        if current_forward is not None
                        else current_input.text,
                    )
                    await self._agent_core.handle_turn(
                        forwarded_input,
                        state,
                        sink=current_sink,
                    )
                    action = None
            except Exception:
                logger.exception(
                    "operation dispatch failed: session_id=%s channel=%s",
                    current_input.session_id,
                    current_input.channel_name,
                )
                self._emit(
                    "command_failed",
                    current_input,
                    command=self._command_name(current_classification),
                    outcome="internal_error",
                )
                self._safe_error(current_sink, _FAILED_MESSAGE)
                action = None
            finally:
                try:
                    # Keep the operation non-idle across this await, then
                    # re-read cancellation before deciding mailbox ownership.
                    await self._drain_if_supported(current_sink)
                    cancelled = state.operation_state == "cancelling" or bool(
                        getattr(token, "is_cancelled", False)
                    )
                    if cancelled:
                        unapplied = len(state.pending_interjections)
                        state.pending_interjections.clear()
                        if unapplied:
                            self._safe_status(
                                current_sink,
                                f"{unapplied} interjection(s) were unapplied because the operation was cancelled.",
                                level="warning",
                            )
                            await self._drain_if_supported(current_sink)
                    elif state.pending_interjections:
                        late = list(state.pending_interjections)
                        state.pending_interjections.clear()
                        state.restart_queue[0:0] = late
                finally:
                    state.operation_state = "idle"
                    state.accepts_interjections = False
                    state.cancel_token = None

            if first:
                first_action = action
                first = False
            if not state.restart_queue:
                return first_action

            queued = state.restart_queue.pop(0)
            current_input = self._queued_turn_input(queued)
            current_sink = queued.get("sink") or sink
            current_classification = None
            current_forward = queued["text"]
            current_accepts = True
            current_forward_target = "model"
            current_forward_command = ""
            current_ready = queued.get("ready")

    async def _execute_command(
        self,
        classification: CommandClassification,
        turn_input: TurnInput,
        state: RuntimeSessionState,
        sink: OutputSink,
        *,
        queue_forward: bool = False,
        forward_ready: asyncio.Event | None = None,
    ) -> CommandAction | None:
        started_at = time.perf_counter()
        result = await self._router.execute(
            classification,
            self._command_context(turn_input, state, sink),
        )
        await self._render_result(result, sink)
        command = self._command_name(classification)
        duration_ms = round((time.perf_counter() - started_at) * 1000, 1)
        if result.error:
            self._emit(
                "command_failed",
                turn_input,
                command=command,
                duration_ms=duration_ms,
                outcome="failed",
            )
        else:
            self._emit(
                "command_handled",
                turn_input,
                command=command,
                duration_ms=duration_ms,
                outcome="handled",
            )

        if result.forward_text is not None:
            self._emit(
                "command_forwarded",
                turn_input,
                command=command,
                target="model",
            )
            forwarded_input = replace(turn_input, text=result.forward_text)
            if queue_forward:
                self._queue_restart(
                    forwarded_input,
                    sink,
                    state,
                    ready=forward_ready,
                )
            else:
                await self._agent_core.handle_turn(
                    forwarded_input,
                    state,
                    sink=sink,
                )
        return result.action

    async def _run_restarts_if_idle(
        self,
        state: RuntimeSessionState,
        fallback_sink: OutputSink,
    ) -> None:
        if state.operation_state != "idle" or not state.restart_queue:
            return
        queued = state.restart_queue.pop(0)
        turn_input = self._queued_turn_input(queued)
        await self._run_operation(
            turn_input,
            state,
            queued.get("sink") or fallback_sink,
            classification=None,
            forward_text=queued["text"],
            accepts_interjections=True,
            initial_ready=queued.get("ready"),
        )

    async def _handle_now(
        self,
        classification: CommandClassification,
        turn_input: TurnInput,
        state: RuntimeSessionState,
        sink: OutputSink,
        sink_ready: asyncio.Event,
    ) -> CommandAction | None:
        request = classification.request
        payload = request.args if request is not None else ""
        if not payload:
            self._safe_status(sink, "/now requires a message.", level="error")
            self._emit(
                "command_rejected",
                turn_input,
                command="now",
                reason="missing_payload",
            )
            return None

        payload_input = replace(turn_input, text=payload)
        if state.operation_state == "idle":
            action = await self._run_operation(
                payload_input,
                state,
                sink,
                classification=None,
                forward_text=payload,
                accepts_interjections=True,
                forward_command="now",
            )
            self._emit(
                "command_handled",
                turn_input,
                command="now",
                outcome="forwarded",
            )
            return action
        if state.operation_state == "active" and state.accepts_interjections:
            self._queue_interjection(
                payload_input,
                sink,
                state,
                urgency="now",
                ready=sink_ready,
            )
            self._safe_status(sink, "Urgent interjection queued.", level="info")
        else:
            self._queue_restart(
                payload_input,
                sink,
                state,
                urgency="now",
                ready=sink_ready,
            )
            self._safe_status(sink, "Message queued for the next turn.", level="info")
        self._emit(
            "command_handled",
            turn_input,
            command="now",
            outcome="queued",
        )
        return None

    async def _handle_cancel(
        self,
        classification: CommandClassification,
        turn_input: TurnInput,
        state: RuntimeSessionState,
        sink: OutputSink,
        sink_ready: asyncio.Event,
    ) -> CommandAction | None:
        request = classification.request
        args = request.args if request is not None else ""
        mode = args.casefold()
        graceful = mode == "graceful"
        explicit_force = mode == "force"
        payload = "" if graceful or explicit_force else args

        if state.operation_state == "idle":
            if payload:
                action = await self._run_operation(
                    replace(turn_input, text=payload),
                    state,
                    sink,
                    classification=None,
                    forward_text=payload,
                    accepts_interjections=True,
                    forward_command="cancel",
                )
                self._emit(
                    "command_handled",
                    turn_input,
                    command="cancel",
                    outcome="forwarded",
                )
                return action
            self._safe_status(sink, "No active operation to cancel.", level="info")
            self._emit(
                "command_handled",
                turn_input,
                command="cancel",
                outcome="no_op",
            )
            return None

        state.operation_state = "cancelling"
        token = state.cancel_token
        if token is not None:
            token.cancel("graceful" if graceful else "force")
        if payload:
            replacement = self._queue_entry(
                replace(turn_input, text=payload),
                sink,
                urgency="now",
                ready=sink_ready,
            )
            state.restart_queue[:] = [replacement]
            self._safe_status(
                sink,
                "Cancellation requested; replacement task queued.",
                level="warning",
            )
        else:
            level = "graceful" if graceful else "force"
            self._safe_status(
                sink,
                f"{level.capitalize()} cancellation requested.",
                level="warning",
            )
        self._emit(
            "command_handled",
            turn_input,
            command="cancel",
            outcome="cancelling",
            level="graceful" if graceful else "force",
        )
        return None

    async def _reject_busy(
        self,
        classification: CommandClassification,
        turn_input: TurnInput,
        sink: OutputSink,
    ) -> None:
        self._safe_status(sink, _BUSY_MESSAGE, level="error")
        self._emit(
            "command_rejected",
            turn_input,
            command=self._command_name(classification),
            reason="busy",
        )

    async def _render_result(self, result: CommandResult, sink: OutputSink) -> None:
        queued_attachments: list[tuple[Any, Any]] = []
        try:
            if result.response_text is not None:
                self._safe_status(sink, result.response_text, level=result.level)
            elif result.error:
                self._safe_error(sink, result.error)
            for attachment in result.attachments:
                queued_attachments.append(
                    (attachment, self._safe_attachment(sink, attachment))
                )
            if result.attachments:
                try:
                    await self._flush_attachments_if_supported(sink)
                finally:
                    await self._drain_if_supported(sink)
        finally:
            self._cleanup_temporary_attachments(
                result.temporary_attachments,
                sink,
                tuple(queued_attachments),
            )

    @staticmethod
    def _cleanup_temporary_attachments(
        attachments: tuple[Any, ...],
        sink: OutputSink,
        queued_attachments: tuple[tuple[Any, Any], ...],
    ) -> None:
        defer_cleanup = getattr(sink, "defer_temporary_attachment_cleanup", None)
        for attachment in attachments:
            receipt = next(
                (
                    queued_receipt
                    for queued_attachment, queued_receipt in queued_attachments
                    if queued_attachment is attachment
                    or queued_attachment == attachment
                ),
                None,
            )
            if callable(defer_cleanup):
                try:
                    if defer_cleanup(receipt):
                        continue
                except Exception:
                    logger.exception(
                        "output sink temporary attachment ownership failed: "
                        "attachment=%s",
                        attachment,
                    )
            try:
                path = Path(attachment)
                path.unlink(missing_ok=True)
                path.parent.rmdir()
            except OSError:
                logger.exception(
                    "temporary command attachment cleanup failed: attachment=%s",
                    attachment,
                )

    def _command_context(
        self,
        turn_input: TurnInput,
        state: RuntimeSessionState,
        sink: OutputSink,
    ) -> CommandContext:
        return CommandContext(
            components=self._components,
            config=self._config,
            session_state=state,
            sink=sink,
            channel_name=turn_input.channel_name,
            session_id=turn_input.session_id,
            message_id=str(turn_input.metadata.get("message_id", "")),
            metadata=turn_input.metadata,
        )

    def _emit_received(
        self,
        classification: CommandClassification,
        turn_input: TurnInput,
    ) -> None:
        self._emit(
            "command_received",
            turn_input,
            command=self._command_name(classification),
        )

    def _emit(self, name: str, turn_input: TurnInput, **fields: object) -> None:
        collector = _active_event_collector.get()
        if collector is not None:
            collector.emit(name, **fields)
        if self._event_hook is not None:
            try:
                self._event_hook(
                    RuntimeEvent(
                        name=name,
                        session_id=turn_input.session_id,
                        channel_name=turn_input.channel_name,
                        fields=fields,
                        metadata=turn_input.metadata,
                    )
                )
            except Exception:
                logger.exception("command event hook failed: event=%s", name)

    @staticmethod
    def _command_name(
        classification: CommandClassification | None,
    ) -> str:
        if classification is None:
            return ""
        if classification.descriptor is not None:
            return classification.descriptor.name
        if classification.request is not None:
            return classification.request.name
        return classification.skill_id or ""

    @classmethod
    def _queue_entry(
        cls,
        turn_input: TurnInput,
        sink: OutputSink,
        *,
        urgency: str = "normal",
        ready: asyncio.Event | None = None,
    ) -> _QueueEntry:
        entry: _QueueEntry = {
            "text": turn_input.text,
            "from_user": str(
                turn_input.metadata.get("user_id")
                or turn_input.metadata.get("sender")
                or ""
            ),
            "arrived_at": time.time(),
            "urgency": urgency,
            "turn_input": turn_input,
            "sink": sink,
        }
        if ready is not None:
            entry["ready"] = ready
        return entry

    @classmethod
    def _queue_interjection(
        cls,
        turn_input: TurnInput,
        sink: OutputSink,
        state: RuntimeSessionState,
        *,
        urgency: str = "normal",
        ready: asyncio.Event | None = None,
    ) -> None:
        state.pending_interjections.append(
            cls._queue_entry(turn_input, sink, urgency=urgency, ready=ready)
        )

    @classmethod
    def _queue_restart(
        cls,
        turn_input: TurnInput,
        sink: OutputSink,
        state: RuntimeSessionState,
        *,
        urgency: str = "normal",
        ready: asyncio.Event | None = None,
    ) -> None:
        state.restart_queue.append(
            cls._queue_entry(turn_input, sink, urgency=urgency, ready=ready)
        )

    @staticmethod
    def _queued_turn_input(entry: Mapping[str, Any]) -> TurnInput:
        turn_input = entry.get("turn_input")
        if isinstance(turn_input, TurnInput):
            return replace(turn_input, text=str(entry["text"]))
        return TurnInput.from_text(str(entry["text"]))

    @staticmethod
    def _safe_status(sink: Any, text: str, *, level: str) -> None:
        try:
            sink.on_status(text, level=level)
        except Exception:
            logger.exception("command output sink status failed")

    @staticmethod
    def _safe_error(sink: Any, error: str) -> None:
        try:
            sink.on_error(error)
        except Exception:
            logger.exception("command output sink error reporting failed")

    @staticmethod
    def _safe_attachment(sink: Any, attachment: Any) -> Any:
        try:
            return sink.queue_attachment(attachment)
        except Exception:
            logger.exception("command output sink attachment failed")
            return None

    @staticmethod
    async def _drain_if_supported(sink: Any) -> None:
        drain = getattr(sink, "drain", None)
        if not callable(drain):
            return
        try:
            result = drain()
            if hasattr(result, "__await__"):
                await result
        except Exception:
            logger.exception("command output sink drain failed")

    @staticmethod
    async def _flush_attachments_if_supported(sink: Any) -> None:
        flush = getattr(sink, "flush_attachments", None)
        if not callable(flush):
            return
        result = flush()
        if hasattr(result, "__await__"):
            await result
