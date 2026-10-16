"""Clearing the context: the one tool that forgets a conversation on purpose. Split out of BuiltinTools._register (see the remaining-big-files survey, section 2c)."""

from __future__ import annotations

def register_context_tools(
    r,
    *,
    context_tools,
) -> None:
        r.register(
            "clear_context",
            "Reset the conversation context to just the current user request. "
            "Use this when the conversation history has grown too large or "
            "triggered a content policy filter. The current task will be "
            "preserved as a summary, and the agent will restart with a clean "
            "context.",
            {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "A brief summary of the original request and what has been done so far (optional, auto-generated if empty).",
                        "default": "",
                    },
                },
                "required": [],
            },
            context_tools.clear,
            source="builtin",
        )
