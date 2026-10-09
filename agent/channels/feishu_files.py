"""Naming and typing the files that arrive from Feishu.

Why this module exists
----------------------
A resource Feishu hands over is addressed by an opaque key and arrives with a
filename that may be empty, relative, or a full path somebody else chose.  Three
small translations stand between that and a file on disk:

- ``safe_resource_filename`` keeps only the basename and falls back when there
  is nothing left -- the boundary between a name that came off the wire and a
  path this process is about to write to.
- ``attachment_mime_type`` prefers the name's own type and only then the message
  type, because ``.png`` on a file Feishu called "image" is the better answer.
- ``resource_fallback_filename`` invents a name from the resource key when the
  message carried none, so a download is never anonymous on disk.

They were ``@staticmethod``/methods on ``FeishuChannel``, inside a 1973-line
module, and they are pure: no client, no session, no state.  They have no direct
tests of their own -- the download path exercises them -- which is exactly why
they are worth a file a reader can check without the SDK.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path


def safe_resource_filename(filename: str, fallback: str) -> str:
    name = Path(str(filename or fallback)).name.strip()
    return name or fallback


def attachment_mime_type(path: Path, msg_type: str) -> str:
    guessed = mimetypes.guess_type(path.name)[0]
    if guessed:
        return guessed
    if msg_type == "image":
        return "image/png"
    if msg_type == "audio":
        return "audio/mpeg"
    if msg_type in ("media", "video"):
        return "video/mp4"
    return "application/octet-stream"


def resource_fallback_filename(resource_key: str, resource_type: str) -> str:
    if resource_type == "image":
        return f"{resource_key}.png"
    if resource_type == "audio":
        return f"{resource_key}.mp3"
    if resource_type in ("media", "video"):
        return f"{resource_key}.mp4"
    return resource_key
