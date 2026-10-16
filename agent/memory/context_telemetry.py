"""Telemetry and persistence at the end of a turn: recording what the turn did, what it cost, and where the provider checkpoint was left. Moved out of ContextManager (see the remaining-big-files survey work order). The store, the staging buffer and the consolidation engine arrive as collaborators."""

from __future__ import annotations

from agent import shared
from .models import ConversationWriteResult

from typing import Any, Optional, Sequence

__all__ = [
    "record_turn_result",
    "load_provider_checkpoint",
    "observe_token_usage",
    "record_usage_event",
]


def record_turn_result(
    *,
    user_content: str,
    assistant_content: str = "",
    channel: str = "",
    message_id: str = "",
    assistant_message_id: str = "",
    reply_to_id: str = "",
    metadata: Optional[dict[str, Any]] = None,
    interjections: Optional[Sequence[dict[str, Any]]] = None,
    store,
    staging,
) -> ConversationWriteResult:
    """Persist an exchange and expose which event rows were created."""
    session_id = staging.session_id
    write_result = store.write_conversation_exchange(
        session_id=session_id,
        user_content=user_content,
        assistant_content=assistant_content,
        channel=channel,
        message_id=message_id,
        assistant_message_id=assistant_message_id,
        reply_to_id=reply_to_id,
        metadata=metadata,
        interjections=interjections,
    )
    return write_result


def load_provider_checkpoint(
    *,
    store,
    staging,
) -> dict[str, Any] | None:
    load = getattr(store, "load_provider_checkpoint", None)
    if not callable(load):
        return None
    return load(staging.session_id)


def observe_token_usage(
    *,
    predicted: int,
    actual: int,
    fingerprint: str = "",
    head_estimate: int = 0,
    body_raw: int = 0,
    consolidation,
) -> None:
    """Feed one call's usage back to the engine that calibrates the estimator.

        The choice between the split-aware observer and the plain one belongs to the
        engine: the turn loop used to make it through two ``getattr`` calls on
        ``manager.consolidation``, which is a consumer reading a collaborator of this
        service rather than the service answering a question.
        """
    consolidation = consolidation
    observe_split = getattr(consolidation, "observe_payload_usage", None)
    if callable(observe_split) and fingerprint:
        with shared._suppress_with_log("token calibration skipped"):
            observe_split(
                fingerprint=fingerprint,
                head_estimate=head_estimate,
                body_raw=body_raw,
                actual=actual,
            )
        return
    observe_plain = getattr(consolidation, "observe_actual_usage", None)
    if callable(observe_plain):
        with shared._suppress_with_log("token calibration skipped"):
            observe_plain(predicted, actual)


def record_usage_event(
    *,
    session_id: str,
    phase: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    turn_id: str = "",
    metadata: Optional[dict[str, Any]] = None,
    store,
) -> None:
    """Persist one provider-usage row through the store, when it keeps them.

        The store's ``append_usage_event`` is deliberately outside ``MemoryStore`` (it is
        accounting rather than memory), so this member is where that optional capability
        is allowed to be *optional*: the consumer asks the service, and a service whose
        store does not keep usage rows does nothing.
        """
    record = getattr(store, "append_usage_event", None)
    if not callable(record):
        return
    with shared._suppress_with_log("provider usage persistence skipped"):
        record(
            session_id=session_id,
            turn_id=turn_id,
            phase=phase,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            metadata=metadata,
        )
