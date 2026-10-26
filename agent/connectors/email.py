"""A read-only IMAP mailbox: search and read, never send and never change a flag.

Why IMAP and not an MCP server
------------------------------
There is no official MCP server for a personal mailbox, and the third-party ones
all need their own daemon and their own credential handling.  ``imaplib`` is in the
standard library and speaks the protocol the mail provider already offers, so a
mailbox can be reached without adding a runtime or a proxy.

The three things that make this read-only rather than "read-only by convention"
-----------------------------------------------------------------------------
1. The mailbox is selected with ``readonly=True``.  The server itself refuses a
   write in that mode, so this is enforced below the code that could get it wrong.
2. Bodies are fetched with ``BODY.PEEK[]``, which is the IMAP spelling of "show me
   this without marking it seen".  A plain ``BODY[]`` would quietly mark every
   message the agent read as read -- visible to the person in their own mailbox,
   and impossible to undo from here.
3. Only ``login`` / ``select`` / ``search`` / ``fetch`` / ``logout`` are ever
   issued.  There is no function in this module that could emit ``STORE``,
   ``COPY``, ``MOVE``, ``EXPUNGE`` or ``APPEND``, so no prompt or parameter can
   reach one.

Search is a substring match done here, not IMAP ``SEARCH``
----------------------------------------------------------
IMAP's own search needs a per-server charset story for non-ASCII and behaves
differently on every provider; a Chinese query is the common case for this agent
and the worst case for that.  Instead one batched ``FETCH`` of the newest window of
headers comes back in a single round trip, and the match runs on this side, where
"contains" means the same thing for every provider.
"""

from __future__ import annotations

import email
import imaplib
import os
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from typing import Any

from .contract import ConnectorStatus

__all__ = ["EmailConnector", "EmailSettings"]

DEFAULT_PORT = 993
#: How many of the newest messages one search looks through.  One batched fetch, so
#: this bounds the bytes off the server rather than the number of round trips.
DEFAULT_SEARCH_WINDOW = 60
#: A body longer than this is cut, and the reply says it was cut.  "Read my whole
#: inbox" must not be able to spend a context window on one newsletter.
MAX_BODY_CHARS = 20000
#: Results are capped well below the window: the window decides what is *looked at*,
#: this decides what is *returned*.
MAX_RESULTS = 50


@dataclass(frozen=True)
class EmailSettings:
    """Everything the connector needs, with no secret read from the config file."""

    host: str = ""
    port: int = DEFAULT_PORT
    user: str = ""
    password: str = ""
    mailbox: str = "INBOX"
    search_window: int = DEFAULT_SEARCH_WINDOW
    timeout: float = 15.0
    #: The variable the password *would* come from, so a reason can name it.
    password_env: str = ""
    #: A secret found in the config file itself.  Recorded rather than used:
    #: the config file is what gets pasted into issues and committed to dotfiles.
    password_in_config: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password)

    def missing(self) -> list[str]:
        missing: list[str] = []
        if not self.host:
            missing.append("host")
        if not self.user:
            missing.append("user")
        if not self.password:
            missing.append(
                f"password (env var {self.password_env or 'EMAIL_PASSWORD'})"
            )
        return missing

    @classmethod
    def from_config(cls, cfg: Any) -> "EmailSettings":
        """Read ``connectors.email``; the password comes from the environment.

        A literal ``password`` key is *detected and refused* rather than honoured:
        silently accepting it would mean the first person to try the obvious thing
        commits their mailbox password to a dotfiles repo.  The status then says so
        in a sentence that names the key to use instead.
        """
        section = (
            (cfg.get("connectors") or {}).get("email")
            if isinstance(cfg, dict) and isinstance(cfg.get("connectors"), dict)
            else None
        )
        if not isinstance(section, dict):
            return cls()
        password_env = str(section.get("password_env") or "EMAIL_PASSWORD").strip()
        password = os.environ.get(password_env, "") if password_env else ""
        return cls(
            host=str(section.get("host") or "").strip(),
            port=_bounded_int(section.get("port"), DEFAULT_PORT, 1, 65535),
            user=str(section.get("user") or "").strip(),
            password=password,
            mailbox=str(section.get("mailbox") or "INBOX").strip() or "INBOX",
            search_window=_bounded_int(
                section.get("search_window"), DEFAULT_SEARCH_WINDOW, 1, 500
            ),
            timeout=float(section.get("timeout") or 15.0),
            password_env=password_env,
            password_in_config=bool(str(section.get("password") or "").strip()),
        )


