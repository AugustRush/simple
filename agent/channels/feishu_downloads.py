"""Attachments on an incoming feishu message: which resources the message names, and fetching them into the input directory. Moved out of FeishuChannel (see the remaining-big-files survey work order). The download itself stays on the channel and arrives as a capability, so a test that swaps it out still intercepts."""

from __future__ import annotations

from agent.channels.feishu_parsing import extract_post_content

from agent.channels.feishu_files import attachment_mime_type, resource_fallback_filename, safe_resource_filename

from agent.core.attachments import MessageAttachment, attachment_kind_for_mime

__all__ = [
    "_extract_message_attachments",
    "_download_message_attachments",
    "_message_attachment_specs",
]


def _message_attachment_specs(
    msg_type: str,
    content_json: dict,
) -> list[tuple[str, str, str]]:
    specs: list[tuple[str, str, str]] = []
    if msg_type == "image":
        key = content_json.get("image_key") or content_json.get("file_key")
        if key:
            filename = safe_resource_filename(
                content_json.get("file_name", ""),
                resource_fallback_filename(str(key), "image"),
            )
            specs.append((str(key), "image", filename))
    elif msg_type == "post":
        _text, image_keys = extract_post_content(content_json)
        for key in image_keys:
            specs.append(
                (
                    str(key),
                    "image",
                    resource_fallback_filename(str(key), "image"),
                )
            )
    elif msg_type in ("file", "audio", "media", "video"):
        key = (
            content_json.get("file_key")
            or content_json.get("media_key")
            or content_json.get("audio_key")
        )
        if key:
            resource_type = "file" if msg_type == "file" else msg_type
            filename = safe_resource_filename(
                content_json.get("file_name", "") or content_json.get("name", ""),
                resource_fallback_filename(str(key), resource_type),
            )
            specs.append((str(key), resource_type, filename))
    return specs


async def _download_message_attachments(
    *,
    message_id: str,
    specs: list[tuple[str, str, str]],
    download_resource,
) -> tuple[MessageAttachment, ...]:
    attachments: list[MessageAttachment] = []
    for resource_key, resource_type, filename in specs:
        path = await download_resource(
            message_id,
            resource_key,
            resource_type,
            filename,
        )
        if path is None:
            continue
        mime_type = attachment_mime_type(path, resource_type)
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        attachments.append(
            MessageAttachment(
                kind=attachment_kind_for_mime(mime_type),
                mime_type=mime_type,
                filename=path.name,
                local_path=path,
                source="feishu",
                source_ref=resource_key,
                size_bytes=size,
                metadata={"message_id": message_id, "resource_type": resource_type},
            )
        )
    return tuple(attachments)


async def _extract_message_attachments(
    *,
    message_id: str,
    msg_type: str,
    content_json: dict,
    download_resource,
) -> tuple[list[tuple[str, str, str]], tuple[MessageAttachment, ...]]:
    """The attachments of one inbound message, and the specs they came from.

        Both halves are returned because the caller needs both: the downloader
        hands back only what it managed to fetch, and the difference is reported
        as a download failure.  This existed as a composition whose only callers
        were its tests, while the message path spelled the same two calls out by
        hand -- so the tests covered a path the product did not take.
        """
    specs = _message_attachment_specs(msg_type, content_json)
    attachments = await _download_message_attachments(
        message_id=message_id,
        specs=specs, download_resource=download_resource
    )
    return specs, attachments
