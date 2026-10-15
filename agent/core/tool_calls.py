"""Running one batch of tool calls, in the order the model asked for them.

Moved out of ``agent/core/agent.py`` (see the remaining-big-files survey).  It needs a list
of calls and an executor, and nothing about the agent that owns them -- which is also what
made it the easiest thing in that file to test on its own: the tests already build a fake
executor and hand it calls.

The serial-when-``shell`` rule it implements is the interesting part: a medium-risk shell
command blocks on the human consent menu, and nothing else may print while that menu is
open (parallel results and heartbeats corrupt the prompt).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

__all__ = ["_execute_regular_tool_calls"]


async def _execute_regular_tool_calls(
    regular_calls: list[tuple[int, dict]],
    executor: Any,
) -> list[str]:
    """Run a batch of non-orchestration tool calls, preserving call order.

    Batches containing a ``shell`` call run sequentially: a medium-risk
    shell command blocks on the human consent menu, and other tools must
    not execute or print while that menu is open (parallel results and
    heartbeats corrupt the prompt).  Pure parallel batches keep the fast
    ``asyncio.gather`` path.
    """
    results: list[str] = [""] * len(regular_calls)
    if any(tu.get("name") == "shell" for _idx, tu in regular_calls):
        for position, (_idx, tu) in enumerate(regular_calls):
            try:
                results[position] = await executor.run(tu)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                results[position] = json.dumps(
                    {"ok": False, "error": f"tool '{tu['name']}' raised: {exc}"}
                )
        return results
    raw = await asyncio.gather(
        *[executor.run(tu) for _idx, tu in regular_calls],
        return_exceptions=True,
    )
    for position, (_idx, _tu) in enumerate(regular_calls):
        outcome = raw[position]
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
        if isinstance(outcome, Exception):
            results[position] = json.dumps(
                {"ok": False, "error": f"tool '{_tu['name']}' raised: {outcome}"}
            )
        elif isinstance(outcome, BaseException):
            raise outcome
        else:
            results[position] = outcome
    return results
