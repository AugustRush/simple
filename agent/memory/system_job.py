"""``memory_tidy`` as a registered system job rather than a branch in the builder.

What moved
----------
The body lived in ``build_scheduler_service``:

    if job_name == "memory_tidy":
        memory_palace.force_tidy()
        await memory_palace.tidy()
        return ExecutionResult(summary="memory tidied", text_output="")

which meant the scheduler's builder knew how to tidy memory. It is a plugin now,
and the difference is not cosmetic: it is the difference between "the scheduler
can run maintenance" and "the scheduler can run whatever maintenance is
registered", which is what makes a plugin's own periodic job possible.

Why it is a plugin that injects ``memory``
------------------------------------------
The job needs the palace, and ``memory`` is the service that provides it. A
closure over the builder's local would work only when the builder happened to be
handed a palace; declaring the dependency is what makes "this composition has no
memory service" a state the plugin is *pending* in rather than a crash at 3am
inside a scheduled run.
"""

from __future__ import annotations

from typing import Any

__all__ = ["MemoryTidyJobPlugin", "JOB_NAME"]

JOB_NAME = "memory_tidy"


class MemoryTidyJobPlugin:
    """Registers the ``memory_tidy`` system job."""

    name = "system_job.memory_tidy"

    #: ``system_jobs`` because ``ctx.system_jobs`` is an attribute access, and the
    #: kernel refuses one that was not declared -- the declaration is what keeps a
    #: plugin from reading a service whose provider has not come up yet.
    inject = ("memory", "system_jobs")

    def setup(self, ctx: Any) -> None:
        memory = ctx.get("memory")

        async def tidy(*, task, run, payload, cfg):
            from agent.scheduler.models import ExecutionResult

            # ``force_tidy`` then ``tidy``: the first drops the "have I written
            # enough files to be worth it" guard, because a scheduled job is a
            # deliberate request rather than an opportunistic one.
            memory.force_tidy()
            await memory.tidy()
            return ExecutionResult(summary="memory tidied", text_output="")

        ctx.system_jobs.add(JOB_NAME, tidy)
