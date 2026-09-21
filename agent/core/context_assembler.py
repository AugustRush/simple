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
_SCHEDULE_TOOLS = {
    "schedule_create",
    "schedule_list",
    "schedule_delete",
    # The maintenance half: changing, pausing, starting and stopping a task
    # that already exists.  Held back with the creators rather than shipped on
    # every turn, because their schemas are the largest in the group --
    # `schedule_update` names twenty fields, each with a sentence explaining it
    # -- and that cost is paid on every turn of every conversation, while what
    # they can change is a task the sentence has to have named anyway: an edit
    # is addressed by an id that came from `schedule_list` or `schedule_runs`.
    "schedule_update",
    "schedule_set_enabled",
    "schedule_run",
    "schedule_cancel",
}
_WORKFLOW_TOOLS = {
    "workflow_create",
    "workflow_list",
    "workflow_delete",
    "workflow_update",
}
#: The half of the two groups that only *looks*.  Cheaper to get wrong in the
#: permissive direction: a listing nobody needed costs tokens, while a creator
#: nobody asked for costs a task that outlives the conversation.
#:
#: `schedule_runs` is deliberately not in here, and so is not held back at all.
#: It answers "did the thing I asked for actually run", which is a question
#: about *this* feature but phrased in the user's own words -- asking whether
#: yesterday's report went through names neither a cadence nor the word 定时 --
#: and an observation tool that is not there when the question arrives is one
#: the caller cannot use.
_SCHEDULED_WORK_READ_TOOLS = {"schedule_list", "workflow_list"}
#: The half that *builds or destroys*.  These are the schemas whose absence is
#: worth the most tokens, so the request terms below decide whether they ride
#: along -- but that decision is about **budget, not permission**.  The gate
#: cannot be a guard and was never able to be one: calls are dispatched by name
#: against the whole registry, and the system prompt names every tool to the
#: model in the same turn, so a schema that was not sent is still a tool that
#: can be called.  What actually refuses an unasked creation is the executor's
#: ``requires_request`` check, which makes the call quote the words that asked
#: for it.  Keeping the terms here only means a caller that *did* ask is not
#: charged for the schemas, and one that did not is not invited by them.
_SCHEDULED_WORK_WRITE_TOOLS = (
    _SCHEDULE_TOOLS | _WORKFLOW_TOOLS
) - _SCHEDULED_WORK_READ_TOOLS
#: Pulling the trigger, rather than loading the gun.  A hand-made signal is
#: only ever emitted to start a task waiting on that name -- everything else
#: follows on `task:<id>:succeeded`, which the run emits for itself.  Measured
#: on this machine: of the 22 signals ever recorded, 21 are those automatic
#: ones, and the single custom name was emitted two seconds after the unwanted
#: task above was created, by the same turn nobody had asked for.  A stray
#: emission is not inert, either: it starts whatever subscribed to that name,
#: so a question must not be able to reach it.  A scheduled run keeps it.
_SIGNAL_TOOLS = {"emit_signal"}

#: Meaningful only inside a scheduled run -- outside one it refuses, because
#: there is a person to tell instead.  Gated on *the run* rather than on words:
#: no sentence in a conversation makes it usable, and a schema that can only be
#: called in error is not worth the tokens.
_RUN_SELF_REPORT_TOOLS = {"report_outcome"}
_DEEP_MEMORY_TOOLS = {"memory_index", "memory_clear"}
_SKILL_RUNTIME_TOOLS = {"activate_skill", "list_skill_files", "read_skill_file"}
_ORCHESTRATION_TOOLS = {"spawn_agent"}
_SEARCH_EXTRAS = {"tavily_search"}
# Only useful when the turn actually carries audio; gated so the schema
# does not ride along in every prompt.
_ATTACHMENT_GATED_TOOLS = {"transcribe_audio"}

#: Words that name or describe scheduled work.  These open the *reading*
#: tools: "我有哪些定时任务" and "你们的工作流怎么用" are both questions about
#: this feature, and both want the list.
#:
#: Words that merely *describe a process* belong here and nowhere else,
#: because that is how a question is phrased far more often than how a request
#: is.  Measured on this machine: a question about how a trade takes orders --
#: "订单都是如何接的，具体流程是什么" -- matched 流程 and shipped
#: schedule_create and workflow_create into the turn, and the model answered
#: the question by building a signal-triggered task nobody had asked for.
#:
#: The product's own words are here because they are what the user reads on
#: the automation page and therefore what they type: a gate that understood
#: only 定时 answered "帮我创建一个自动化" as though the agent could not do it.
#: A name is not a request on its own, which is why the verbs below decide.
_SCHEDULED_WORK_TERMS = (
    "schedule",
    "remind",
    "recurring",
    "cron",
    "automation",
    "automate",
    "workflow",
    "pipeline",
    "signal",
    "定时",
    "提醒",
    "周期",
    "定期",
    "自动化",
    "工作流",
    "流程",
    "流水线",
    "串联",
    "链路",
    "日程",
    "信号",
    "每天",
    "每日",
    "每周",
    "每月",
    "每年",
)

