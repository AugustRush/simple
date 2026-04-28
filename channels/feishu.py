"""Feishu/Lark channel for the personal agent.

Uses the lark-oapi WebSocket long-connection mode — no public IP or webhook
required.  The WebSocket reconnects automatically on disconnect.

Install dependency::

    pip install lark-oapi

Configuration in ``~/.agent/config.json``::

    {
      "channels": {
        "feishu": {
          "enabled": true,
          "app_id": "cli_xxxxxxxxxxxxxxxx",
          "app_secret": "xxxxxxxxxxxxxxxxxxxxxxxxxxxx",
          "encrypt_key": "",
          "verification_token": "",
          "allow_from": [],
          "react_emoji": "THUMBSUP",
          "group_policy": "mention"
        }
      }
    }

``allow_from``
    List of Feishu ``open_id`` values allowed to use the bot.
    Empty list (default) allows everyone.

``group_policy``
    ``"mention"`` (default) — only respond in group chats when @mentioned.
    ``"open"``    — respond to every message in group chats.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional

from agent import _active_sink, _fmt_tool_inputs, shared
from agent.channels import Channel, IncomingMessage
from agent.core import OutputSink, SubAgentProgressEvent

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

LARK_AVAILABLE = importlib.util.find_spec("lark_oapi") is not None
FEISHU_UPLOAD_MAX_BYTES = 30 * 1024 * 1024


def build_feishu_client(config: "FeishuConfig") -> Any:
    import lark_oapi as lark  # type: ignore[import]

    return (
        lark.Client.builder()
        .app_id(config.app_id)
        .app_secret(config.app_secret)
        .log_level(lark.LogLevel.WARNING)
        .build()
    )


def _preview_text(text: object, limit: int = 80) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _interaction_log(event: str, **fields: object) -> None:
    payload = shared._trace_fields(**fields)
    message = f"interaction component=feishu_channel event={event}"
    if payload:
        message += f" {payload}"
    logger.info(message)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FeishuConfig:
    """Feishu channel configuration."""

    app_id: str = ""
    app_secret: str = ""
    encrypt_key: str = ""
    verification_token: str = ""
    # Whitelist of sender open_ids; empty = accept all
    allow_from: list[str] = field(default_factory=list)
    # Emoji added as reaction when a message is received
    react_emoji: str = "THUMBSUP"
    # "mention": only respond in groups when @mentioned; "open": respond to all
    group_policy: Literal["open", "mention"] = "mention"
    streaming: bool = True
    enabled: bool = False


_STREAM_ELEMENT_ID = "streaming_md"


@dataclass
class _FeishuStreamBuf:
    """State for one streaming Feishu response."""

    text: str = ""
    card_id: str | None = None
    sequence: int = 0
    last_edit: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Message parsing helpers  (adapted from nanobot reference implementation)
# ─────────────────────────────────────────────────────────────────────────────

# Regex to strip Feishu @-mention placeholders (@_user_N) from message text.
_AT_PLACEHOLDER_RE = re.compile(r"@_user_\d+\s*")


def _clean_at_mentions(text: str) -> str:
    """Remove @_user_N placeholders left by Feishu in message text."""
    return _AT_PLACEHOLDER_RE.sub("", text).strip()


def _is_group_chat_type(chat_type: str) -> bool:
    normalized = str(chat_type or "").strip().lower()
    return normalized in {"group", "group_chat", "chat"}


def _extract_post_content(content_json: dict) -> tuple[str, list[str]]:
    """Extract plain text and embedded image keys from a Feishu *post* message.

    Handles three payload shapes:
    - Direct:    ``{"title": "...", "content": [[...]]}``
    - Localized: ``{"zh_cn": {"title": "...", "content": [...]}}``
    - Wrapped:   ``{"post": {"zh_cn": {...}}}``

    Returns ``(text, image_keys)``.
    """

    def _parse_block(block: dict) -> tuple[Optional[str], list[str]]:
        if not isinstance(block, dict) or not isinstance(block.get("content"), list):
            return None, []
        texts: list[str] = []
        images: list[str] = []
        if title := block.get("title"):
            texts.append(str(title))
        for row in block["content"]:
            if not isinstance(row, list):
                continue
            for el in row:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag")
                if tag in ("text", "a"):
                    texts.append(el.get("text", ""))
                elif tag == "at":
                    texts.append(f"@{el.get('user_name', 'user')}")
                elif tag == "code_block":
                    lang = el.get("language", "")
                    code = el.get("text", "")
                    texts.append(f"\n```{lang}\n{code}\n```\n")
                elif tag == "img" and (key := el.get("image_key")):
                    images.append(key)
        return (" ".join(texts).strip() or None), images

    root = content_json
    if isinstance(root, dict) and isinstance(root.get("post"), dict):
        root = root["post"]
    if not isinstance(root, dict):
        return "", []

    if "content" in root:
        text, imgs = _parse_block(root)
        if text or imgs:
            return text or "", imgs

    for locale in ("zh_cn", "en_us", "ja_jp"):
        if locale in root:
            text, imgs = _parse_block(root[locale])
            if text or imgs:
                return text or "", imgs

    for val in root.values():
        if isinstance(val, dict):
            text, imgs = _parse_block(val)
            if text or imgs:
                return text or "", imgs

    return "", []


# ─────────────────────────────────────────────────────────────────────────────
# FeishuOutputSink
# ─────────────────────────────────────────────────────────────────────────────


class FeishuOutputSink(OutputSink):
    """OutputSink implementation that sends responses to a Feishu chat.

    When CardKit streaming is enabled, streamed chunks are rendered into a
    single live-updating card.  If CardKit is unavailable, the sink falls
    back to the existing send-on-complete behavior.

    Tool-start events are shown inline when a streaming card is active,
    otherwise they are surfaced as small "Tool Call" code-block cards.

    Generated files can also be uploaded and sent back to the chat.

    All Feishu API calls are synchronous (lark-oapi's sync client).  They are
    run in a thread-pool executor so the asyncio event loop is never blocked.
    Pending sends are tracked in ``self._pending`` and must be awaited via
    ``await sink.drain()`` before the handler returns.
    """

    # ── Smart format detection ────────────────────────────────────────────────
    # Complex markdown (code, tables, headings) → interactive card
    _COMPLEX_MD_RE = re.compile(
        r"```"
        r"|^\|.+\|.*\n\s*\|[-:\s|]+"  # markdown table
        r"|^#{1,6}\s+",  # headings
        re.MULTILINE,
    )
    # Simple inline formatting that post/text can't render → card
    _SIMPLE_MD_RE = re.compile(
        r"\*\*.+?\*\*"
        r"|__.+?__"
        r"|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"
        r"|~~.+?~~",
        re.DOTALL,
    )
    _MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
    _LIST_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)
    _OLIST_RE = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)

    # Card element building
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)"
        r"(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)"
        r"(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )
    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    _CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)

    # Strip inline formatting for plain-text surfaces (table cells, headings)
    _MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    _MD_BOLD_US_RE = re.compile(r"__(.+?)__")
    _MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
    _MD_STRIKE_RE = re.compile(r"~~(.+?)~~")
    _IMAGE_EXTS = {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
        ".ico",
        ".tiff",
        ".tif",
    }
    _FILE_TYPE_MAP = {
        ".pdf": "pdf",
        ".doc": "doc",
        ".docx": "doc",
        ".xls": "xls",
        ".xlsx": "xls",
        ".ppt": "ppt",
        ".pptx": "ppt",
    }
    _STREAM_EDIT_INTERVAL = 0.2

    _TEXT_MAX_LEN = 200  # plain-text ceiling; longer → post
    _POST_MAX_LEN = 2000  # post ceiling; longer → interactive card
    _SUPPRESSED_TOOL_HINTS = {
        "current_time",
        "schedule_create",
        "schedule_list",
        "schedule_delete",
    }

    def __init__(
        self,
        client: Any,
        receive_id_type: str,
        receive_id: str,
        reply_message_id: Optional[str] = None,
        output_dir: Optional[Path] = None,
        streaming: bool = True,
        trace_id: Optional[str] = None,
    ) -> None:
        self._client = client
        self._receive_id_type = receive_id_type
        self._receive_id = receive_id
        self._reply_message_id = reply_message_id
        self._trace_id = str(trace_id or reply_message_id or receive_id)
        self._output_dir = output_dir
        self.streaming = streaming
        self._chunks: list[str] = []
        self._pending: list[asyncio.Task] = []
        self._send_tail: asyncio.Task | None = None
        self._first_reply = True  # first send of this response uses Reply API
        self._turn_start = time.time()
        self._stream_buf = _FeishuStreamBuf()
        self._stream_flush_pending = False
        self._progress_buf = _FeishuStreamBuf()
        self._progress_flush_pending = False
        self._progress_fail_count = 0
        self._last_batch_progress_key: tuple[int, int] | None = None
        self._attachments: list[Path] = []
        self._attachment_keys: set[str] = set()

    def _trace_latency(self, stage: str, **fields: object) -> None:
        if not shared._latency_trace_enabled():
            return
        payload = shared._trace_fields(
            trace_id=self._trace_id,
            receive_id=self._receive_id,
            **fields,
        )
        message = f"latency_trace component=feishu_sink stage={stage}"
        if payload:
            message += f" {payload}"
        logger.warning(message)

    def _interaction_log(self, event: str, **fields: object) -> None:
        payload = shared._trace_fields(
            trace_id=self._trace_id,
            receive_id=self._receive_id,
            **fields,
        )
        message = f"interaction component=feishu_sink event={event}"
        if payload:
            message += f" {payload}"
        logger.info(message)

    # ── OutputSink interface ──────────────────────────────────────────────────

    def on_stream_chunk(self, chunk: str) -> None:
        """Accumulate final summary text and update the primary reply surface."""
        self._chunks.append(chunk)
        self._stream_buf.text += chunk
        if not self.streaming:
            return
        if not self._stream_flush_pending:
            self._stream_flush_pending = True
            self._schedule(self._flush_stream_async(), label="flush_stream")

    def on_turn_complete(self, full_text: str, tool_calls: list[str]) -> None:
        text = full_text or "".join(self._chunks)
        self._chunks.clear()
        if (
            text.strip()
            or self._output_dir is not None
            or self._progress_buf.card_id
            or self._progress_buf.text.strip()
        ):
            self._schedule(self._finish_turn_async(text), label="finish_turn")

    def on_tool_start(self, name: str, inputs: dict) -> None:
        if name in self._SUPPRESSED_TOOL_HINTS:
            return
        hint = f"{name}{_fmt_tool_inputs(name, inputs)}"
        if self.streaming:
            if self._progress_fail_count >= 3:
                return
            self._append_progress_text(f"**Tool Call**\n\n```text\n{hint}\n```")
            if not self._progress_flush_pending:
                self._progress_flush_pending = True
                self._schedule(
                    self._flush_progress_async(force=True),
                    label="flush_progress",
                )
            return
        self._schedule(self._send_tool_hint_async(hint), label="send_tool_hint")

    def on_tool_end(self, name: str, result: str) -> None:
        if name != "write_file":
            return
        path = self._extract_written_path(result)
        if path is not None:
            self._queue_attachment(path)

    def on_tool_blocked(self, name: str, reason: str) -> None:
        self._schedule(
            self._send_plain_async(f"🚫 Tool `{name}` blocked: {reason}"),
            label="send_blocked_notice",
        )

    def on_error(self, error: str) -> None:
        self._schedule(
            self._send_plain_async(f"❌ {error}"),
            label="send_error_notice",
        )

    def on_subagent_event(self, event: SubAgentProgressEvent) -> None:
        if event.kind == "batch_started":
            self._last_batch_progress_key = None
        elif event.kind == "batch_progress":
            key = (event.completed, event.total)
            if self._last_batch_progress_key == key:
                return
            self._last_batch_progress_key = key
        elif event.kind == "batch_finished":
            self._last_batch_progress_key = None
        line = self._format_mode_aware_subagent_event(event)
        if not line:
            return
        if not self.streaming:
            self._schedule(self._send_plain_async(line), label="send_progress_plain")
            return
        if self._progress_fail_count >= 3:
            return
        self._append_progress_text(line)
        if not self._progress_flush_pending:
            self._progress_flush_pending = True
            self._schedule(self._flush_progress_async(), label="flush_progress")

    def on_info(self, content: Any) -> None:
        if isinstance(content, str):
            self._schedule(self._send_plain_async(content), label="send_info")

    def on_status(self, text: str, *, level: str = "info") -> None:
        if level in ("error", "warning"):
            self._schedule(self._send_plain_async(text), label="send_status")

    def queue_attachment(self, path: Path) -> None:
        self._queue_attachment(path)

    async def drain(self) -> None:
        """Await all pending send tasks before the handler returns."""
        while self._pending:
            pending = list(self._pending)
            await asyncio.gather(*pending, return_exceptions=True)
            self._pending.clear()
        if self._send_tail is not None and self._send_tail.done():
            self._send_tail = None

    # ── Internal scheduling ───────────────────────────────────────────────────

    def _schedule(self, coro: Any, *, label: str = "task") -> None:
        """Schedule a coroutine as a tracked asyncio task."""
        previous = self._send_tail
        queued_at = time.perf_counter()
        self._trace_latency(
            "task_queued",
            op=label,
            pending_before=len(self._pending),
        )

        async def _run_in_order() -> None:
            if previous is not None:
                try:
                    await previous
                except Exception:
                    pass
            started_at = time.perf_counter()
            self._trace_latency(
                "task_started",
                op=label,
                queue_wait_ms=f"{(started_at - queued_at) * 1000:.1f}",
            )
            try:
                await coro
            finally:
                finished_at = time.perf_counter()
                self._trace_latency(
                    "task_finished",
                    op=label,
                    run_ms=f"{(finished_at - started_at) * 1000:.1f}",
                    total_ms=f"{(finished_at - queued_at) * 1000:.1f}",
                )

        try:
            task = asyncio.ensure_future(_run_in_order())
            self._pending.append(task)
            self._send_tail = task
        except RuntimeError:
            if inspect.iscoroutine(coro):
                coro.close()
            logger.warning("FeishuOutputSink: no running event loop to schedule send")

    # ── Async send helpers ────────────────────────────────────────────────────

    async def _finish_turn_async(self, text: str) -> None:
        started_at = time.perf_counter()
        finish_mode = "attachments_only"
        if self.streaming and (self._stream_buf.card_id or self._stream_buf.text.strip()):
            finish_mode = "stream_finalize"
        elif text.strip():
            finish_mode = "send_response"
        self._trace_latency(
            "finish_turn_started",
            mode=finish_mode,
            text_len=len(text),
            streaming=self.streaming,
            attachment_count=len(self._attachments),
        )
        try:
            if self.streaming and (
                self._stream_buf.card_id or self._stream_buf.text.strip()
            ):
                await self._finalize_stream_async(text)
            elif text.strip():
                await self._send_response_async(text)
            await self._send_attachments_async()
        finally:
            self._trace_latency(
                "finish_turn_finished",
                mode=finish_mode,
                text_len=len(text),
                streaming=self.streaming,
                duration_ms=f"{(time.perf_counter() - started_at) * 1000:.1f}",
            )
            self._progress_fail_count = 0
            self._last_batch_progress_key = None
            self._attachments = []
            self._attachment_keys = set()

    def _render_primary_markdown(self) -> str:
        parts: list[str] = []
        progress = self._progress_buf.text.strip()
        final_text = self._stream_buf.text.strip()
        if progress and self._progress_fail_count < 3:
            parts.append(progress)
        if final_text:
            if parts:
                parts.append("---")
            parts.append(final_text)
        return "\n\n".join(parts).strip()

    async def _ensure_primary_surface_card(self) -> str | None:
        if self._stream_buf.card_id is not None:
            return self._stream_buf.card_id
        loop = asyncio.get_running_loop()
        card_id = await loop.run_in_executor(None, self._create_streaming_card_sync)
        if card_id:
            self._stream_buf.card_id = card_id
            self._stream_buf.sequence = 0
        return card_id

    async def _flush_primary_surface_async(self, force: bool = False) -> None:
        content = self._render_primary_markdown()
        if not self.streaming or not content:
            return
        if self._progress_fail_count >= 3:
            return

        loop = asyncio.get_running_loop()
        now = time.monotonic()
        card_id = await self._ensure_primary_surface_card()
        if not card_id:
            self._progress_fail_count += 1
            self._progress_buf = _FeishuStreamBuf()
            return

        if not force and (now - self._stream_buf.last_edit) < self._STREAM_EDIT_INTERVAL:
            return

        self._stream_buf.sequence += 1
        ok = await loop.run_in_executor(
            None,
            self._stream_update_text_sync,
            card_id,
            content,
            self._stream_buf.sequence,
        )
        if ok:
            self._stream_buf.last_edit = now
        else:
            self._progress_fail_count += 1
            self._progress_buf = _FeishuStreamBuf()

    async def _flush_stream_async(self, force: bool = False) -> None:
        try:
            await self._flush_primary_surface_async(force=force)
        finally:
            self._stream_flush_pending = False

    async def _flush_progress_async(self, force: bool = False) -> None:
        try:
            await self._flush_primary_surface_async(force=force)
        finally:
            self._progress_flush_pending = False

    async def _finalize_stream_async(self, text: str) -> None:
        loop = asyncio.get_running_loop()
        final_text = text or self._stream_buf.text or "".join(self._chunks)
        card_id = self._stream_buf.card_id
        self._stream_buf.text = final_text
        content = self._render_primary_markdown()

        try:
            if not card_id:
                if final_text.strip():
                    await self._send_response_async(final_text)
                return

            self._stream_buf.text = final_text
            self._stream_buf.sequence += 1
            ok = await loop.run_in_executor(
                None,
                self._stream_update_text_sync,
                card_id,
                content,
                self._stream_buf.sequence,
            )
            if not ok:
                if final_text.strip():
                    await self._send_response_async(final_text)
                return

            self._stream_buf.sequence += 1
            await loop.run_in_executor(
                None,
                self._close_streaming_mode_sync,
                card_id,
                self._stream_buf.sequence,
            )
        finally:
            self._stream_buf = _FeishuStreamBuf()
            self._progress_buf = _FeishuStreamBuf()
            self._stream_flush_pending = False

    async def _finalize_progress_async(self) -> None:
        self._progress_flush_pending = False

    async def _send_response_async(self, text: str) -> None:
        """Send the final response using the optimal Feishu message format."""
        started_at = time.perf_counter()
        loop = asyncio.get_running_loop()
        fmt = self._detect_msg_format(text)
        group_count = 1

        try:
            if fmt == "text":
                body = json.dumps({"text": text.strip()}, ensure_ascii=False)
                await loop.run_in_executor(None, self._do_send, "text", body)

            elif fmt == "post":
                body = self._markdown_to_post(text)
                await loop.run_in_executor(None, self._do_send, "post", body)

            else:  # "interactive" — full card with table/heading support
                elements = self._build_card_elements(text)
                groups = self._split_elements_by_table_limit(elements)
                group_count = len(groups)
                for group in groups:
                    card = {"config": {"wide_screen_mode": True}, "elements": group}
                    await loop.run_in_executor(
                        None,
                        self._do_send,
                        "interactive",
                        json.dumps(card, ensure_ascii=False),
                    )
        finally:
            self._trace_latency(
                "response_send_finished",
                format=fmt,
                text_len=len(text),
                groups=group_count,
                duration_ms=f"{(time.perf_counter() - started_at) * 1000:.1f}",
            )

    async def _send_tool_hint_async(self, hint: str) -> None:
        """Send a 'Tool Call' code-block card so the user sees progress."""
        loop = asyncio.get_running_loop()
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"**Tool Call**\n\n```text\n{hint}\n```",
                }
            ],
        }
        await loop.run_in_executor(
            None,
            self._do_send,
            "interactive",
            json.dumps(card, ensure_ascii=False),
        )

    async def _send_plain_async(self, text: str) -> None:
        loop = asyncio.get_running_loop()
        body = json.dumps({"text": text}, ensure_ascii=False)
        await loop.run_in_executor(None, self._do_send, "text", body)

    async def _send_file_async(self, file_path: Path) -> None:
        started_at = time.perf_counter()
        path = Path(file_path)
        if not path.is_file():
            logger.warning("Feishu file send skipped, file not found: %s", path)
            self._trace_latency(
                "attachment_send_finished",
                path=path,
                result="missing",
                duration_ms=f"{(time.perf_counter() - started_at) * 1000:.1f}",
            )
            return

        loop = asyncio.get_running_loop()
        ext = path.suffix.lower()
        if ext in self._IMAGE_EXTS:
            key = await loop.run_in_executor(None, self._upload_image_sync, path)
            if key:
                await loop.run_in_executor(
                    None,
                    self._do_send,
                    "image",
                    json.dumps({"image_key": key}, ensure_ascii=False),
                )
            self._trace_latency(
                "attachment_send_finished",
                path=path,
                kind="image",
                uploaded=bool(key),
                duration_ms=f"{(time.perf_counter() - started_at) * 1000:.1f}",
            )
            return

        key = await loop.run_in_executor(None, self._upload_file_sync, path)
        if key:
            await loop.run_in_executor(
                None,
                self._do_send,
                "file",
                json.dumps({"file_key": key}, ensure_ascii=False),
            )
        self._trace_latency(
            "attachment_send_finished",
            path=path,
            kind="file",
            uploaded=bool(key),
            duration_ms=f"{(time.perf_counter() - started_at) * 1000:.1f}",
        )

    def _queue_attachment(self, file_path: Path) -> None:
        try:
            path = Path(file_path).resolve()
        except OSError:
            return
        key = str(path)
        if key in self._attachment_keys:
            return
        self._attachment_keys.add(key)
        self._attachments.append(path)

    async def _send_attachments_async(self) -> None:
        for path in self._collect_output_dir_files():
            self._queue_attachment(path)
        for path in self._attachments:
            await self._send_file_async(path)

    # ── Synchronous Feishu API calls (run in thread-pool) ─────────────────────

    def _create_streaming_card_sync(self) -> str | None:
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
            resp = self._client.cardkit.v1.card.create(req)
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

            self._do_send(
                "interactive",
                json.dumps({"type": "card", "data": {"card_id": card_id}}),
            )
            return card_id
        except Exception as exc:
            logger.warning("Feishu streaming card create error: %s", exc)
            return None
        finally:
            self._trace_latency(
                "stream_card_create_finished",
                card_id=card_id,
                success=bool(card_id),
                duration_ms=f"{(time.perf_counter() - started_at) * 1000:.1f}",
            )

    def _stream_update_text_sync(
        self, card_id: str, content: str, sequence: int
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
            resp = self._client.cardkit.v1.card_element.content(req)
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
            self._trace_latency(
                "stream_card_update_finished",
                card_id=card_id,
                sequence=sequence,
                content_len=len(content),
                success=success,
                duration_ms=f"{(time.perf_counter() - started_at) * 1000:.1f}",
            )

    def _close_streaming_mode_sync(self, card_id: str, sequence: int) -> bool:
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
            resp = self._client.cardkit.v1.card.settings(req)
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
            self._trace_latency(
                "stream_card_close_finished",
                card_id=card_id,
                sequence=sequence,
                success=success,
                duration_ms=f"{(time.perf_counter() - started_at) * 1000:.1f}",
            )

    def _upload_image_sync(self, file_path: Path) -> str | None:
        from lark_oapi.api.im.v1 import (  # type: ignore[import]
            CreateImageRequest,
            CreateImageRequestBody,
        )

        try:
            with Path(file_path).open("rb") as handle:
                req = (
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image_type("message")
                        .image(handle)
                        .build()
                    )
                    .build()
                )
                resp = self._client.im.v1.image.create(req)
                if resp.success():
                    return resp.data.image_key
                logger.error(
                    "Feishu image upload failed: code=%s msg=%s",
                    resp.code,
                    resp.msg,
                )
        except Exception as exc:
            logger.error("Feishu image upload error: %s", exc)
        return None

    def _upload_file_sync(self, file_path: Path) -> str | None:
        from lark_oapi.api.im.v1 import (  # type: ignore[import]
            CreateFileRequest,
            CreateFileRequestBody,
        )

        path = Path(file_path)
        file_type = self._FILE_TYPE_MAP.get(path.suffix.lower(), "stream")
        try:
            if not path.exists():
                logger.error("Feishu file upload skipped, file not found: %s", path)
                return None
            size_bytes = path.stat().st_size
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
                resp = self._client.im.v1.file.create(req)
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

    def _extract_written_path(self, result: str) -> Optional[Path]:
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or not payload.get("ok"):
            return None
        path = payload.get("path")
        if isinstance(path, str) and path:
            return Path(path)
        return None

    def _collect_output_dir_files(self) -> list[Path]:
        if self._output_dir is None or not self._output_dir.exists():
            return []

        files: list[Path] = []
        for candidate in self._output_dir.rglob("*"):
            if not candidate.is_file():
                continue
            try:
                if candidate.stat().st_mtime >= self._turn_start:
                    files.append(candidate)
            except OSError:
                continue
        return sorted(files)

    def _append_progress_text(self, line: str) -> None:
        if not line.strip():
            return
        self._progress_phase_closed = False
        if not self._progress_buf.text:
            self._progress_buf.text = "## Multi-Agent Progress\n\n" + line
            return
        self._progress_buf.text += "\n\n" + line

    def _format_mode_aware_subagent_event(self, event: SubAgentProgressEvent) -> str:
        if event.kind == "batch_started":
            formatted = self._format_batch_started_event(event)
            if formatted:
                return formatted
        if event.kind == "batch_finished":
            formatted = self._format_batch_finished_event(event)
            if formatted:
                return formatted
        return self._format_subagent_event(event)

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            candidate = int(value)
            return candidate if candidate > 0 else None
        return None

    @classmethod
    def _event_spec_count(cls, event: SubAgentProgressEvent) -> int | None:
        return cls._positive_int(event.metrics.get("spec_count")) or cls._positive_int(
            event.total
        )

    @classmethod
    def _format_seconds(cls, value: Any, *, precision: int) -> str | None:
        if isinstance(value, bool):
            return None
        if not isinstance(value, (int, float)):
            return None
        return f"{float(value):.{precision}f}s"

    @staticmethod
    def _pluralize(value: int, singular: str) -> str:
        return singular if value == 1 else f"{singular}s"

    def _format_batch_started_event(self, event: SubAgentProgressEvent) -> str | None:
        metrics = event.metrics or {}
        mode = str(metrics.get("execution_mode", "") or "").strip().lower()
        spec_count = self._event_spec_count(event)
        if mode == "parallel" and spec_count is not None:
            max_parallel_agents = self._positive_int(metrics.get("max_parallel_agents"))
            if max_parallel_agents is None:
                return None
            return (
                f"Parallel batch: {spec_count} subtasks, "
                f"max concurrency {max_parallel_agents}"
            )
        if mode == "pipeline" and spec_count is not None:
            return (
                f"Pipeline batch: {spec_count} subtasks, "
                "dependency-driven execution"
            )
        if mode == "rendezvous" and spec_count is not None:
            max_rounds = self._positive_int(
                metrics.get("max_rounds") or metrics.get("max_rendezvous_rounds")
            )
            if max_rounds is not None:
                return (
                    f"Rendezvous batch: {spec_count} subtasks, "
                    f"max {max_rounds} {self._pluralize(max_rounds, 'round')}"
                )
            return f"Rendezvous batch: {spec_count} subtasks"
        return None

    def _format_batch_finished_event(self, event: SubAgentProgressEvent) -> str | None:
        metrics = event.metrics or {}
        mode = str(metrics.get("execution_mode", "") or "").strip().lower()
        spec_count = self._event_spec_count(event)
        completed = self._positive_int(event.completed) or 0
        duration = self._format_seconds(metrics.get("duration_seconds"), precision=2)

        if mode == "parallel" and spec_count is not None and completed and duration:
            line = f"Parallel batch finished: {completed}/{spec_count} in {duration}"
            scope_check = self._format_seconds(
                metrics.get("write_scope_check_seconds"),
                precision=3,
            )
            if scope_check is not None:
                line += f" (scope check {scope_check})"
            return line

        if mode == "pipeline" and spec_count is not None and completed and duration:
            stage_count = self._positive_int(metrics.get("stage_count"))
            if stage_count is None:
                return None
            prefix = (
                "Pipeline batch ended early"
                if completed < spec_count
                else "Pipeline batch finished"
            )
            return (
                f"{prefix}: {completed}/{spec_count} across {stage_count} "
                f"{self._pluralize(stage_count, 'stage')} in {duration}"
            )

        if mode == "rendezvous" and spec_count is not None and duration:
            rounds_completed = self._positive_int(metrics.get("rounds_completed"))
            if rounds_completed is None:
                return None
            return (
                f"Rendezvous batch finished: {spec_count} subtasks, "
                f"{rounds_completed} {self._pluralize(rounds_completed, 'round')} "
                f"in {duration}"
            )

        return None

    @staticmethod
    def _format_subagent_event(event: SubAgentProgressEvent) -> str:
        role = event.role or "agent"
        kind = event.kind
        # Phase events: runtime generates good messages; prefix with an
        # emoji for visual hierarchy.
        if kind == "phase_started":
            prefix = "▸ "
            return f"{prefix}{event.message}" if event.message else f"{prefix}Started"
        if kind == "phase_finished":
            prefix = "▾ "
            return f"{prefix}{event.message}" if event.message else f"{prefix}Complete"
        if kind == "phase_note":
            return f"💬 {event.message}" if event.message else ""
        # Agent lifecycle events: always use the new structured format
        # since it includes role, status, and duration.
        if kind == "agent_started":
            return f"→ **{role}** started"
        if kind == "agent_finished":
            dur = event.metrics.get("duration_seconds")
            dur_str = f" ({dur:.1f}s)" if isinstance(dur, (int, float)) else ""
            return f"✓ **{role}** finished{dur_str}"
        if kind == "agent_failed":
            return f"✗ **{role}** failed"
        if kind == "agent_retry":
            return f"↻ **{role}** retry"
        # Batch-level events: use the runtime's pre-computed message
        # when available, otherwise a minimal fallback.
        if kind == "batch_started":
            return event.message or f"Starting {event.total} sub-agents"
        if kind == "batch_progress":
            return event.message or f"Running: {event.completed}/{event.total}"
        if kind == "batch_finished":
            return event.message or f"Batch finished: {event.completed}/{event.total}"
        return event.message or ""

    def _do_send(self, msg_type: str, content: str) -> None:
        """Send one message, using Reply API for the first message of a response."""
        from lark_oapi.api.im.v1 import (  # type: ignore[import]
            CreateMessageRequest,
            CreateMessageRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        started_at = time.perf_counter()
        route = "create"
        fallback = False
        success = False
        error: str | None = None
        # First message of this response: try to reply to the user's message
        # so Feishu shows a thread/quote context.
        try:
            if self._reply_message_id and self._first_reply:
                route = "reply"
                self._first_reply = False
                try:
                    req = (
                        ReplyMessageRequest.builder()
                        .message_id(self._reply_message_id)
                        .request_body(
                            ReplyMessageRequestBody.builder()
                            .msg_type(msg_type)
                            .content(content)
                            .build()
                        )
                        .build()
                    )
                    resp = self._client.im.v1.message.reply(req)
                    if resp.success():
                        success = True
                        return
                    fallback = True
                    logger.debug(
                        "Feishu reply failed (%s), falling back to send",
                        resp.msg,
                    )
                except Exception as exc:
                    fallback = True
                    logger.debug("Feishu reply error: %s", exc)

            route = "create"
            req = (
                CreateMessageRequest.builder()
                .receive_id_type(self._receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(self._receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )
            resp = self._client.im.v1.message.create(req)
            if not resp.success():
                logger.error(
                    "Feishu send failed: code=%s msg=%s log_id=%s",
                    resp.code,
                    resp.msg,
                    resp.get_log_id(),
                )
                return
            success = True
        except Exception as exc:
            error = str(exc)
            logger.error("Feishu send error: %s", exc)
        finally:
            if success:
                self._interaction_log(
                    "message_sent",
                    route=route,
                    fallback=fallback,
                    msg_type=msg_type,
                    content_len=len(content),
                    duration_ms=f"{(time.perf_counter() - started_at) * 1000:.1f}",
                )
            self._trace_latency(
                "do_send_finished",
                route=route,
                fallback=fallback,
                msg_type=msg_type,
                success=success,
                error=error,
                duration_ms=f"{(time.perf_counter() - started_at) * 1000:.1f}",
            )

    # ── Format detection ──────────────────────────────────────────────────────

    @classmethod
    def _detect_msg_format(cls, content: str) -> str:
        """Choose the optimal Feishu message format for *content*.

        Returns ``"text"``, ``"post"``, or ``"interactive"``.
        """
        s = content.strip()
        if cls._COMPLEX_MD_RE.search(s):
            return "interactive"
        if len(s) > cls._POST_MAX_LEN:
            return "interactive"
        if cls._SIMPLE_MD_RE.search(s):
            return "interactive"
        if cls._LIST_RE.search(s) or cls._OLIST_RE.search(s):
            return "interactive"
        if cls._MD_LINK_RE.search(s):
            return "post"
        if len(s) <= cls._TEXT_MAX_LEN:
            return "text"
        return "post"

    # ── Post (rich text) builder ──────────────────────────────────────────────

    @classmethod
    def _markdown_to_post(cls, content: str) -> str:
        """Convert markdown (links only) to Feishu post JSON."""
        paragraphs: list[list[dict]] = []
        for line in content.strip().split("\n"):
            elements: list[dict] = []
            last_end = 0
            for m in cls._MD_LINK_RE.finditer(line):
                before = line[last_end : m.start()]
                if before:
                    elements.append({"tag": "text", "text": before})
                elements.append({"tag": "a", "text": m.group(1), "href": m.group(2)})
                last_end = m.end()
            remaining = line[last_end:]
            if remaining:
                elements.append({"tag": "text", "text": remaining})
            if not elements:
                elements.append({"tag": "text", "text": ""})
            paragraphs.append(elements)
        return json.dumps({"zh_cn": {"content": paragraphs}}, ensure_ascii=False)

    # ── Interactive card builder ──────────────────────────────────────────────

    @classmethod
    def _strip_md_formatting(cls, text: str) -> str:
        """Remove inline markdown markers for plain-text surfaces."""
        text = cls._MD_BOLD_RE.sub(r"\1", text)
        text = cls._MD_BOLD_US_RE.sub(r"\1", text)
        text = cls._MD_ITALIC_RE.sub(r"\1", text)
        text = cls._MD_STRIKE_RE.sub(r"\1", text)
        return text

    @classmethod
    def _parse_md_table(cls, table_text: str) -> Optional[dict]:
        """Parse a markdown table string into a Feishu card table element."""
        lines = [ln.strip() for ln in table_text.strip().split("\n") if ln.strip()]
        if len(lines) < 3:
            return None

        def _split(line: str) -> list[str]:
            return [c.strip() for c in line.strip("|").split("|")]

        headers = [cls._strip_md_formatting(h) for h in _split(lines[0])]
        rows = [[cls._strip_md_formatting(c) for c in _split(ln)] for ln in lines[2:]]
        columns = [
            {"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
            for i, h in enumerate(headers)
        ]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [
                {f"c{i}": (r[i] if i < len(r) else "") for i in range(len(headers))}
                for r in rows
            ],
        }

    def _split_headings(self, content: str) -> list[dict]:
        """Split content on headings; convert headings to bold div elements."""
        # Protect code blocks from heading detection
        protected = content
        code_blocks: list[str] = []
        for m in self._CODE_BLOCK_RE.finditer(content):
            code_blocks.append(m.group(1))
            protected = protected.replace(
                m.group(1), f"\x00CODE{len(code_blocks) - 1}\x00", 1
            )

        elements: list[dict] = []
        last_end = 0
        for m in self._HEADING_RE.finditer(protected):
            before = protected[last_end : m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            text = self._strip_md_formatting(m.group(2).strip())
            elements.append(
                {"tag": "div", "text": {"tag": "lark_md", "content": f"**{text}**"}}
            )
            last_end = m.end()
        remaining = protected[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})

        # Restore code blocks
        for i, cb in enumerate(code_blocks):
            for el in elements:
                if el.get("tag") == "markdown":
                    el["content"] = el["content"].replace(f"\x00CODE{i}\x00", cb)

        return elements or [{"tag": "markdown", "content": content}]

    def _build_card_elements(self, content: str) -> list[dict]:
        """Build card elements list from markdown content."""
        elements: list[dict] = []
        last_end = 0
        for m in self._TABLE_RE.finditer(content):
            before = content[last_end : m.start()]
            if before.strip():
                elements.extend(self._split_headings(before))
            parsed = self._parse_md_table(m.group(1))
            elements.append(parsed or {"tag": "markdown", "content": m.group(1)})
            last_end = m.end()
        remaining = content[last_end:]
        if remaining.strip():
            elements.extend(self._split_headings(remaining))
        return elements or [{"tag": "markdown", "content": content}]

    @staticmethod
    def _split_elements_by_table_limit(
        elements: list[dict], max_tables: int = 1
    ) -> list[list[dict]]:
        """Split elements so each group has at most *max_tables* table elements.

        Feishu cards only allow one table per card (API error 11310), so when
        the response contains multiple markdown tables each table goes in its
        own card message.
        """
        if not elements:
            return [[]]
        groups: list[list[dict]] = []
        current: list[dict] = []
        table_count = 0
        for el in elements:
            if el.get("tag") == "table":
                if table_count >= max_tables:
                    if current:
                        groups.append(current)
                    current = []
                    table_count = 0
                current.append(el)
                table_count += 1
            else:
                current.append(el)
        if current:
            groups.append(current)
        return groups or [[]]


# ─────────────────────────────────────────────────────────────────────────────
# FeishuChannel
# ─────────────────────────────────────────────────────────────────────────────


class FeishuChannel(Channel):
    """Feishu/Lark channel using WebSocket long connection.

    No public IP or webhook configuration is needed.  The bot connects
    outbound to Feishu's WebSocket gateway and receives events over the
    persistent connection.  The connection is kept alive automatically and
    reconnects on error with a 5-second back-off.

    Requires:
    - ``lark-oapi`` package installed (``pip install lark-oapi``)
    - App credentials from the Feishu Open Platform (bot capability + im.message.receive_v1 subscription)
    """

    def __init__(self, config: FeishuConfig) -> None:
        self._config = config
        self._client: Any = None
        self._ws_thread: Optional[threading.Thread] = None
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._handler: Optional[Callable] = None
        self._output_dir: Optional[Path] = None
        # Per-chat lock: ensures messages from the same chat are processed
        # one at a time even if they arrive in rapid succession.
        self._chat_locks: dict[str, asyncio.Lock] = {}
        # Ordered LRU cache for message-id deduplication
        self._processed_ids: OrderedDict[str, None] = OrderedDict()

    @staticmethod
    def _register_optional_event(builder: Any, method_name: str, handler: Any) -> Any:
        """Register an event handler only when the SDK supports it."""
        method = inspect.getattr_static(builder, method_name, None)
        if method is None:
            return builder
        method = getattr(builder, method_name, None)
        return method(handler) if callable(method) else builder

    # ── Channel interface ─────────────────────────────────────────────────────

    async def start(
        self,
        handler: Callable[[IncomingMessage, "FeishuOutputSink"], Any],
    ) -> None:
        """Connect to Feishu WebSocket and process messages until stopped."""
        if not LARK_AVAILABLE:
            raise RuntimeError("lark-oapi is not installed. Run: pip install lark-oapi")
        if not self._config.app_id or not self._config.app_secret:
            raise RuntimeError(
                "Feishu channel: app_id and app_secret must be set in config"
            )

        import lark_oapi as lark  # type: ignore[import]

        self._running = True
        self._loop = asyncio.get_running_loop()
        self._handler = handler

        # Lark REST client for sending messages / adding reactions
        self._client = build_feishu_client(self._config)

        # WebSocket event dispatcher
        builder = (
            lark.EventDispatcherHandler.builder(
                self._config.encrypt_key or "",
                self._config.verification_token or "",
            )
            .register_p2_im_message_receive_v1(self._on_message_sync)
        )
        builder = self._register_optional_event(
            builder,
            "register_p2_im_message_reaction_created_v1",
            self._on_reaction_created,
        )
        builder = self._register_optional_event(
            builder,
            "register_p2_im_message_reaction_deleted_v1",
            self._on_reaction_deleted,
        )
        builder = self._register_optional_event(
            builder,
            "register_p2_im_message_message_read_v1",
            self._on_message_read,
        )
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
            self._on_bot_p2p_chat_entered,
        )
        event_handler = builder.build()

        ws_client = lark.ws.Client(
            self._config.app_id,
            self._config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.WARNING,
        )

        # Run the WebSocket client in a dedicated thread.
        # A fresh event loop is created for that thread so that lark_oapi's
        # module-level ``loop = asyncio.get_event_loop()`` picks up an idle
        # loop rather than the already-running main loop (which would raise
        # "This event loop is already running").
        def _run_ws() -> None:
            import time
            import lark_oapi.ws.client as _ws_mod  # type: ignore[import]

            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)
            _ws_mod.loop = ws_loop
            try:
                while self._running:
                    try:
                        ws_client.start()
                    except Exception as exc:
                        logger.warning("Feishu WebSocket error: %s", exc)
                    if self._running:
                        time.sleep(5)  # back-off before reconnect
            finally:
                ws_loop.close()

        self._ws_thread = threading.Thread(
            target=_run_ws, daemon=True, name="feishu-ws"
        )
        self._ws_thread.start()
        logger.info("Feishu bot started (WebSocket long connection)")

        # Keep the coroutine alive until stop() is called
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        self._running = False
        logger.info("Feishu bot stopped")

    def set_output_dir(self, output_dir: Optional[Path]) -> None:
        self._output_dir = output_dir

    def create_sink(self, msg: IncomingMessage) -> FeishuOutputSink:
        assert self._client is not None, "FeishuChannel.start() not called"
        chat_id = msg.metadata["chat_id"]
        chat_type = msg.metadata.get("chat_type", "p2p")
        receive_id_type = "chat_id" if _is_group_chat_type(chat_type) else "open_id"
        return FeishuOutputSink(
            client=self._client,
            receive_id_type=receive_id_type,
            receive_id=chat_id,
            reply_message_id=msg.metadata.get("message_id"),
            trace_id=msg.metadata.get("message_id"),
            output_dir=self._output_dir,
            streaming=self._config.streaming,
        )

    # ── WebSocket event handlers ──────────────────────────────────────────────

    def _on_message_sync(self, data: Any) -> None:
        """Called from the WebSocket thread; schedules async processing."""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    def _on_reaction_created(self, data: Any) -> None:
        """Ignore reaction-created events to avoid SDK 'processor not found' noise."""

    def _on_reaction_deleted(self, data: Any) -> None:
        """Ignore reaction-deleted events to avoid SDK 'processor not found' noise."""

    def _on_message_read(self, data: Any) -> None:
        """Ignore message-read events to avoid SDK 'processor not found' noise."""

    def _on_bot_p2p_chat_entered(self, data: Any) -> None:
        """Ignore bot-entered-chat events to avoid SDK 'processor not found' noise."""

    async def _on_message(self, data: Any) -> None:
        """Process one incoming Feishu message event (main event loop)."""
        try:
            event = data.event
            message = event.message
            sender = event.sender

            # ── Deduplication ────────────────────────────────────────────────
            message_id: str = message.message_id
            if message_id in self._processed_ids:
                return
            self._processed_ids[message_id] = None
            while len(self._processed_ids) > 1000:
                self._processed_ids.popitem(last=False)

            # ── Skip bot self-messages ───────────────────────────────────────
            if sender.sender_type == "bot":
                return

            sender_id: str = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id: str = message.chat_id
            chat_type: str = message.chat_type
            msg_type: str = message.message_type
            _interaction_log(
                "message_received",
                message_id=message_id,
                sender_id=sender_id,
                chat_id=chat_id,
                chat_type=chat_type,
                msg_type=msg_type,
            )

            # ── Allow-list check ─────────────────────────────────────────────
            if self._config.allow_from and sender_id not in self._config.allow_from:
                logger.debug(
                    "Feishu: ignoring message from %s (not in allow_from)", sender_id
                )
                return

            # ── Group policy ─────────────────────────────────────────────────
            is_group_chat = _is_group_chat_type(chat_type)
            if is_group_chat and not self._is_bot_mentioned(message):
                if self._config.group_policy != "open":
                    logger.debug("Feishu: skipping group message (not mentioned)")
                    return

            # ── Add reaction immediately so the user knows we got it ─────────
            await self._add_reaction(message_id, self._config.react_emoji)

            # ── Parse message content ────────────────────────────────────────
            try:
                content_json = json.loads(message.content) if message.content else {}
            except json.JSONDecodeError:
                content_json = {}

            content_parts: list[str] = []

            if msg_type == "text":
                raw = content_json.get("text", "")
                cleaned = _clean_at_mentions(raw)
                if cleaned:
                    content_parts.append(cleaned)

            elif msg_type == "post":
                text, _ = _extract_post_content(content_json)
                if text:
                    content_parts.append(_clean_at_mentions(text))

            elif msg_type in ("image", "audio", "file", "sticker", "media"):
                content_parts.append(f"[{msg_type}]")

            else:
                content_parts.append(f"[{msg_type}]")

            content = "\n".join(content_parts).strip()
            if not content:
                return

            # ── Route to handler with per-chat serialisation ─────────────────
            # Group chats reply to the group (chat_id); DMs reply to the user
            reply_to = chat_id if is_group_chat else sender_id
            msg_obj = IncomingMessage(
                text=content,
                channel_name="feishu",
                metadata={
                    "sender_id": sender_id,
                    "chat_id": reply_to,
                    "chat_type": chat_type,
                    "message_id": message_id,
                    "msg_type": msg_type,
                },
            )
            _interaction_log(
                "message_accepted",
                message_id=message_id,
                sender_id=sender_id,
                chat_id=reply_to,
                chat_type=chat_type,
                msg_type=msg_type,
                text_len=len(content),
                text_preview=_preview_text(content),
            )

            if content.startswith("/send "):
                requested = content[len("/send ") :].strip()
                _interaction_log(
                    "send_command_received",
                    message_id=message_id,
                    chat_id=reply_to,
                    requested_path=requested,
                )
                sink = self.create_sink(msg_obj)
                path = self._resolve_send_path(requested)
                if path is None or not path.is_file():
                    await sink._send_plain_async(f"File not found: {requested}")
                    await sink.drain()
                    return
                await sink._send_file_async(path)
                await sink.drain()
                return

            if self._handler:
                lock = self._chat_locks.setdefault(reply_to, asyncio.Lock())
                async with lock:
                    sink = self.create_sink(msg_obj)
                    _interaction_log(
                        "message_dispatched",
                        message_id=message_id,
                        chat_id=reply_to,
                        sink=type(sink).__name__,
                    )
                    await self._handler(msg_obj, sink)

        except Exception as exc:
            _interaction_log("message_processing_failed", error=str(exc))
            logger.error("Feishu: error processing message: %s", exc)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _is_bot_mentioned(self, message: Any) -> bool:
        """Return True if the bot is @mentioned in *message*."""
        raw = message.content or ""
        if "@_all" in raw:
            return True
        for mention in getattr(message, "mentions", None) or []:
            mid = getattr(mention, "id", None)
            if not mid:
                continue
            # Bot mentions have no user_id but carry a valid ou_* open_id
            if not getattr(mid, "user_id", None) and (
                getattr(mid, "open_id", None) or ""
            ).startswith("ou_"):
                return True
        return False

    def _resolve_send_path(self, raw_path: str) -> Optional[Path]:
        candidate = Path(os.path.expandvars(raw_path)).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        if self._output_dir is not None:
            return (self._output_dir / candidate).resolve()
        return candidate.resolve()

    async def _add_reaction(self, message_id: str, emoji_type: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self._add_reaction_sync, message_id, emoji_type
        )

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> None:
        from lark_oapi.api.im.v1 import (  # type: ignore[import]
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        try:
            req = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                )
                .build()
            )
            resp = self._client.im.v1.message_reaction.create(req)
            if not resp.success():
                logger.debug("Feishu: reaction failed: %s", resp.msg)
        except Exception as exc:
            logger.debug("Feishu: reaction error: %s", exc)
