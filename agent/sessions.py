"""Session discovery helpers for the ``--name`` session model.

In this model a session is a named agent home (``~/.agent-<name>``) managed by
one live process at a time.  Configuration is shared by default: a session only
uses its own ``config.json`` when one is explicitly placed in its home.

This module is intentionally small — it only lists discoverable session homes
and their basic shape.  The heavy lifting stays in the existing memory and
runtime layers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent import shared


@dataclass
class SessionInfo:
    name: str
    home: str
    config_source: str
    has_palace_db: bool
    is_current: bool


def _config_source(home: Any) -> str:
    try:
        if (home / "config.json").is_file():
            return "session"
    except OSError:
        pass
    return "shared"


def list_sessions() -> list[SessionInfo]:
    """Return discoverable sessions, default first.

    The default session is always listed even if its directory does not exist
    yet; named sessions are only listed when their directory already exists.
    """
    infos: list[SessionInfo] = []
    current = str(shared.AGENT_HOME)
    for name, home in shared.iter_session_homes():
        if name != "default" and not home.is_dir():
            continue
        config_source = "shared" if name == "default" else _config_source(home)
        infos.append(
            SessionInfo(
                name=name,
                home=str(home),
                config_source=config_source,
                has_palace_db=(home / "context" / "palace.db").is_file(),
                is_current=str(home) == current,
            )
        )
    return infos


def session_lines() -> list[str]:
    """Render sessions as a Markdown table for ``/sessions``."""
    infos = list_sessions()
    if not infos:
        return []
    lines = ["## Sessions", "", "Name | Config | Data | Home"]
    lines.append("--- | --- | --- | ---")
    for info in infos:
        name = info.name.replace("|", "\\|")
        marker = " (current)" if info.is_current else ""
        config = "own" if info.config_source == "session" else "shared"
        data = "ready" if info.has_palace_db else "empty"
        home = info.home.replace("|", "\\|")
        lines.append(f"{name}{marker} | {config} | {data} | {home}")
    return lines
