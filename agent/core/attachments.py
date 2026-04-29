from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


def attachment_kind_for_mime(mime_type: str) -> str:
    normalized = str(mime_type or "").lower()
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("audio/"):
        return "audio"
    if normalized.startswith("video/"):
        return "video"
    if normalized in {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/plain",
        "text/csv",
    }:
        return "document"
    if normalized in {"application/zip", "application/x-tar", "application/gzip"}:
        return "archive"
    return "unknown"


@dataclass(frozen=True)
class MessageAttachment:
    kind: str
    mime_type: str
    local_path: Path
    filename: str = ""
    source: str = ""
    source_ref: str = ""
    size_bytes: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        path = Path(self.local_path)
        object.__setattr__(self, "local_path", path)
        if not self.filename:
            object.__setattr__(self, "filename", path.name)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


def format_attachment_context(attachments: list[MessageAttachment] | tuple[MessageAttachment, ...]) -> str:
    if not attachments:
        return ""
    lines = [
        "用户随消息上传了附件：",
    ]
    for index, attachment in enumerate(attachments, start=1):
        size = (
            f", {attachment.size_bytes} bytes"
            if attachment.size_bytes is not None
            else ""
        )
        lines.append(
            f"{index}. {attachment.kind} ({attachment.mime_type or 'unknown'}{size}): "
            f"{attachment.local_path}"
        )
    lines.append("")
    lines.append(
        "如果当前模型不能直接读取附件，请使用合适的工具或技能读取这些本地文件后再回答。"
    )
    return "\n".join(lines)
