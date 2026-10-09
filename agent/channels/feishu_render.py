"""Turning model output into the shapes the Feishu API accepts.

Why this module exists
----------------------
These eight functions were ``@classmethod``/``@staticmethod`` members of
``FeishuOutputSink``, a 1514-line class.  They are its pure half: choosing a
message format, rendering markdown to post JSON, building card elements,
parsing a table, and splitting text and elements to the sizes Feishu allows.
None of them touches the lark client, a thread, or the event loop -- but they
could only be reached through the sink, and they could only be tested by naming
the sink class.

The rules they encode are the kind that must not drift, because Feishu's
failure mode for violating them is a runtime API error rather than a wrong
answer:

- **One table per card (API error 11310).**  ``split_elements_by_table_limit``
  exists only for that limit, and it is why a response with two tables becomes
  two cards rather than one rejected card.
- **A card markdown element has a character ceiling.**
  ``build_card_elements`` splits an oversized element through
  ``split_long_markdown_text``, which prefers paragraph, then line, then
  sentence boundaries before cutting anywhere.
- **Format follows what the content needs, not its length alone.**  A code
  block, a heading or a table needs a card; a link without them fits in a post;
  short plain text fits in a text message -- with 200 and 2000 as the two
  ceilings, named ``TEXT_MAX_LEN`` and ``POST_MAX_LEN``.

Behaviour is preserved exactly, including two warts this move did not tidy:
``split_long_markdown_text``'s "word boundaries" comment describes an intent
the loop does not implement (it cuts at ``start + limit``), and its long-line
branch mixes three- and four-space indentation.
"""

from __future__ import annotations

import json
import re
from typing import Optional


# Complex markdown (code, tables, headings) → interactive card
COMPLEX_MD_RE = re.compile(
    r"```"
    r"|^\|.+\|.*\n\s*\|[-:\s|]+"  # markdown table
    r"|^#{1,6}\s+",  # headings
    re.MULTILINE,
)
# Simple inline formatting that post/text can't render → card
SIMPLE_MD_RE = re.compile(
    r"\*\*.+?\*\*"
    r"|__.+?__"
    r"|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"
    r"|~~.+?~~",
    re.DOTALL,
)
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
LIST_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)
OLIST_RE = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)


# Card element building
TABLE_RE = re.compile(
    r"((?:^[ \t]*\|.+\|[ \t]*\n)"
    r"(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)"
    r"(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
    re.MULTILINE,
)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)


# Strip inline formatting for plain-text surfaces (table cells, headings)
MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
MD_BOLD_US_RE = re.compile(r"__(.+?)__")
MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
MD_STRIKE_RE = re.compile(r"~~(.+?)~~")


TEXT_MAX_LEN = 200  # plain-text ceiling; longer → post
POST_MAX_LEN = 2000  # post ceiling; longer → interactive card
MD_ELEMENT_CHAR_LIMIT = 5000  # Feishu card markdown element char limit


def detect_msg_format(content: str) -> str:
    """Choose the optimal Feishu message format for *content*.

    Returns ``"text"``, ``"post"``, or ``"interactive"``.
    """
    s = content.strip()
    if COMPLEX_MD_RE.search(s):
        return "interactive"
    if len(s) > POST_MAX_LEN:
        return "interactive"
    if SIMPLE_MD_RE.search(s):
        return "interactive"
    if LIST_RE.search(s) or OLIST_RE.search(s):
        return "interactive"
    if MD_LINK_RE.search(s):
        return "post"
    if len(s) <= TEXT_MAX_LEN:
        return "text"
    return "post"


def markdown_to_post(content: str) -> str:
    """Convert markdown (links only) to Feishu post JSON."""
    paragraphs: list[list[dict]] = []
    for line in content.strip().split("\n"):
        elements: list[dict] = []
        last_end = 0
        for m in MD_LINK_RE.finditer(line):
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


def strip_md_formatting(text: str) -> str:
    """Remove inline markdown markers for plain-text surfaces."""
    text = MD_BOLD_RE.sub(r"\1", text)
    text = MD_BOLD_US_RE.sub(r"\1", text)
    text = MD_ITALIC_RE.sub(r"\1", text)
    text = MD_STRIKE_RE.sub(r"\1", text)
    return text