def _bounded_int(raw: Any, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _decode_header(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return str(make_header(decode_header(text)))
    except Exception:
        return text


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", text)).strip()


def _text_body(message: Message) -> "tuple[str, bool]":
    """The message's readable text: plain part first, HTML as the fallback.

    Attachments are skipped by content type rather than by disposition, because a
    part with no disposition and a non-text type is an attachment in every way that
    matters here.  The flag returned says whether the cut happened, so the tool can
    tell the model it is looking at a prefix instead of letting it assume it has the
    whole message.
    """
    plain = ""
    html = ""
    for part in message.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if ctype == "text/plain" and not plain:
            plain = text
        elif ctype == "text/html" and not html:
            html = _strip_html(text)
    body = (plain or html).replace("\r\n", "\n").strip()
    if len(body) > MAX_BODY_CHARS:
        return body[:MAX_BODY_CHARS], True
    return body, False


class EmailConnector:
    """One mailbox, read-only."""

    name = "email"
    kind = "email"

    def __init__(self, settings: EmailSettings) -> None:
        self.settings = settings

    # -- status ------------------------------------------------------------

    def status(self) -> ConnectorStatus:
        settings = self.settings
        detail = {
            "host": settings.host,
            "user": settings.user,
            "mailbox": settings.mailbox,
            "search_window": settings.search_window,
        }
        if settings.password_in_config:
            return ConnectorStatus(
                self.name,
                self.kind,
                False,
                reason=(
                    "the password is written into the config file. Move it to the "
                    f"environment (variable {settings.password_env or 'EMAIL_PASSWORD'}) "
                    "and leave only password_env in the config."
                ),
                detail=detail,
            )
        missing = settings.missing()
        if missing:
            return ConnectorStatus(
                self.name,
                self.kind,
                False,
                reason=(
                    "not configured yet: "
                    + ", ".join(missing)
                    + ". Fill in host / user / password_env under connectors.email in "
                    "config.json."
                ),
                detail=detail,
            )
        return ConnectorStatus(self.name, self.kind, True, detail=detail)

    # -- reading -----------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> "list[dict[str, Any]]":
        """Messages whose subject or sender contains *query*, newest first.

        An empty *query* means "the newest few", which is the useful default for
        "what came in today".
        """
        raw_limit = _bounded_int(limit, 10, 1, MAX_RESULTS)
        needle = str(query or "").strip().casefold()
        client = self._open()
        try:
            status, data = client.search(None, "ALL")
            if status != "OK":
                raise RuntimeError(f"IMAP search failed ({status})")
            uids = (data[0].split() if data and data[0] else [])
            window = uids[-self.settings.search_window :]
            if not window:
                return []
            status, rows = client.fetch(
                b",".join(window).decode(), "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])"
            )
            if status != "OK":
                raise RuntimeError(f"IMAP fetch failed ({status})")
            headers = self._parse_headers(rows)
        finally:
            self._close(client)

        found: list[dict[str, Any]] = []
        for item in headers:
            if needle and needle not in (
                f"{item['subject']}\n{item['from']}".casefold()
            ):
                continue
            found.append(item)
        found.sort(key=lambda entry: int(entry["uid"]), reverse=True)
        return found[:raw_limit]

    def read(self, item_id: str) -> "dict[str, Any]":
        """One message in full, by the ``uid`` a search returned.

        Deliberately a full fetch of one message and not a "range": the caller has an
        id it got from this connector, so there is nothing to guess at.
        """
        uid = str(item_id or "").strip()
        if not uid.isdigit():
            raise ValueError("item_id must be the numeric uid a search returned")
        client = self._open()
        try:
            status, rows = client.fetch(uid, "(BODY.PEEK[])")
            if status != "OK":
                raise RuntimeError(f"邮箱读取失败（{status}）")
        finally:
            self._close(client)
        raw = next(
            (row[1] for row in (rows or []) if isinstance(row, tuple) and len(row) > 1),
            None,
        )
        if raw is None:
            raise KeyError(f"no such message: {uid}")
        message = email.message_from_bytes(raw)
        body, truncated = _text_body(message)
        return {
            "uid": uid,
            "subject": _decode_header(message.get("Subject", "")),
            "from": _decode_header(message.get("From", "")),
            "date": _decode_header(message.get("Date", "")),
            "body": body,
            "truncated": truncated,
        }

    # -- connection --------------------------------------------------------

    def _open(self) -> Any:
        """Log in and select the mailbox **read-only**.

        Read-only is enforced by the server from here on: in this mode IMAP itself
        rejects a write, which is a stronger guarantee than a code review of this
        file.  It is also why ``select`` is the only state-changing call that exists.
        """
        settings = self.settings
        if not settings.configured:
            raise RuntimeError("mailbox is not configured: " + ", ".join(settings.missing()))
        client = imaplib.IMAP4_SSL(settings.host, settings.port, timeout=settings.timeout)
        try:
            client.login(settings.user, settings.password)
            status, _ = client.select(settings.mailbox, readonly=True)
            if status != "OK":
                raise RuntimeError(
                    f"cannot open mailbox {settings.mailbox!r} ({status})"
                )
        except Exception:
            self._close(client)
            raise
        return client

    @staticmethod
    def _close(client: Any) -> None:
        """Log out, best effort.

        ``logout`` closes the selected mailbox without expunging it, which is the
        only close that is safe on a read-only selection.  A failure here must not
        mask the error that made us close in the first place, so it is swallowed --
        the connection is gone either way once the process drops it.
        """
        try:
            client.logout()
        except Exception:
            pass

    @staticmethod
    def _parse_headers(rows: Any) -> "list[dict[str, Any]]":
        """One ``FETCH`` response into one row per message.

        The batched form returns a flat list with ``(meta, payload)`` tuples and
        literal ``b")"`` separators between messages; anything that is not a tuple
        is framing.  Parsed rather than trusted to be well-formed, because this is
        the server's bytes.
        """
        items: list[dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, tuple) or len(row) < 2:
                continue
            meta, payload = row[0], row[1]
            text = meta.decode("ascii", errors="replace") if isinstance(meta, bytes) else str(meta)
            head = text.split(" ", 1)[0].strip()
            if not head.isdigit():
                continue
            headers = email.message_from_bytes(payload or b"")
            items.append(
                {
                    "uid": head,
                    "subject": _decode_header(headers.get("Subject", "")),
                    "from": _decode_header(headers.get("From", "")),
                    "date": _decode_header(headers.get("Date", "")),
                }
            )
        return items
