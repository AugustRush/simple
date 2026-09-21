"""ContextAssembler: tool gating and budget allocation contracts.

The keyword routing deliberately hides management/schedule/orchestration
tools by default to save prompt tokens. The load-bearing invariant is the
escape hatch: a tool the user names explicitly is ALWAYS available, no
matter which group it belongs to or whether the phrase matched a keyword.
These tests pin that, because a silent regression here removes capability
without any error surfacing.

The second invariant is the boundary between *mentioning* scheduled work and
*asking for* it. Shipping a listing nobody wanted costs tokens; shipping a
creator nobody asked for costs a task that outlives the conversation, so the
two halves are gated separately and the creator side is the strict one.
"""

from __future__ import annotations

import pytest

from agent.core.context_assembler import ContextAssembler


def _tools(*names: str) -> list[dict]:
    return [{"name": name} for name in names]


ALL_GROUPS = _tools(
    "read_file",              # untouched baseline tool
    "write_file",             # untouched baseline tool
    "schedule_create",        # _SCHEDULE_TOOLS
    "memory_index",           # _DEEP_MEMORY_TOOLS
    "spawn_agent",            # _ORCHESTRATION_TOOLS
    "tavily_search",          # _SEARCH_EXTRAS
    "install_plugin",         # _MANAGEMENT_TOOLS
    "list_skill_files",       # _SKILL_RUNTIME_TOOLS
    "transcribe_audio",       # attachment-gated
)

#: Both halves of the scheduled-work group, so a test can tell which one a
#: sentence opened.
SCHEDULED_WORK_TOOLS = _tools(
    "read_file",
    "schedule_list", "schedule_create", "schedule_delete", "schedule_runs",
    "schedule_update", "schedule_set_enabled", "schedule_run", "schedule_cancel",
    "workflow_list", "workflow_create", "workflow_delete", "workflow_update",
    "emit_signal",
)

_CREATORS = {"schedule_create", "workflow_create"}


def _selected_names(assembler: ContextAssembler, query: str, **kwargs) -> set[str]:
    return {
        tool["name"]
        for tool in assembler.select_tools(ALL_GROUPS, query, **kwargs)
    }


def _scheduled(query: str, **kwargs) -> set[str]:
    return {
        tool["name"]
        for tool in ContextAssembler().select_tools(
            SCHEDULED_WORK_TOOLS, query, **kwargs
        )
    }


def test_baseline_tools_survive_a_query_with_no_keywords():
    names = _selected_names(ContextAssembler(), "帮我写个报告")
    assert {"read_file", "write_file"} <= names


def test_gated_groups_are_hidden_by_default():
    names = _selected_names(ContextAssembler(), "帮我写个报告")
    assert "schedule_create" not in names
    assert "spawn_agent" not in names
    assert "tavily_search" not in names
    assert "install_plugin" not in names


def test_keywords_reopen_their_group():
    assembler = ContextAssembler()
    assert "schedule_create" in _selected_names(assembler, "每天早上提醒我喝水")
    assert "spawn_agent" in _selected_names(assembler, "并行跑几个子代理")
    assert "tavily_search" in _selected_names(assembler, "帮我搜一下最新资料")
    assert "memory_index" in _selected_names(assembler, "记住这个偏好")


def test_explicit_name_always_wins_over_group_gating():
    """The escape hatch for every keyword miss.

    A user (or a model echoing a tool name) can always reach a gated tool
    by naming it, even when the surrounding words match no keyword at all.
    """
    names = _selected_names(ContextAssembler(), "调用 schedule_create 建一个任务")
    assert "schedule_create" in names

    names = _selected_names(ContextAssembler(), "please use install_plugin here")
    assert "install_plugin" in names

    names = _selected_names(ContextAssembler(), "run tavily_search for this")
    assert "tavily_search" in names


def test_explicit_name_match_respects_word_boundaries():
    """A derived word must not unlock a gated tool; the standalone token must."""
    tools = _tools("read_file", "spawn_agent")

    def selected(query: str) -> set[str]:
        return {
            tool["name"]
            for tool in ContextAssembler().select_tools(tools, query)
        }

    # "spawn_agents" is a different word: no exact-name match, and the
    # orchestration group's keywords do not appear either.
    assert "spawn_agent" not in selected("spawn_agents are cool")

    # The standalone token is an explicit request for that tool.
    assert "spawn_agent" in selected("call spawn_agent now")


def test_required_skills_open_the_skill_runtime_tools():
    names = _selected_names(
        ContextAssembler(), "do the thing", required_skills=("skill-manager",)
    )
    assert "list_skill_files" in names


def test_audio_attachment_opens_transcription():
    assembler = ContextAssembler()
    assert "transcribe_audio" not in _selected_names(assembler, "看看这个")
    assert "transcribe_audio" in _selected_names(
        assembler, "看看这个", attachment_kinds=("audio",)
    )


