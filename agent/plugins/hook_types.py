"""The payloads a plugin hook receives and returns.

Split out of the loader so the loader is not the module that has to be imported to
*name* a hook event.  Three things depended on that and none of them is the loader:
``agent/plugins/protocols.py`` (the service contract), ``agent/plugins/formats.py``
(a format's ``translate_hook_output`` strategy returns a ``HookResult``), and every
third-party plugin that annotates its own hook methods.  The loader re-exports all
five names, so existing imports keep working.

What lives here and what does not
---------------------------------
Here: the **data** -- what a hook is told, and what it answers with.  Those types are
the cross-ecosystem surface, which is why they are frozen shapes rather than this
runtime's richer event classes (``agent/core/events.py``).  A plugin written against
another product's documentation produces one of these; it cannot construct a
``ToolPreExecute``, and it should not have to.

Not here: the dispatch, the matchers, the hook environments, the subprocess runner.
Those are :mod:`agent.plugins.hook_dispatch`, which imports this module -- one
direction, so neither has to be imported to name the other's types.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "HookResult",
    "TurnEvent",
    "SessionEvent",
    "PreToolEvent",
    "PostToolEvent",
]


@dataclass
class TurnEvent:
    """Emitted after each assistant turn completes."""

    user_input: str
    agent_response: str
    tool_calls: list[str]
    session_id: str = ""
    timestamp: str = ""
    turn_index: int = 0


@dataclass
class SessionEvent:
    """Emitted when the interactive session ends."""

    messages: list[dict]
    tools_used: list[str]
    session_id: str = ""
    timestamp: str = ""
    turn_count: int = 0


@dataclass
class PreToolEvent:
    """Emitted before a tool call executes."""

    tool_name: str
    tool_kwargs: dict


@dataclass
class PostToolEvent:
    """Emitted after a tool call completes."""

    tool_name: str
    tool_kwargs: dict
    result: str


@dataclass
class HookResult:
    """Return value from plugin hook methods.

    Actions:
        ``"noop"``     — no effect (default).
        ``"block"``    — prevent the operation (tool execution, prompt delivery).
        ``"continue"`` — (turn_end only) run another turn with ``message`` as prompt.
        ``"context"``  — inject ``context`` into the next turn's system instructions.
        ``"warning"``  — surface ``message`` as a user-visible warning.
    """

    action: str = "noop"
    message: str = ""
    context: str = ""
