from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from agent import shared
from agent.core.output import _active_sink, _fmt_tool_inputs
from agent.plugins.catalog import PostToolEvent, PreToolEvent
from agent.core.output import _active_event_collector


class RegularToolExecutor:
    """Executes non-orchestration tool calls behind one side-effect boundary.

    Emits ``tool_started``, ``tool_completed``, ``tool_failed``,
    ``tool_timed_out``, and ``tool_blocked`` ``RuntimeEvent`` facts into
    the active ``EventCollector`` (when one is set by AgentCore).
    """

    def __init__(
        self,
        registry: Any,
        *,
        plugin_catalog: Any = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._registry = registry
        self._plugin_catalog = plugin_catalog
        self._timeout_seconds = (
            shared.REGULAR_TOOL_TIMEOUT
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds))
        )

    @staticmethod
    def _emit(event_name: str, **fields: Any) -> None:
        collector = _active_event_collector.get()
        if collector is not None:
            collector.emit(event_name, **fields)

    async def run(self, tool_use: dict) -> str:
        name = tool_use["name"]
        inputs = tool_use["input"]
        sink = _active_sink.get()

        if self._plugin_catalog:
            pre = await self._plugin_catalog.fire_pre_tool(
                PreToolEvent(tool_name=name, tool_kwargs=inputs)
            )
            if pre.action == "block":
                self._emit("tool_blocked", tool_name=name, reason=pre.message)
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

        self._emit("tool_started", tool_name=name)
        if sink:
            sink.on_tool_start(name, inputs)
        else:
            shared.CONSOLE.print(f"\n[cyan]→ {name}[/cyan]{_fmt_tool_inputs(name, inputs)}")

        started_at = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._registry.call(name, inputs),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._emit(
                "tool_timed_out",
                tool_name=name,
                timeout_seconds=self._timeout_seconds,
            )
            result = json.dumps(
                {
                    "ok": False,
                    "error": f"tool '{name}' timed out after {self._timeout_seconds}s",
                }
            )

        duration_ms = (time.monotonic() - started_at) * 1000
        try:
            data = json.loads(result)
            ok = data.get("ok", True)
        except Exception:
            ok = True
        self._emit(
            "tool_completed" if ok else "tool_failed",
            tool_name=name,
            ok=ok,
            duration_ms=round(duration_ms, 1),
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
