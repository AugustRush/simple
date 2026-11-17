"""System jobs: the scheduled work that maintains this install, as a registry.

Why this exists
---------------
``build_scheduler_service`` had one supported system job:

    if job_name == "memory_tidy":
        memory_palace.force_tidy()
        await memory_palace.tidy()
        return ExecutionResult(summary="memory tidied", text_output="")
    raise RuntimeError(f"Unsupported system job: {job_name}")

So a plugin could not add periodic maintenance -- reindexing a search index,
rotating a log, refreshing a cache, pruning a third party's export directory.
Every one of those is a task the scheduler is already the right shape for: a
trigger, a run row, a history, a report when it fails. The only missing piece was
that the *body* had to be written in this file.

The shape
---------
A **job** is one async callable per name:

    async def run(*, task, run, payload, cfg) -> ExecutionResult

It owns its whole result, including its summary, because only the job knows what
"done" looked like. It must not retry or catch broadly: the run's own retry
policy and failure classification wrap it, and a job that swallowed its error
would make both lie -- the same rule the delivery senders follow.

Why the name is a plain string
------------------------------
It is already a plain string in the task payload (``payload["job_name"]``), and
it is written into task definitions a person can edit through
``automation_create``. An open string means the refusal happens at run time and
names the jobs that *do* exist, which is the answer somebody can act on.

Why this is a separate registry from delivery targets
----------------------------------------------------
Two different questions with two different failures. A delivery target answers
"where does the output go"; a system job answers "what does the run do". Folding
them into one registry would mean a name registered for one purpose could be
looked up for the other.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from agent.kernel.service import ScopedContribution

__all__ = [
    "SystemJob",
    "SystemJobRegistry",
    "ScopedSystemJobs",
    "shared_registry",
]

#: ``async def run(*, task, run, payload, cfg) -> ExecutionResult``
SystemJob = Callable[..., Awaitable[Any]]


class SystemJobRegistry:
    """Which system jobs exist, and what runs them."""

    def __init__(self) -> None:
        self._jobs: dict[str, SystemJob] = {}
        self._owners: dict[str, str] = {}

    # -- writing ------------------------------------------------------------

    def register(
        self,
        job_name: str,
        job: SystemJob,
        *,
        owner: str = "",
        replace: bool = False,
    ) -> Callable[[], None]:
        """Add one job. Returns the undo that removes exactly it, or restores
        whatever it replaced.

        A silent overwrite is refused for the same reason it is in the delivery
        registry: the job name lives in task definitions on disk, so which
        implementation answers it must not depend on load order.
        """
        clean = str(job_name or "").strip()
        if not clean:
            raise ValueError("a system job needs a name")
        if not callable(job):
            raise ValueError(f"system job {clean!r} needs a callable")
        if clean in self._jobs and not replace:
            existing = self._owners.get(clean) or "a built-in"
            raise ValueError(f"system job {clean!r} is already provided by {existing}")
        previous = self._jobs.get(clean)
        previous_owner = self._owners.get(clean, "")
        self._jobs[clean] = job
        self._owners[clean] = str(owner or "")

        def undo() -> None:
            if self._jobs.get(clean) is not job:
                return
            if previous is not None:
                self._jobs[clean] = previous
                self._owners[clean] = previous_owner
            else:
                self.unregister(clean)

        return undo

    def unregister(self, job_name: str) -> None:
        clean = str(job_name or "").strip()
        self._jobs.pop(clean, None)
        self._owners.pop(clean, None)

    # -- reading ------------------------------------------------------------

    def job_for(self, job_name: str) -> Optional[SystemJob]:
        return self._jobs.get(str(job_name or "").strip())

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._jobs))

    def scoped(self, ctx: Any) -> "ScopedSystemJobs":
        return ScopedSystemJobs(self, ctx)


class ScopedSystemJobs(ScopedContribution):
    """One plugin's view: attribution plus an undo that unwinds with it."""

    def add(
        self,
        job_name: str,
        job: SystemJob,
        *,
        replace: bool = False,
    ) -> None:
        self._contribute(
            self._registry.register(job_name, job, owner=self.owner, replace=replace)
        )

    def names(self) -> tuple[str, ...]:
        return self._registry.names()


#: The process-wide registry. Module-level for the same reason the delivery one
#: is: which maintenance jobs this install has is a property of the process, and
#: the scheduler builder may be reached without a composition (a test, a one-off
#: script).
_SHARED = SystemJobRegistry()


def shared_registry() -> SystemJobRegistry:
    return _SHARED