#: The sentence that caused this: a research question, matched on 流程, shipped
#: both creators, and came back with a signal-triggered task nobody wanted.
_THE_REPORTED_SENTENCE = "听一个真实的案例，订单都是如何接的，具体流程是什么，帮我做一下调研"


#: Sentences that name or describe scheduled work while asking about it.  A
#: creator here leaves a task behind for a question that was answered in words.
_QUESTIONS_ABOUT_SCHEDULED_WORK = [
    _THE_REPORTED_SENTENCE,
    "订单都是如何接的，具体流程是什么，帮我做一下调研",
    "我每天都在做订单，这个流程能优化吗",
    "什么会触发这个流程",
    "你们的工作流怎么用",
    "我有哪些定时任务",
    "怎么设置定时任务",
    "为什么我的定时任务没跑",
    "现在有哪些 workflow",
    "这个流程挺好的",
    "帮我梳理一下发布流程",
    "看看信号有哪些",
    "how do I address a failing workflow",
    "show me the pipeline",
]


#: Sentences that do ask for scheduled work to exist, by cadence or by verb.
_REQUESTS_FOR_SCHEDULED_WORK = [
    "把这个流程创建为一个workflow",
    "帮我搞个定时任务",
    "每天早上9点提醒我",
    "每天给我发一份简报",
    "建一个工作流把这几步串起来",
    "把这个流程拆成几个步骤",
    "帮我做个自动化",
    "新建一个 workflow",
    "给我加一个提醒",
    "设一个每周提醒",
    "能不能帮我建一个定时任务",
    "帮我设置一个每日提醒",
    "把 xhs.package.ready 这个信号发出去",
]


@pytest.mark.parametrize("query", _QUESTIONS_ABOUT_SCHEDULED_WORK)
def test_a_question_about_scheduled_work_ships_no_creator(query: str):
    assert not (_CREATORS & _scheduled(query))


@pytest.mark.parametrize("query", _QUESTIONS_ABOUT_SCHEDULED_WORK)
def test_a_question_about_scheduled_work_still_ships_the_listing(query: str):
    """Reading is what answers the question, so it is never what gets withheld."""
    assert {"schedule_list", "workflow_list"} <= _scheduled(query)


@pytest.mark.parametrize("query", _REQUESTS_FOR_SCHEDULED_WORK)
def test_a_request_for_scheduled_work_ships_the_creators(query: str):
    assert _CREATORS <= _scheduled(query)


def test_an_incidental_cadence_does_not_count_as_a_request():
    """`每天` describes a routine far more often than it requests a schedule."""
    assert not (_CREATORS & _scheduled("我每天都在做订单，这个流程能优化吗"))


def test_a_verb_still_wins_over_the_question_marker():
    """Asking *whether* you may is still asking for it."""
    assert "schedule_create" in _scheduled("能不能帮我建一个定时任务")


def test_emitting_a_signal_needs_a_request_but_a_run_keeps_it():
    """A stray emission is not inert: it starts whatever subscribed to the name.

    The only custom signal this machine ever recorded was emitted two seconds
    after the unwanted task was created, by the same turn nobody had asked for.
    """
    assert "emit_signal" not in _scheduled("订单都是如何接的，具体流程是什么")
    assert "emit_signal" not in _scheduled("看看信号有哪些")
    assert "emit_signal" in _scheduled("把 xhs.package.ready 这个信号发出去")
    assert "emit_signal" in _scheduled("做点事", scheduled_run=True)


_MUTATORS = {
    "schedule_update",
    "schedule_set_enabled",
    "schedule_run",
    "schedule_cancel",
    "workflow_update",
}


@pytest.mark.parametrize("query", _QUESTIONS_ABOUT_SCHEDULED_WORK)
def test_a_question_about_scheduled_work_ships_no_mutator(query: str):
    """Changing a task is an action too, and held back for the same reason.

    It cannot create anything, but it can change or stop something, and the
    schemas are the largest in the group -- twenty fields for
    ``schedule_update`` alone -- so they cost on every turn of every
    conversation rather than only on the turns that could use them.
    """
    assert not (_MUTATORS & _scheduled(query))


@pytest.mark.parametrize("query", _REQUESTS_FOR_SCHEDULED_WORK)
def test_a_request_for_scheduled_work_ships_the_mutators(query: str):
    assert _MUTATORS <= _scheduled(query)


def test_the_run_history_is_never_withheld():
    """It answers "did it actually run", in words that name no cadence.

    Asking whether yesterday's report went through is a question about this
    feature phrased entirely in the user's own terms, and an observation tool
    that is not there when the question arrives is one nobody can use.
    """
    for query in ("昨天的日报跑成功了吗", "那个任务到底跑了没有", "换个大模型"):
        assert "schedule_runs" in _scheduled(query)


def test_ascii_request_verbs_respect_word_boundaries():
    """`add` inside `address` and `how` inside `show` are not requests."""
    assert "workflow_create" not in _scheduled("how do I address a failing workflow")
    assert "workflow_create" not in _scheduled("show me the pipeline")
    assert "workflow_create" in _scheduled("build a workflow for the releases")
