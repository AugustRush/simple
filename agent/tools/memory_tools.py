"""The memory tools, apart from the class that hosts forty more.

Why this module exists
----------------------
``memory_write`` / ``set_identity`` / ``memory_read`` / ``memory_search`` /
``memory_index`` / ``memory_clear`` are six of the forty-four tools registered
by ``BuiltinTools``, a 5514-line class.  The survey measured them as the
cheapest cluster to lift out: 84 lines, two pieces of state (the memory store
and the chapter normaliser), and no sibling-method calls except the response
builder.

What they answer with is the one thing this cluster shares with all the others
-- ``{"ok": True/False, ...}``.  That shape is deliberately **not** re-declared
here: the two builders are injected, so the memory tools and the class that
mounts them cannot drift into two spellings of "ok".  Later clusters take the
same two callables, which is how ``BuiltinTools`` becomes a host rather than a
namespace.

Behaviour is preserved exactly, including the consent protocol in ``clear``:
memory is only cleared after an interactive human sink approves, and "nobody to
ask" is an error carrying ``requires_confirmation`` rather than a silent
success.
"""

from __future__ import annotations

import contextlib
from typing import Any, Callable, Optional

# ``clear`` asks the sink serving the current turn for consent.  This is the
# same ContextVar the host class reads, and it has to be imported here rather
# than reached through the old module: a verbatim move copies the body, not the
# names the body was standing on.
from agent.core.output import _active_sink


class MemoryTools:
    """The six memory tools, with the state and responders they need."""

    def __init__(
        self,
        memory: Any,
        chapter_normalizer: Callable[[str], str],
        ok: Callable[..., dict[str, Any]],
        error: Callable[..., dict[str, Any]],
    ) -> None:
        self._memory = memory
        self._normalize_chapter = chapter_normalizer
        self._ok = ok
        self._error = error

    def write(
        self, chapter: str, name: str, content: str, append: bool = False
    ) -> dict[str, Any]:
        self._memory.write(chapter, name, content, append=append)
        normalized = self._normalize_chapter(chapter)
        return self._ok(
            action="append" if append else "write",
            path=f"{normalized}/{name}",
            bytes=len(content.encode("utf-8")),
        )

    def set_identity(
        self,
        subject: str = "assistant",
        name: Optional[str] = None,
        role: Optional[str] = None,
        persona: Optional[str] = None,
    ) -> dict[str, Any]:
        if name is None and role is None and persona is None:
            return self._error(
                "Nothing to set: provide at least one of name, role, or persona."
            )
        try:
            applied = self._memory.set_identity(
                subject=subject, name=name, role=role, persona=persona
            )
        except ValueError as exc:
            return self._error(str(exc))
        return self._ok(subject=subject, applied=applied)

    def read(self, chapter: str, name: str) -> dict[str, Any]:
        content = self._memory.read(chapter, name)
        normalized = self._normalize_chapter(chapter)
        if not content:
            return self._error(f"No memory file: {normalized}/{name}")
        return self._ok(path=f"{normalized}/{name}", content=content)

    def search(self, query: str, top_k: int = 10) -> dict[str, Any]:
        results = self._memory.search(query)
        top_k = max(1, min(int(top_k), 20))
        items = results[:top_k]
        return self._ok(query=query, count=len(items), items=items)

    def index(self) -> dict[str, Any]:
        return self._ok(content=self._memory.read_index())

    async def clear(self) -> dict[str, Any]:
        """Clear memory only after consent from the active human sink."""
        from agent.core.output import _APPROVAL_LOCK

        sink = _active_sink.get()
        if sink is None or not getattr(sink, "interactive_confirmation", False):
            return self._error(
                "Clearing memory requires interactive human approval.",
                requires_confirmation=True,
            )
        async with _APPROVAL_LOCK:
            approved = await sink.on_tool_confirmation(
                "memory_clear",
                command=(
                    "永久清空全部长期记忆、可检索对话历史、事实索引、"
                    "工作状态和待整理记忆"
                ),
                risk_level="high",
                reason="该操作不可恢复",
                confirmation_token="",
                scope=None,
            )
        if not approved:
            return self._error("Memory clear was declined by the user.")

        context_manager = None
        with contextlib.suppress(Exception):
            from agent.core.agent import _active_agent_context

            active_ctx = _active_agent_context.get()
            context_manager = (
                active_ctx.metadata.get("context_manager")
                if active_ctx is not None
                else None
            )
        if context_manager is not None:
            context_manager.on_memory_cleared()
        deleted = self._memory.clear()
        return self._ok(
            action="clear",
            deleted=sum(deleted.values()),
            deleted_by_store=deleted,
        )
