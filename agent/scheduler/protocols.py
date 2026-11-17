"""The delivery-target registry's interface, as a protocol.

Why this is a protocol and not just a class
-------------------------------------------
Every capability service in this tree declares what a *provider* has to offer, so
that "a plugin provides ``delivery_targets`` instead" is a claim with a shape
rather than a name. ``tests/test_service_contracts.py`` holds this one against
``DeliveryTargetRegistry``: same members, same parameter names, same async-ness.

The surface is deliberately four methods plus the view factory, and each one is
here because a caller exists:

* ``sender_for`` / ``types`` -- ``SchedulerDelivery.deliver_channel`` (the lookup,
  and the names in its refusal);
* ``register`` / ``unregister`` -- ``ScopedDeliveryTargets.add`` and the undo it
  records, which is how a contribution is reversed by identity;
* ``scoped`` -- the kernel's ``PluginContext``, which is how a plugin reaches this
  as ``ctx.delivery_targets``.

Nothing here is "for completeness". A member nobody calls is a guess, and this
file's guard fails on one.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

__all__ = ["DeliverySender", "DeliveryTargetService"]

#: ``async def send(*, target, text, cfg, output_dir) -> str``
DeliverySender = Callable[..., Awaitable[str]]


@runtime_checkable
class DeliveryTargetService(Protocol):
    """What a provider of ``delivery_targets`` has to offer."""

    def sender_for(self, target_type: str) -> Optional[DeliverySender]:
        """The sender for one target type, or ``None`` when nothing claims it."""

    def types(self) -> "tuple[str, ...]":
        """Every target type this install can deliver to."""

    def register(
        self,
        target_type: str,
        sender: DeliverySender,
        *,
        owner: str = "",
        replace: bool = False,
    ) -> Callable[[], None]:
        """Add one target type; returns the undo that removes exactly it."""

    def unregister(self, target_type: str) -> None:
        """Drop one target type, whatever registered it."""

    def scoped(self, ctx: Any) -> Any:
        """A per-plugin view: ``add`` is attributed and unwinds with the plugin."""
