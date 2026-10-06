"""Remembering whether a streaming attempt reached the user.

Retrying an LLM call replays it from the beginning.  For a *streaming* call that
means every chunk is pushed to the sink a second time, and sinks are append-only:
the CLI has already written the first attempt's text to the terminal, so there is
nothing to take back.  A transient error after the first chunk therefore has to
surface as a failure rather than silently produce a doubled reply.

One flag covers both channels -- the answer and the thinking -- because the sink
cannot unsay either, and thinking is precisely the emission a thinking-heavy turn
has produced when it dies before the first answer token.

Moved out of ``agent/core/agent.py`` so that the response-acquisition path
(:mod:`agent.core.llm_attempt`) can use it without importing the agent module it
is called from.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = ["StreamEmissionTracker"]


class StreamEmissionTracker:
    """Wraps a turn's stream callbacks and remembers whether anything was emitted.

    The wrappers are plain callables, not coroutine functions, so they work for
    sync and async callbacks alike -- transports await the *result* when it is
    awaitable rather than inspecting the callable.
    """

    __slots__ = ("_callback", "emitted")

    def __init__(self, callback: Callable[[str], Any]):
        self._callback = callback
        self.emitted = False

    def nothing_emitted(self) -> bool:
        return not self.emitted

    def __call__(self, chunk: str) -> Any:
        if chunk:
            self.emitted = True
        return self._callback(chunk)

    def wrap_reasoning(self, callback: Callable[[str], Any]) -> Callable[[str], Any]:
        """The reasoning channel, counted against the same no-replay rule."""

        def reasoning(chunk: str) -> Any:
            if chunk:
                self.emitted = True
            return callback(chunk)

        return reasoning


#: The name it had inside agent.py; kept so the move needs no call-site edits.
_StreamEmissionTracker = StreamEmissionTracker