#: One of the two ways to ask for scheduled work: state a cadence, and the
#: cadence is the whole request.  Nobody says "每天早上" or "提醒我" except to
#: ask for one, so nothing else has to accompany it.  The feature's own name
#: is *not* here -- "你们的工作流怎么用" names it without asking for anything.
_SCHEDULED_WORK_CADENCE = (
    "提醒",
    "remind",
    "定时",
    "cron",
    "recurring",
    "定期",
    "周期",
    "日程",
    "每天",
    "每日",
    "每周",
    "每月",
    "每年",
)

#: The other way: apply a verb meaning "make a thing exist" to whatever the
#: sentence named.  Only consulted alongside a term above -- so "帮我做一个流程"
#: builds, while "帮我做一下调研" does not, because 调研 names no scheduled work
#: and the work was wanted in this turn.  Loose helpers ("帮我做", "帮我搞")
#: are deliberately absent: they are how *any* job is asked for, which is
#: exactly the sentence that was misread into building a task.
_SCHEDULED_WORK_VERBS = (
    "创建",
    "新建",
    "建立一个",
    "建一个",
    "建个",
    "做一个",
    "做个",
    "搞一个",
    "搞个",
    "弄一个",
    "弄个",
    "加一个",
    "加个",
    "来一个",
    "设一个",
    "设置一个",
    "安排",
    "排一个",
    "排个",
    "拆成",
    "拆开",
    "拆分",
    "串成",
    "串起来",
    "发出",
    "发一个",
    "发个",
    "create",
    "make",
    "build",
    "add",
    "set up",
    "setup",
    "arrange",
)

#: Asking about scheduled work is not asking for it.  A question vetoes the
#: cadence half of the rule and never the verb half: "能不能帮我建一个定时任务"
#: still builds, while "我每天都在做订单，这个流程能优化吗" does not.
_SCHEDULED_WORK_QUESTIONS = (
    "怎么",
    "如何",
    "为什么",
    "为啥",
    "哪些",
    "哪几",
    "什么",
    "是不是",
    "有没有",
    "吗",
    "呢",
    "how",
    "what",
    "why",
    "which",
)


def _matches(text: str, terms: Iterable[str]) -> bool:
    lowered = text.casefold()
    return any(term in lowered for term in terms)


#: An ASCII term has to stand alone as a word: "how" is not found inside
#: "show", "add" not inside "address".  Only ASCII counts as a word character,
#: so a Chinese sentence still matches -- "建一个workflow" has to be able to.
_LEFT_BOUNDARY = r"(?<![A-Za-z0-9_-])"
_RIGHT_BOUNDARY = r"(?![A-Za-z0-9_-])"


def _asks_for(text: str, terms: Iterable[str]) -> bool:
    """Whether *text* asks for one of *terms*, rather than merely containing it."""
    lowered = text.casefold()
    for term in terms:
        term = term.casefold()
        if not term.isascii():
            if term in lowered:
                return True
            continue
        if re.search(_LEFT_BOUNDARY + re.escape(term) + _RIGHT_BOUNDARY, lowered):
            return True
    return False


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
        scheduled_run: bool = False,
    ) -> list[dict[str, Any]]:
        names = {str(tool.get("name") or "") for tool in tools}
        selected = names - (
            _MANAGEMENT_TOOLS
            | _SCHEDULE_TOOLS
            | _WORKFLOW_TOOLS
            | _SIGNAL_TOOLS
            | _RUN_SELF_REPORT_TOOLS
            | _DEEP_MEMORY_TOOLS
            | _ORCHESTRATION_TOOLS
            | _SEARCH_EXTRAS
            | _ATTACHMENT_GATED_TOOLS
        )

        if _matches(query, _SCHEDULED_WORK_TERMS):
            selected |= _SCHEDULED_WORK_READ_TOOLS
            # Sending a creator is an invitation to build something, so it is
            # held back until the sentence looks like a request rather than a
            # question -- reading the list is not an invitation, it is what
            # answers the question.  This decides what the turn is *offered*;
            # whether a creation is allowed is decided by the executor, which
            # will refuse a creator call that cannot quote its request.
            asked = _asks_for(query, _SCHEDULED_WORK_VERBS) or (
                _asks_for(query, _SCHEDULED_WORK_CADENCE)
                and not _asks_for(query, _SCHEDULED_WORK_QUESTIONS)
            )
            if asked:
                selected |= _SCHEDULED_WORK_WRITE_TOOLS | _SIGNAL_TOOLS
        if scheduled_run:
            selected |= _RUN_SELF_REPORT_TOOLS | _SIGNAL_TOOLS
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
