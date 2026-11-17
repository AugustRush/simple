"""Provides ``system_jobs``: the scheduled maintenance this install can run.

Named for what it holds. Nothing is injected -- a registry has no dependencies,
which is what lets a plugin contribute a job before the scheduler that runs it is
built; a contributor whose ``inject`` named the scheduler would be pending until
the scheduler existed, and the scheduler is built with this registry in hand.
"""

from __future__ import annotations

from typing import Any

__all__ = ["SystemJobsPlugin"]


class SystemJobsPlugin:
    """Provides ``system_jobs``."""

    name = "system_jobs"

    inject = ()

    def setup(self, ctx: Any) -> None:
        from agent.scheduler.system_jobs import SystemJobRegistry

        # One registry per composition. A job a plugin registers must not land in
        # a process-wide singleton: two compositions in one process would collide
        # on the second registration, which is what a test suite does every time.
        ctx.provide("system_jobs", SystemJobRegistry())
