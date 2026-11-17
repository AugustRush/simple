"""Where a run's output can be delivered, as a registry rather than a branch.

Why this exists
---------------
``SchedulerDelivery.deliver_channel`` had exactly one supported target:

    if target.target_type != "feishu_chat":
        raise ValueError(f"Unsupported channel target: {target.target_type}")

So "deliver this to Slack", "deliver this by email", "deliver this to a webhook
the operator runs" were not capabilities a plugin could add -- they were edits to
this file. Every other kind of contribution in this tree is a capability service
(tools, skills, channels, commands, agents, MCP servers); delivery was the one
that stayed a hardcoded branch.

The shape
---------
A **sender** is one async callable per target type:

    async def send(*, target, text, cfg, output_dir) -> str

It returns the delivery status (``"delivered"`` or ``"sent"``) and raises on
failure -- ``deliver`` owns the retry loop, so a sender must not retry. A sender
that swallowed its own errors would make the retry count a lie.

Registration is ``registry.register(target_type, sender)``; a plugin reaches it
through ``ctx.delivery_targets.add(...)``, whose undo unwinds with the plugin --
the same capability-service shape as ``ctx.channels`` and ``ctx.tools``.

Why the target type is a plain string
-------------------------------------
It is already a plain string on the row (``delivery_target.target_type``), and it
is written into task definitions the person edited. A closed enum here would
mean a plugin's target type is rejected by a validator in a module the plugin
cannot see; an open string means the refusal happens at delivery time, naming
the types that *are* available -- which is the answer somebody can act on.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from agent.kernel.service import ScopedContribution

__all__ = [
    "DeliverySender",
    "DeliveryTargetRegistry",
    "ScopedDeliveryTargets",
    "register_delivery_target",
    "shared_registry",
]

#: ``async def send(*, target, text, cfg, output_dir) -> str``
DeliverySender = Callable[..., Awaitable[str]]


class DeliveryTargetRegistry:
    """Which target types can be delivered to, and by whom.

    Not a service with policy: it owns the records and nothing else. The retry
    loop, the standalone write and the "was anything produced" decision all stay
    in ``SchedulerDelivery``, because they are the same for every target type.
    """

    def __init__(self) -> None:
        self._senders: dict[str, DeliverySender] = {}
        #: target_type -> the plugin that registered it ("" for a built-in).
        #: Kept because "which plugin owns this" is the question a broken
        #: delivery asks, and the registry is the only place that knows.
        self._owners: dict[str, str] = {}

    # -- writing ------------------------------------------------------------

    def register(
        self,
        target_type: str,
        sender: DeliverySender,
        *,
        owner: str = "",
        replace: bool = False,
    ) -> Callable[[], None]:
        """Add one target type. Refuses a silent overwrite.

        Two plugins claiming one target type is a composition error, not a
        last-one-wins: the task definitions on disk name a type, and which
        implementation answers it would depend on load order.
        """
        clean = str(target_type or "").strip()
        if not clean:
            raise ValueError("a delivery target needs a type name")
        if not callable(sender):
            raise ValueError(f"delivery target {clean!r} needs a callable sender")
        if clean in self._senders and not replace:
            existing = self._owners.get(clean) or "a built-in"
            raise ValueError(
                f"delivery target {clean!r} is already provided by {existing}"
            )
        # What this replaces, so the undo can put it back.  Captured before the
        # write, because "restore the previous one" and "delete the type" are
        # different answers and only the first is right: a plugin that replaced
        # the built-in and then unloaded must leave the built-in in place, or
        # every task that delivers to that type breaks on an unload that had
        # nothing to do with it.
        previous = self._senders.get(clean)
        previous_owner = self._owners.get(clean, "")
        self._senders[clean] = sender
        self._owners[clean] = str(owner or "")

        def undo() -> None:
            # By identity: a second plugin that has since claimed the type must
            # not be removed by this one's unload.
            if self._senders.get(clean) is not sender:
                return
            if previous is not None:
                self._senders[clean] = previous
                self._owners[clean] = previous_owner
            else:
                self.unregister(clean)

        return undo

    def unregister(self, target_type: str) -> None:
        self._senders.pop(str(target_type or "").strip(), None)
        self._owners.pop(str(target_type or "").strip(), None)

    # -- reading ------------------------------------------------------------

    def sender_for(self, target_type: str) -> Optional[DeliverySender]:
        return self._senders.get(str(target_type or "").strip())

    def types(self) -> tuple[str, ...]:
        return tuple(sorted(self._senders))

    def scoped(self, ctx: Any) -> "ScopedDeliveryTargets":
        return ScopedDeliveryTargets(self, ctx)


class ScopedDeliveryTargets(ScopedContribution):
    """One plugin's view: attribution plus an undo that unwinds with it.

    The same base every other capability view uses (``ScopedChannels``, the tool
    registry's view, ``ScopedSkillCatalog``).  What stays here is only what
    differs: which registry it holds, and what a contribution is.
    """

    def add(
        self,
        target_type: str,
        sender: DeliverySender,
        *,
        replace: bool = False,
    ) -> None:
        """Contribute one target type; removed when the plugin unloads.

        ``_contribute`` is the base's, not re-implemented here: it records the
        undo on the plugin's own scope, which is the whole of "attributed and
        reversible" and the one step two earlier views got wrong by hand.
        """
        self._contribute(
            self._registry.register(
                target_type, sender, owner=self.owner, replace=replace
            )
        )

    def types(self) -> tuple[str, ...]:
        return self._registry.types()

    def has(self, target_type: str) -> bool:
        return self._registry.sender_for(target_type) is not None


#: The registry the process uses.  Module-level for the same reason
#: ``agent/core/providers.py`` keeps one: a delivery target is a property of the
#: process (which integrations this install has), not of a session, and the two
#: callers that construct a ``SchedulerDelivery`` without a composition -- the
#: notification test endpoint, and tests -- must see the same set a run does.
#: A composition may hand its own registry instead; nothing here requires it.
_SHARED = DeliveryTargetRegistry()


def shared_registry() -> DeliveryTargetRegistry:
    return _SHARED


def register_delivery_target(
    target_type: str,
    sender: DeliverySender,
    *,
    replace: bool = False,
) -> None:
    """Module-level registration, for a caller holding no context."""
    _SHARED.register(target_type, sender, owner="", replace=replace)


# The one sender every install has, registered at import so a
# ``SchedulerDelivery`` built without a composition still delivers to Feishu.
# Same precedent as ``agent/core/providers.py``, which registers its two wire
# formats at module level for the same reason.  A plugin that replaces it does
# so through ``ScopedDeliveryTargets.add(..., replace=True)``, which is visible
# as an owner change rather than as a silent substitution.
def _install_builtin() -> None:
    from .delivery_feishu import TARGET_TYPE, send_feishu_chat

    _SHARED.register(TARGET_TYPE, send_feishu_chat)


_install_builtin()
