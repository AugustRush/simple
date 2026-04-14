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
import json
import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional

# Import base abstractions from agent.  These are always available because
# agent.py is the entry point and channels/ is only imported after it loads.
from agent import (
    Channel,
    IncomingMessage,
    OutputSink,
    _active_sink,
    _fmt_tool_inputs,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

LARK_AVAILABLE = importlib.util.find_spec("lark_oapi") is not None

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
    enabled: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Message parsing helpers  (adapted from nanobot reference implementation)
# ─────────────────────────────────────────────────────────────────────────────

# Regex to strip Feishu @-mention placeholders (@_user_N) from message text.
_AT_PLACEHOLDER_RE = re.compile(r"@_user_\d+\s*")


def _clean_at_mentions(text: str) -> str:
    """Remove @_user_N placeholders left by Feishu in message text."""
    return _AT_PLACEHOLDER_RE.sub("", text).strip()


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

    Streaming chunks are accumulated silently (Feishu does not support
    real-time message editing via the basic API).  After
    ``on_turn_complete()`` is called the full response is sent using the
    optimal message format selected by ``_detect_msg_format()``.

    Tool-start events are surfaced as small "Tool Call" code-block cards so
    the user sees progress while the agent works.

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

    _TEXT_MAX_LEN = 200  # plain-text ceiling; longer → post
    _POST_MAX_LEN = 2000  # post ceiling; longer → interactive card

    def __init__(
        self,
        client: Any,
        receive_id_type: str,
        receive_id: str,
        reply_message_id: Optional[str] = None,
    ) -> None:
        self._client = client
        self._receive_id_type = receive_id_type
        self._receive_id = receive_id
        self._reply_message_id = reply_message_id
        self._chunks: list[str] = []
        self._pending: list[asyncio.Task] = []
        self._first_reply = True  # first send of this response uses Reply API

    # ── OutputSink interface ──────────────────────────────────────────────────

    def on_stream_chunk(self, chunk: str) -> None:
        """Accumulate chunks; send everything at once in on_turn_complete."""
        self._chunks.append(chunk)

    def on_turn_complete(self, full_text: str, tool_calls: list[str]) -> None:
        text = full_text or "".join(self._chunks)
        self._chunks.clear()
        if text.strip():
            self._schedule(self._send_response_async(text))

    def on_tool_start(self, name: str, inputs: dict) -> None:
        hint = f"{name}{_fmt_tool_inputs(name, inputs)}"
        self._schedule(self._send_tool_hint_async(hint))

    def on_tool_end(self, name: str, result: str) -> None:
        pass  # result is internal; don't surface on Feishu

    def on_tool_blocked(self, name: str, reason: str) -> None:
        self._schedule(self._send_plain_async(f"🚫 Tool `{name}` blocked: {reason}"))

    def on_error(self, error: str) -> None:
        self._schedule(self._send_plain_async(f"❌ {error}"))

    def on_info(self, content: Any) -> None:
        if isinstance(content, str):
            self._schedule(self._send_plain_async(content))

    def on_status(self, text: str, *, level: str = "info") -> None:
        if level in ("error", "warning"):
            self._schedule(self._send_plain_async(text))

    async def drain(self) -> None:
        """Await all pending send tasks before the handler returns."""
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
            self._pending.clear()

    # ── Internal scheduling ───────────────────────────────────────────────────

    def _schedule(self, coro: Any) -> None:
        """Schedule a coroutine as a tracked asyncio task."""
        try:
            task = asyncio.ensure_future(coro)
            self._pending.append(task)
        except RuntimeError:
            logger.warning("FeishuOutputSink: no running event loop to schedule send")

    # ── Async send helpers ────────────────────────────────────────────────────

    async def _send_response_async(self, text: str) -> None:
        """Send the final response using the optimal Feishu message format."""
        loop = asyncio.get_running_loop()
        fmt = self._detect_msg_format(text)

        if fmt == "text":
            body = json.dumps({"text": text.strip()}, ensure_ascii=False)
            await loop.run_in_executor(None, self._do_send, "text", body)

        elif fmt == "post":
            body = self._markdown_to_post(text)
            await loop.run_in_executor(None, self._do_send, "post", body)

        else:  # "interactive" — full card with table/heading support
            elements = self._build_card_elements(text)
            for group in self._split_elements_by_table_limit(elements):
                card = {"config": {"wide_screen_mode": True}, "elements": group}
                await loop.run_in_executor(
                    None,
                    self._do_send,
                    "interactive",
                    json.dumps(card, ensure_ascii=False),
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

    # ── Synchronous Feishu API calls (run in thread-pool) ─────────────────────

    def _do_send(self, msg_type: str, content: str) -> None:
        """Send one message, using Reply API for the first message of a response."""
        from lark_oapi.api.im.v1 import (  # type: ignore[import]
            CreateMessageRequest,
            CreateMessageRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        # First message of this response: try to reply to the user's message
        # so Feishu shows a thread/quote context.
        if self._reply_message_id and self._first_reply:
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
                    return
                logger.debug("Feishu reply failed (%s), falling back to send", resp.msg)
            except Exception as exc:
                logger.debug("Feishu reply error: %s", exc)

        try:
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
        except Exception as exc:
            logger.error("Feishu send error: %s", exc)

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
        # Per-chat lock: ensures messages from the same chat are processed
        # one at a time even if they arrive in rapid succession.
        self._chat_locks: dict[str, asyncio.Lock] = {}
        # Ordered LRU cache for message-id deduplication
        self._processed_ids: OrderedDict[str, None] = OrderedDict()

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
        self._client = (
            lark.Client.builder()
            .app_id(self._config.app_id)
            .app_secret(self._config.app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

        # WebSocket event dispatcher
        event_handler = (
            lark.EventDispatcherHandler.builder(
                self._config.encrypt_key or "",
                self._config.verification_token or "",
            )
            .register_p2_im_message_receive_v1(self._on_message_sync)
            .build()
        )

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

    def create_sink(self, msg: IncomingMessage) -> FeishuOutputSink:
        assert self._client is not None, "FeishuChannel.start() not called"
        chat_id = msg.metadata["chat_id"]
        chat_type = msg.metadata.get("chat_type", "p2p")
        receive_id_type = "chat_id" if chat_type == "group" else "open_id"
        return FeishuOutputSink(
            client=self._client,
            receive_id_type=receive_id_type,
            receive_id=chat_id,
            reply_message_id=msg.metadata.get("message_id"),
        )

    # ── WebSocket event handlers ──────────────────────────────────────────────

    def _on_message_sync(self, data: Any) -> None:
        """Called from the WebSocket thread; schedules async processing."""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

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

            # ── Allow-list check ─────────────────────────────────────────────
            if self._config.allow_from and sender_id not in self._config.allow_from:
                logger.debug(
                    "Feishu: ignoring message from %s (not in allow_from)", sender_id
                )
                return

            # ── Group policy ─────────────────────────────────────────────────
            if chat_type == "group" and not self._is_bot_mentioned(message):
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
            reply_to = chat_id if chat_type == "group" else sender_id
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

            if self._handler:
                lock = self._chat_locks.setdefault(reply_to, asyncio.Lock())
                async with lock:
                    sink = self.create_sink(msg_obj)
                    await self._handler(msg_obj, sink)

        except Exception as exc:
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
