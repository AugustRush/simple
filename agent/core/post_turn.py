"""What is owed after a turn: staging, compaction, consolidation.

Extracted from ``BaseAgent`` (see the remaining-big-files survey).  It was already a
``@staticmethod`` taking everything it needs as keyword arguments and reading nothing off the
instance -- a free function in all but syntax, and 201 lines of the class's size for no reason
that survives reading it.

It stays reachable as ``BaseAgent._post_turn_maintenance`` because the turn loop, the CLI and
several tests call it there; the implementation is here.
"""

from __future__ import annotations

from typing import Any

from agent import shared
from agent.core.turn_context import TURN_IN_PROGRESS_KEY

__all__ = ["post_turn_maintenance"]


def post_turn_maintenance(
    *,
    ctx_mgr: Any,
    agent: "BaseAgent",
    ctx: "AgentContext",
    user_content: str,
    assistant_content: str,
    channel: str = "",
    record_kwargs: dict[str, Any] | None = None,
    memory_worker: Any = None,
    system_prompt: str = "",
    task_context: str = "",
    error: str = "",
) -> None:
    """Shared turn-post-processing: staging, compaction, consolidation.

    Called by both the CLI interactive loop and the ChannelRunner's
    message handler after every agent turn.  Keeping this in one place
    prevents the two call sites from diverging.
    """
    metadata = getattr(ctx, "metadata", {})
    agent_max_tokens = int(getattr(agent, "max_tokens", 0) or 0)
    default_input_budget = max(
        2048,
        int(getattr(agent, "context_window", 0) or 0) - agent_max_tokens,
    )
    input_token_budget = int(
        metadata.get("_last_input_token_budget")
        or default_input_budget
    )
    # Read here, not at the compaction block below: `metadata` is rebound to
    # the caller's record_kwargs partway through this method, and the mark
    # lives on `ctx.metadata`.  See `TURN_IN_PROGRESS_KEY`.
    between_turns = not metadata.get(TURN_IN_PROGRESS_KEY)
    if ctx_mgr:
        consume_suppression = getattr(
            ctx_mgr, "consume_memory_clear_suppression", None
        )
        if callable(consume_suppression) and consume_suppression():
            save_checkpoint = getattr(ctx_mgr, "save_provider_checkpoint", None)
            if callable(save_checkpoint):
                with shared._suppress_with_log("provider checkpoint save skipped"):
                    checkpoint = save_checkpoint(
                        getattr(ctx, "messages", []),
                        token_budget=max(2048, min(24000, input_token_budget)),
                    )
                    if isinstance(checkpoint, dict) and isinstance(metadata, dict):
                        ctx.metadata["_checkpoint_summary"] = str(
                            checkpoint.get("summary") or ""
                        )
                        ctx.metadata["_has_provider_checkpoint"] = True
            return
        if record_kwargs is None:
            record_kwargs = {}
        # The messages the user sent while this turn was running.  They are
        # part of what this turn answered, so they are journaled with it,
        # between the request and the answer -- see
        # `MemoryStore.write_conversation_exchange`.  Read (not cleared)
        # from metadata: a turn that drained the mailbox twice wrote both
        # batches, and clearing here would drop the second on the floor if
        # this method ever ran twice.
        interjections = (
            metadata.get("_turn_interjections")
            if isinstance(metadata, dict)
            else None
        )
        if not isinstance(interjections, list):
            interjections = []
        record_turn_result = getattr(ctx_mgr, "record_turn_result", None)
        if callable(record_turn_result):
            write_result = record_turn_result(
                user_content=user_content,
                assistant_content=assistant_content or "",
                channel=channel,
                interjections=interjections,
                **record_kwargs,
            )
            new_turn = bool(getattr(write_result, "changed", False))
            stage_user = bool(
                getattr(write_result, "user_created", False)
                or getattr(write_result, "first_assistant_for_user", False)
            )
            stage_assistant = bool(
                getattr(write_result, "assistant_created", False)
            )
            # The user's words, in the order they said them: the memory
            # pipeline is built from what the user said, and an interjection
            # is something they said.  Only the rows this call created, so a
            # retried turn does not stage the same text twice.
            stage_interjections = [
                str(text)
                for text in getattr(write_result, "interjections_created", ()) or ()
                if str(text).strip()
            ]
        else:
            fallback_kwargs = dict(record_kwargs)
            fallback_kwargs.pop("assistant_message_id", None)
            recorded = ctx_mgr.record_turn(
                user_content=user_content,
                assistant_content=assistant_content or "",
                channel=channel,
                **fallback_kwargs,
            )
            new_turn = recorded is not False
            stage_user = new_turn
            stage_assistant = new_turn and bool(assistant_content)
            stage_interjections = []
        record_runtime_event = getattr(ctx_mgr, "record_runtime_event", None)
        if new_turn and callable(record_runtime_event):
            metadata = (
                record_kwargs.get("metadata", {})
                if isinstance(record_kwargs, dict)
                else {}
            )
            if not isinstance(metadata, dict):
                metadata = {}
            turn_id = str(
                record_kwargs.get("message_id")
                or metadata.get("turn_id")
                or metadata.get("message_id")
                or ""
            )
            record_runtime_event(
                "turn_finished",
                {
                    "user_content": user_content,
                    "assistant_content": assistant_content or "",
                    "error": error or "",
                    "channel": channel,
                    "metadata": record_kwargs,
                },
                turn_id=turn_id,
            )
        if new_turn:
            if stage_user:
                ctx_mgr.stage_turn("user", user_content)
            # Interjections are the user's own words, spoken during this
            # turn: the staging buffer is what consolidation reads, so
            # leaving them out would make the memory a record of only the
            # sentences a turn happened to *start* with.
            for text in stage_interjections:
                ctx_mgr.stage_turn("user", text)
            if stage_assistant:
                ctx_mgr.stage_turn("assistant", assistant_content)
            if ctx_mgr.should_enqueue_consolidation():
                ctx_mgr.enqueue_consolidation("staged_turns")
    # Post-turn compaction serves two purposes:
    # 1. Trim working memory when a session-level context manager
    #    has a lower threshold than the agent default (multi-tenant).
    # 2. Wake the background memory worker so staged content gets
    #    consolidated without delaying the interactive loop.
    # The pre-loop check in send_message handles the common case.
    #
    # Both edits below rewrite a payload that a provider's prefix cache is
    # holding: compaction cuts the front of the body, and `_with_task_context`
    # rebuilds the head.  Each is correct once per *turn*, but a turn issues
    # one provider call per step and those calls are meant to share a prefix,
    # so doing either one mid-turn re-bills the rest of the turn in full.
    # `between_turns` makes "only between turns" a precondition instead of an
    # assumption -- the sole production caller (`complete_turn`) already
    # satisfies it, and a future caller inside a tool loop would otherwise
    # reintroduce the miss silently.  Everything after this block -- turn
    # recording, staging, the checkpoint -- touches no payload and is
    # deliberately not gated.
    if (
        between_turns
        and ctx_mgr
        and ctx_mgr.should_compact_messages(
            ctx.messages, input_token_budget=input_token_budget
        )
    ):
        ctx.messages = ctx_mgr.compact_messages(
            ctx.messages, input_token_budget=input_token_budget
        )
        if system_prompt:
            # Compaction may summarize away the message that carried the
            # original request; keep it reachable via the task context.
            # Same rule as RuntimeSessionState.set_session_prompt — the
            # runtime layer owns the prompt during a turn; maintenance
            # only rebuilds it after compaction, outside any turn.
            ctx.system_prompt = shared._with_task_context(
                system_prompt, task_context
            )
        if (
            memory_worker is not None
            and ctx_mgr.has_enough_staged()
        ):
            ctx_mgr.enqueue_consolidation("compact_triggered")
            memory_worker.wake()
    if ctx_mgr:
        save_checkpoint = getattr(ctx_mgr, "save_provider_checkpoint", None)
        if callable(save_checkpoint):
            with shared._suppress_with_log("provider checkpoint save skipped"):
                checkpoint = save_checkpoint(
                    ctx.messages,
                    token_budget=max(2048, min(24000, input_token_budget)),
                )
                if isinstance(checkpoint, dict) and isinstance(metadata, dict):
                    ctx.metadata["_checkpoint_summary"] = str(
                        checkpoint.get("summary") or ""
                    )
                    ctx.metadata["_has_provider_checkpoint"] = True
