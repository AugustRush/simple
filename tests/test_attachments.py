from __future__ import annotations

from pathlib import Path


def test_attachment_context_lists_local_paths(tmp_path):
    from agent.core.attachments import MessageAttachment, format_attachment_context

    photo = tmp_path / "photo.png"
    photo.write_bytes(b"fake")

    context = format_attachment_context(
        [
            MessageAttachment(
                kind="image",
                mime_type="image/png",
                filename="photo.png",
                local_path=photo,
                source="feishu",
            )
        ]
    )

    assert "用户随消息上传了附件" in context
    assert "image/png" in context
    assert str(photo) in context
    assert "如果当前模型不能直接读取附件" in context


def test_attachment_context_tells_agent_to_transcribe_audio(tmp_path):
    from agent.core.attachments import MessageAttachment, format_attachment_context

    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"fake")

    context = format_attachment_context(
        [
            MessageAttachment(
                kind="audio",
                mime_type="audio/mpeg",
                local_path=audio,
            )
        ]
    )

    assert "transcribe_audio" in context
    assert "read_file" in context


def test_attachment_kind_infers_from_mime_type():
    from agent.core.attachments import attachment_kind_for_mime

    assert attachment_kind_for_mime("image/png") == "image"
    assert attachment_kind_for_mime("application/pdf") == "document"
    assert attachment_kind_for_mime("audio/mpeg") == "audio"
    assert attachment_kind_for_mime("video/mp4") == "video"
    assert attachment_kind_for_mime("") == "unknown"


def test_message_attachment_defaults_filename_from_path(tmp_path):
    from agent.core.attachments import MessageAttachment

    path = tmp_path / "report.pdf"
    path.write_bytes(b"pdf")

    attachment = MessageAttachment(
        kind="document",
        mime_type="application/pdf",
        local_path=path,
    )

    assert attachment.filename == "report.pdf"
    assert isinstance(attachment.local_path, Path)


def test_turn_input_preserves_attachments(tmp_path):
    from agent.core.attachments import MessageAttachment
    from agent.runtime import TurnInput

    path = tmp_path / "photo.png"
    path.write_bytes(b"fake")
    attachment = MessageAttachment(
        kind="image",
        mime_type="image/png",
        local_path=path,
    )

    turn_input = TurnInput.from_text("describe", attachments=[attachment])

    assert turn_input.attachments == (attachment,)
