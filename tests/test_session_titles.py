"""A conversation's name, from its first sentence.

Why this exists
---------------
Every row in the sidebar read "未命名会话" beside a session id.  Twenty
conversations, none of them identifiable: the only way to find one was to open it
and read.  Nothing in the product ever wrote a title -- ``rename_session``
existed, but only a person typing one ever called it.

The first message of a conversation is the best title it will ever have, and it is
already paid for.  So the title is derived from it, mechanically:

* **no model call** -- a round-trip on every new conversation to produce a worse
  summary than the sentence itself, and the latency would land exactly where the
  page is waiting for its first token;
* **a default, not a decision** -- ``ensure_auto_title`` only writes when the title
  is still empty, so a conversation somebody renamed keeps that name;
* **one line, always** -- a pasted block or a code fence titles the conversation by
  what it opens with, not by its body.
"""

from __future__ import annotations

import pytest

from agent.session_service import SessionService


# ── the derivation ──────────────────────────────────────────────────────────


def test_a_sentence_becomes_the_title() -> None:
    assert SessionService.title_from_message("帮我每天早上九点提醒我看日志") == (
        "帮我每天早上九点提醒我看日志"
    )


def test_the_title_is_the_first_line_only() -> None:
    """A pasted block titles the conversation by what it opens with."""
    assert (
        SessionService.title_from_message(
            "帮我建一个自动化任务：\n- 要做什么：读日志\n- 什么时候做：每天早上"
        )
        == "帮我建一个自动化任务："
    )


def test_markdown_markers_are_stripped() -> None:
    """A title is text in a sidebar row; ``##`` and ``-`` would show as characters."""
    assert SessionService.title_from_message("## 总结这个仓库") == "总结这个仓库"
    assert SessionService.title_from_message("- 先看 README") == "先看 README"
    assert SessionService.title_from_message("> 引用一段话") == "引用一段话"


def test_a_code_fence_is_skipped_for_the_line_after_it() -> None:
    assert (
        SessionService.title_from_message("```python\nprint(1)\n```")
        == "print(1)"
    )


def test_whitespace_is_collapsed() -> None:
    """A title is one line, always -- two spaces in a row would wrap the row."""
    assert SessionService.title_from_message("  帮   我   看 看  ") == "帮 我 看 看"


def test_a_long_first_line_is_cut_at_a_boundary() -> None:
    """Cut mid-word reads as broken; the ellipsis says "abbreviated"."""
    # 60+ characters: longer than any sidebar row can show, so it has to be cut.
    title = SessionService.title_from_message(
        "帮我把这个仓库里所有跟调度器有关的测试都重新跑一遍，然后把每一个失败的原因"
        "都写清楚，最后再给我一个按优先级排好的修复计划"
    )
    assert title.endswith("…")
    assert len(title) <= SessionService.AUTO_TITLE_LIMIT + 1


def test_an_empty_message_yields_no_title() -> None:
    """Nothing to say is not a reason to write "未命名" into the store."""
    assert SessionService.title_from_message("") == ""
    assert SessionService.title_from_message("   \n  \n") == ""


# ── the write rule ──────────────────────────────────────────────────────────


class _Store:
    """Just the two calls the service makes: read the titles, set one."""

    def __init__(self) -> None:
        self.titles: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []

    def list_session_titles(self) -> dict[str, str]:
        return dict(self.titles)

    def set_session_title(self, session_id: str, title: str) -> None:
        self.titles[session_id] = title
        self.writes.append((session_id, title))


def _service() -> tuple[SessionService, _Store]:
    store = _Store()
    service = SessionService(store=store)
    service._store_for_session = lambda session_id: store  # type: ignore[attr-defined]
    return service, store


def test_the_first_message_names_the_conversation() -> None:
    service, store = _service()
    title = service.ensure_auto_title("s1", "帮我查一下昨天的构建失败")

    assert title == "帮我查一下昨天的构建失败"
    assert store.writes == [("s1", "帮我查一下昨天的构建失败")]


def test_a_name_somebody_chose_is_not_overwritten() -> None:
    """The rule that makes the default safe: it only fills a gap."""
    service, store = _service()
    store.titles["s1"] = "我自己起的名"

    title = service.ensure_auto_title("s1", "帮我查一下昨天的构建失败")

    assert title == "我自己起的名"
    assert store.writes == [], "an existing title must not be rewritten"


def test_a_second_message_does_not_rename_the_conversation() -> None:
    """Once named, the conversation keeps the name of the turn that opened it."""
    service, store = _service()
    service.ensure_auto_title("s1", "第一句")
    service.ensure_auto_title("s1", "第二句")

    assert store.titles["s1"] == "第一句"
    assert len(store.writes) == 1


def test_an_empty_first_message_writes_nothing() -> None:
    service, store = _service()
    assert service.ensure_auto_title("s1", "   ") == ""
    assert store.writes == []


# ── the wiring: a title never breaks a turn ─────────────────────────────────


def test_a_title_failure_does_not_fail_the_turn() -> None:
    """``_handle_text`` treats the title as a convenience, not as part of the turn.

    A store that cannot write titles is a composition detail; a conversation that
    refuses to start because it could not be named is a worse answer than an
    unnamed one.
    """
    import asyncio

    from agent.channels.web import WebChannel, WebConfig

    class _Service:
        def __init__(self) -> None:
            self.touched: list[tuple[str, str]] = []
            self.titles: list[tuple[str, str]] = []

        def touch_session(self, session_id: str, *, status: str = "active") -> None:
            self.touched.append((session_id, status))

        def ensure_auto_title(self, session_id: str, text: str) -> str:
            self.titles.append((session_id, text))
            raise RuntimeError("titles are broken today")

    channel = WebChannel(WebConfig())
    service = _Service()
    channel._service = lambda: service  # type: ignore[assignment]

    seen: list[str] = []

    async def handler(message, sink):
        seen.append(message.text)

    channel._handler = handler  # type: ignore[assignment]

    class _Sink:
        def write(self, text: str) -> None:  # pragma: no cover - not used
            pass

    asyncio.run(
        channel._handle_text("s1", "帮我看看日志", _Sink(), message_id="m1")
    )

    assert seen == ["帮我看看日志"], "the turn must still run"
    assert service.titles == [("s1", "帮我看看日志")], "it did try to name it"
