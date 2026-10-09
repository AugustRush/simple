"""The context tools, asserted directly.

``context_retrieve`` and ``clear_context`` were the least-guarded pair in
``BuiltinTools``: the survey measured two tests, and the behaviours below were
only reachable through them.  The extraction into
``agent/tools/context_tools.py`` is why this module exists -- a move that leaves
a rule guarded only by accident is a rule that can be changed by accident.

Each case here is one of the rules the code carries a comment about: the plural
``queries`` parameter, the bare-string trap, the active-turn manager, and the
empty result that must explain itself.
"""

from __future__ import annotations

from typing import Any

from agent.core.agent import AgentContext, _active_agent_context
from agent.tools.context_tools import ContextTools


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _error(message: str, **payload: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **payload}


class _Manager:
    """Records how it was asked and returns whatever it was told to."""

    def __init__(self, result: str = "") -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def retrieve_context(self, queries, *, top_k=5, exclude_message_id=""):
        self.calls.append(
            {"queries": list(queries), "top_k": top_k, "exclude_message_id": exclude_message_id}
        )
        return self.result


def _tools(manager=None) -> ContextTools:
    return ContextTools(registry=None, context_manager=manager, ok=_ok, error=_error)


# ─────────────────────────────────────────────────────────────────────────────
# context_retrieve
# ─────────────────────────────────────────────────────────────────────────────


def test_a_bare_string_is_one_query_and_not_its_characters():
    """A model filling an array parameter with a string is routine.

    Iterating that string would yield its characters -- silently turning one
    good query into dozens of one-character ones that match everything.
    """
    manager = _Manager("## Conversation History\n\nsomething")
    _tools(manager).retrieve(queries="kubernetes deployment")

    assert manager.calls[0]["queries"] == ["kubernetes deployment"]


def test_queries_and_query_are_merged_in_order_without_duplicates():
    manager = _Manager("## Conversation History\n\nx")
    _tools(manager).retrieve(queries=["a", "b", "a"], query="c")

    assert manager.calls[0]["queries"] == ["a", "b", "c"]


def test_a_blank_bare_string_is_no_query_at_all():
    manager = _Manager()
    result = _tools(manager).retrieve(queries="   ")

    assert result["ok"] is False
    assert "Provide `queries`" in result["error"]
    assert "phrasings" in result["hint"]
    assert manager.calls == []


def test_the_active_turn_manager_wins_over_the_constructed_one():
    """Tool definitions are shared across multiplexed sessions.

    The bootstrap manager's staging session is not user-facing, so resolving
    from the active turn is what keeps a session's search in its own history.
    """
    constructed = _Manager("constructed")
    active = _Manager("## Conversation History\n\nactive")
    tools = _tools(constructed)

    ctx = AgentContext(metadata={"context_manager": active, "turn_id": "turn-7"})
    token = _active_agent_context.set(ctx)
    try:
        result = tools.retrieve("what did we say")
    finally:
        _active_agent_context.reset(token)

    assert result["content"] == "## Conversation History\n\nactive"
    assert constructed.calls == []
    # The current turn is excluded: retrieving it would hand the answer back
    # as though it were a memory.
    assert active.calls[0]["exclude_message_id"] == "turn-7"


def test_no_manager_anywhere_is_an_error():
    result = _tools(None).retrieve("anything")

    assert result["ok"] is False
    assert "Context manager not available" in result["error"]


def test_an_empty_result_explains_how_to_ask_again():
    """It is far more often a badly-shaped query than an absent memory.

    Without saying so, the model concludes "no record" and tells the user that
    -- which is the answer the hint exists to prevent.
    """
    result = _tools(_Manager("")).retrieve("zzzz nonexistent qqqq")

    assert result["ok"] is True
    assert result["count"] == 0
    assert result["sections"] == []
    assert "lexical" in result["hint"]


def test_blank_segments_are_not_counted_as_sections():
    """The count is what the model reads: an empty block must not inflate it.

    Sections keep their own internal whitespace -- only wholly blank segments
    are dropped -- so the assertion is on the stripped text and the count.
    """
    manager = _Manager("one\n\ntwo\n\n\n\nthree")
    result = _tools(manager).retrieve("q")

    assert [s.strip() for s in result["sections"]] == ["one", "two", "three"]
    assert result["count"] == 3
    assert result["content"] == "one\n\ntwo\n\n\n\nthree"


# ─────────────────────────────────────────────────────────────────────────────
# clear_context
# ─────────────────────────────────────────────────────────────────────────────


def _with_context(messages: list[dict[str, Any]], summary: str = "") -> dict[str, Any]:
    token = _active_agent_context.set(AgentContext(messages=messages))
    try:
        return _tools().clear(summary)
    finally:
        _active_agent_context.reset(token)


def test_clear_refuses_without_an_active_conversation():
    result = _tools().clear()

    assert result["ok"] is False
    assert "No active agent context" in result["error"]


def test_clear_carries_the_latest_request_and_the_summary_forward():
    result = _with_context(
        [
            {"role": "user", "content": "an old task"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "the current task"},
        ],
        summary="did the first thing",
    )

    assert result["ok"] is True
    assert result["clear_context_requested"] is True
    assert result["current_request"] == "the current task"
    assert result["summary_provided"] is True
    assert "did the first thing" in result["restart_message"]
    assert "the current task" in result["restart_message"]


def test_clear_skips_a_user_message_that_is_only_a_tool_result():
    """Anthropic tool results arrive as role=user messages too.

    Taking the last ``user`` message blindly would make the "current request"
    a blob of tool output.
    """
    result = _with_context(
        [
            {"role": "user", "content": "please read the file"},
            {
                "role": "user",
                "content": [{"type": "tool_result", "content": "file contents"}],
            },
        ]
    )

    assert result["current_request"] == "please read the file"


def test_clear_truncates_the_echoed_request():
    long_request = "x" * 900
    result = _with_context([{"role": "user", "content": long_request}])

    assert len(result["current_request"]) == 500
    # The restart message carries the request in full: only the echo is bounded.
    assert long_request in result["restart_message"]
