"""ContextAssembler: tool-block stability and budget allocation contracts.

The tool schemas sit ahead of every message in the request, so a provider's
prefix cache can only reuse what precedes their first difference -- and the
whole conversation sits behind it.  A set chosen from each turn's own words
therefore re-prices the entire body on every turn whose words change it, which
is what this module used to do: measured on this machine, a head that moved on
most turns pinned the cache read at ~7.4k of a ~100k payload.

The invariant these tests pin is that membership is a function of the agent
instance and nothing else.  What a call is *allowed* to do is the executor's
capability check, which runs against the whole registry whether or not a schema
was sent -- so withholding a schema was never a permission boundary, and
paying a full re-price for it was the only thing it actually bought.

The one gate left keys on the run: ``report_outcome`` refuses outside one, so
its schema is shipped only where it can be used.  A run's context carries
``scheduler_run_id`` from construction, so that gate is session-stable too.

The escape hatch the old gate needed -- a tool the user names explicitly is
always reachable -- is now held by construction rather than by a matcher, and
is still pinned here because a silent regression would remove capability with
no error surfacing.
"""

from __future__ import annotations

import pytest

from agent.core.context_assembler import ContextAssembler


def _tools(*names: str) -> list[dict]:
    return [{"name": name} for name in names]


ALL_TOOLS = _tools(
    "read_file",
    "write_file",
    "schedule_create",
    "schedule_delete",
    "memory_index",
    "spawn_agent",
    "tavily_search",
    "install_plugin",
    "list_skill_files",
    "transcribe_audio",
    "report_outcome",
    "emit_signal",
)

#: Sentences that used to open one group and close another.  They are here to
#: prove they no longer decide anything: every one of them must select the
#: same set, because the set is not a function of the sentence.
_QUERIES = [
    "帮我写个报告",
    "每天早上9点提醒我",
    "订单都是如何接的，具体流程是什么，帮我做一下调研",
    "并行跑几个子代理",
    "记住这个偏好",
    "调用 schedule_create 建一个任务",
    "spawn_agents are cool",
    "how do I address a failing workflow",
    "",
]


def _selected_names(assembler: ContextAssembler, query: str = "", **kwargs) -> list[str]:
    return [tool["name"] for tool in assembler.select_tools(ALL_TOOLS, **kwargs)]


@pytest.mark.parametrize("query", _QUERIES)
def test_every_registered_tool_is_offered(query: str):
    """Membership does not depend on the words -- including the words of a
    question that used to withhold a creator."""
    names = set(_selected_names(ContextAssembler()))
    assert names == {tool["name"] for tool in ALL_TOOLS} - {"report_outcome"}


def test_the_selected_set_is_identical_across_queries():
    """The property the cache pays for: one session, one tool block.

    The query is not even a parameter any more.  This asserts the observable
    consequence rather than the signature: whatever a session's turns say, the
    schemas they send are the same list in the same order.
    """
    assembler = ContextAssembler()
    reference = _selected_names(assembler)
    for query in _QUERIES:
        assert _selected_names(assembler) == reference


def test_registry_order_is_preserved():
    """No reordering: the assembler's output is the registry's list, filtered.

    The old "always-on first" sort existed only to soften the per-turn gate's
    damage, and could not do even that -- the divergence it failed to avoid sat
    ahead of the body either way.
    """
    assert _selected_names(ContextAssembler()) == [
        name
        for name in (tool["name"] for tool in ALL_TOOLS)
        if name != "report_outcome"
    ]


def test_explicitly_named_tools_are_always_available():
    """The escape hatch the old keyword gate needed, now held by construction.

    A user (or a model echoing a tool name) must always be able to reach a
    tool by naming it.  With membership no longer decided by words, this is a
    property of the whole list rather than of a matcher -- and it stays pinned
    because losing it would remove capability with no error surfacing.
    """
    names = set(_selected_names(ContextAssembler()))
    assert {"schedule_create", "install_plugin"} <= names


def test_report_outcome_is_shipped_only_inside_a_run():
    """The one remaining gate, and it is a function of session-stable state.

    Outside a run the tool refuses, so shipping its schema would be shipping a
    tool whose only possible outcome is an error.  A run's context carries its
    ``scheduler_run_id`` from construction, so this does not move per turn.
    """
    assert "report_outcome" not in _selected_names(ContextAssembler())
    assert "report_outcome" in _selected_names(ContextAssembler(), scheduled_run=True)

    assembler = ContextAssembler()
    for _ in _QUERIES:
        assert "report_outcome" in _selected_names(assembler, scheduled_run=True)
        assert "report_outcome" not in _selected_names(assembler)
