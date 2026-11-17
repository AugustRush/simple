"""Provides ``delivery_targets``: where a run's output can be delivered.

Named for what it holds, not for the feature it is about -- the same rule
``MCPServersPlugin`` follows. A service called ``delivery`` would be confusable
with ``SchedulerDelivery``, which is a *consumer* of this registry and not a
provider of it.

Nothing is injected: a registry has no dependencies, which is exactly what lets
a plugin contribute a target type before the scheduler that reads it is built.
That ordering is not incidental -- a contributor whose ``inject`` named the
scheduler would be pending until the scheduler existed, and the scheduler is
built with this registry in hand.
"""

from __future__ import annotations

from typing import Any

__all__ = ["DeliveryTargetsPlugin"]


class DeliveryTargetsPlugin:
    """Provides ``delivery_targets``."""

    name = "delivery_targets"

    inject = ()

    def setup(self, ctx: Any) -> None:
        from agent.scheduler.delivery_targets import DeliveryTargetRegistry

        # One registry per composition, not the process-wide fallback. A
        # plugin's contribution must not land in a singleton: two compositions in
        # one process (a test suite is the obvious one) would collide on the
        # second ``add``. The built-in senders are installed by the constructor,
        # so a fresh registry is never empty.
        ctx.provide("delivery_targets", DeliveryTargetRegistry())
