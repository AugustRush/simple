"""The context tools: searching long-term memory, and clearing the window.

Why this module exists
----------------------
Two tools that are about what the model can *see*: ``context_retrieve`` searches
the conversation history by one or more phrasings, and ``clear_context`` asks
the agent loop to reset the window, carrying a summary and the current request
forward.

They were methods on ``BuiltinTools`` -- and among the least guarded in it.  The
survey measured two tests; the behaviours below were only reachable through
them, which is why this move added a direct test module rather than trust two.

Three rules travelled with the code, each of which was written because the
obvious version is wrong:

- **``queries`` is plural on purpose, and a bare string is accepted on
  purpose.**  The dominant retrieval failure is not ranking but that one
  unmodified question is a poor query against a lexical index -- measured, 7 of
  7 stage-1 misses were recoverable by re-asking in the memory's own wording.
  And a model filling an array parameter with a single string is routine:
  iterating that string would yield its *characters*, silently turning one good
  query into dozens of one-character ones.
- **The manager is resolved from the active turn, not from construction.**
  Tool definitions are shared across multiplexed sessions, so a captured
  bootstrap manager would be the wrong session's -- and its staging session is
  not user-facing.
- **An empty result is a hint, not an error.**  It is far more often a
  badly-shaped query than an absent memory, and without saying so the model
  concludes "no record" and tells the user that.
"""

from __future__ import annotations

import contextlib
from typing import Any, Callable, Optional


class ContextTools:
    """The context tools, with the manager and responders they need."""

    def __init__(
        self,
        registry: Any,
        context_manager: Optional[Any],
        ok: Callable[..., dict[str, Any]],
        error: Callable[..., dict[str, Any]],
    ) -> None:
        # Public on purpose: ``ToolRegistry.call`` reads ``fn.__self__.registry``
        # to install the calling registry's merged context on the registry this
        # tool implementation reads.  A cluster that hid it would silently stop
        # receiving a session's own context manager.
        self.registry = registry
        self._context_manager = context_manager
        self._ok = ok
        self._error = error

    def retrieve(
        self,
        queries: Optional[list[str] | str] = None,
        query: str = "",
        top_k: int = 5,
    ) -> dict[str, Any]:
        """Search LTM with one or more phrasings.

        Plural by design.  The dominant retrieval failure is not ranking but
        that a single unmodified question is a poor query against a lexical
        index; measured on the eval set, 7 of 7 stage-1 misses were
        recoverable by re-asking in the memory's own wording or language.
        Encoding that in the parameter shape makes reformulation the default
        path, which prose in a tool description does not reliably achieve.

        ``queries`` accepts a bare string as well as a list.  A model filling
        an array parameter with a single string is routine, and iterating a
        str yields its characters — silently turning one good query into
        dozens of one-character ones that match everything.
        """
        if isinstance(queries, str):
            queries = [queries] if queries.strip() else []
        wanted = [str(q).strip() for q in (queries or []) if str(q).strip()]
        if query and str(query).strip():
            wanted.append(str(query).strip())
        # Preserve order, drop duplicates.
        seen: set[str] = set()
        wanted = [q for q in wanted if not (q in seen or seen.add(q))]
        if not wanted:
            return self._error(
                "Provide `queries` (preferred) or `query`.",
                hint="Pass 2-4 phrasings, including the other language.",
            )
        context_manager = self._context_manager
        current_turn_id = ""
        # Tool definitions are shared across multiplexed sessions. Resolve the
        # manager from the active turn instead of capturing the bootstrap
        # manager, whose staging session is not user-facing.
        with contextlib.suppress(Exception):
            from agent.core.agent import _active_agent_context

            active_ctx = _active_agent_context.get()
            if active_ctx is not None:
                context_manager = active_ctx.metadata.get(
                    "context_manager",
                    context_manager,
                )
                current_turn_id = str(active_ctx.metadata.get("turn_id") or "")
        if context_manager is None:
            return self._error("Context manager not available.")
        result = context_manager.retrieve_context(
            wanted,
            top_k=top_k,
            exclude_message_id=current_turn_id,
        )
        sections = [s for s in result.split("\n\n") if s.strip()] if result else []
        if not sections:
            # Feedback at the point of failure: an empty result is far more
            # often a badly-shaped query than an absent memory, and without
            # saying so the model concludes "no record" and tells the user.
            return self._ok(
                queries=wanted,
                count=0,
                content="",
                sections=[],
                hint=(
                    "Nothing matched. This index is lexical, so try again with "
                    "different WORDS before concluding the memory is absent: "
                    "the other language, the concrete nouns the memory would "
                    "use, or a name rather than a description."
                ),
            )
        return self._ok(
            queries=wanted,
            count=len(sections),
            content=result,
            sections=sections,
        )

    def clear(self, summary: str = "") -> dict[str, Any]:
        """Request a context reset after the current tool batch finishes.

        The agent loop applies the reset after all concurrently requested tools
        return. Mutating ctx.messages from inside one concurrent tool would
        leave sibling tool results detached from their assistant tool-call.
        """
        try:
            from agent.core.agent import _active_agent_context

            ctx = _active_agent_context.get()
            if ctx is None or not ctx.messages:
                return self._error(
                    "No active agent context available to clear. "
                    "This tool must be used during an active conversation."
                )

            def _text_from_user_message(msg: dict[str, Any]) -> str:
                content = msg.get("content", "")
                if isinstance(content, list):
                    # Anthropic tool results are represented as role=user
                    # messages too; skip those when finding the active task.
                    if any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in content
                    ):
                        return ""
                    return "\n".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                return str(content or "")

            # Find the latest real user request. In a multi-turn conversation,
            # the first user message may belong to an old task.
            current_user = None
            for msg in reversed(ctx.messages):
                if msg.get("role") == "user":
                    content = _text_from_user_message(msg).strip()
                    if content and str(content).strip():
                        current_user = str(content).strip()
                        break

            if not current_user:
                return self._error("No current user message found in context.")

            # Build a clean context with the summary and current task.
            summary_text = str(summary or "").strip()
            restart_message = (
                "[Context has been cleared to reduce token usage.]\n\n"
            )
            if summary_text:
                restart_message += (
                    f"## Summary of work so far\n{summary_text}\n\n"
                )
            restart_message += (
                f"## Current request\n{current_user}\n\n"
                "Please continue from where you left off, using the summary "
                "above as context for what has already been done."
            )

            return self._ok(
                clear_context_requested=True,
                restart_message=restart_message,
                current_request=current_user[:500],
                summary_provided=bool(summary_text),
            )
        except ImportError:
            return self._error(
                "clear_context is not available — agent runtime not accessible."
            )
        except Exception as e:
            return self._error(f"Failed to clear context: {e}")
