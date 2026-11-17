"""Where a run's output can be delivered, as a contribution rather than a branch.

``SchedulerDelivery.deliver_channel`` used to be:

    if target.target_type != "feishu_chat":
        raise ValueError(f"Unsupported channel target: {target.target_type}")

So "deliver this to Slack" was not a capability a plugin could add -- it was an
edit to the scheduler. Every other kind of contribution in this tree is a
capability service; delivery was the one that stayed a hardcoded branch.

These tests are written from the *outside*: a plugin contributes a target type
through the same ``ctx`` shape every other capability uses, and a run delivers to
it. Asserting the registry's own methods would only prove the registry works.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.scheduler.delivery import SchedulerDelivery
from agent.scheduler.delivery_targets import DeliveryTargetRegistry
from agent.scheduler.models import DeliveryTarget


class _PluginCtx:
    """The two things ``ScopedContribution`` reads off a plugin's context."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._undos: list = []

    def on_unwind(self, undo) -> None:
        self._undos.append(undo)

    def unload(self) -> None:
        for undo in reversed(self._undos):
            undo()
        self._undos.clear()


def _delivery(registry: DeliveryTargetRegistry) -> SchedulerDelivery:
    return SchedulerDelivery(cfg={}, targets=registry)


def test_a_plugin_can_add_a_target_type_a_run_then_delivers_to() -> None:
    """The whole point: a type the scheduler has never heard of, delivered to."""
    sent: list[tuple[str, str]] = []

    async def send_webhook(*, target, text, cfg, output_dir=None):
        sent.append((target.payload["url"], text))
        return "delivered"

    registry = DeliveryTargetRegistry()
    ctx = _PluginCtx("my-webhook")
    registry.scoped(ctx).add("webhook", send_webhook)

    result = asyncio.run(
        _delivery(registry).deliver_channel(
            target=DeliveryTarget("webhook", {"url": "https://example.test/hook"}),
            text="今天筛出 3 只",
        )
    )

    assert result == "delivered"
    assert sent == [("https://example.test/hook", "今天筛出 3 只")]


def test_an_unknown_target_type_names_what_is_available() -> None:
    """"Unsupported: slack" leaves the reader guessing whether the name or the
    capability is wrong -- and those want different fixes."""
    registry = DeliveryTargetRegistry()

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(
            _delivery(registry).deliver_channel(
                target=DeliveryTarget("slack", {"chat_id": "x"}), text="hi"
            )
        )

    message = str(excinfo.value)
    assert "slack" in message
    assert "can deliver to" in message, message


def test_a_plugin_that_replaced_a_target_does_not_take_it_with_it_on_unload() -> None:
    """Undo is by identity, not by name.

    A plugin that replaced the built-in and then unloaded must not remove the
    slot it did not create -- that is the attribution bug the shared
    ``ScopedContribution`` base exists to make impossible.
    """
    async def builtin(*, target, text, cfg, output_dir=None):
        return "delivered"

    async def replacement(*, target, text, cfg, output_dir=None):
        return "delivered"

    registry = DeliveryTargetRegistry()
    registry.register("email", builtin)
    ctx = _PluginCtx("email-plugin")
    registry.scoped(ctx).add("email", replacement, replace=True)

    assert registry.sender_for("email") is replacement
    ctx.unload()

    assert registry.sender_for("email") is builtin, (
        "unloading the replacer must restore what it replaced, not delete the type"
    )


def test_two_plugins_claiming_one_type_is_refused_not_last_one_wins() -> None:
    """The task definitions on disk name a type; which implementation answers it
    must not depend on load order."""
    async def first(*, target, text, cfg, output_dir=None):
        return "delivered"

    async def second(*, target, text, cfg, output_dir=None):
        return "delivered"

    registry = DeliveryTargetRegistry()
    registry.scoped(_PluginCtx("a")).add("pager", first)

    with pytest.raises(ValueError) as excinfo:
        registry.scoped(_PluginCtx("b")).add("pager", second)

    assert "already provided by a" in str(excinfo.value)


def test_unloading_a_plugin_removes_only_its_own_target_type() -> None:
    async def send_one(*, target, text, cfg, output_dir=None):
        return "delivered"

    async def send_two(*, target, text, cfg, output_dir=None):
        return "delivered"

    registry = DeliveryTargetRegistry()
    one, two = _PluginCtx("one"), _PluginCtx("two")
    registry.scoped(one).add("alpha", send_one)
    registry.scoped(two).add("beta", send_two)

    one.unload()

    # The built-in survives: it is installed by the constructor, not by a plugin.
    assert registry.types() == ("beta", "feishu_chat")


def test_a_sender_that_raises_is_what_deliver_retries() -> None:
    """A sender must not retry on its own: ``deliver`` owns the loop, and a
    sender that swallowed its error would make the retry count a lie."""
    attempts: list[int] = []

    async def flaky(*, target, text, cfg, output_dir=None):
        attempts.append(1)
        raise RuntimeError("gateway said no")

    registry = DeliveryTargetRegistry()
    registry.scoped(_PluginCtx("flaky")).add("flaky", flaky)
    service = _delivery(registry)

    # deliver_channel is the sender boundary: it must let the error through.
    with pytest.raises(RuntimeError, match="gateway said no"):
        asyncio.run(
            service.deliver_channel(target=DeliveryTarget("flaky", {}), text="x")
        )

    assert len(attempts) == 1, "the sender is called once; retrying is deliver()'s job"
