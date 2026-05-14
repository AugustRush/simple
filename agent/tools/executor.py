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
    _fmt_tool_inputs,
)
from agent.plugins.catalog import PostToolEvent, PreToolEvent

# Capabilities that require the assistant to declare intent before acting.
# Pure "read" tools are excluded — observation needs no explanation.
_INTENT_REQUIRED_CAPABILITIES = frozenset(
    {"workspace_write", "output_write", "shell", "state_write", "side_effect"}
)

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

    async def _await_tool_result(
        self,
        *,
        operation_id: str,
        tool_name: str,
        inputs: dict,
        started_at: float,
        progress_state: dict[str, Any],
    ) -> str:
        call_task = asyncio.create_task(self._registry.call(tool_name, inputs))
        deadline = started_at + self._timeout_seconds

        while True:
            remaining = max(0.0, deadline - time.monotonic())
            done, _pending = await asyncio.wait({call_task}, timeout=remaining)
            if done:
                return await call_task

            now = time.monotonic()
            last_progress_at = float(progress_state["last_progress_at"])
            stale_for = now - last_progress_at
            explicit_progress_count = int(progress_state["explicit_progress_count"])
            if (
                explicit_progress_count > 0
                and self._stale_timeout_seconds > 0
                and stale_for <= self._stale_timeout_seconds
            ):
                deadline = now + self._stale_timeout_seconds
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

            call_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await call_task
            raise asyncio.TimeoutError

    async def _emit_heartbeats(
        self,
        *,
        operation_id: str,
        tool_name: str,
        started_at: float,
        done: asyncio.Event,
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    done.wait(),
                    timeout=self._HEARTBEAT_INTERVAL_SECONDS,
                )
                return
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - started_at
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

    def _check_intent(self, tool_name: str) -> str:
        """Return an error message if intent is undeclared, or "" if OK.

        Only tools with write/shell/state/side_effect capabilities are
        checked.  Pure read tools are exempt — observation needs no
        prior explanation.
        """
        # Look up the tool's capabilities; exempt if registry is not introspectable
        tools = getattr(self._registry, "_tools", None)
        cap = tools.get(tool_name) if isinstance(tools, dict) else None
        if cap is None:
            return ""  # can't determine capability — skip check
        if not (getattr(cap, "capabilities", frozenset()) & _INTENT_REQUIRED_CAPABILITIES):
            return ""
        intent = _active_assistant_text.get()
        if not intent or len(intent.strip()) < 10:
            return (
                f"Intent required: before using '{tool_name}', explain what you "
                "are about to do and why. Add a sentence describing the action, "
                "then call the tool again."
            )
        # Shell requires explicit mention — the highest-risk tool
        if tool_name == "shell":
            signal_words = ("shell", "command", "run ", "execute", "exec ", "bash",
                            "运行", "执行", "命令")
            if not any(w in intent.lower() for w in signal_words):
                return (
                    "Intent declaration too vague for shell. Explicitly state "
                    "that you are about to run a shell command, what it does, "
                    "and why it is necessary. Then call the tool again."
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

        self._emit(
            "tool_started",
            operation_id=operation_id,
            tool_name=name,
            timeout_seconds=self._timeout_seconds,
            stale_timeout_seconds=self._stale_timeout_seconds,
        )
        if sink:
            sink.on_tool_start(name, inputs)
        else:
            shared.CONSOLE.print(f"\n[cyan]→ {name}[/cyan]{_fmt_tool_inputs(name, inputs)}")

        # ── Intent-before-action protocol ───────────────────────────────────
        # Tools with side effects must be preceded by a declared intent.
        intent_blocked = self._check_intent(name)
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
        done = asyncio.Event()
        progress_token = _active_tool_progress.set(
            self._progress_reporter(
                operation_id=operation_id,
                tool_name=name,
                started_at=started_at,
                progress_state=progress_state,
                sink=sink,
            )
        )
        heartbeat_task = asyncio.create_task(
            self._emit_heartbeats(
                operation_id=operation_id,
                tool_name=name,
                started_at=started_at,
                done=done,
            )
        )
        try:
            result = await self._await_tool_result(
                operation_id=operation_id,
                tool_name=name,
                inputs=inputs,
                started_at=started_at,
                progress_state=progress_state,
            )
        except asyncio.TimeoutError:
            stale_for = time.monotonic() - float(progress_state["last_progress_at"])
            self._emit(
                "tool_timed_out",
                operation_id=operation_id,
                tool_name=name,
                timeout_seconds=self._timeout_seconds,
                stale_timeout_seconds=self._stale_timeout_seconds,
                explicit_progress_count=progress_state["explicit_progress_count"],
                stale_for_ms=round(stale_for * 1000, 1),
            )
            result = json.dumps(
                {
                    "ok": False,
                    "error": f"tool '{name}' timed out after {self._timeout_seconds}s",
                }
            )
        finally:
            _active_tool_progress.reset(progress_token)
            done.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

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
