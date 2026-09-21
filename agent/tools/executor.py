from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import time
from typing import Any
import uuid

from agent import shared
from agent.core.output import (
    _active_assistant_text,
    _active_event_collector,
    _active_sink,
    _consent_pending,
    _fmt_tool_inputs,
)
from agent.plugins.catalog import PostToolEvent, PreToolEvent

# Tools opt into the intent-before-action protocol by declaring the
# ``requires_intent`` capability.  Today only ``shell`` does (its command
# string is opaque); other write tools self-declare via structured params.
_INTENT_REQUIRED_CAPABILITY = "requires_intent"

#: The stronger one, for tools whose result outlives the conversation.
#:
#: ``requires_intent`` asks the caller to *say* why, and a caller can always
#: say something.  This asks it to *quote*, and the quote is checked against
#: the text that commissioned the turn, so an invented reason fails and the
#: asker's own words pass.  It is here rather than in the tool because the
#: thing being decided is "was this asked for", which no single tool can see.
#:
#: This exists because the alternative was tried and cannot work.  A keyword
#: gate over the user's sentence was supposed to keep a task from being created
#: unasked; it only decides *which schemas are sent*, while calls are dispatched
#: by name against the whole registry and the system prompt lists every tool by
#: name and description.  So a tool the gate hid was still a tool the model
#: could call, and the gate was a token filter wearing a guard's uniform.
_REQUEST_REQUIRED_CAPABILITY = "requires_request"

#: Why the quote is not also listed in the tools' JSON ``required``: a schema
#: rejection says "missing required property intent", which is true and useless
#: -- the caller learns nothing about what to put there, and the check would
#: then live in two places that disagree the moment either changes.  The field
#: is described in the schema so it is filled in; the refusal happens here, in
#: one place, in a sentence that says what to quote and what to do instead.

#: How much of the request a quote has to reproduce to count as evidence of it
#: rather than a summary of it.  Six characters of Chinese is already a clause;
#: an English quote reaches the same bar in fewer words.  This is the ceiling,
#: not the rule -- see :meth:`RegularToolExecutor._quote_bar`.
_QUOTE_MIN_CHARS = 6

_active_tool_progress: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "active_tool_progress",
    default=None,
)


def report_tool_progress(
    *,
    status: str = "running",
    message: str = "",
    current: int | float | None = None,
    total: int | float | None = None,
    **fields: Any,
) -> None:
    """Emit progress for the current tool operation when one is active."""
    reporter = _active_tool_progress.get()
    if callable(reporter):
        reporter(
            status=status,
            message=message,
            current=current,
            total=total,
            **fields,
        )


