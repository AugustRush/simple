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

Behaviour is preserved exactly, including the consent protocol in ``clear``.
That protocol changed once, deliberately: what used to be "an interactive sink
must approve, otherwise refuse" is now "any human surface can approve", because
consent became a shared record rather than a live callback.  A chat channel can
answer a ``memory_clear`` question now; before, the operation was impossible
there rather than merely awkward.
"""

from __future__ import annotations

import contextlib
from typing import Any, Callable, Optional

# ``clear`` asks the sink serving the current turn for consent.  This is the
# same ContextVar the host class reads, and it has to be imported here rather
# than reached through the old module: a verbatim move copies the body, not the
# names the body was standing on.
from agent.core.output import _active_sink

#: The consent kind and rule for one destructive operation.  ``tool:`` rather
#: than ``command:`` because what is granted is a tool call, not a shell line;
#: the value is the tool's id, which is also what makes the record legible in
#: ``/trust`` ("这个工具：memory_clear").
_MEMORY_CLEAR_ID = "memory_clear"
_MEMORY_CLEAR_KIND = "memory_clear"
_MEMORY_CLEAR_SUMMARY = (
    "永久清空全部长期记忆、可检索对话历史、事实索引、工作状态和待整理记忆"
)


def memory_clear_rule() -> str:
    """The rule one memory-clear answer remembers, in the shared vocabulary."""
    from agent.security import consent

    return consent.make_rule(consent.TOOL, _MEMORY_CLEAR_ID)


def memory_clear_pending_records(scope: Any) -> list[Any]:
    """Outstanding memory-clear questions for *scope*, oldest first.

    Listed by the command coordinator so the token this tool returns can be
    answered on a chat channel -- the whole point of minting one.
    """
    from agent.security import consent

    return [
        record
        for record in consent.SHELL.pending(scope)
        if record.kind == _MEMORY_CLEAR_KIND
    ]


class MemoryTools:
    """The six memory tools, with the state and responders they need."""

    def __init__(
        self,
        registry: Any,
        memory: Any,
        chapter_normalizer: Callable[[str], str],
        ok: Callable[..., dict[str, Any]],
        error: Callable[..., dict[str, Any]],
    ) -> None:
        # Public on purpose: ``ToolRegistry.call`` reads ``fn.__self__.registry``
        # to install the calling registry's merged context on the registry this
        # tool implementation reads.  A cluster that hid it would silently stop
        # receiving a session's own write scope, workspace and output dir.
        self.registry = registry
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
        """Clear memory only after an explicit human answer, from any surface.

        Three ways in, one way out: a recorded approval from an earlier answer,
        a live prompt on a surface that can ask, or a token the human answers
        elsewhere and then the agent retries.  All three end in
        :meth:`_perform_clear`.
        """
        from agent.core.output import _APPROVAL_LOCK

        # Consent is a record, not a live callback, so a channel that cannot put
        # a question on screen can still be answered.  This tool used to refuse
        # outright there ("requires interactive human approval"), which made
        # memory clear *impossible* on a chat channel rather than merely
        # inconvenient; now it asks the same way the shell and plugin tools do --
        # mint a pending approval, refuse this call, and let the retry that
        # follows the human's answer find the record.
        from agent.security.tool_approval import authorization_scope

        scope = authorization_scope()
        from agent.security import consent

        rule = memory_clear_rule()
        if consent.SHELL.approved(rule, scope=scope):
            return await self._perform_clear(rule, scope)

        sink = _active_sink.get()
        can_ask = sink is not None and bool(
            getattr(sink, "interactive_confirmation", False)
        )
        approved: Optional[bool] = None
        if can_ask:
            async with _APPROVAL_LOCK:
                approved = await sink.on_tool_confirmation(
                    "memory_clear",
                    command=_MEMORY_CLEAR_SUMMARY,
                    risk_level="high",
                    reason="该操作不可恢复",
                    confirmation_token="",
                    scope=None,
                )
        if approved is True:
            # The interactive surface already asked and was answered; record it
            # so the two paths arrive at the same place.
            consent.SHELL.remember(rule, consent.ONCE, scope=scope)
            return await self._perform_clear(rule, scope)
        if approved is False:
            return self._error("Memory clear was declined by the user.")

        # Nobody could be asked, or nobody answered: leave a question behind
        # instead of a refusal, and name the token so it can be answered from
        # wherever the human is.
        token = consent.SHELL.ask(
            kind=_MEMORY_CLEAR_KIND,
            summary=_MEMORY_CLEAR_SUMMARY,
            rule=rule,
            scope=scope,
        )
        return self._error(
            "Clearing memory needs an explicit human answer, and none was "
            f"available here. Ask the user to confirm with /confirm {token} "
            "(or to reply with the approval), then call memory.clear again.",
            requires_confirmation=True,
            confirmation_token=token,
        )

    async def _perform_clear(self, rule: str, scope: Any) -> dict[str, Any]:
        """Do the clear, then spend the approval that authorised it.

        Separate so every consent path ends in the same call -- and the
        approval is *consumed*, because an answer to "may I destroy everything"
        authorises that destruction, not any destruction for the next five
        minutes.  A remembered window is right for a shell command; it is not
        right for this.
        """
        from agent.security import consent

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
        consent.SHELL.revoke(rule, scope=scope)
        return self._ok(
            action="clear",
            deleted=sum(deleted.values()),
            deleted_by_store=deleted,
        )
