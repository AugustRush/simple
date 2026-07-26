"""Tests for channels/feishu.py — FeishuOutputSink, FeishuConfig, helpers,
and the package-backed gateway channel factory.

lark-oapi is available in the test environment (verified at collection time).
All Feishu API calls are patched with unittest.mock so no real credentials
are required.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import threading
from dataclasses import asdict
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from channels.feishu import (
    FeishuChannel,
    FeishuConfig,
    FeishuOutputSink,
    FEISHU_UPLOAD_MAX_BYTES,
    _clean_at_mentions,
    _extract_post_content,
)
from agent import (
    IncomingMessage,
    SubAgentProgressEvent,
    _active_sink,
    _build_gateway_channels,
)
from agent.commands import (
    CommandCoordinator,
    CommandDescriptor,
    CommandResult,
    CommandRouter,
    register_builtin_commands,
)
from agent.runtime import RuntimeSessionState, TurnInput


# ─────────────────────────────────────────────────────────────────────────────
# FeishuConfig
# ─────────────────────────────────────────────────────────────────────────────


def test_feishu_config_defaults():
    cfg = FeishuConfig()
    assert cfg.app_id == ""
    assert cfg.app_secret == ""
    assert cfg.enabled is False
    assert cfg.group_policy == "mention"
    assert cfg.react_emoji == "THUMBSUP"
    assert cfg.allow_from == []
    assert cfg.streaming is True


def test_feishu_config_custom():
    cfg = FeishuConfig(app_id="cli_abc", app_secret="secret", enabled=True)
    assert cfg.app_id == "cli_abc"
    assert cfg.enabled is True


def test_feishu_config_allow_from_is_independent():
    """Mutable default (list) must not be shared between instances."""
    cfg1 = FeishuConfig()
    cfg2 = FeishuConfig()
    cfg1.allow_from.append("ou_xxx")
    assert cfg2.allow_from == []


# ─────────────────────────────────────────────────────────────────────────────
# Message helpers
# ─────────────────────────────────────────────────────────────────────────────


def test_clean_at_mentions_removes_placeholders():
    assert _clean_at_mentions("@_user_1 hello world") == "hello world"
    assert _clean_at_mentions("@_user_42 @_user_3 hi") == "hi"


def test_clean_at_mentions_no_op_when_no_placeholder():
    text = "hello @real_user how are you"
    assert _clean_at_mentions(text) == text


def test_extract_post_content_direct():
    payload = {
        "title": "My Title",
        "content": [
            [{"tag": "text", "text": "Hello"}, {"tag": "text", "text": " world"}]
        ],
    }
    text, imgs = _extract_post_content(payload)
    assert "Hello" in text
    assert "world" in text
    assert imgs == []


def test_extract_post_content_localized_zh_cn():
    payload = {
        "zh_cn": {
            "title": "Title",
            "content": [[{"tag": "text", "text": "你好"}]],
        }
    }
    text, imgs = _extract_post_content(payload)
    assert "你好" in text


def test_extract_post_content_wrapped_post():
    payload = {
        "post": {
            "en_us": {
                "content": [
                    [{"tag": "a", "text": "link text", "href": "https://x.com"}]
                ]
            }
        }
    }
    text, imgs = _extract_post_content(payload)
    assert "link text" in text


def test_extract_post_content_code_block():
    payload = {
        "content": [
            [{"tag": "code_block", "language": "python", "text": "print('hi')"}]
        ]
    }
    text, _ = _extract_post_content(payload)
    assert "print" in text
    assert "```" in text


def test_extract_post_content_image_keys():
    payload = {
        "content": [
            [
                {"tag": "text", "text": "see image"},
                {"tag": "img", "image_key": "img_key_123"},
            ]
        ]
    }
    text, imgs = _extract_post_content(payload)
    assert imgs == ["img_key_123"]


def test_extract_post_content_empty():
    text, imgs = _extract_post_content({})
    assert text == ""
    assert imgs == []


def test_feishu_channel_extracts_image_attachment(monkeypatch, tmp_path):
    channel = FeishuChannel(FeishuConfig(app_id="x", app_secret="y"))
    channel._client = MagicMock()
    channel._input_dir = tmp_path
    saved = tmp_path / "msg_1" / "img_key_123.png"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"fake")

    async def fake_download(message_id, resource_key, resource_type, filename):
        assert message_id == "msg_1"
        assert resource_key == "img_key_123"
        assert resource_type == "image"
        return saved

    monkeypatch.setattr(channel, "_download_message_resource", fake_download)

    attachments = asyncio.run(
        channel._extract_message_attachments(
            message_id="msg_1",
            msg_type="image",
            content_json={"image_key": "img_key_123"},
        )
    )

    assert len(attachments) == 1
    assert attachments[0].kind == "image"
    assert attachments[0].mime_type == "image/png"
    assert attachments[0].local_path == saved


def test_feishu_channel_extracts_post_image_attachments(monkeypatch, tmp_path):
    channel = FeishuChannel(FeishuConfig(app_id="x", app_secret="y"))
    channel._client = MagicMock()
    channel._input_dir = tmp_path
    saved = tmp_path / "msg_1" / "post_img_key.png"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"fake")

    async def fake_download(message_id, resource_key, resource_type, filename):
        return saved

    monkeypatch.setattr(channel, "_download_message_resource", fake_download)

    attachments = asyncio.run(
        channel._extract_message_attachments(
            message_id="msg_1",
            msg_type="post",
            content_json={
                "content": [[{"tag": "img", "image_key": "post_img_key"}]],
            },
        )
    )

    assert [attachment.local_path for attachment in attachments] == [saved]


def test_feishu_channel_extracts_file_attachment(monkeypatch, tmp_path):
    channel = FeishuChannel(FeishuConfig(app_id="x", app_secret="y"))
    channel._client = MagicMock()
    channel._input_dir = tmp_path
    saved = tmp_path / "msg_1" / "report.pdf"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"fake")

    async def fake_download(message_id, resource_key, resource_type, filename):
        assert resource_key == "file_key_123"
        assert resource_type == "file"
        assert filename == "report.pdf"
        return saved

    monkeypatch.setattr(channel, "_download_message_resource", fake_download)

    attachments = asyncio.run(
        channel._extract_message_attachments(
            message_id="msg_1",
            msg_type="file",
            content_json={"file_key": "file_key_123", "file_name": "report.pdf"},
        )
    )

    assert len(attachments) == 1
    assert attachments[0].kind == "document"
    assert attachments[0].mime_type == "application/pdf"


def test_feishu_channel_extracts_audio_attachment_with_default_extension(
    monkeypatch, tmp_path
):
    channel = FeishuChannel(FeishuConfig(app_id="x", app_secret="y"))
    channel._client = MagicMock()
    channel._input_dir = tmp_path
    saved = tmp_path / "msg_1" / "audio_key_123.mp3"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"fake")

    async def fake_download(message_id, resource_key, resource_type, filename):
        assert resource_key == "audio_key_123"
        assert resource_type == "audio"
        assert filename == "audio_key_123.mp3"
        return saved

    monkeypatch.setattr(channel, "_download_message_resource", fake_download)

    attachments = asyncio.run(
        channel._extract_message_attachments(
            message_id="msg_1",
            msg_type="audio",
            content_json={"file_key": "audio_key_123"},
        )
    )

    assert len(attachments) == 1
    assert attachments[0].kind == "audio"
    assert attachments[0].mime_type == "audio/mpeg"


def test_feishu_channel_audio_download_retries_as_file_on_invalid_param(tmp_path):
    channel = FeishuChannel(FeishuConfig(app_id="x", app_secret="y"))
    seen_types = []

    class _Resp:
        def __init__(self, *, ok, code=0, msg=""):
            self.code = code
            self.msg = msg
            self.file = None
            if ok:
                from io import BytesIO

                self.file = BytesIO(b"audio")

        def success(self):
            return self.code == 0

    def fake_get(req):
        seen_types.append(req.type)
        if req.type == "audio":
            return _Resp(ok=False, code=234001, msg="Invalid request param.")
        return _Resp(ok=True)

    channel._client = MagicMock()
    channel._client.im.v1.message_resource.get.side_effect = fake_get
    channel._input_dir = tmp_path

    path = asyncio.run(
        channel._download_message_resource(
            "msg_1",
            "file_v3_audio",
            "audio",
            "file_v3_audio.mp3",
        )
    )

    assert seen_types == ["audio", "file"]
    assert path == tmp_path / "msg_1" / "file_v3_audio.mp3"
    assert path.read_bytes() == b"audio"


# ─────────────────────────────────────────────────────────────────────────────
# FeishuOutputSink — format detection
# ─────────────────────────────────────────────────────────────────────────────


def test_detect_msg_format_short_plain_text():
    assert FeishuOutputSink._detect_msg_format("hello world") == "text"


def test_detect_msg_format_medium_plain_text():
    text = "A" * 250  # > 200 chars, ≤ 2000, no formatting
    assert FeishuOutputSink._detect_msg_format(text) == "post"


def test_detect_msg_format_long_plain_text():
    text = "A" * 2100
    assert FeishuOutputSink._detect_msg_format(text) == "interactive"


def test_detect_msg_format_code_block():
    assert (
        FeishuOutputSink._detect_msg_format("```python\nprint()\n```") == "interactive"
    )


def test_detect_msg_format_heading():
    assert FeishuOutputSink._detect_msg_format("# Title\n\nBody text") == "interactive"


def test_detect_msg_format_table():
    table = "| A | B |\n|---|---|\n| 1 | 2 |"
    assert FeishuOutputSink._detect_msg_format(table) == "interactive"


def test_detect_msg_format_bold():
    assert (
        FeishuOutputSink._detect_msg_format("This is **bold** text.") == "interactive"
    )


def test_detect_msg_format_unordered_list():
    assert (
        FeishuOutputSink._detect_msg_format("- item one\n- item two") == "interactive"
    )


def test_detect_msg_format_ordered_list():
    assert FeishuOutputSink._detect_msg_format("1. first\n2. second") == "interactive"


def test_detect_msg_format_link():
    text = "See [docs](https://example.com) for details."
    assert FeishuOutputSink._detect_msg_format(text) == "post"


# ─────────────────────────────────────────────────────────────────────────────
# FeishuOutputSink — card and post builders
# ─────────────────────────────────────────────────────────────────────────────


def test_markdown_to_post_plain_line():
    result = json.loads(FeishuOutputSink._markdown_to_post("Hello world"))
    paragraphs = result["zh_cn"]["content"]
    assert len(paragraphs) == 1
    assert paragraphs[0][0]["tag"] == "text"
    assert "Hello world" in paragraphs[0][0]["text"]


def test_markdown_to_post_with_link():
    result = json.loads(
        FeishuOutputSink._markdown_to_post("See [docs](https://example.com) here.")
    )
    elements = result["zh_cn"]["content"][0]
    tags = [el["tag"] for el in elements]
    assert "a" in tags
    link = next(el for el in elements if el["tag"] == "a")
    assert link["href"] == "https://example.com"
    assert link["text"] == "docs"


def test_parse_md_table_valid():
    table = "| Name | Age |\n|------|-----|\n| Alice | 30 |\n| Bob | 25 |"
    result = FeishuOutputSink._parse_md_table(table)
    assert result is not None
    assert result["tag"] == "table"
    assert len(result["columns"]) == 2
    assert len(result["rows"]) == 2


def test_parse_md_table_too_few_lines():
    assert FeishuOutputSink._parse_md_table("| A |\n|---|") is None


def test_parse_md_table_strips_bold_from_headers():
    table = "| **Name** | Age |\n|----------|-----|\n| Alice | 30 |"
    result = FeishuOutputSink._parse_md_table(table)
    assert result is not None
    assert result["columns"][0]["display_name"] == "Name"


def test_build_card_elements_plain_text():
    elements = FeishuOutputSink(MagicMock(), "open_id", "x")._build_card_elements(
        "Hello"
    )
    assert len(elements) >= 1
    assert elements[0]["tag"] in ("markdown", "div")


def test_build_card_elements_with_heading():
    content = "# Section\n\nSome text here."
    sink = FeishuOutputSink(MagicMock(), "open_id", "x")
    elements = sink._build_card_elements(content)
    tags = [el["tag"] for el in elements]
    assert "div" in tags  # heading becomes div


def test_split_elements_by_table_limit_one_table():
    elements = [
        {"tag": "markdown", "content": "intro"},
        {"tag": "table", "page_size": 3, "columns": [], "rows": []},
        {"tag": "markdown", "content": "outro"},
    ]
    groups = FeishuOutputSink._split_elements_by_table_limit(elements, max_tables=1)
    assert len(groups) == 1
    assert sum(1 for el in groups[0] if el["tag"] == "table") == 1


def test_split_elements_by_table_limit_two_tables_split():
    elements = [
        {"tag": "table", "page_size": 2, "columns": [], "rows": []},
        {"tag": "markdown", "content": "between"},
        {"tag": "table", "page_size": 2, "columns": [], "rows": []},
    ]
    groups = FeishuOutputSink._split_elements_by_table_limit(elements, max_tables=1)
    assert len(groups) == 2


# ─────────────────────────────────────────────────────────────────────────────
# FeishuOutputSink — OutputSink interface + drain
# ─────────────────────────────────────────────────────────────────────────────


def _make_feishu_sink() -> FeishuOutputSink:
    client = MagicMock()
    return FeishuOutputSink(
        client=client,
        receive_id_type="open_id",
        receive_id="ou_test",
        reply_message_id="msg_001",
        streaming=False,
    )


def test_feishu_sink_stream_chunk_accumulation():
    sink = _make_feishu_sink()
    sink.on_stream_chunk("hello ")
    sink.on_stream_chunk("world")
    assert sink._chunks == ["hello ", "world"]
    # No sends scheduled yet
    assert sink._pending == []


def test_feishu_sink_stream_chunk_schedules_stream_flush_when_streaming_enabled():
    sink = _make_feishu_sink()
    sink.streaming = True

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_stream_async",
                new=AsyncMock(),
            ) as mock_flush:
                sink.on_stream_chunk("hello")
                assert sink._stream_buf.text == "hello"
                assert sink._stream_flush_pending is True
                assert len(sink._pending) == 1
                await sink.drain()
                mock_flush.assert_awaited_once()

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_on_turn_complete_schedules_send():
    sink = _make_feishu_sink()
    sink.on_stream_chunk("hi")

    # on_turn_complete must schedule exactly one task
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_finish_turn_async",
                new=AsyncMock(),
            ) as mock_finish:
                sink.on_turn_complete("hi", [])
                assert len(sink._pending) == 1
                assert sink._chunks == []  # cleared
                await sink.drain()
                mock_finish.assert_awaited_once_with("hi")

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_on_turn_complete_empty_text_no_send():
    sink = _make_feishu_sink()
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            sink.on_turn_complete("   ", [])
            # Whitespace-only → no send scheduled
            assert sink._pending == []

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_flush_attachments_consumes_queue_once(tmp_path):
    sink = _make_feishu_sink()
    target = tmp_path / "report.txt"
    target.write_text("report", encoding="utf-8")
    sink.queue_attachment(target)

    async def _run():
        with patch.object(
            sink,
            "_send_file_sync",
        ) as mock_send:
            await sink.flush_attachments()
            assert sink._attachments == []
            assert sink._attachment_keys == set()
            sink.on_turn_complete("done", [])
            await sink.drain()
            mock_send.assert_called_once_with(target.resolve())

    asyncio.run(_run())


def test_feishu_sink_duplicate_queue_returns_same_cleanup_receipt(tmp_path):
    target = tmp_path / "report.txt"
    target.write_text("report", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    sink = _make_feishu_sink()

    first = sink.queue_attachment(target)
    duplicate = sink.queue_attachment(nested / ".." / "report.txt")

    assert first is duplicate
    assert len(sink._attachments) == 1


def test_feishu_sink_turn_completion_clears_attachment_receipts(tmp_path):
    target = tmp_path / "report.txt"
    target.write_text("report", encoding="utf-8")
    sink = _make_feishu_sink()
    sink.queue_attachment(target)

    async def _run():
        with patch.object(
            sink,
            "_send_response_async",
            new=AsyncMock(),
        ), patch.object(
            sink,
            "_send_file_async",
            new=AsyncMock(),
        ):
            sink.on_turn_complete("done", [])
            await sink.drain()

    asyncio.run(_run())

    assert sink._attachment_receipts == {}


def test_feishu_flush_cancelled_during_initial_drain_settles_pending_batch(
    tmp_path,
):
    async def scenario() -> None:
        private_dir = tmp_path / ".send-test"
        private_dir.mkdir()
        attachment = private_dir / "report.txt"
        attachment.write_text("report", encoding="utf-8")
        sink = _make_feishu_sink()
        receipt = sink.queue_attachment(attachment)
        drain_started = asyncio.Event()
        release_drain = asyncio.Event()

        async def blocking_drain() -> None:
            drain_started.set()
            await release_drain.wait()

        sink.drain = blocking_drain  # type: ignore[method-assign]
        flush = asyncio.create_task(sink.flush_attachments())
        await drain_started.wait()
        flush.cancel()
        outcome = (await asyncio.gather(flush, return_exceptions=True))[0]
        cleanup_transferred = sink.defer_temporary_attachment_cleanup(receipt)

        assert isinstance(outcome, asyncio.CancelledError)
        assert cleanup_transferred is True
        assert not attachment.exists()
        assert not private_dir.exists()
        assert sink._attachments == []
        assert sink._attachment_keys == set()
        assert sink._attachment_receipts == {}

    asyncio.run(scenario())


def test_feishu_thread_start_failure_settles_and_forgets_pending_batch(tmp_path):
    async def scenario() -> None:
        private_dir = tmp_path / ".send-test"
        private_dir.mkdir()
        attachment = private_dir / "report.txt"
        attachment.write_text("report", encoding="utf-8")
        sink = _make_feishu_sink()
        receipt = sink.queue_attachment(attachment)

        with patch(
            "channels.feishu.threading.Thread.start",
            side_effect=RuntimeError("thread start failed"),
        ):
            with pytest.raises(RuntimeError, match="thread start failed"):
                await sink.flush_attachments()

        cleanup_transferred = sink.defer_temporary_attachment_cleanup(receipt)
        replacement = sink.queue_attachment(attachment)

        assert cleanup_transferred is True
        assert not attachment.exists()
        assert not private_dir.exists()
        assert replacement is not receipt
        assert len(sink._attachments) == 1

    asyncio.run(scenario())


def test_feishu_worker_can_finish_before_thread_start_reports_failure(tmp_path):
    async def scenario() -> None:
        private_dir = tmp_path / ".send-test"
        private_dir.mkdir()
        attachment = private_dir / "report.txt"
        attachment.write_text("report", encoding="utf-8")
        sink = _make_feishu_sink()
        receipt = sink.queue_attachment(attachment)
        worker_finished = threading.Event()

        def send_file(path: Path) -> None:
            assert path == attachment.resolve()
            worker_finished.set()

        sink._send_file_sync = send_file  # type: ignore[method-assign]
        original_start = threading.Thread.start

        def start_then_fail(worker: threading.Thread) -> None:
            original_start(worker)
            assert worker_finished.wait(timeout=1)
            raise RuntimeError("start failed after worker ran")

        with patch(
            "channels.feishu.threading.Thread.start",
            new=start_then_fail,
        ):
            with pytest.raises(RuntimeError, match="start failed after worker ran"):
                await sink.flush_attachments()

        cleanup_transferred = sink.defer_temporary_attachment_cleanup(receipt)
        replacement = sink.queue_attachment(attachment)

        assert cleanup_transferred is True
        assert not attachment.exists()
        assert not private_dir.exists()
        assert replacement is not receipt
        assert len(sink._attachments) == 1

    asyncio.run(scenario())


def test_feishu_attachment_batches_are_globally_bounded_across_sinks(
    tmp_path, monkeypatch
):
    async def scenario() -> None:
        monkeypatch.setattr(
            "channels.feishu._ATTACHMENT_BATCH_CAPACITY",
            threading.BoundedSemaphore(2),
            raising=False,
        )
        release_workers = threading.Event()
        started_paths: list[Path] = []
        started_lock = threading.Lock()
        sinks: list[FeishuOutputSink] = []
        attachments: list[Path] = []
        receipts: list[object | None] = []

        def blocking_send(path: Path) -> None:
            with started_lock:
                started_paths.append(path)
            release_workers.wait()

        for index in range(3):
            private_dir = tmp_path / f".send-{index}"
            private_dir.mkdir()
            attachment = private_dir / f"report-{index}.txt"
            attachment.write_text(f"report-{index}", encoding="utf-8")
            sink = _make_feishu_sink()
            sink._send_file_sync = blocking_send  # type: ignore[method-assign]
            sinks.append(sink)
            attachments.append(attachment)
            receipts.append(sink.queue_attachment(attachment))

        running = [
            asyncio.create_task(sinks[index].flush_attachments())
            for index in range(2)
        ]
        for _attempt in range(100):
            with started_lock:
                started_count = len(started_paths)
            if started_count == 2:
                break
            await asyncio.sleep(0.01)

        excess = asyncio.create_task(sinks[2].flush_attachments())
        for _attempt in range(100):
            with started_lock:
                started_count = len(started_paths)
            if excess.done() or started_count == 3:
                break
            await asyncio.sleep(0.01)

        excess_finished_promptly = excess.done()
        excess_outcome = None
        if excess_finished_promptly:
            excess_outcome = (await asyncio.gather(excess, return_exceptions=True))[0]
        cleanup_transferred = sinks[2].defer_temporary_attachment_cleanup(
            receipts[2]
        )
        live_workers = [
            worker
            for worker in threading.enumerate()
            if worker.name == "feishu-attachment-batch" and worker.is_alive()
        ]
        existing_attachments = [path for path in attachments if path.exists()]

        for index in range(2):
            sinks[index].defer_temporary_attachment_cleanup(receipts[index])
        release_workers.set()
        if not excess.done():
            running.append(excess)
        await asyncio.gather(*running, return_exceptions=True)

        fourth_dir = tmp_path / ".send-3"
        fourth_dir.mkdir()
        fourth_attachment = fourth_dir / "report-3.txt"
        fourth_attachment.write_text("report-3", encoding="utf-8")
        fourth_sink = _make_feishu_sink()
        fourth_sink._send_file_sync = MagicMock()  # type: ignore[method-assign]
        fourth_receipt = fourth_sink.queue_attachment(fourth_attachment)
        await fourth_sink.flush_attachments()
        fourth_cleanup_transferred = (
            fourth_sink.defer_temporary_attachment_cleanup(fourth_receipt)
        )
        for _attempt in range(100):
            remaining_workers = [
                worker
                for worker in threading.enumerate()
                if worker.name == "feishu-attachment-batch" and worker.is_alive()
            ]
            if not remaining_workers:
                break
            await asyncio.sleep(0.01)

        assert excess_finished_promptly is True
        assert isinstance(excess_outcome, RuntimeError)
        assert str(excess_outcome) == "Feishu attachment upload capacity exhausted"
        assert cleanup_transferred is True
        assert len(live_workers) == 2
        assert all(worker.daemon for worker in live_workers)
        assert existing_attachments == attachments[:2]
        assert fourth_cleanup_transferred is True
        assert not fourth_attachment.exists()
        assert not fourth_dir.exists()
        assert remaining_workers == []

    asyncio.run(scenario())


def test_feishu_batch_reserves_global_capacity_for_each_attachment(
    tmp_path, monkeypatch
):
    async def scenario() -> None:
        monkeypatch.setattr(
            "channels.feishu._ATTACHMENT_BATCH_CAPACITY",
            threading.BoundedSemaphore(2),
        )
        release_worker = threading.Event()
        worker_started = threading.Event()
        attempted: list[Path] = []

        def blocking_send(path: Path) -> None:
            attempted.append(path)
            worker_started.set()
            release_worker.wait()

        first_sink = _make_feishu_sink()
        first_sink._send_file_sync = blocking_send  # type: ignore[method-assign]
        first_attachments: list[Path] = []
        first_receipts: list[object | None] = []
        for index in range(2):
            private_dir = tmp_path / f".batch-{index}"
            private_dir.mkdir()
            attachment = private_dir / f"report-{index}.txt"
            attachment.write_text(f"report-{index}", encoding="utf-8")
            first_attachments.append(attachment)
            first_receipts.append(first_sink.queue_attachment(attachment))

        first_flush = asyncio.create_task(first_sink.flush_attachments())
        assert await asyncio.to_thread(worker_started.wait, 1)

        excess_dir = tmp_path / ".excess"
        excess_dir.mkdir()
        excess_attachment = excess_dir / "excess.txt"
        excess_attachment.write_text("excess", encoding="utf-8")
        excess_sink = _make_feishu_sink()
        excess_sink._send_file_sync = blocking_send  # type: ignore[method-assign]
        excess_receipt = excess_sink.queue_attachment(excess_attachment)
        excess_flush = asyncio.create_task(excess_sink.flush_attachments())
        for _attempt in range(100):
            if excess_flush.done() or len(attempted) > 1:
                break
            await asyncio.sleep(0.01)

        excess_finished_promptly = excess_flush.done()
        excess_outcome = None
        if excess_finished_promptly:
            excess_outcome = (
                await asyncio.gather(excess_flush, return_exceptions=True)
            )[0]
        excess_cleanup_transferred = (
            excess_sink.defer_temporary_attachment_cleanup(excess_receipt)
        )
        live_workers = [
            worker
            for worker in threading.enumerate()
            if worker.name == "feishu-attachment-batch" and worker.is_alive()
        ]
        existing_attachments = [
            path
            for path in (*first_attachments, excess_attachment)
            if path.exists()
        ]

        for receipt in first_receipts:
            first_sink.defer_temporary_attachment_cleanup(receipt)
        release_worker.set()
        pending = [first_flush]
        if not excess_flush.done():
            pending.append(excess_flush)
        await asyncio.gather(*pending, return_exceptions=True)
        for _attempt in range(100):
            remaining_workers = [
                worker
                for worker in threading.enumerate()
                if worker.name == "feishu-attachment-batch" and worker.is_alive()
            ]
            if not remaining_workers:
                break
            await asyncio.sleep(0.01)

        assert excess_finished_promptly is True
        assert isinstance(excess_outcome, RuntimeError)
        assert str(excess_outcome) == "Feishu attachment upload capacity exhausted"
        assert excess_cleanup_transferred is True
        assert len(live_workers) == 1
        assert existing_attachments == first_attachments
        assert not excess_attachment.exists()
        assert not excess_dir.exists()
        assert remaining_workers == []

    asyncio.run(scenario())


def test_feishu_excess_command_returns_and_cleans_while_capacity_is_blocked(
    tmp_path, monkeypatch
):
    async def scenario() -> None:
        monkeypatch.setattr(
            "channels.feishu._ATTACHMENT_BATCH_CAPACITY",
            threading.BoundedSemaphore(1),
        )
        release_worker = threading.Event()
        worker_started = threading.Event()

        def blocking_send(path: Path) -> None:
            worker_started.set()
            release_worker.wait()

        blocker_dir = tmp_path / ".blocker"
        blocker_dir.mkdir()
        blocker_attachment = blocker_dir / "blocker.txt"
        blocker_attachment.write_text("blocker", encoding="utf-8")
        blocker_sink = _make_feishu_sink()
        blocker_sink._send_file_sync = blocking_send  # type: ignore[method-assign]
        blocker_receipt = blocker_sink.queue_attachment(blocker_attachment)
        blocker_flush = asyncio.create_task(blocker_sink.flush_attachments())
        assert await asyncio.to_thread(worker_started.wait, 1)

        excess_dir = tmp_path / ".excess-command"
        excess_dir.mkdir()
        excess_attachment = excess_dir / "excess.txt"
        excess_attachment.write_text("excess", encoding="utf-8")

        async def handler(request, context):
            return CommandResult(
                attachments=(excess_attachment,),
                temporary_attachments=(excess_attachment,),
            )

        excess_sink = _make_feishu_sink()
        excess_sink._send_file_sync = blocking_send  # type: ignore[method-assign]
        excess_sink._send_plain_async = AsyncMock()  # type: ignore[method-assign]
        router = CommandRouter(
            core_commands=[CommandDescriptor("report", handler)]
        )

        class UnusedCore:
            async def handle_turn(self, *args, **kwargs):
                raise AssertionError("command must not forward to the model")

        coordinator = CommandCoordinator(UnusedCore(), router)  # type: ignore[arg-type]
        state = RuntimeSessionState(ctx=MagicMock(messages=[]))
        returned_promptly = True
        try:
            await asyncio.wait_for(
                coordinator.handle(
                    TurnInput.from_text(
                        "/report",
                        session_id="s-1",
                        channel_name="feishu",
                    ),
                    state,
                    excess_sink,
                ),
                timeout=0.3,
            )
        except TimeoutError:
            returned_promptly = False

        excess_cleaned_while_blocked = (
            not excess_attachment.exists() and not excess_dir.exists()
        )
        live_workers = [
            worker
            for worker in threading.enumerate()
            if worker.name == "feishu-attachment-batch" and worker.is_alive()
        ]

        blocker_sink.defer_temporary_attachment_cleanup(blocker_receipt)
        release_worker.set()
        await asyncio.gather(blocker_flush, return_exceptions=True)
        for _attempt in range(100):
            remaining_workers = [
                worker
                for worker in threading.enumerate()
                if worker.name == "feishu-attachment-batch" and worker.is_alive()
            ]
            if not remaining_workers:
                break
            await asyncio.sleep(0.01)

        assert returned_promptly is True
        assert excess_cleaned_while_blocked is True
        assert len(live_workers) == 1
        assert remaining_workers == []

    asyncio.run(scenario())


def test_feishu_coordinator_flushes_temp_before_cleanup(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    source = output_dir / "report.txt"
    source.write_text("report", encoding="utf-8")
    sink = _make_feishu_sink()
    uploaded: list[tuple[Path, bool, str]] = []

    def upload(path: Path):
        uploaded.append((path, path.exists(), path.read_text(encoding="utf-8")))
        return "file-key"

    sink._upload_file_sync = MagicMock(side_effect=upload)
    sink._do_send = MagicMock()
    router = CommandRouter()
    register_builtin_commands(router)

    class UnusedCore:
        async def handle_turn(self, *args, **kwargs):
            raise AssertionError("command must not forward to the model")

    coordinator = CommandCoordinator(
        UnusedCore(),  # type: ignore[arg-type]
        router,
        components={"output_dir": output_dir},
    )
    state = RuntimeSessionState(ctx=MagicMock(messages=[]))

    asyncio.run(
        coordinator.handle(
            TurnInput.from_text(
                "/send report.txt",
                session_id="s-1",
                channel_name="feishu",
            ),
            state,
            sink,
        )
    )

    assert len(uploaded) == 1
    attachment, existed_during_upload, content = uploaded[0]
    assert existed_during_upload is True
    assert content == "report"
    assert not attachment.exists()
    assert not attachment.parent.exists()
    assert sink._attachments == []
    assert sink._attachment_keys == set()
    sink._do_send.assert_called_once()


def test_feishu_flush_error_clears_queue_and_coordinator_cleans_temp(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "report.txt").write_text("report", encoding="utf-8")
    sink = _make_feishu_sink()
    attempted: list[Path] = []

    def fail_send(path: Path) -> None:
        attempted.append(path)
        assert path.is_file()
        raise RuntimeError("upload failed")

    sink._send_file_sync = fail_send  # type: ignore[method-assign]
    router = CommandRouter()
    register_builtin_commands(router)

    class UnusedCore:
        async def handle_turn(self, *args, **kwargs):
            raise AssertionError("command must not forward to the model")

    coordinator = CommandCoordinator(
        UnusedCore(),  # type: ignore[arg-type]
        router,
        components={"output_dir": output_dir},
    )
    state = RuntimeSessionState(ctx=MagicMock(messages=[]))

    asyncio.run(
        coordinator.handle(
            TurnInput.from_text(
                "/send report.txt",
                session_id="s-1",
                channel_name="feishu",
            ),
            state,
            sink,
        )
    )

    assert len(attempted) == 1
    assert not attempted[0].exists()
    assert not attempted[0].parent.exists()
    assert sink._attachments == []
    assert sink._attachment_keys == set()


def test_feishu_flush_cancellation_returns_before_sync_uploader_and_defers_cleanup(
    tmp_path, monkeypatch
):
    async def scenario() -> None:
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / "report.txt").write_text("report", encoding="utf-8")
        sink = _make_feishu_sink()
        worker_started = threading.Event()
        release_worker = threading.Event()
        worker_finished = threading.Event()
        attempted: list[Path] = []
        observed_content: list[str] = []

        def blocking_upload(path: Path):
            attempted.append(path)
            worker_started.set()
            release_worker.wait()
            try:
                observed_content.append(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                observed_content.append("missing")
            finally:
                worker_finished.set()
            return "file-key"

        sink._upload_file_sync = MagicMock(side_effect=blocking_upload)
        sink._do_send = MagicMock()
        monkeypatch.setattr(
            "channels.feishu._ATTACHMENT_CANCEL_GRACE_SECONDS",
            0.01,
            raising=False,
        )
        router = CommandRouter()
        register_builtin_commands(router)

        class UnusedCore:
            async def handle_turn(self, *args, **kwargs):
                raise AssertionError("command must not forward to the model")

        coordinator = CommandCoordinator(
            UnusedCore(),  # type: ignore[arg-type]
            router,
            components={"output_dir": output_dir},
        )
        state = RuntimeSessionState(ctx=MagicMock(messages=[]))
        running = asyncio.create_task(
            coordinator.handle(
                TurnInput.from_text(
                    "/send report.txt",
                    session_id="s-1",
                    channel_name="feishu",
                ),
                state,
                sink,
            )
        )

        assert await asyncio.to_thread(worker_started.wait, 1)
        safety_release = threading.Timer(5, release_worker.set)
        safety_release.daemon = True
        safety_release.start()
        running.cancel()
        await asyncio.sleep(0)
        running.cancel()
        await asyncio.sleep(0.05)
        cancellation_returned_before_worker = running.done()
        snapshot_existed_while_blocked = attempted[0].is_file()
        worker_was_still_blocked = not worker_finished.is_set()
        current = asyncio.current_task()
        detached_uploads = [
            task
            for task in asyncio.all_tasks()
            if task not in (current, running) and not task.done()
        ]
        for task in detached_uploads:
            task.cancel()
        await asyncio.gather(*detached_uploads, return_exceptions=True)
        await asyncio.sleep(0)
        snapshot_existed_after_async_upload_cancellation = attempted[0].is_file()
        release_worker.set()
        safety_release.cancel()
        outcome = (await asyncio.gather(running, return_exceptions=True))[0]
        assert await asyncio.to_thread(worker_finished.wait, 1)
        for _attempt in range(100):
            if not attempted[0].exists() and not attempted[0].parent.exists():
                break
            await asyncio.sleep(0.01)

        assert cancellation_returned_before_worker is True
        assert snapshot_existed_while_blocked is True
        assert worker_was_still_blocked is True
        assert snapshot_existed_after_async_upload_cancellation is True
        assert isinstance(outcome, asyncio.CancelledError)
        assert observed_content == ["report"]
        assert not attempted[0].exists()
        assert not attempted[0].parent.exists()
        assert sink._attachments == []
        assert sink._attachment_keys == set()

    asyncio.run(scenario())


def test_feishu_sync_worker_cleans_deferred_snapshot_after_event_loop_closes(
    tmp_path, monkeypatch
):
    private_dir = tmp_path / ".send-test"
    private_dir.mkdir()
    attachment = private_dir / "report.txt"
    attachment.write_text("report", encoding="utf-8")
    sink = _make_feishu_sink()
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()
    observed_content: list[str] = []

    def blocking_upload(path: Path):
        worker_started.set()
        release_worker.wait()
        try:
            observed_content.append(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            observed_content.append("missing")
        finally:
            worker_finished.set()
        return "file-key"

    sink._upload_file_sync = MagicMock(side_effect=blocking_upload)
    sink._do_send = MagicMock()
    receipt = sink.queue_attachment(attachment)
    monkeypatch.setattr(
        "channels.feishu._ATTACHMENT_CANCEL_GRACE_SECONDS",
        0.01,
    )

    async def start_and_transfer() -> tuple[BaseException, bool]:
        flush = asyncio.create_task(sink.flush_attachments())
        assert await asyncio.to_thread(worker_started.wait, 1)
        flush.cancel()
        outcome = (await asyncio.gather(flush, return_exceptions=True))[0]
        return outcome, sink.defer_temporary_attachment_cleanup(receipt)

    loop = asyncio.new_event_loop()
    try:
        outcome, cleanup_transferred = loop.run_until_complete(start_and_transfer())
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    finally:
        loop.close()

    existed_after_loop_close = attachment.is_file()
    release_worker.set()
    assert worker_finished.wait(timeout=1)
    for _attempt in range(100):
        if not attachment.exists() and not private_dir.exists():
            break
        threading.Event().wait(0.01)

    assert isinstance(outcome, asyncio.CancelledError)
    assert cleanup_transferred is True
    assert existed_after_loop_close is True
    assert observed_content == ["report"]
    assert not attachment.exists()
    assert not private_dir.exists()


def test_feishu_cancellation_cleans_never_started_upload_when_executor_saturated(
    tmp_path, monkeypatch
):
    async def scenario() -> None:
        attachments: list[Path] = []
        for index in range(2):
            private_dir = tmp_path / f".send-{index}"
            private_dir.mkdir()
            attachment = private_dir / f"report-{index}.txt"
            attachment.write_text(f"report-{index}", encoding="utf-8")
            attachments.append(attachment)

        async def handler(request, context):
            return CommandResult(
                attachments=tuple(attachments),
                temporary_attachments=tuple(attachments),
            )

        sink = _make_feishu_sink()
        upload_started = threading.Event()
        release_upload = threading.Event()
        uploaded: list[Path] = []

        def blocking_upload(path: Path):
            uploaded.append(path)
            upload_started.set()
            release_upload.wait()
            return "file-key"

        sink._upload_file_sync = MagicMock(side_effect=blocking_upload)
        sink._do_send = MagicMock()
        monkeypatch.setattr(
            "channels.feishu._ATTACHMENT_CANCEL_GRACE_SECONDS",
            0.01,
        )
        blocker_started = threading.Event()
        release_blocker = threading.Event()

        def occupy_default_executor() -> None:
            blocker_started.set()
            release_blocker.wait()

        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        blocker = loop.run_in_executor(None, occupy_default_executor)
        for _attempt in range(100):
            if blocker_started.is_set():
                break
            await asyncio.sleep(0.01)

        router = CommandRouter(
            core_commands=[CommandDescriptor("report", handler)]
        )

        class UnusedCore:
            async def handle_turn(self, *args, **kwargs):
                raise AssertionError("command must not forward to the model")

        coordinator = CommandCoordinator(UnusedCore(), router)  # type: ignore[arg-type]
        state = RuntimeSessionState(ctx=MagicMock(messages=[]))
        running = asyncio.create_task(
            coordinator.handle(
                TurnInput.from_text(
                    "/report",
                    session_id="s-1",
                    channel_name="feishu",
                ),
                state,
                sink,
            )
        )

        for _attempt in range(20):
            if upload_started.is_set():
                break
            await asyncio.sleep(0.01)
        worker_started_despite_saturated_executor = upload_started.is_set()
        running.cancel()
        await asyncio.sleep(0)
        running.cancel()
        outcome = (await asyncio.gather(running, return_exceptions=True))[0]
        second_worker_never_started = len(uploaded) <= 1
        release_upload.set()
        release_blocker.set()
        await asyncio.gather(blocker, return_exceptions=True)
        executor.shutdown(wait=True)
        for _attempt in range(100):
            if all(
                not path.exists() and not path.parent.exists()
                for path in attachments
            ):
                break
            await asyncio.sleep(0.01)

        assert isinstance(outcome, asyncio.CancelledError)
        assert worker_started_despite_saturated_executor is True
        assert second_worker_never_started is True
        assert all(not path.exists() for path in attachments)
        assert all(not path.parent.exists() for path in attachments)

    asyncio.run(scenario())


def test_feishu_coordinator_handoff_uses_queue_receipt_for_path_alias(tmp_path):
    async def scenario() -> None:
        private_dir = tmp_path / ".send-test"
        private_dir.mkdir()
        attachment = private_dir / "report.txt"
        attachment.write_text("report", encoding="utf-8")
        alias = private_dir / "nested" / ".." / "report.txt"
        (private_dir / "nested").mkdir()

        async def handler(request, context):
            return CommandResult(
                attachments=(alias,),
                temporary_attachments=(alias,),
            )

        sink = _make_feishu_sink()
        worker_started = threading.Event()
        release_worker = threading.Event()
        observed_content: list[str] = []

        def blocking_upload(path: Path):
            worker_started.set()
            release_worker.wait()
            try:
                observed_content.append(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                observed_content.append("missing")
            return "file-key"

        sink._upload_file_sync = MagicMock(side_effect=blocking_upload)
        sink._do_send = MagicMock()
        router = CommandRouter(
            core_commands=[CommandDescriptor("report", handler)]
        )

        class UnusedCore:
            async def handle_turn(self, *args, **kwargs):
                raise AssertionError("command must not forward to the model")

        coordinator = CommandCoordinator(UnusedCore(), router)  # type: ignore[arg-type]
        state = RuntimeSessionState(ctx=MagicMock(messages=[]))
        running = asyncio.create_task(
            coordinator.handle(
                TurnInput.from_text(
                    "/report",
                    session_id="s-1",
                    channel_name="feishu",
                ),
                state,
                sink,
            )
        )

        assert await asyncio.to_thread(worker_started.wait, 1)
        running.cancel()
        await asyncio.sleep(0)
        running.cancel()
        outcome = (await asyncio.gather(running, return_exceptions=True))[0]
        existed_before_worker_release = attachment.is_file()
        release_worker.set()
        for _attempt in range(100):
            if not attachment.exists():
                break
            await asyncio.sleep(0.01)

        assert isinstance(outcome, asyncio.CancelledError)
        assert existed_before_worker_release is True
        assert observed_content == ["report"]
        assert not attachment.exists()

    asyncio.run(scenario())


def test_feishu_sink_on_tool_start_schedules_hint():
    sink = _make_feishu_sink()
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_send_tool_hint_async",
                new=AsyncMock(),
            ) as mock_hint:
                sink.on_tool_start("bash", {"command": "ls"})
                assert len(sink._pending) == 1
                await sink.drain()
                mock_hint.assert_awaited_once()

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_on_tool_end_is_noop():
    sink = _make_feishu_sink()
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            sink.on_tool_end("bash", "output")
            assert sink._pending == []

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_stream_chunk_does_not_emit_summary_before_turn_complete():
    sink = _make_feishu_sink()
    sink.streaming = True
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_stream_async",
                new=AsyncMock(),
            ) as mock_flush:
                sink.on_stream_chunk("hello")
                assert sink._chunks == ["hello"]
                assert len(sink._pending) == 1
                await sink.drain()
                mock_flush.assert_awaited_once()

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_write_file_tool_end_does_not_queue_attachment(tmp_path):
    sink = _make_feishu_sink()
    target = tmp_path / "report.txt"
    target.write_text("hello", encoding="utf-8")
    result = json.dumps({"ok": True, "path": str(target)})

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_send_file_async",
                new=AsyncMock(),
            ) as mock_send:
                sink.on_tool_end("write_file", result)
                assert len(sink._pending) == 0
                assert sink._attachments == []
                sink.on_turn_complete("done", [])
                await sink.drain()
                mock_send.assert_not_awaited()

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_turn_complete_does_not_auto_send_output_dir_files(tmp_path):
    sink = FeishuOutputSink(
        client=MagicMock(),
        receive_id_type="open_id",
        receive_id="ou_test",
        reply_message_id="msg_001",
        output_dir=tmp_path,
    )
    generated = tmp_path / "artifact.txt"
    generated.write_text("artifact", encoding="utf-8")

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_send_response_async",
                new=AsyncMock(),
            ) as mock_send_response, patch.object(
                sink,
                "_send_file_async",
                new=AsyncMock(),
            ) as mock_send_file:
                sink.on_turn_complete("done", [])
                await sink.drain()
                mock_send_response.assert_awaited_once_with("done")
                mock_send_file.assert_not_awaited()

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_error_keeps_partial_stream_text(tmp_path):
    sink = _make_feishu_sink()
    sink.streaming = True
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            seen = []

            async def fake_finish(text: str):
                seen.append(("finish", text))

            async def fake_send_plain(text: str):
                seen.append(("error", text))

            with patch.object(
                sink,
                "_finish_turn_async",
                new=fake_finish,
            ), patch.object(
                sink,
                "_send_plain_async",
                new=fake_send_plain,
            ):
                sink.on_stream_chunk("前半句")
                sink.on_turn_complete("", [])
                sink.on_error("Model response was truncated (finish_reason=length)")
                await sink.drain()

            assert seen[0] == ("finish", "前半句")
            assert seen[1] == (
                "error",
                "❌ Model response was truncated (finish_reason=length)",
            )

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_subagent_event_schedules_process_card_update():
    sink = _make_feishu_sink()
    sink.streaming = True
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_progress_async",
                new=AsyncMock(),
            ) as mock_flush:
                sink.on_subagent_event(
                    SubAgentProgressEvent(
                        kind="agent_started",
                        role="researcher",
                        task="inspect code",
                        message="researcher started",
                    )
                )
                assert len(sink._pending) == 1
                await sink.drain()
                mock_flush.assert_awaited_once()

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_latency_trace_logs_scheduled_work(monkeypatch, caplog):
    monkeypatch.setenv("SIMPLE_TRACE_LATENCY", "1")
    caplog.set_level(logging.WARNING, logger="channels.feishu")
    sink = _make_feishu_sink()
    sink.streaming = True
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_progress_async",
                new=AsyncMock(),
            ):
                sink.on_subagent_event(
                    SubAgentProgressEvent(
                        kind="agent_started",
                        role="researcher",
                        task="inspect code",
                    )
                )
                await sink.drain()

        loop.run_until_complete(_run())
    finally:
        loop.close()

    assert "latency_trace component=feishu_sink stage=task_queued" in caplog.text
    assert "latency_trace component=feishu_sink stage=task_finished" in caplog.text
    assert "trace_id=msg_001" in caplog.text
    assert "op=flush_progress" in caplog.text


def test_feishu_sink_latency_trace_logs_finish_turn(monkeypatch, caplog):
    monkeypatch.setenv("SIMPLE_TRACE_LATENCY", "1")
    caplog.set_level(logging.WARNING, logger="channels.feishu")
    sink = _make_feishu_sink()
    sink.streaming = False
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_send_response_async",
                new=AsyncMock(),
            ) as mock_send, patch.object(
                sink,
                "_send_attachments_async",
                new=AsyncMock(),
            ) as mock_attachments:
                await sink._finish_turn_async("hello")
                mock_send.assert_awaited_once_with("hello")
                mock_attachments.assert_awaited_once()

        loop.run_until_complete(_run())
    finally:
        loop.close()

    assert "latency_trace component=feishu_sink stage=finish_turn_started" in caplog.text
    assert "latency_trace component=feishu_sink stage=finish_turn_finished" in caplog.text
    assert "trace_id=msg_001" in caplog.text
    assert "text_len=5" in caplog.text


def test_feishu_sink_dedupes_duplicate_batch_progress_events():
    sink = _make_feishu_sink()
    sink.streaming = True
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_progress_async",
                new=AsyncMock(),
            ):
                event = SubAgentProgressEvent(kind="batch_progress", completed=0, total=3)
                sink.on_subagent_event(event)
                sink.on_subagent_event(event)
                await sink.drain()

            assert sink._progress_buf.text.count("Running: 0/3") == 1

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_parallel_batch_started_uses_mode_aware_summary():
    sink = _make_feishu_sink()
    sink.streaming = True
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_progress_async",
                new=AsyncMock(),
            ):
                sink.on_subagent_event(
                    SubAgentProgressEvent(
                        kind="batch_started",
                        total=3,
                        metrics={
                            "execution_mode": "parallel",
                            "spec_count": 3,
                            "max_parallel_agents": 2,
                        },
                    )
                )
                await sink.drain()

            assert "Parallel batch: 3 subtasks, max concurrency 2" in sink._progress_buf.text

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_parallel_batch_finished_shows_detailed_metrics():
    sink = _make_feishu_sink()
    sink.streaming = True
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_progress_async",
                new=AsyncMock(),
            ):
                sink.on_subagent_event(
                    SubAgentProgressEvent(
                        kind="batch_finished",
                        completed=3,
                        total=3,
                        metrics={
                            "execution_mode": "parallel",
                            "spec_count": 3,
                            "duration_seconds": 1.24,
                            "write_scope_check_seconds": 0.004,
                        },
                    )
                )
                await sink.drain()

            assert (
                "Parallel batch finished: 3/3 in 1.24s (scope check 0.004s)"
                in sink._progress_buf.text
            )

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_pipeline_batch_finished_shows_stage_count():
    sink = _make_feishu_sink()
    sink.streaming = True
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_progress_async",
                new=AsyncMock(),
            ):
                sink.on_subagent_event(
                    SubAgentProgressEvent(
                        kind="batch_finished",
                        completed=3,
                        total=3,
                        metrics={
                            "execution_mode": "pipeline",
                            "spec_count": 3,
                            "stage_count": 2,
                            "duration_seconds": 1.02,
                        },
                    )
                )
                await sink.drain()

            assert (
                "Pipeline batch finished: 3/3 across 2 stages in 1.02s"
                in sink._progress_buf.text
            )

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_pipeline_batch_finished_marks_early_stop():
    sink = _make_feishu_sink()
    sink.streaming = True
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_progress_async",
                new=AsyncMock(),
            ):
                sink.on_subagent_event(
                    SubAgentProgressEvent(
                        kind="batch_finished",
                        completed=2,
                        total=3,
                        metrics={
                            "execution_mode": "pipeline",
                            "spec_count": 3,
                            "stage_count": 2,
                            "duration_seconds": 0.88,
                        },
                    )
                )
                await sink.drain()

            assert (
                "Pipeline batch ended early: 2/3 across 2 stages in 0.88s"
                in sink._progress_buf.text
            )

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_rendezvous_batch_finished_shows_rounds():
    sink = _make_feishu_sink()
    sink.streaming = True
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_progress_async",
                new=AsyncMock(),
            ):
                sink.on_subagent_event(
                    SubAgentProgressEvent(
                        kind="batch_finished",
                        completed=2,
                        total=2,
                        metrics={
                            "execution_mode": "rendezvous",
                            "spec_count": 2,
                            "rounds_completed": 2,
                            "duration_seconds": 1.88,
                        },
                    )
                )
                await sink.drain()

            assert (
                "Rendezvous batch finished: 2 subtasks, 2 rounds in 1.88s"
                in sink._progress_buf.text
            )

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_rendezvous_phase_events_show_runtime_stage_messages():
    sink = _make_feishu_sink()
    sink.streaming = True
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_progress_async",
                new=AsyncMock(),
            ):
                sink.on_subagent_event(
                    SubAgentProgressEvent(
                        kind="phase_started",
                        message="Debate round 1/2 started: 2 participants (researcher, critic)",
                        metrics={
                            "execution_mode": "rendezvous",
                            "phase_kind": "round",
                            "phase_index": 1,
                            "phase_total": 2,
                        },
                    )
                )
                sink.on_subagent_event(
                    SubAgentProgressEvent(
                        kind="phase_note",
                        message="Lead summary ready for round 2/2: 2 continue (researcher, critic)",
                        metrics={
                            "execution_mode": "rendezvous",
                            "phase_kind": "lead_summary",
                            "phase_index": 1,
                            "phase_total": 2,
                        },
                    )
                )
                await sink.drain()

            assert (
                "▸ Debate round 1/2 started: 2 participants (researcher, critic)"
                in sink._progress_buf.text
            )
            assert (
                "💬 Lead summary ready for round 2/2: 2 continue (researcher, critic)"
                in sink._progress_buf.text
            )

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_batch_events_fall_back_without_metrics():
    sink = _make_feishu_sink()
    sink.streaming = True
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_progress_async",
                new=AsyncMock(),
            ):
                sink.on_subagent_event(
                    SubAgentProgressEvent(
                        kind="batch_finished",
                        completed=2,
                        total=3,
                    )
                )
                await sink.drain()

            assert "Batch finished: 2/3" in sink._progress_buf.text

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_tool_start_always_uses_progress_card_when_streaming():
    sink = _make_feishu_sink()
    sink.streaming = True
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_progress_async",
                new=AsyncMock(),
            ) as mock_progress, patch.object(
                sink,
                "_send_tool_hint_async",
                new=AsyncMock(),
            ) as mock_hint, patch.object(
                sink,
                "_flush_stream_async",
                new=AsyncMock(),
            ) as mock_stream:
                sink.on_tool_start("bash", {"command": "ls"})
                await sink.drain()
                mock_progress.assert_not_called()
                mock_hint.assert_not_called()
                mock_stream.assert_awaited_once()

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_tool_start_suppresses_internal_scheduler_hints():
    sink = _make_feishu_sink()
    sink.streaming = True
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_progress_async",
                new=AsyncMock(),
            ) as mock_progress, patch.object(
                sink,
                "_send_tool_hint_async",
                new=AsyncMock(),
            ) as mock_hint:
                sink.on_tool_start("current_time", {})
                sink.on_tool_start(
                    "schedule_create",
                    {"name": "测试提醒", "trigger_type": "once"},
                )
                await sink.drain()
                mock_progress.assert_not_called()
                mock_hint.assert_not_called()

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_tool_start_uses_process_card_when_progress_active():
    sink = _make_feishu_sink()
    sink.streaming = True
    sink._progress_buf.text = "Progress"
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_stream_async",
                new=AsyncMock(),
            ) as mock_flush, patch.object(
                sink,
                "_send_tool_hint_async",
                new=AsyncMock(),
            ) as mock_hint:
                sink.on_tool_start("bash", {"command": "ls"})
                await sink.drain()
                mock_flush.assert_awaited_once()
                mock_hint.assert_not_called()

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_tool_start_appends_inline_to_stream_buf_for_chronological_order():
    sink = _make_feishu_sink()
    sink.streaming = True
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            sink.on_stream_chunk("Some text")
            await sink.drain()
            sink.on_tool_start("bash", {"command": "ls"})
            await sink.drain()
            # Tool calls now go inline into _stream_buf.text so they
            # appear in chronological order between text chunks.
            assert "Some text" in sink._stream_buf.text
            assert "**Tool Call**" in sink._stream_buf.text
            assert sink._stream_buf.text.index("Some text") < sink._stream_buf.text.index("**Tool Call**")

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_tool_start_keeps_streamed_text_as_stable_prefix():
    sink = _make_feishu_sink()
    sink.streaming = True
    updates: list[str] = []

    def _record_update(_card_id: str, content: str, _sequence: int) -> bool:
        updates.append(content)
        return True

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_create_streaming_card_sync",
                return_value="card_stream",
            ), patch.object(
                sink,
                "_stream_update_text_sync",
                side_effect=_record_update,
            ):
                sink.on_stream_chunk("Summary draft")
                await sink.drain()
                sink.on_tool_start("bash", {"command": "ls"})
                await sink.drain()

            assert updates[0] == "Summary draft"
            assert updates[1].startswith(updates[0])
            assert "**Tool Call**" in updates[1]

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_tool_progress_updates_stable_progress_section():
    sink = _make_feishu_sink()
    sink.streaming = True
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_progress_async",
                new=AsyncMock(),
            ) as mock_flush:
                sink.on_tool_progress(
                    "web_fetch",
                    {
                        "operation_id": "op_1",
                        "status": "downloading",
                        "current": 50,
                        "total": 100,
                        "bytes_done": 1024,
                        "bytes_total": 2048,
                    },
                )
                await sink.drain()
                mock_flush.assert_awaited_once()

            content = sink._render_primary_markdown()
            assert "## Tool Progress" in content
            assert "- **web_fetch**: downloading 50% (1.0KB/2.0KB)" in content

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_tool_progress_replaces_existing_operation_line():
    sink = _make_feishu_sink()
    sink.streaming = True
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(sink, "_flush_progress_async", new=AsyncMock()):
                sink.on_tool_progress(
                    "web_fetch",
                    {"operation_id": "op_1", "status": "downloading", "bytes_done": 1024},
                )
                sink.on_tool_progress(
                    "web_fetch",
                    {"operation_id": "op_1", "status": "downloading", "bytes_done": 2048},
                )
                await sink.drain()

        loop.run_until_complete(_run())
    finally:
        loop.close()

    content = sink._render_primary_markdown()

    assert "1.0KB" not in content
    assert "2.0KB" in content
    assert content.count("**web_fetch**") == 1


def test_feishu_sink_turn_complete_uses_single_primary_surface_for_progress_and_final():
    sink = _make_feishu_sink()
    sink.streaming = True
    sink._progress_buf.text = "Running"
    sink._stream_buf.card_id = "card_primary"

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_send_response_async",
                new=AsyncMock(),
            ) as mock_send_response, patch.object(
                sink,
                "_finalize_stream_async",
                new=AsyncMock(),
            ) as mock_finalize_stream:
                sink.on_turn_complete("final answer", [])
                await sink.drain()
                mock_finalize_stream.assert_awaited_once_with("final answer")
                mock_send_response.assert_not_awaited()

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_drain_preserves_progress_before_final_answer_order():
    sink = _make_feishu_sink()
    sink.streaming = True
    events: list[str] = []

    async def _slow_flush_stream(*args, **kwargs):
        await asyncio.sleep(0.01)
        events.append("flush_stream")

    async def _send_response(text):
        events.append(f"final:{text}")

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_stream_async",
                new=_slow_flush_stream,
            ), patch.object(
                sink,
                "_send_response_async",
                new=_send_response,
            ):
                sink.on_tool_start("shell", {"command": "echo hi"})
                sink.on_turn_complete("done", [])
                await sink.drain()

            assert events == ["flush_stream", "final:done"]

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_stream_waits_for_progress_phase_barrier():
    sink = _make_feishu_sink()
    sink.streaming = True
    events: list[str] = []

    async def _flush_progress(*args, **kwargs):
        events.append("flush_progress")

    async def _flush_stream(*args, **kwargs):
        events.append("flush_stream")

    async def _finalize_stream(text):
        events.append(f"finalize_stream:{text}")

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_flush_progress_async",
                new=_flush_progress,
            ), patch.object(
                sink,
                "_flush_stream_async",
                new=_flush_stream,
            ), patch.object(
                sink,
                "_finalize_stream_async",
                new=_finalize_stream,
            ):
                sink.on_subagent_event(
                    SubAgentProgressEvent(
                        kind="agent_started",
                        role="researcher",
                        task="inspect code",
                        message="researcher started",
                    )
                )
                sink.on_stream_chunk("answer chunk")
                sink.on_turn_complete("answer chunk", [])
                await sink.drain()

            assert events == [
                "flush_progress",
                "flush_stream",
                "finalize_stream:answer chunk",
            ]

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_progress_failure_drops_progress_and_keeps_final_answer():
    sink = _make_feishu_sink()
    sink.streaming = True
    events: list[str] = []

    async def _send_response(text: str):
        events.append(f"final:{text}")

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_create_streaming_card_sync",
                return_value=None,
            ), patch.object(
                sink,
                "_send_response_async",
                new=_send_response,
            ):
                sink.on_subagent_event(
                    SubAgentProgressEvent(
                        kind="agent_started",
                        role="researcher",
                        task="inspect code",
                        message="researcher started",
                    )
                )
                sink.on_turn_complete("final answer", [])
                await sink.drain()

            assert events == ["final:final answer"]

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_turn_complete_finalizes_stream_card_without_extra_final_message():
    sink = _make_feishu_sink()
    sink.streaming = True
    sink._stream_buf.card_id = "card_stream"
    sink._stream_buf.text = "hello"

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_finalize_stream_async",
                new=AsyncMock(),
            ) as mock_finalize, patch.object(
                sink,
                "_send_response_async",
                new=AsyncMock(),
            ) as mock_send_response:
                sink.on_turn_complete("hello", [])
                await sink.drain()
                mock_finalize.assert_awaited_once_with("hello")
                mock_send_response.assert_not_awaited()

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_on_tool_blocked_schedules_notice():
    sink = _make_feishu_sink()
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_send_plain_async",
                new=AsyncMock(),
            ) as mock_plain:
                sink.on_tool_blocked("bash", "policy violation")
                assert len(sink._pending) == 1
                await sink.drain()
                mock_plain.assert_awaited_once()

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_on_error_schedules_message():
    sink = _make_feishu_sink()
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(
                sink,
                "_send_plain_async",
                new=AsyncMock(),
            ) as mock_plain:
                sink.on_error("something broke")
                assert len(sink._pending) == 1
                await sink.drain()
                mock_plain.assert_awaited_once()

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_drain_clears_pending():
    """drain() must await all tasks and clear the pending list."""
    sink = _make_feishu_sink()

    async def _run():
        # Patch _do_send to avoid real API call
        with patch.object(sink, "_do_send"):
            sink.on_turn_complete("hello world", [])
            assert len(sink._pending) == 1
            await sink.drain()
            assert sink._pending == []

    asyncio.run(_run())


def test_feishu_sink_reply_used_first_then_create():
    """First _do_send call should attempt the Reply API; subsequent ones use Create."""
    sink = _make_feishu_sink()  # has reply_message_id="msg_001"

    # Simulate a successful reply
    mock_resp = MagicMock()
    mock_resp.success.return_value = True
    sink._client.im.v1.message.reply.return_value = mock_resp

    sink._do_send("text", '{"text":"hi"}')
    assert sink._client.im.v1.message.reply.called
    assert not sink._client.im.v1.message.create.called
    assert sink._first_reply is False  # consumed


def test_feishu_sink_latency_trace_logs_api_send(monkeypatch, caplog):
    monkeypatch.setenv("SIMPLE_TRACE_LATENCY", "1")
    caplog.set_level(logging.WARNING, logger="channels.feishu")
    sink = _make_feishu_sink()

    mock_resp = MagicMock()
    mock_resp.success.return_value = True
    sink._client.im.v1.message.reply.return_value = mock_resp

    sink._do_send("text", '{"text":"hi"}')

    assert "latency_trace component=feishu_sink stage=do_send_finished" in caplog.text
    assert "trace_id=msg_001" in caplog.text
    assert "route=reply" in caplog.text
    assert "msg_type=text" in caplog.text


def test_feishu_sink_logs_send_success(caplog):
    caplog.set_level(logging.INFO, logger="channels.feishu")
    sink = _make_feishu_sink()

    mock_resp = MagicMock()
    mock_resp.success.return_value = True
    sink._client.im.v1.message.reply.return_value = mock_resp

    sink._do_send("text", '{"text":"hi"}')

    assert "interaction component=feishu_sink event=message_sent" in caplog.text
    assert "trace_id=msg_001" in caplog.text
    assert "route=reply" in caplog.text
    assert "msg_type=text" in caplog.text


def test_feishu_sink_skips_large_file_upload_with_clear_log(tmp_path, caplog):
    sink = _make_feishu_sink()
    target = tmp_path / "movie.mp4"
    target.write_bytes(b"x" * 10)

    real_stat = Path.stat

    def fake_stat(path_self):
        if path_self == target:
            return type("Stat", (), {"st_size": FEISHU_UPLOAD_MAX_BYTES + 1})()
        return real_stat(path_self)

    with patch.object(Path, "stat", fake_stat):
        result = sink._upload_file_sync(target)

    assert result is None
    assert "too large for Feishu upload" in caplog.text


def test_feishu_sink_falls_back_to_create_on_reply_failure():
    """If Reply API fails, _do_send must fall back to CreateMessage."""
    sink = _make_feishu_sink()

    fail_resp = MagicMock()
    fail_resp.success.return_value = False
    fail_resp.msg = "failed"
    sink._client.im.v1.message.reply.return_value = fail_resp

    ok_resp = MagicMock()
    ok_resp.success.return_value = True
    sink._client.im.v1.message.create.return_value = ok_resp

    sink._do_send("text", '{"text":"hi"}')
    assert sink._client.im.v1.message.create.called


def test_feishu_sink_second_send_uses_create_directly():
    """After the first message (reply consumed), _do_send goes straight to Create."""
    sink = _make_feishu_sink()
    sink._first_reply = False  # already consumed

    ok_resp = MagicMock()
    ok_resp.success.return_value = True
    sink._client.im.v1.message.create.return_value = ok_resp

    sink._do_send("text", '{"text":"follow-up"}')
    assert not sink._client.im.v1.message.reply.called
    assert sink._client.im.v1.message.create.called


# ─────────────────────────────────────────────────────────────────────────────
# FeishuChannel._is_bot_mentioned
# ─────────────────────────────────────────────────────────────────────────────


def test_is_bot_mentioned_at_all():
    channel = FeishuChannel(FeishuConfig())
    msg = MagicMock()
    msg.content = '{"text":"@_all please help"}'
    msg.mentions = []
    assert channel._is_bot_mentioned(msg) is True


def test_is_bot_mentioned_via_mention_object():
    channel = FeishuChannel(FeishuConfig())
    msg = MagicMock()
    msg.content = '{"text":"@_user_1 hello"}'
    mention = MagicMock()
    mention.id.user_id = None
    mention.id.open_id = "ou_abc123"
    msg.mentions = [mention]
    assert channel._is_bot_mentioned(msg) is True


def test_is_bot_mentioned_human_mention_only():
    channel = FeishuChannel(FeishuConfig())
    msg = MagicMock()
    msg.content = '{"text":"@_user_1 hello"}'
    mention = MagicMock()
    mention.id.user_id = "u_human"
    mention.id.open_id = "ou_human"
    msg.mentions = [mention]
    assert channel._is_bot_mentioned(msg) is False


# ─────────────────────────────────────────────────────────────────────────────
# FeishuChannel.start() — error handling
# ─────────────────────────────────────────────────────────────────────────────


def test_feishu_channel_start_raises_without_lark():
    channel = FeishuChannel(FeishuConfig(app_id="x", app_secret="y"))
    with patch("channels.feishu.LARK_AVAILABLE", False):
        with pytest.raises(RuntimeError, match="lark-oapi"):
            asyncio.run(channel.start(lambda msg, sink: True))


def test_feishu_channel_start_raises_missing_credentials():
    channel = FeishuChannel(FeishuConfig())  # app_id/app_secret empty
    with pytest.raises(RuntimeError, match="app_id"):
        asyncio.run(channel.start(lambda msg, sink: True))


def test_register_optional_event_calls_builder_when_method_exists():
    channel = FeishuChannel(FeishuConfig())
    builder = MagicMock()
    handler = object()
    method = MagicMock(return_value=builder)
    builder.register_demo_event = method

    result = channel._register_optional_event(builder, "register_demo_event", handler)

    assert result is builder
    method.assert_called_once_with(handler)


def test_register_optional_event_noops_when_method_missing():
    channel = FeishuChannel(FeishuConfig())
    builder = MagicMock()

    result = channel._register_optional_event(builder, "register_missing_event", object())

    assert result is builder


def test_feishu_optional_event_handlers_are_noops():
    channel = FeishuChannel(FeishuConfig())

    assert channel._on_reaction_created(MagicMock()) is None
    assert channel._on_reaction_deleted(MagicMock()) is None
    assert channel._on_message_read(MagicMock()) is None
    assert channel._on_bot_p2p_chat_entered(MagicMock()) is None


def test_feishu_channel_create_sink_passes_output_dir():
    channel = FeishuChannel(FeishuConfig(app_id="x", app_secret="y", streaming=False))
    channel._client = MagicMock()
    channel._output_dir = Path("/tmp/feishu-output")

    msg = IncomingMessage(
        text="hi",
        channel_name="feishu",
        metadata={"chat_id": "ou_test", "chat_type": "p2p", "message_id": "msg_1"},
    )
    sink = channel.create_sink(msg)
    assert sink._output_dir == Path("/tmp/feishu-output")
    assert sink.streaming is False


def test_feishu_channel_uses_output_dir_for_inbound_attachments(tmp_path):
    channel = FeishuChannel(FeishuConfig(app_id="x", app_secret="y"))
    output_dir = tmp_path / "output"

    channel.set_output_dir(output_dir)

    assert channel._input_dir == output_dir / "feishu-input"


def test_feishu_channel_explicit_input_dir_overrides_output_dir(tmp_path):
    channel = FeishuChannel(FeishuConfig(app_id="x", app_secret="y"))
    input_dir = tmp_path / "incoming"

    channel.set_input_dir(input_dir)
    channel.set_output_dir(tmp_path / "output")

    assert channel._input_dir == input_dir


def test_feishu_channel_create_sink_treats_group_chat_type_as_chat_id():
    channel = FeishuChannel(FeishuConfig(app_id="x", app_secret="y", streaming=False))
    channel._client = MagicMock()

    msg = IncomingMessage(
        text="hi",
        channel_name="feishu",
        metadata={
            "chat_id": "oc_test_chat",
            "chat_type": "group_chat",
            "message_id": "msg_1",
        },
    )

    sink = channel.create_sink(msg)

    assert sink._receive_id_type == "chat_id"
    assert sink._receive_id == "oc_test_chat"


def test_feishu_channel_send_command_uses_output_dir(tmp_path):
    channel = FeishuChannel(FeishuConfig(app_id="x", app_secret="y"))
    channel._client = MagicMock()
    channel._handler = AsyncMock()
    channel._output_dir = tmp_path
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")

    mock_sink = MagicMock()
    mock_sink._send_file_async = AsyncMock()
    mock_sink.drain = AsyncMock()

    message = MagicMock()
    message.message_id = "msg_123"
    message.chat_id = "ou_sender"
    message.chat_type = "p2p"
    message.message_type = "text"
    message.content = json.dumps({"text": "/send note.txt"})
    message.mentions = []

    sender = MagicMock()
    sender.sender_type = "user"
    sender.sender_id.open_id = "ou_sender"

    data = MagicMock()
    data.event.message = message
    data.event.sender = sender

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(channel, "_add_reaction", new=AsyncMock()), patch.object(
                channel,
                "create_sink",
                return_value=mock_sink,
            ):
                await channel._on_message(data)
                mock_sink._send_file_async.assert_awaited_once_with(target)
                mock_sink.drain.assert_awaited_once()
                channel._handler.assert_not_called()

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_channel_logs_received_message(caplog):
    caplog.set_level(logging.INFO, logger="agent")
    channel = FeishuChannel(FeishuConfig(app_id="x", app_secret="y"))
    channel._client = MagicMock()
    channel._handler = AsyncMock()
    mock_sink = MagicMock()

    message = MagicMock()
    message.message_id = "msg_123"
    message.chat_id = "ou_sender"
    message.chat_type = "p2p"
    message.message_type = "text"
    message.content = json.dumps({"text": "hello logger"})
    message.mentions = []

    sender = MagicMock()
    sender.sender_type = "user"
    sender.sender_id.open_id = "ou_sender"

    data = MagicMock()
    data.event.message = message
    data.event.sender = sender

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(channel, "_add_reaction", new=AsyncMock()), patch.object(
                channel,
                "create_sink",
                return_value=mock_sink,
            ):
                await channel._on_message(data)

        loop.run_until_complete(_run())
    finally:
        loop.close()

    assert "interaction component=feishu_channel event=message_received" in caplog.text
    assert "interaction component=feishu_channel event=message_dispatched" in caplog.text
    assert "message_id=msg_123" in caplog.text
    assert "text_len=12" in caplog.text


def test_feishu_channel_dispatches_image_message_with_attachment(tmp_path):
    channel = FeishuChannel(FeishuConfig(app_id="x", app_secret="y"))
    channel._client = MagicMock()
    channel._handler = AsyncMock()
    channel._input_dir = tmp_path
    mock_sink = MagicMock()
    saved = tmp_path / "msg_123" / "img_key_123.png"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"fake")

    message = MagicMock()
    message.message_id = "msg_123"
    message.chat_id = "ou_sender"
    message.chat_type = "p2p"
    message.message_type = "image"
    message.content = json.dumps({"image_key": "img_key_123"})
    message.mentions = []

    sender = MagicMock()
    sender.sender_type = "user"
    sender.sender_id.open_id = "ou_sender"

    data = MagicMock()
    data.event.message = message
    data.event.sender = sender

    async def fake_download(message_id, resource_key, resource_type, filename):
        return saved

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(channel, "_add_reaction", new=AsyncMock()), patch.object(
                channel,
                "create_sink",
                return_value=mock_sink,
            ), patch.object(
                channel,
                "_download_message_resource",
                new=fake_download,
            ):
                await channel._on_message(data)

        loop.run_until_complete(_run())
    finally:
        loop.close()

    dispatched = channel._handler.await_args.args[0]
    assert dispatched.text == "[image]"
    assert len(dispatched.attachments) == 1
    assert dispatched.attachments[0].local_path == saved


def test_feishu_channel_dispatches_audio_message_with_attachment(tmp_path):
    channel = FeishuChannel(FeishuConfig(app_id="x", app_secret="y"))
    channel._client = MagicMock()
    channel._handler = AsyncMock()
    channel._input_dir = tmp_path
    mock_sink = MagicMock()
    saved = tmp_path / "msg_123" / "audio_key_123.mp3"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"fake")

    message = MagicMock()
    message.message_id = "msg_123"
    message.chat_id = "ou_sender"
    message.chat_type = "p2p"
    message.message_type = "audio"
    message.content = json.dumps({"file_key": "audio_key_123"})
    message.mentions = []

    sender = MagicMock()
    sender.sender_type = "user"
    sender.sender_id.open_id = "ou_sender"

    data = MagicMock()
    data.event.message = message
    data.event.sender = sender

    async def fake_download(message_id, resource_key, resource_type, filename):
        return saved

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(channel, "_add_reaction", new=AsyncMock()), patch.object(
                channel,
                "create_sink",
                return_value=mock_sink,
            ), patch.object(
                channel,
                "_download_message_resource",
                new=fake_download,
            ):
                await channel._on_message(data)

        loop.run_until_complete(_run())
    finally:
        loop.close()

    dispatched = channel._handler.await_args.args[0]
    assert dispatched.text == "[audio]"
    assert len(dispatched.attachments) == 1
    assert dispatched.attachments[0].local_path == saved


def test_feishu_channel_audio_download_failure_reaches_agent(tmp_path):
    channel = FeishuChannel(FeishuConfig(app_id="x", app_secret="y"))
    channel._client = MagicMock()
    channel._handler = AsyncMock()
    channel._input_dir = tmp_path
    mock_sink = MagicMock()

    message = MagicMock()
    message.message_id = "msg_123"
    message.chat_id = "ou_sender"
    message.chat_type = "p2p"
    message.message_type = "audio"
    message.content = json.dumps({"file_key": "audio_key_123"})
    message.mentions = []

    sender = MagicMock()
    sender.sender_type = "user"
    sender.sender_id.open_id = "ou_sender"

    data = MagicMock()
    data.event.message = message
    data.event.sender = sender

    async def fake_download(message_id, resource_key, resource_type, filename):
        return None

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(channel, "_add_reaction", new=AsyncMock()), patch.object(
                channel,
                "create_sink",
                return_value=mock_sink,
            ), patch.object(
                channel,
                "_download_message_resource",
                new=fake_download,
            ):
                await channel._on_message(data)

        loop.run_until_complete(_run())
    finally:
        loop.close()

    dispatched = channel._handler.await_args.args[0]
    assert "audio attachment download failed" in dispatched.text
    assert dispatched.metadata["attachment_download_failed_count"] == 1
    assert dispatched.attachments == ()


def test_feishu_channel_dispatches_post_image_only_message_with_attachment(tmp_path):
    channel = FeishuChannel(FeishuConfig(app_id="x", app_secret="y"))
    channel._client = MagicMock()
    channel._handler = AsyncMock()
    channel._input_dir = tmp_path
    mock_sink = MagicMock()
    saved = tmp_path / "msg_123" / "img_key_123.png"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"fake")

    message = MagicMock()
    message.message_id = "msg_123"
    message.chat_id = "ou_sender"
    message.chat_type = "p2p"
    message.message_type = "post"
    message.content = json.dumps(
        {"content": [[{"tag": "img", "image_key": "img_key_123"}]]}
    )
    message.mentions = []

    sender = MagicMock()
    sender.sender_type = "user"
    sender.sender_id.open_id = "ou_sender"

    data = MagicMock()
    data.event.message = message
    data.event.sender = sender

    async def fake_download(message_id, resource_key, resource_type, filename):
        return saved

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(channel, "_add_reaction", new=AsyncMock()), patch.object(
                channel,
                "create_sink",
                return_value=mock_sink,
            ), patch.object(
                channel,
                "_download_message_resource",
                new=fake_download,
            ):
                await channel._on_message(data)

        loop.run_until_complete(_run())
    finally:
        loop.close()

    dispatched = channel._handler.await_args.args[0]
    assert dispatched.text == "[post]"
    assert [attachment.local_path for attachment in dispatched.attachments] == [saved]


# ─────────────────────────────────────────────────────────────────────────────
# _build_gateway_channels factory
# ─────────────────────────────────────────────────────────────────────────────


def test_build_gateway_channels_empty_config_returns_no_channels():
    """No channels configured → empty list (gateway should warn and exit)."""
    channels = _build_gateway_channels({})
    assert channels == []


def test_build_gateway_channels_feishu_disabled():
    cfg = {"channels": {"feishu": {"enabled": False}}}
    channels = _build_gateway_channels(cfg)
    assert channels == []


def test_build_gateway_channels_feishu_enabled():
    cfg = {
        "channels": {
            "feishu": {
                "enabled": True,
                "app_id": "cli_test",
                "app_secret": "secret",
            }
        }
    }
    channels = _build_gateway_channels(cfg)
    assert len(channels) == 1
    assert isinstance(channels[0], FeishuChannel)
    assert channels[0]._config.app_id == "cli_test"


def test_build_gateway_channels_feishu_extra_keys_ignored():
    """Unknown keys in feishu config must not cause an error."""
    cfg = {
        "channels": {
            "feishu": {
                "enabled": True,
                "app_id": "cli_test",
                "app_secret": "secret",
                "_readme": "this is a comment key",
            }
        }
    }
    channels = _build_gateway_channels(cfg)
    assert isinstance(channels[0], FeishuChannel)


def test_build_gateway_channels_falls_back_to_empty_on_import_error():
    """If FeishuChannel import fails, returns empty list (no CLI fallback)."""
    cfg = {
        "channels": {
            "feishu": {
                "enabled": True,
                "app_id": "x",
                "app_secret": "y",
            }
        }
    }
    import sys
    import channels.feishu as _feishu_mod  # ensure loaded

    saved = sys.modules.pop("channels.feishu")
    try:
        sys.modules["channels.feishu"] = None  # type: ignore[assignment]
        channels = _build_gateway_channels(cfg)
        assert channels == []  # no fallback to CLI
    finally:
        sys.modules["channels.feishu"] = saved


def test_missing_feishu_dependency_hint_mentions_uv_tool_env(monkeypatch):
    import agent as agent_module

    monkeypatch.setattr(
        agent_module.sys,
        "executable",
        "/Users/shike/.local/share/uv/tools/simple/bin/python",
    )

    hint = agent_module._missing_feishu_dependency_hint()

    assert "uv tool environment" in hint
    assert "uv run simple gateway" in hint
    assert "uv tool install --reinstall --editable . --with lark-oapi" in hint


def _selective_import_error(name, *args, **kwargs):
    import builtins

    if "channels.feishu" in name:
        raise ImportError("mocked import error")
    return builtins.__import__(name, *args, **kwargs)
