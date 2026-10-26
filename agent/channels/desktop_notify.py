"""Reaching the person when no page is open: one desktop notification.

Why this exists
---------------
Every other way this system tells you something is *pull*: the 自动化 page, the
red badge on the navigation entry, the run history. All of them wait for you to
come and look, which is exactly what you are not doing when a scheduled task
fails at 07:00 and you open the laptop at 11:00. A personal agent whose only
failure channel is a page you have to visit is an agent that is silent by
default.

What is deliberately narrow
---------------------------
Only runs that a person has to look at get a notification -- the same population
the badge counts (`ATTENTION_STATUSES`, plus the "earlier occurrences were
skipped while nothing was running" note). A notification per successful run
would be noise, and noise is how the one that mattered gets dismissed without
being read.

Only on macOS, and only when ``osascript`` exists. Elsewhere this is a silent
no-op rather than a logged error on every run: a headless Linux gateway has no
notification centre, and complaining about that would be a permanent, useless
line in its log.

What it does not do
-------------------
It does not retry, queue, or fail anything. A notification is a courtesy; a run's
bookkeeping must not depend on whether a banner appeared, so every failure here
is swallowed after a debug line.

The text is passed as **argv**, never interpolated into the AppleScript source:
a task name or an error message can contain a quote, and building the script by
concatenation is how a message becomes code.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys

from agent.scheduler.models import ATTENTION_STATUSES, status_wants_a_person

__all__ = [
    "desktop_notifications_enabled",
    "notify",
    "notify_run_needs_attention",
    "should_notify_run",
]

logger = logging.getLogger(__name__)

#: `osascript` reads its arguments into `argv` when the script declares
#: ``on run argv``, which is what lets the message arrive as *data*.
_SCRIPT = (
    "on run argv\n"
    "  display notification (item 1 of argv) with title (item 2 of argv)\n"
    "end run\n"
)

#: Notification Center truncates hard anyway; cutting here keeps the log line and
#: the banner saying the same thing.
_MAX_MESSAGE_CHARS = 180


def desktop_notifications_enabled(cfg: object) -> bool:
    """Whether this deployment wants desktop notifications.

    Default **on** for a macOS deployment: the alternative is a scheduler that
    fails silently, which is the gap this closes. The switch is one key
    (``notifications.desktop``), and a value that is not a dict is treated as
    "leave the default" rather than as a truthy accident.
    """
    section = cfg.get("notifications") if isinstance(cfg, dict) else None
    if isinstance(section, dict):
        value = section.get("desktop")
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            # `"false"` is a *truthy string*, which is the exact trap the config
            # validator warns about -- but a warning plus "still enabled" would
            # be worse than reading it.  Anything unrecognised keeps the
            # default, and validation says to use a boolean.
            return value.strip().lower() not in {"", "0", "false", "no", "off", "none"}
    return True


def should_notify_run(status: str, missed_count: int = 0) -> bool:
    """The badge's rule, borrowed rather than restated.

    A second definition of "needs a person" would drift from the red badge and
    from the acknowledgement logic, and the drift would be invisible until a
    failure stopped producing either one.  ``status_wants_a_person`` is that one
    definition; this name exists so the notifier's callers read like prose.
    """
    return status_wants_a_person(status, missed_count)


async def notify(title: str, message: str, *, timeout: float = 5.0) -> bool:
    """Post one notification.  ``False`` when the platform cannot, never raises.

    The command's own timeout is not decoration: ``osascript`` can block on a
    permission prompt, and a scheduled run waiting forever on a banner is worse
    than no banner.
    """
    if sys.platform != "darwin":
        return False
    executable = shutil.which("osascript")
    if not executable:
        return False
    text = " ".join(str(message or "").split())[:_MAX_MESSAGE_CHARS] or "（无内容）"
    caption = " ".join(str(title or "").split()) or "Simple Agent"
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "-e",
            _SCRIPT,
            text,
            caption,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as exc:  # pragma: no cover - a platform whose shell refuses
        logger.debug("desktop notification could not start: %s", exc)
        return False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
        logger.debug("desktop notification timed out after %ss", timeout)
        return False
    if process.returncode != 0:
        logger.debug("desktop notification exited %s", process.returncode)
        return False
    return True


async def notify_run_needs_attention(
    *,
    task_name: str,
    status: str,
    summary: str = "",
    error: str = "",
    missed_count: int = 0,
    enabled: bool = True,
) -> bool:
    """Tell the person a scheduled run wants them, if it does and they asked.

    The message is the run's own words -- its error first, because that is the
    question a failed run raises -- and never the agent's self-assessment: a
    notification is a pointer to evidence, not a verdict.
    """
    if not enabled or not should_notify_run(status, missed_count):
        return False
    reason = str(error or "").strip() or str(summary or "").strip() or "运行结束"
    first_line = reason.splitlines()[0] if reason else ""
    label = str(task_name or "").strip() or "定时任务"
    detail = f"{label}：{first_line}"
    if int(missed_count or 0) > 0:
        detail = f"{label}：有 {int(missed_count)} 次触发在停机期间被跳过"
    return await notify("Simple Agent", detail)
