"""The feishu side of a reply: creating the streaming card, updating its text, closing it, and uploading a file into the conversation. Moved out of FeishuOutputSink (see the remaining-big-files survey work order). The client, the upload limit and the latency tracer arrive as collaborators; the send itself stays on the sink and is passed in, so its own state (the first-reply flag) keeps working."""

from __future__ import annotations
import uuid
import time
import json

from pathlib import Path

__all__ = [
    "_create_streaming_card_sync",
    "_stream_update_text_sync",
    "_close_streaming_mode_sync",
    "_upload_file_sync",
]


FEISHU_UPLOAD_MAX_BYTES = 30 * 1024 * 1024

_STREAM_ELEMENT_ID = "streaming_md"


def _create_streaming_card_sync(
    *,
    client,
    do_send,
    trace_latency,
    logger,
) -> str | None:
    """Create and send a CardKit streaming card, returning the card id."""
    from lark_oapi.api.cardkit.v1 import (  # type: ignore[import]
        CreateCardRequest,
        CreateCardRequestBody,
    )

    started_at = time.perf_counter()
    card_id: str | None = None
    card_json = {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
            "update_multi": True,
            "streaming_mode": True,
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "",
                    "element_id": _STREAM_ELEMENT_ID,
                }
            ]
        },
    }
    try:
        req = (
            CreateCardRequest.builder()
            .request_body(
                CreateCardRequestBody.builder()
                .type("card_json")
                .data(json.dumps(card_json, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = client.cardkit.v1.card.create(req)
        if not resp.success():
            logger.warning(
                "Feishu streaming card create failed: code=%s msg=%s",
                resp.code,
                resp.msg,
            )
            return None

        card_id = getattr(resp.data, "card_id", None)
        if not card_id:
            return None

        do_send(
            "interactive",
            json.dumps({"type": "card", "data": {"card_id": card_id}}),
        )
        return card_id
    except Exception as exc:
        logger.warning("Feishu streaming card create error: %s", exc)
        return None
    finally:
        trace_latency(
            "stream_card_create_finished",
            card_id=card_id,
            success=bool(card_id),
            duration_ms=f"{(time.perf_counter() - started_at) * 1000:.1f}",
        )


def _stream_update_text_sync(
    card_id: str,
    content: str,
    sequence: int,
    *,
    client,
    trace_latency,
    logger,
) -> bool:
    from lark_oapi.api.cardkit.v1 import (  # type: ignore[import]
        ContentCardElementRequest,
        ContentCardElementRequestBody,
    )

    started_at = time.perf_counter()
    success = False
    try:
        req = (
            ContentCardElementRequest.builder()
            .card_id(card_id)
            .element_id(_STREAM_ELEMENT_ID)
            .request_body(
                ContentCardElementRequestBody.builder()
                .content(content)
                .sequence(sequence)
                .build()
            )
            .build()
        )
        resp = client.cardkit.v1.card_element.content(req)
        if not resp.success():
            logger.warning(
                "Feishu stream update failed: card_id=%s code=%s msg=%s",
                card_id,
                resp.code,
                resp.msg,
            )
            return False
        success = True
        return True
    except Exception as exc:
        logger.warning("Feishu stream update error: %s", exc)
        return False
    finally:
        trace_latency(
            "stream_card_update_finished",
            card_id=card_id,
            sequence=sequence,
            content_len=len(content),
            success=success,
            duration_ms=f"{(time.perf_counter() - started_at) * 1000:.1f}",
        )


def _close_streaming_mode_sync(
    card_id: str,
    sequence: int,
    *,
    client,
    trace_latency,
    logger,
) -> bool:
    from lark_oapi.api.cardkit.v1 import (  # type: ignore[import]
        SettingsCardRequest,
        SettingsCardRequestBody,
    )

    settings_payload = json.dumps({"config": {"streaming_mode": False}})
    started_at = time.perf_counter()
    success = False
    try:
        req = (
            SettingsCardRequest.builder()
            .card_id(card_id)
            .request_body(
                SettingsCardRequestBody.builder()
                .settings(settings_payload)
                .sequence(sequence)
                .uuid(str(uuid.uuid4()))
                .build()
            )
            .build()
        )
        resp = client.cardkit.v1.card.settings(req)
        if not resp.success():
            logger.warning(
                "Feishu close streaming failed: card_id=%s code=%s msg=%s",
                card_id,
                resp.code,
                resp.msg,
            )
            return False
        success = True
        return True
    except Exception as exc:
        logger.warning("Feishu close streaming error: %s", exc)
        return False
    finally:
        trace_latency(
            "stream_card_close_finished",
            card_id=card_id,
            sequence=sequence,
            success=success,
            duration_ms=f"{(time.perf_counter() - started_at) * 1000:.1f}",
        )


def _upload_file_sync(
    file_path: Path,
    *,
    client,
    file_type_map,
    logger,
) -> str | None:
    from lark_oapi.api.im.v1 import (  # type: ignore[import]
        CreateFileRequest,
        CreateFileRequestBody,
    )

    path = Path(file_path)
    file_type = file_type_map.get(path.suffix.lower(), "stream")
    try:
        try:
            size_bytes = path.stat().st_size
        except FileNotFoundError:
            logger.error("Feishu file upload skipped, file not found: %s", path)
            return None
        if size_bytes > FEISHU_UPLOAD_MAX_BYTES:
            logger.error(
                "Feishu file upload skipped, file too large for Feishu upload: path=%s size=%s limit=%s",
                path,
                size_bytes,
                FEISHU_UPLOAD_MAX_BYTES,
            )
            return None
        with path.open("rb") as handle:
            req = (
                CreateFileRequest.builder()
                .request_body(
                    CreateFileRequestBody.builder()
                    .file_type(file_type)
                    .file_name(path.name)
                    .file(handle)
                    .build()
                )
                .build()
            )
            resp = client.im.v1.file.create(req)
            if resp.success():
                return resp.data.file_key
            logger.error(
                "Feishu file upload failed: path=%s size=%s code=%s msg=%s",
                path,
                size_bytes,
                resp.code,
                resp.msg,
            )
    except Exception as exc:
        logger.error("Feishu file upload error: path=%s error=%s", path, exc)
    return None
