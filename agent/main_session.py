"""The one conversation the app opens.

Why this exists
---------------
A conversation is a container for a turn log; it is not something a person
should have to manage. This product has one relationship -- the person, and the
agent that works for them -- and every "new conversation" button asked them to
decide which container a sentence belonged to. The honest answer was always the
same one.

So the install remembers one session id and hands it back on boot. It is not a
pin or a favourite: it is *the* conversation. Old sessions stay on disk and stay
addressable (``#/t/<id>`` still opens one), and a scheduled run still writes into
the session its task was born in -- what disappears is the idea that the person
starts a conversation to talk to their agent.

Why a file and not a column
---------------------------
The session registry is derived (it merges the registry table with the journal),
and a "main" flag there would have to survive every migration of a table that is
already rebuilt from the journals on some paths. One pointer, written by one
process, read at boot: a file says exactly that much.

The id is re-checked against the live list on every read: a conversation that was
deleted is not "the main one" any more, and a pointer to it would leave the app
opening a thread no socket can be attached to.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent import shared

#: The pointer file, inside the agent home. Name says what it holds rather than
#: what wrote it: `main_session.json` is readable by a person debugging an
#: install, which a hashed or dotted name would not be.
FILE_NAME = "main_session.json"


def _path() -> Path:
    """Resolved per call, not at import.

    ``AGENT_HOME`` is replaced at runtime (tests, ``--home``, the web channel's
    per-session homes), and a module-level path would keep writing to whichever
    home happened to be configured the first time this module was imported.
    """
    return Path(shared.AGENT_HOME) / FILE_NAME


def read_main_session_id() -> str:
    """The recorded conversation id, or ``""`` when nothing is recorded.

    An unreadable or malformed file reads as "nothing recorded" rather than
    raising: the cost of the fallback is one new session, and the cost of an
    exception here is an app that will not open.
    """
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("session_id") or "").strip()


def write_main_session_id(session_id: str) -> None:
    """Record *session_id* as the conversation, atomically.

    Written through a temporary file and renamed: a half-written pointer that
    parses as JSON but carries no id would silently start a second conversation
    on the next boot.
    """
    clean = str(session_id or "").strip()
    path = _path()
    if not clean:
        clear_main_session_id()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps({"session_id": clean}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def clear_main_session_id() -> None:
    """Forget the recorded conversation (used when it is deleted)."""
    try:
        _path().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # A pointer we cannot remove is re-validated on the next read, so the
        # failure costs nothing worse than one extra check.
        pass


def resolve_main_session(*, create) -> tuple[str, bool]:
    """The conversation id, minting one when nothing is recorded.

    The pointer is authoritative -- it is not re-checked against the session list
    on every boot. Enumerating sessions to validate one id was both expensive
    (two stores on every page load) and wrong: a session that was just created
    exists by construction but may not be *listed* yet (the registry is derived
    from the journal and lags an untouched session), so the check minted a second
    conversation on the second call. Deletion clears this file instead, which is
    the one event that can make the pointer point at nothing.

    ``create`` is the service's own session factory, passed in rather than
    imported: this module stays a pointer, not a second way to make sessions.

    Returns ``(session_id, created)``. ``created`` is what tells the caller
    whether an id was just minted -- the only difference between booting into an
    existing conversation and booting into a blank one.
    """
    current = read_main_session_id()
    if current:
        return current, False
    session_id = str(create() or "").strip()
    if session_id:
        write_main_session_id(session_id)
    return session_id, True
