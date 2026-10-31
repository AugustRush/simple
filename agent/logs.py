"""Where a long-running process leaves evidence.

Why this module exists
----------------------
``simple gateway`` is meant to run for days.  Until now its only output was the terminal
it was started from: close the window, or start it from a launcher, and every record of
what happened -- a scheduler tick that failed, a channel that dropped, a turn that raised
-- went with it.  The one question a person asks about a daemon ("what happened at 3am?")
had no answer, and the honest one was "nothing was kept".

So the gateway and the scheduler attach a rotating file handler under
``~/.agent/logs/``.  Three properties matter, and they are the reason this is a module
with tests rather than two lines in ``cli.py``:

* **It never kills the process.**  A logging failure (read-only home, a full disk, a
  sandbox that refuses the path) returns ``None`` and the process keeps serving without
  the file.  Losing logs is bad; refusing to start because logs cannot be written is
  worse.
* **It is idempotent.**  Starting twice in one process (tests, an embedded gateway) must
  not add a second handler, because two handlers means every line twice and two open
  descriptors on the same file.
* **It is bounded.**  A file that grows without limit turns "I have logs" into "I have a
  full disk", which is the same class of problem as the retry loops this repository has
  already fixed: an unbounded thing nobody watches.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

#: Rotate at 5 MB, keep three generations (~20 MB total).  Small on purpose: this is a
#: personal agent's debug trail, not an audit archive.
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3

FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

#: The marker that tells an already-attached file apart from any other handler.
_HANDLER_NAME = "agent-file-log"


def logs_dir(home: Optional[Path] = None) -> Path:
    """``<agent home>/logs``, without creating it."""
    if home is None:
        from agent import shared

        home = shared.AGENT_HOME
    return Path(home) / "logs"


def attach_file_log(
    command: str = "gateway",
    *,
    home: Optional[Path] = None,
    level: int = logging.INFO,
) -> Optional[Path]:
    """Also write the runtime log to ``<home>/logs/<command>.log``.

    Returns the path when a handler was attached (or already was), and ``None`` when it
    could not be -- never raises.  The caller prints the path, because a log nobody can
    find is the same as no log.
    """
    target_dir = logs_dir(home)
    path = target_dir / f"{command}.log"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    root = logging.getLogger()
    if any(getattr(handler, "name", "") == _HANDLER_NAME for handler in root.handlers):
        # Already attached in this process.  Returning the path keeps the caller's
        # "here is where to look" line truthful.
        return path

    try:
        from logging.handlers import RotatingFileHandler

        handler = RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
        )
    except (OSError, ValueError):
        return None
    handler.name = _HANDLER_NAME
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(FORMAT))

    # The root logger already has the console handler from `basicConfig`; the file gets
    # the same level so the two trails agree.
    if root.level == logging.WARNING and level == logging.INFO:
        root.setLevel(level)
    root.addHandler(handler)
    return path


def describe(path: Optional[Path]) -> str:
    """The line a caller prints: where the trail is, or that there is none."""
    if path is None:
        return "log file unavailable (the logs directory could not be written)"
    try:
        shown = path if os.path.isabs(path) else Path.cwd() / path
        return str(shown)
    except OSError:  # pragma: no cover - pathlib is not supposed to raise here
        return str(path)
