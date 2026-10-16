"""Turning a provider failure into something a reader can act on: the account-exhaustion check and the message that goes with it. Moved out of BaseAgent (see the remaining-big-files survey work order)."""

from __future__ import annotations

from agent.core.llm_errors import is_account_exhausted
import asyncio

from agent.memory.system import ContextLimitError

__all__ = [
    "_format_agent_error",
    "_is_account_exhausted",
]


def _format_agent_error(
    exc: Exception,
) -> str:
    if isinstance(exc, ContextLimitError):
        return f"ContextLimitError: {exc}"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "Model request timed out"
    if isinstance(exc, ValueError):
        return f"Invalid model request: {exc}"
    text = str(exc)
    # Account-level exhaustion (spent quota, empty balance) reads like a
    # transient 429 but is not: the retry loop already refuses to replay
    # it, and what the user needs is to be told *that*, not the raw body.
    if _is_account_exhausted(exc):
        # The provider's own body carries the one thing the user cannot
        # guess -- when the window reopens -- so quote it instead of
        # paraphrasing: "It will reset at 2026-09-23 00:03:07 +0800 CST".
        reset_hint = ""
        at = text.lower().find("reset")
        if at != -1:
            clause = text[at:at + 80].split(".")[0].strip()
            reset_hint = f"，配额重置时间：{clause}"
        return (
            "模型服务账户不可用：当前 provider 的配额已用尽或账户已欠费，"
                f"请求没有被发送到模型，重试不会恢复{reset_hint}。"
                "请在配置里换一个仍有额度的 provider/模型，或为该账户充值。"
                f"原始错误：{text}"
        )
    # A model id reached an endpoint that does not serve it.  The raw
    # 400 tells the user which names the endpoint wanted, but not that
    # the fix is a config one -- the model's provider ownership in
    # config.json, or the model picker -- which is the thing to say.
    if "supported api model names" in text.lower() and "you passed" in text.lower():
        return (
            "模型被发送到了不支持它的接口（400）。"
                "这通常是配置问题：检查 config.json 里该模型归属的 provider"
                "（models/default_model），或在模型选择器里换一个当前服务支持的模型。"
                f"原始错误：{text}"
        )
    # The endpoint answered with a web page.  Measured once in this
    # project's run history: 5663 characters of opencode's own "Not Found"
    # page (title `Not Found | opencode`) stored verbatim as the run's
    # error -- which says nothing, because the interesting fact is that the
    # request reached a site and not an API route.  Whose page it is comes
    # from the title, and that is what makes the message usable.
    if text.lstrip().startswith("<") and "<html" in text[:512].lower():
        title = ""
        lowered = text.lower()
        opened = lowered.find("<title")
        if opened != -1:
            start = lowered.find(">", opened)
            end = lowered.find("</title", start + 1) if start != -1 else -1
            if end != -1:
                title = " ".join(text[start + 1:end].split())[:80]
        named = f"，页面标题：{title}" if title else ""
        return (
            f"模型接口返回的是网页而不是 API 响应（共 {len(text)} 字符{named}）。"
                "这通常说明该 provider 的 base_url 指向了站点首页或一个不存在的路径，"
                "而不是 API 路由：先确认 base_url，再重试。"
                f"响应开头：{text[:200]}"
        )
    return text or exc.__class__.__name__


def _is_account_exhausted(exc: Exception) -> bool:
    """See :func:`agent.core.llm_errors.is_account_exhausted`."""
    return is_account_exhausted(exc)
