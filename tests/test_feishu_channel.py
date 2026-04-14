"""Tests for channels/feishu.py — FeishuOutputSink, FeishuConfig, helpers,
and the _build_channels factory function in agent.py.

lark-oapi is available in the test environment (verified at collection time).
All Feishu API calls are patched with unittest.mock so no real credentials
are required.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from channels.feishu import (
    FeishuChannel,
    FeishuConfig,
    FeishuOutputSink,
    _clean_at_mentions,
    _extract_post_content,
)
from agent import _build_channels, _active_sink


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
    )


def test_feishu_sink_stream_chunk_accumulation():
    sink = _make_feishu_sink()
    sink.on_stream_chunk("hello ")
    sink.on_stream_chunk("world")
    assert sink._chunks == ["hello ", "world"]
    # No sends scheduled yet
    assert sink._pending == []


def test_feishu_sink_on_turn_complete_schedules_send():
    sink = _make_feishu_sink()
    sink.on_stream_chunk("hi")

    # on_turn_complete must schedule exactly one task
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            sink.on_turn_complete("hi", [])
            assert len(sink._pending) == 1
            assert sink._chunks == []  # cleared

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


def test_feishu_sink_on_tool_start_schedules_hint():
    sink = _make_feishu_sink()
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            sink.on_tool_start("bash", {"command": "ls"})
            assert len(sink._pending) == 1

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


def test_feishu_sink_on_tool_blocked_schedules_notice():
    sink = _make_feishu_sink()
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            sink.on_tool_blocked("bash", "policy violation")
            assert len(sink._pending) == 1

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_feishu_sink_on_error_schedules_message():
    sink = _make_feishu_sink()
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            sink.on_error("something broke")
            assert len(sink._pending) == 1

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


# ─────────────────────────────────────────────────────────────────────────────
# _build_channels factory
# ─────────────────────────────────────────────────────────────────────────────


def test_build_channels_default_is_cli():
    from agent import CliChannel

    channels = _build_channels({})
    assert len(channels) == 1
    assert isinstance(channels[0], CliChannel)


def test_build_channels_feishu_disabled():
    from agent import CliChannel

    cfg = {"channels": {"feishu": {"enabled": False}}}
    channels = _build_channels(cfg)
    assert isinstance(channels[0], CliChannel)


def test_build_channels_feishu_enabled():
    cfg = {
        "channels": {
            "feishu": {
                "enabled": True,
                "app_id": "cli_test",
                "app_secret": "secret",
            }
        }
    }
    channels = _build_channels(cfg)
    assert len(channels) == 1
    assert isinstance(channels[0], FeishuChannel)
    assert channels[0]._config.app_id == "cli_test"


def test_build_channels_feishu_extra_keys_ignored():
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
    channels = _build_channels(cfg)
    assert isinstance(channels[0], FeishuChannel)


def test_build_channels_falls_back_to_cli_on_import_error():
    """If FeishuChannel import fails, _build_channels must fall back to CLI."""
    from agent import CliChannel

    cfg = {
        "channels": {
            "feishu": {
                "enabled": True,
                "app_id": "x",
                "app_secret": "y",
            }
        }
    }
    # Patch the import inside _build_channels by temporarily hiding channels.feishu
    import sys
    import channels.feishu as _feishu_mod  # ensure it's loaded first

    saved = sys.modules.pop("channels.feishu")
    try:
        # With channels.feishu removed from sys.modules and the import guarded
        # by try/except ImportError, _build_channels must return CliChannel.
        # We also need to block re-importing by marking it as None.
        sys.modules["channels.feishu"] = None  # type: ignore[assignment]
        channels = _build_channels(cfg)
        assert isinstance(channels[0], CliChannel)
    finally:
        sys.modules["channels.feishu"] = saved


def _selective_import_error(name, *args, **kwargs):
    import builtins

    if "channels.feishu" in name:
        raise ImportError("mocked import error")
    return builtins.__import__(name, *args, **kwargs)


def _build_channels_fallback(cfg):
    """Used in the fallback test only."""
    from agent import CliChannel, CONSOLE

    return [CliChannel(CONSOLE)]
