"""The scheduler as a plugin, so a composition can be one without it.

What was there
--------------
Two CLI commands built the scheduler inline: the `scheduler` service and the gateway
that runs it in the background.  Both did the same five things -- resolve the run
parameters, take the store, build the service, run it, close the store -- and both knew
which services the builder reads.

As a plugin, "this composition has no scheduler" is a state a composition can be in, and
`/schedule` reports a missing service rather than half-working.  That is the same gain
Ralph's plugin produced, and it is the plan's point about execution pieces.

The store belongs to the plugin, and that is not the same as belonging to the service
----------------------------------------------------------------------------------------
An earlier attempt made ``SchedulerService.shutdown`` close the store and was reverted:
a caller may legitimately read the store after the service stops, and the tests did.  The
service is *given* the store; it does not own it.  The plugin **creates** it, so the
plugin closes it -- registered on its own scope, at the moment of creation, the way every
other registration in this kernel works.

Which is why the store's lifetime cannot be delegated to the service's ``shutdown``:
``shutdown`` is a coroutine and scope unwinding is synchronous, so an async teardown
cannot be scope-owned.  Closing a store is synchronous, so this half can be.

The two parameters a plugin cannot get from `ctx` are the two the CLI resolves
--------------------------------------------------------------------------------
``SchedulerSettings`` comes from config *and* argv, and the config loader comes from
``--bundle``.  A plugin has only a context, so both arrive as data the caller puts in the
composition before mounting -- see ``_scheduler_inputs`` in ``agent/cli.py``.  Nothing
here imports the CLI, and nothing here reads ``sys.argv``.
"""

from __future__ import annotations

from typing import Any

from agent.scheduler.build import build_scheduler_service
from agent.scheduler.settings import SchedulerSettings
from agent.scheduler.store import scheduler_store

__all__ = ["SchedulerPlugin"]


class SchedulerPlugin:
    """Provides ``scheduler``."""

    name = "scheduler"

    #: What this cannot work without.  ``memory`` because the builder read it as a hard
    #: subscript -- a composition without it must leave the plugin *pending*, not fail
    #: inside a run.  ``runtime_factory`` because a run that finds a changed config on
    #: disk builds its own runtime, and the factory's default would reach for the
    #: composition root, which is what this plugin exists to avoid.
    inject = ("memory", "runtime_factory")

    def setup(self, ctx: Any) -> None:
        cfg = ctx.get("cfg", None) or {}
        # Resolved by the caller when argv had something to say; otherwise from the
        # config, which is what a composition with no flags means.
        settings = ctx.get("scheduler_settings", None) or SchedulerSettings.resolve(cfg)

        store = scheduler_store()
        # Registered here rather than closed by each caller: this plugin created it, and
        # a store closed on one path and not another is the leak that arrangement
        # produces.  Synchronous, so it can be scope-owned.
        ctx.on_unwind(store.close)

        service, _store = build_scheduler_service(
            cfg,
            poll_seconds=settings.poll_seconds,
            lease_seconds=settings.lease_seconds,
            max_concurrent_runs=settings.max_concurrent_runs,
            signal_max_depth=settings.signal_max_depth,
            memory=ctx.get("memory"),
            output_dir=ctx.get("output_dir", None),
            workspace_root=ctx.get("workspace_root", None),
            registry=ctx.get("registry", None),
            # A named bundle's loader, when the caller named one.  Absent means the
            # builder's default: read the config from disk for each run.
            config_loader=ctx.get("scheduler_config_loader", None),
            runtime_factory=ctx.get("runtime_factory", None),
            # Handed in rather than opened here.  This plugin owns the store; a builder
            # that opened its own would leave this scope's `close` reaching a connection
            # nothing used, while the service's connection stayed open -- two handles to
            # one file and only one of them ever closed.
            store=store,
        )
        ctx.provide("scheduler", service)

        # The builder was handed this very store, so the handle it returns is the same
        # object and needs no second close.  Asserted rather than assumed: a second
        # `close` would be silent -- sqlite tolerates it -- while a `close` on a store
        # the service is still using is not.
        assert _store is store, "the builder must use the store it was given"
