from __future__ import annotations

import asyncio
import json
from typing import Any

from agent import shared
from agent.core.output import _active_sink, _fmt_tool_inputs
from agent.plugins.catalog import PostToolEvent, PreToolEvent


class RegularToolExecutor:
    """Executes non-orchestration tool calls behind one side-effect boundary."""

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

    async def run(self, tool_use: dict) -> str:
        name = tool_use["name"]
        inputs = tool_use["input"]
        sink = _active_sink.get()

        if self._plugin_catalog:
            pre = await self._plugin_catalog.fire_pre_tool(
                PreToolEvent(tool_name=name, tool_kwargs=inputs)
            )
            if pre.action == "block":
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

        if sink:
            sink.on_tool_start(name, inputs)
        else:
            shared.CONSOLE.print(f"\n[cyan]→ {name}[/cyan]{_fmt_tool_inputs(name, inputs)}")

        try:
            result = await asyncio.wait_for(
                self._registry.call(name, inputs),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            result = json.dumps(
                {
                    "ok": False,
                    "error": f"tool '{name}' timed out after {self._timeout_seconds}s",
                }
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
