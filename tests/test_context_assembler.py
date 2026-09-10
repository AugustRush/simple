"""ContextAssembler: tool gating and budget allocation contracts.

The keyword routing deliberately hides management/schedule/orchestration
tools by default to save prompt tokens. The load-bearing invariant is the
escape hatch: a tool the user names explicitly is ALWAYS available, no
matter which group it belongs to or whether the phrase matched a keyword.
These tests pin that, because a silent regression here removes capability
without any error surfacing.
"""

from __future__ import annotations

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


def _selected_names(assembler: ContextAssembler, query: str, **kwargs) -> set[str]:
    return {
        tool["name"]
        for tool in assembler.select_tools(ALL_GROUPS, query, **kwargs)
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
