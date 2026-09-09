"""Build bounded provider inputs from session state and task intent."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable, Iterable

from agent import shared


_MANAGEMENT_TOOLS = {
    "install_plugin", "uninstall_plugin", "list_installed_plugins",
    "create_tool", "update_tool", "delete_tool", "list_tools",
    "install_tool_dependency", "create_skill", "update_skill",
    "delete_skill", "write_skill_file",
}
_SCHEDULE_TOOLS = {"schedule_create", "schedule_list", "schedule_delete"}
_DEEP_MEMORY_TOOLS = {"memory_index", "memory_clear"}
_SKILL_RUNTIME_TOOLS = {"activate_skill", "list_skill_files", "read_skill_file"}
_ORCHESTRATION_TOOLS = {"spawn_agent"}
_SEARCH_EXTRAS = {"tavily_search"}


def _matches(text: str, terms: Iterable[str]) -> bool:
    lowered = text.casefold()
    return any(term in lowered for term in terms)


@dataclass(frozen=True)
class ContextBudget:
    static_tokens: int
    message_tokens: int
    retrieval_tokens: int
    input_tokens: int


class ContextAssembler:
    """Select tools and allocate retrieval from one shared input budget."""

    def select_tools(
        self,
        tools: list[dict[str, Any]],
        query: str,
        *,
        required_skills: Iterable[str] = (),
        attachment_kinds: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        names = {str(tool.get("name") or "") for tool in tools}
        selected = names - (
            _MANAGEMENT_TOOLS
            | _SCHEDULE_TOOLS
            | _DEEP_MEMORY_TOOLS
            | _ORCHESTRATION_TOOLS
            | _SEARCH_EXTRAS
        )

        if _matches(query, ("schedule", "remind", "recurring", "cron", "定时", "提醒", "周期")):
            selected |= _SCHEDULE_TOOLS
        if _matches(query, ("memory", "remember", "forget", "记忆", "记住", "忘记", "上下文")):
            selected |= _DEEP_MEMORY_TOOLS
        if _matches(query, ("plugin", "skill", "tool", "插件", "技能", "工具")):
            selected |= _MANAGEMENT_TOOLS | _SKILL_RUNTIME_TOOLS
        if tuple(required_skills):
            selected |= _SKILL_RUNTIME_TOOLS
        if _matches(query, ("parallel", "sub-agent", "subagent", "并行", "子代理", "多代理")):
            selected |= _ORCHESTRATION_TOOLS
        if _matches(query, ("research", "search", "latest", "调研", "搜索", "最新")):
            selected |= _SEARCH_EXTRAS
        if "audio" in set(attachment_kinds):
            selected.add("transcribe_audio")

        # Explicit tool names are always honored, including user and MCP tools.
        lowered = query.casefold()
        for name in names:
            if name and re.search(rf"(?<![\w-]){re.escape(name.casefold())}(?![\w-])", lowered):
                selected.add(name)
        return [tool for tool in tools if str(tool.get("name") or "") in selected]

    def allocate(
        self,
        *,
        context_window: int,
        output_tokens: int,
        system_prompt: str,
        tools: list[dict[str, Any]],
        current_messages: list[dict[str, Any]],
        current_user_content: Any,
        estimate: Callable[[list[dict[str, Any]]], int],
        configured_retrieval_tokens: int = 0,
    ) -> ContextBudget:
        static = estimate([
            {"role": "system", "content": system_prompt},
            {
                "role": "system",
                "content": json.dumps(tools, ensure_ascii=False, sort_keys=True),
            },
        ])
        input_tokens = max(0, int(context_window) - int(output_tokens) - static)
        messages = estimate(current_messages)
        current = estimate([{"role": "user", "content": current_user_content}])
        # The newest request and a small protocol margin are mandatory. Retrieval
        # receives only a fraction of what remains after those costs.
        remaining = max(0, input_tokens - messages - current - 256)
        desired = (
            int(configured_retrieval_tokens)
            if configured_retrieval_tokens > 0
            else int(remaining * shared.RETRIEVAL_BUDGET_FRACTION)
        )
        retrieval = max(0, min(desired, remaining))
        return ContextBudget(
            static_tokens=static,
            message_tokens=messages + current,
            retrieval_tokens=retrieval,
            input_tokens=input_tokens,
        )


__all__ = ["ContextAssembler", "ContextBudget"]