class RegularToolExecutor:
    """Executes non-orchestration tool calls behind one side-effect boundary.

    Emits ``tool_started``, ``tool_progress``, ``tool_completed``,
    ``tool_failed``, ``tool_timed_out``, and ``tool_blocked`` ``RuntimeEvent``
    facts into the active ``EventCollector`` (when one is set by AgentCore).
    """

    _HEARTBEAT_INTERVAL_SECONDS = 10.0

    def __init__(
        self,
        registry: Any,
        *,
        plugin_catalog: Any = None,
        timeout_seconds: float | None = None,
        stale_timeout_seconds: float | None = None,
    ) -> None:
        self._registry = registry
        self._plugin_catalog = plugin_catalog
        self._timeout_seconds = (
            shared.REGULAR_TOOL_TIMEOUT
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds))
        )
        self._stale_timeout_seconds = (
            min(30.0, max(1.0, self._timeout_seconds / 4))
            if stale_timeout_seconds is None
            else max(0.0, float(stale_timeout_seconds))
        )

    @staticmethod
    def _emit(event_name: str, **fields: Any) -> None:
        collector = _active_event_collector.get()
        if collector is not None:
            collector.emit(event_name, **fields)

    @staticmethod
    def _operation_id() -> str:
        return "tool_" + uuid.uuid4().hex[:12]

    @staticmethod
    def _notify_sink_progress(sink: Any, tool_name: str, fields: dict[str, Any]) -> None:
        on_progress = getattr(sink, "on_tool_progress", None)
        if callable(on_progress):
            on_progress(tool_name, fields)

    def _progress_reporter(
        self,
        *,
        operation_id: str,
        tool_name: str,
        started_at: float,
        progress_state: dict[str, Any],
        sink: Any,
    ) -> Any:
        def _report(**fields: Any) -> None:
            now = time.monotonic()
            progress_state["last_progress_at"] = now
            progress_state["explicit_progress_count"] += 1
            elapsed = now - started_at
            event_fields = {
                "operation_id": operation_id,
                "tool_name": tool_name,
                "elapsed_ms": round(elapsed * 1000, 1),
                **{k: v for k, v in fields.items() if v is not None},
            }
            self._emit("tool_progress", **event_fields)
            self._notify_sink_progress(sink, tool_name, event_fields)

        return _report

    @staticmethod
    def _tool_timeout(inputs: dict) -> float | None:
        """Extract a tool-declared timeout from its input dict."""
        value = inputs.get("timeout")
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
        return None

    async def _await_tool_result(
        self,
        *,
        operation_id: str,
        tool_name: str,
        inputs: dict,
        started_at: float,
        progress_state: dict[str, Any],
        effective_timeout: float | None = None,
    ) -> str:
        timeout = (
            effective_timeout
            if effective_timeout is not None
            else self._timeout_seconds
        )
        call_task = asyncio.create_task(self._registry.call(tool_name, inputs))
        deadline = started_at + timeout
        next_heartbeat_at = started_at + self._HEARTBEAT_INTERVAL_SECONDS

        while True:
            now = time.monotonic()
            remaining = max(0.0, deadline - now)
            until_heartbeat = max(0.0, next_heartbeat_at - now)
            # Wake whichever comes first: the deadline or the next heartbeat.
            # No extra task — fast tools (< heartbeat interval) finish on the
            # first iteration and never pay for an unused heartbeat coroutine.
            wait_seconds = remaining if remaining < until_heartbeat else until_heartbeat
            done, _pending = await asyncio.wait({call_task}, timeout=wait_seconds)
            if done:
                return await call_task

            now = time.monotonic()
            # Heartbeat tick fired before the deadline — emit and continue waiting.
            if now < deadline:
                elapsed = now - started_at
                self._emit(
                    "tool_progress",
                    operation_id=operation_id,
                    tool_name=tool_name,
                    status="running",
                    elapsed_ms=round(elapsed * 1000, 1),
                    stale_after_seconds=self._stale_timeout_seconds,
                )
                self._notify_sink_progress(
                    _active_sink.get(),
                    tool_name,
                    {
                        "operation_id": operation_id,
                        "tool_name": tool_name,
                        "status": "running",
                        "elapsed_ms": round(elapsed * 1000, 1),
                        "stale_after_seconds": self._stale_timeout_seconds,
                    },
                )
                next_heartbeat_at = now + self._HEARTBEAT_INTERVAL_SECONDS
                continue

            # Deadline crossed.  Honour explicit-progress extensions.
            last_progress_at = float(progress_state["last_progress_at"])
            stale_for = now - last_progress_at
            explicit_progress_count = int(progress_state["explicit_progress_count"])
            if (
                explicit_progress_count > 0
                and self._stale_timeout_seconds > 0
                and stale_for <= self._stale_timeout_seconds
            ):
                deadline = now + self._stale_timeout_seconds
                next_heartbeat_at = now + self._HEARTBEAT_INTERVAL_SECONDS
                self._emit(
                    "tool_progress",
                    operation_id=operation_id,
                    tool_name=tool_name,
                    status="timeout_extended",
                    elapsed_ms=round((now - started_at) * 1000, 1),
                    stale_for_ms=round(stale_for * 1000, 1),
                    next_timeout_seconds=round(self._stale_timeout_seconds, 3),
                )
                self._notify_sink_progress(
                    _active_sink.get(),
                    tool_name,
                    {
                        "operation_id": operation_id,
                        "tool_name": tool_name,
                        "status": "timeout_extended",
                        "elapsed_ms": round((now - started_at) * 1000, 1),
                        "stale_for_ms": round(stale_for * 1000, 1),
                        "next_timeout_seconds": round(self._stale_timeout_seconds, 3),
                    },
                )
                continue

            if _consent_pending():
                # A medium-risk shell command is waiting for the human at
                # the consent menu; keep waiting instead of timing out and
                # silently dropping the consent request.
                deadline = now + self._stale_timeout_seconds
                continue

            call_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await call_task
            # Race guard: the task may have completed between the wait()
            # timeout and the cancel() above.  If so, return the real
            # result instead of fabricating a TimeoutError.
            if call_task.done() and not call_task.cancelled():
                exc = call_task.exception()
                if exc is None:
                    return call_task.result()
            raise asyncio.TimeoutError

    @staticmethod
    def _intent_text_is_specific(intent: str) -> bool:
        text = str(intent or "").strip()
        if not text:
            return False
        compact = "".join(ch for ch in text.lower() if ch.isalnum())
        vague = {
            "run",
            "runshell",
            "execute",
            "executecommand",
            "check",
            "doit",
            "执行",
            "运行",
            "命令",
            "执行命令",
            "运行命令",
            "检查",
            "查看",
            "处理",
        }
        if compact in vague:
            return False
        cjk_count = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        if cjk_count >= 4:
            return True
        return len(text) >= 12

    @staticmethod
    def _structured_intent(inputs: dict) -> str:
        for key in ("intent", "purpose", "reason"):
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _check_intent(self, tool_name: str, inputs: dict) -> str:
        """Return an error message if this call has not earned the right to run.

        Two capabilities are checked here, and which one a tool declares is the
        statement of what it costs to be wrong about it:

        * ``requires_intent`` -- the caller must say what the call will do and
          why, specifically.  A self-declaration, so it catches a call nobody
          thought through, not a call nobody asked for.
        * ``requires_request`` -- the caller must *quote* the words that asked
          for it.  A refusal here means nobody asked, which is the moment to
          propose the change in the reply instead of making it.
        """
        caps = self._registry.tool_capabilities(tool_name) if hasattr(
            self._registry, "tool_capabilities"
        ) else frozenset()
        if _REQUEST_REQUIRED_CAPABILITY in caps:
            return self._check_request(tool_name, inputs)
        if _INTENT_REQUIRED_CAPABILITY not in caps:
            return ""
        structured_intent = self._structured_intent(inputs)
        label = tool_name[:1].upper() + tool_name[1:] if tool_name else "Tool"
        if not structured_intent:
            return (
                f"{label} intent required: include input.intent explaining "
                "what this exact call will do and why it is necessary."
            )
        if not self._intent_text_is_specific(structured_intent):
            return (
                f"{label} intent too vague: input.intent must describe the "
                "specific purpose and expected outcome."
            )
        return ""

    def _request_text(self) -> str:
        """The words that commissioned this turn, published by the agent."""
        getter = getattr(self._registry, "get_context", None)
        if getter is None:
            return ""
        return str(getter("turn_request") or "")

    @staticmethod
    def _quote_bar(request: str) -> int:
        """How much of *request* a quote has to reproduce to count as one.

        Six characters, or the whole request when it is shorter -- a one-word
        instruction has one word to quote, and demanding six would make it
        unquotable.
        """
        return min(_QUOTE_MIN_CHARS, len(request))

    @staticmethod
    def _quotes_request(intent: str, request: str) -> bool:
        """Whether *intent* reproduces a run of characters from *request*.

        Verbatim is the one property a paraphrase cannot fake: it means the
        caller read the words it is acting on rather than describing what it
        thinks the user would want.  Whitespace is collapsed first, so
        re-wrapping a sentence still counts as quoting it.
        """
        intent = " ".join(intent.split())
        request = " ".join(request.split())
        if not request:
            return False
        span = RegularToolExecutor._quote_bar(request)
        if len(intent) < span:
            return False
        windows = {request[i : i + span] for i in range(len(request) - span + 1)}
        return any(
            intent[i : i + span] in windows
            for i in range(len(intent) - span + 1)
        )

    def _check_request(self, tool_name: str, inputs: dict) -> str:
        """Return an error unless this call quotes the request it acts on."""
        label = tool_name[:1].upper() + tool_name[1:] if tool_name else "Tool"
        intent = self._structured_intent(inputs)
        request = self._request_text()
        if not request:
            return (
                f"{label} refused: nothing in this turn asked for it, so there "
                "is no request to quote. Say what you would create and let the "
                "user ask for it, then call again quoting their words."
            )
        bar = self._quote_bar(request)
        if not intent:
            return (
                f"{label} intent required: include input.intent quoting what "
                f"asked for this -- at least {bar} characters of it, verbatim."
            )
        if not self._quotes_request(intent, request):
            return (
                f"{label} refused: input.intent does not quote this turn's "
                f"request. Copy at least {bar} characters of what the user "
                "actually said into input.intent -- explaining why the call "
                "would be useful is not the same as the user asking for it, and "
                "only the second one authorises this. If nobody asked, propose "
                "it in your reply instead."
            )
        return ""

    async def run(self, tool_use: dict) -> str:
        name = tool_use["name"]
        inputs = tool_use["input"]
        sink = _active_sink.get()
        operation_id = self._operation_id()

        if self._plugin_catalog:
            pre = await self._plugin_catalog.fire_pre_tool(
                PreToolEvent(tool_name=name, tool_kwargs=inputs)
            )
            if pre.action == "block":
                self._emit(
                    "tool_blocked",
                    operation_id=operation_id,
                    tool_name=name,
                    reason=pre.message,
                )
                if sink:
                    sink.on_tool_blocked(name, pre.message)
                else:
                    shared.CONSOLE.print(
                        f"\n[cyan]→ {name}[/cyan] "
                        f"[yellow](blocked by plugin: {pre.message})[/yellow]"
                    )
                return json.dumps(
                    {"ok": False, "blocked": True, "reason": pre.message}
                )

        declared_timeout = self._tool_timeout(inputs)
        effective_timeout = (
            max(declared_timeout, self._timeout_seconds)
            if declared_timeout is not None
            else self._timeout_seconds
        )

        self._emit(
            "tool_started",
            operation_id=operation_id,
            tool_name=name,
            timeout_seconds=effective_timeout,
            stale_timeout_seconds=self._stale_timeout_seconds,
        )
        if sink:
            sink.on_tool_start(name, inputs)
        else:
            shared.CONSOLE.print(f"\n[cyan]→ {name}[/cyan]{_fmt_tool_inputs(name, inputs)}")

        # ── Intent-before-action protocol ───────────────────────────────────
        # Tools with side effects must be preceded by a declared intent.
        intent_blocked = self._check_intent(name, inputs)
        if intent_blocked:
            result = json.dumps(
                {"ok": False, "error": intent_blocked, "intent_required": True}
            )
            if sink:
                sink.on_tool_blocked(name, intent_blocked)
            self._emit(
                "tool_blocked",
                operation_id=operation_id,
                tool_name=name,
                reason=intent_blocked,
            )
            return result

        started_at = time.monotonic()
        progress_state: dict[str, Any] = {
            "last_progress_at": started_at,
            "explicit_progress_count": 0,
        }
        progress_token = _active_tool_progress.set(
            self._progress_reporter(
                operation_id=operation_id,
                tool_name=name,
                started_at=started_at,
                progress_state=progress_state,
                sink=sink,
            )
        )
        try:
            result = await self._await_tool_result(
                operation_id=operation_id,
                tool_name=name,
                inputs=inputs,
                started_at=started_at,
                progress_state=progress_state,
                effective_timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            stale_for = time.monotonic() - float(progress_state["last_progress_at"])
            self._emit(
                "tool_timed_out",
                operation_id=operation_id,
                tool_name=name,
                timeout_seconds=effective_timeout,
                stale_timeout_seconds=self._stale_timeout_seconds,
                explicit_progress_count=progress_state["explicit_progress_count"],
                stale_for_ms=round(stale_for * 1000, 1),
            )
            result = json.dumps(
                {
                    "ok": False,
                    "error": f"tool '{name}' timed out after {effective_timeout:.0f}s",
                }
            )
        finally:
            _active_tool_progress.reset(progress_token)

        duration_ms = (time.monotonic() - started_at) * 1000
        try:
            data = json.loads(result)
            ok = data.get("ok", True)
        except Exception:
            ok = True
        self._emit(
            "tool_completed" if ok else "tool_failed",
            operation_id=operation_id,
            tool_name=name,
            ok=ok,
            duration_ms=round(duration_ms, 1),
            explicit_progress_count=progress_state["explicit_progress_count"],
            result_preview=result[:200],
        )

        if sink:
            sink.on_tool_end(name, result)
        else:
            shared.CONSOLE.print(
                f"[dim]{result[:200]}{'...' if len(result) > 200 else ''}[/dim]"
            )

        if self._plugin_catalog:
            await self._plugin_catalog.fire_post_tool(
                PostToolEvent(tool_name=name, tool_kwargs=inputs, result=result)
            )
        return result
