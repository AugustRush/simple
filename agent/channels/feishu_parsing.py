"""What an inbound Feishu payload means.

Why this module exists
----------------------
Three pure translations between Feishu's wire format and the agent's: an
@-mention placeholder is not text and is stripped before anyone reads the
message; ``group_chat`` and ``chat`` are what the agent calls a group; and a
*post* message arrives in one of three shapes (direct, localized, wrapped)
whose text and image keys have to come back as one string and a list.

They were module-level functions in ``agent/channels/feishu.py``, above 2048
lines of channel and sink.  They import nothing from Feishu and nothing from
lark -- which is why they are the part of that file a reader can verify without
the SDK, and why tests can call them directly.

The three-shape handling in ``extract_post_content`` is what earns the file:
the same message looks different depending on whether it arrived direct, under
a locale key, or wrapped in ``{"post": ...}``, and a reader that handled only
one shape would drop the text of a message that arrived in another.
"""

from __future__ import annotations

import re
from typing import Optional


# Regex to strip Feishu @-mention placeholders (@_user_N) from message text.
_AT_PLACEHOLDER_RE = re.compile(r"@_user_\d+\s*")


def clean_at_mentions(text: str) -> str:
    """Remove @_user_N placeholders left by Feishu in message text."""
    return _AT_PLACEHOLDER_RE.sub("", text).strip()


def is_group_chat_type(chat_type: str) -> bool:
    normalized = str(chat_type or "").strip().lower()
    return normalized in {"group", "group_chat", "chat"}


def extract_post_content(content_json: dict) -> tuple[str, list[str]]:
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