def parse_md_table(table_text: str) -> Optional[dict]:
    """Parse a markdown table string into a Feishu card table element."""
    lines = [ln.strip() for ln in table_text.strip().split("\n") if ln.strip()]
    if len(lines) < 3:
        return None

    def _split(line: str) -> list[str]:
        return [c.strip() for c in line.strip("|").split("|")]

    headers = [strip_md_formatting(h) for h in _split(lines[0])]
    rows = [[strip_md_formatting(c) for c in _split(ln)] for ln in lines[2:]]
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


def split_headings(content: str) -> list[dict]:
    """Split content on headings; convert headings to bold div elements."""
    # Protect code blocks from heading detection
    protected = content
    code_blocks: list[str] = []
    for m in CODE_BLOCK_RE.finditer(content):
        code_blocks.append(m.group(1))
        protected = protected.replace(
            m.group(1), f"\x00CODE{len(code_blocks) - 1}\x00", 1
        )

    elements: list[dict] = []
    last_end = 0
    for m in HEADING_RE.finditer(protected):
        before = protected[last_end : m.start()].strip()
        if before:
            elements.append({"tag": "markdown", "content": before})
        text = strip_md_formatting(m.group(2).strip())
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


def build_card_elements(content: str) -> list[dict]:
    """Build card elements list from markdown content."""
    elements: list[dict] = []
    last_end = 0
    for m in TABLE_RE.finditer(content):
        before = content[last_end : m.start()]
        if before.strip():
            elements.extend(split_headings(before))
        parsed = parse_md_table(m.group(1))
        elements.append(parsed or {"tag": "markdown", "content": m.group(1)})
        last_end = m.end()
    remaining = content[last_end:]
    if remaining.strip():
        elements.extend(split_headings(remaining))
    # Split any markdown elements that exceed the Feishu char limit
    result: list[dict] = []
    for el in (elements or [{"tag": "markdown", "content": content}]):
        if el.get("tag") == "markdown" and len(el.get("content", "")) > MD_ELEMENT_CHAR_LIMIT:
            chunks = split_long_markdown_text(
                el["content"], MD_ELEMENT_CHAR_LIMIT
            )
            for chunk in chunks:
                result.append({"tag": "markdown", "content": chunk})
        else:
            result.append(el)
    return result


def split_long_markdown_text(text: str, limit: int) -> list[str]:
    """Split a long markdown string into chunks ≤ *limit* chars.

    Splits at double-newline (paragraph) boundaries first, then at
    single-newline boundaries within oversized paragraphs, and finally
    at sentence boundaries as a last resort.
    """
    if len(text) <= limit:
        return [text]

    # Split at paragraph boundaries first
    paragraphs = re.split(r"\n\n+", text)
    chunks: list[str] = []
    for para in paragraphs:
        if len(para) <= limit:
            if chunks and len(chunks[-1]) + len(para) + 2 <= limit:
                chunks[-1] += "\n\n" + para
            else:
                chunks.append(para)
        else:
            # Paragraph too long — split at line boundaries
            lines = para.split("\n")
            for line in lines:
                if len(line) <= limit:
                   if chunks and len(chunks[-1]) + len(line) + 1 <= limit:
                       chunks[-1] += "\n" + line
                   else:
                       chunks.append(line)
                else:
                   # Single line too long — hard split at sentence or word boundaries
                   # Try sentence boundaries first
                   sentences = re.split(r"(?<=[.。!！?？])\s+", line)
                   for sent in sentences:
                       if len(sent) <= limit:
                           if chunks and len(chunks[-1]) + len(sent) + 1 <= limit:
                               chunks[-1] += " " + sent
                           else:
                               chunks.append(sent)
                       else:
                           # Hard split at word boundaries
                           start = 0
                           while start < len(sent):
                               if chunks and chunks[-1]:
                                   sep = " "
                                   avail = limit - len(chunks[-1]) - 1
                                   if avail >= 20:
                                       chunk = sent[start:start + avail]
                                       chunks[-1] += sep + chunk
                                       start += avail
                                       continue
                               end = min(start + limit, len(sent))
                               chunks.append(sent[start:end])
                               start = end
    return [c.strip() for c in chunks if c.strip()]


def split_elements_by_table_limit(
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
