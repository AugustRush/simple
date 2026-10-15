"""Where one web session's files live, and what happens when its id cannot be resolved.

Three one-line functions and a rule, which is why they are not inline at the call sites: the
rule is that an id which cannot be resolved gets a **shared** ``unknown`` home rather than
sharing a real one, and it has to be the same rule for the output directory and the upload
directory or the isolation this channel exists to provide has a hole in it.

Extracted from ``agent/channels/web.py`` (see the big-file survey's extraction order), and
re-imported there so the existing callers and tests keep the path they have always used.
"""

from __future__ import annotations

import logging
from pathlib import Path

from agent import shared

logger = logging.getLogger(__name__)

__all__ = [
    "_web_session_dir",
    "_web_session_output_dir",
    "_web_session_upload_dir",
]


def _web_session_dir(session_id: str, leaf: str) -> Path:
    """One directory inside a session's isolated home.

    The two callers below differ only in the leaf, so the rule that matters --
    resolve the session's home, and if its id cannot be resolved say so *loudly*
    rather than share a directory with another session -- lives here once.  Every
    caller validates the id first, so reaching the fallback means something
    upstream let an unresolvable id through, and two sessions would then share
    one home: the isolation the web channel exists to provide.
    """
    try:
        return shared.web_session_home(str(session_id)) / leaf
    except ValueError:
        logger.warning(
            "unresolvable web session id, sharing the unknown %s home: %r",
            leaf,
            session_id,
        )
        return shared.AGENT_HOME / "web" / "sessions" / "unknown" / leaf


def _web_session_output_dir(session_id: str) -> Path:
    """The session's output directory: where its artifacts are written."""
    return _web_session_dir(session_id, "output")


def _web_session_upload_dir(session_id: str) -> Path:
    """The session's upload directory: where the browser's uploads land."""
    return _web_session_dir(session_id, "uploads")
