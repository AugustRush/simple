"""The patrol, running on a clock, in the process that is already awake.

Why it lives beside the gateway
-------------------------------
The patrol's input is *silence*: it looks for sessions nobody is in and things
nobody has looked at.  A patrol that only runs when a command is typed finds
nothing, because typing a command is the opposite of silence.  So it runs on a
timer, in the process that is already serving -- and it is deliberately the
cheapest thing that can be: a rules pass over a list, once every N minutes,
with no model call and no tool call.

Why it is not in the scheduler loop
-----------------------------------
The scheduler's loop is about *tasks that are due*: it claims rows, holds
leases, and every tick is a write.  The patrol is about *things that might be
worth doing*: read-only, no lease, no queue.  Putting them in one loop would
mean the patrol's cadence is a function of the task poll, and a patrol that
runs at the scheduler's interval (seconds) would scan the session list far more
often than anything it looks for changes.

What it does with its findings
------------------------------
Writes them.  This delivery has no execution path at all: every candidate
becomes a row in ``proposals.db``, with the admission verdict beside it.  The
loop's only effect on the world is a row and a log line.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from . import PatrolOptions, patrol_tick
from .admission import AdmissionPolicy
from .rules import rule_candidates
from .store import ProposalStore, open_proposal_store

__all__ = ["PatrolService", "PatrolSettings"]

logger = logging.getLogger(__name__)

UTC = timezone.utc


@dataclass
class PatrolSettings:
    """How often, and how much.  Every number is a guess the rows will correct."""

    enabled: bool = True
    #: Minutes between passes.  Ten: the facts the rules read (unreviewed
    #: failures, stalled queues, unfinished tasks) do not change faster than that,
    #: and a faster pass would only scan the same list more often.
    interval_minutes: int = 10
    #: What may be done without asking.  Zero auto-actions is the shipped default:
    #: shadow mode, which is what this delivery is.
    max_auto_actions_per_day: int = 0
    max_proposals_per_day: int = 20
    min_confidence: float = 0.5
    #: A session somebody touched in the last N minutes is skipped.
    warm_window_seconds: int = 15 * 60
    #: A session silent for longer than this is skipped.
    stale_window_seconds: int = 7 * 24 * 60 * 60

    def admission(self) -> AdmissionPolicy:
        return AdmissionPolicy(
            max_auto_actions_per_day=self.max_auto_actions_per_day,
            max_proposals_per_day=self.max_proposals_per_day,
            min_confidence=self.min_confidence,
        )

    def options(self) -> PatrolOptions:
        return PatrolOptions(
            warm_window_seconds=self.warm_window_seconds,
            stale_window_seconds=self.stale_window_seconds,
            admission=self.admission(),
        )

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "PatrolSettings":
        """Read the ``autonomy`` section, tolerating anything in it being absent.

        A missing section means **enabled with shadow defaults**, because the
        feature's whole first version is harmless: it writes rows and nothing
        else.  When a later version can act, the default has to flip to off --
        and the line to change is here, not in the loop.
        """
        section = config.get("autonomy") if isinstance(config, dict) else None
        if not isinstance(section, dict):
            return cls()
        settings = cls()
        for key in (
            "enabled",
            "interval_minutes",
            "max_auto_actions_per_day",
            "max_proposals_per_day",
            "min_confidence",
            "warm_window_seconds",
            "stale_window_seconds",
        ):
            if key not in section:
                continue
            value = section[key]
            try:
                if key == "enabled":
                    setattr(settings, key, bool(value))
                elif key == "min_confidence":
                    setattr(settings, key, float(value))
                else:
                    setattr(settings, key, int(value))
            except (TypeError, ValueError):
                # A value nobody can read is a value nobody meant: keep the
                # default and say so, rather than crashing a serving gateway.
                logger.warning("autonomy.%s is not a number: %r", key, value)
        settings.interval_minutes = max(1, settings.interval_minutes)
        return settings


@dataclass
class PatrolService:
    """One patrol pass every ``interval``, until stopped."""

    store: ProposalStore
    settings: PatrolSettings = field(default_factory=PatrolSettings)
    #: How the session list is read.  Injected so the service is testable without
    #: a gateway, and so the one place that knows how to enumerate sessions stays
    #: where it already is.
    sessions_provider: Optional[Callable[[], list[dict[str, Any]]]] = None
    on_result: Optional[Callable[[dict[str, Any]], None]] = None
    _task: Optional[asyncio.Task[None]] = None
    _stop: Optional[asyncio.Event] = None

    # -- lifetime ----------------------------------------------------------

    def start(self) -> bool:
        """Begin patrolling.  ``False`` when there is nothing to patrol with.

        Never raises: this is started from a gateway's startup, and a patrol that
        cannot find its sessions is a feature that is off, not a reason for the
        process to fail.
        """
        if not self.settings.enabled:
            return False
        if self.sessions_provider is None:
            logger.warning("patrol not started: no session provider was given")
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if self._task is not None and not self._task.done():
            return True
        self._stop = asyncio.Event()
        self._task = loop.create_task(self._run(), name="autonomy-patrol")
        return True

    async def stop(self) -> None:
        task, self._task = self._task, None
        if self._stop is not None:
            self._stop.set()
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown
            pass

    def interval_seconds(self) -> float:
        """The wait between passes, in seconds.

        A method rather than an inline expression because the *test* of "a failing
        pass does not stop the loop" needs a short interval, and waiting a real
        minute to observe the second pass is a test nobody runs.  The production
        value is the configured minutes, floored at one.
        """
        return max(1, int(self.settings.interval_minutes)) * 60

    async def _run(self) -> None:
        interval = self.interval_seconds()
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a patrol failure is not a gateway failure
                logger.exception("patrol tick failed; the next pass will try again")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    # -- one pass ----------------------------------------------------------

    async def tick(self, now: Optional[datetime] = None) -> dict[str, Any]:
        """One pass: read the sessions, propose, write.  Nothing else."""
        sessions: list[dict[str, Any]] = []
        if self.sessions_provider is not None:
            try:
                sessions = list(self.sessions_provider() or [])
            except Exception:  # noqa: BLE001 - a broken list is an empty pass
                logger.exception("patrol could not read the session list")
                sessions = []
        result = patrol_tick(
            store=self.store,
            sessions=sessions,
            now=now,
            options=self.settings.options(),
            suggest=rule_candidates,
        )
        # Expiry is part of the pass: a proposal nobody answered is not live
        # forever, and the table is what the next pass reads.
        try:
            result["expired"] = self.store.expire_stale(now=now)
        except Exception:  # noqa: BLE001
            result["expired"] = 0
        if result.get("proposed"):
            logger.info(
                "patrol: %s proposals from %s sessions (dropped %s, duplicates %s)",
                result["proposed"], result["examined"],
                result.get("dropped", 0), result.get("duplicates", 0),
            )
        if self.on_result is not None:
            try:
                self.on_result(result)
            except Exception:  # noqa: BLE001 - a listener must not break the pass
                logger.exception("patrol result listener failed")
        return result


def open_patrol_service(
    settings: Optional[PatrolSettings] = None,
    sessions_provider: Optional[Callable[[], list[dict[str, Any]]]] = None,
) -> PatrolService:
    """The service, wired to the default store."""
    return PatrolService(
        store=open_proposal_store(),
        settings=settings or PatrolSettings(),
        sessions_provider=sessions_provider,
    )
