"""One place decides which channels a configuration runs.

Task 8 of the refactor asks for composition decisions to be findable in one
place.  For channels that was already *almost* true before this change: a single
function held both the ``web`` and the ``feishu`` switch.  But it lived in
``agent/channels/base.py`` -- the wrong layer, because which channels run is a
composition decision, not a channel implementation detail -- and the scheduler
has no equivalent at all, for a reason recorded below.

These tests hold two things in place: the decision lives with composition, and
nothing else constructs a channel.

**Where "the builder" is changed with the channel plugins.**  Channels are no longer
constructed in ``bootstrap``: each built-in is a plugin that registers on the
``channels`` service, which is what lets a third party contribute one -- something no
plugin could do before, since the gateway's set was a branch per built-in in the
composition root.  So the single place that constructs a channel moved to
``agent/channels/plugin.py``, and this file follows it rather than being relaxed: one
module, asserted, still.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Channel implementations that only the builder may construct.
_CHANNEL_CLASSES = ("WebChannel", "FeishuChannel", "CliChannel")

#: The one module allowed to name them in a call.
#:
#: It was ``agent/bootstrap.py`` until the channels became plugins; the composition root
#: no longer constructs a channel at all -- it mounts the plugins and reads the registry.
_BUILDER_MODULE = "agent/channels/plugin.py"

#: The composition point, which the builder above is reached through.
_COMPOSITION_MODULE = "agent/bootstrap.py"


def _call_sites(source: str) -> list[str]:
    """Names constructed via ``ClassName(...)`` in *source*, ignoring strings."""
    # A tiny AST pass rather than a regex: a regex would match the class
    # *definitions* and any mention inside a docstring, and those are prose.
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in _CHANNEL_CLASSES:
            found.append(name)
    return found


def test_the_builder_lives_with_composition() -> None:
    """``build_gateway_channels`` is in the composition module, not the channel one."""
    from agent.bootstrap import build_gateway_channels

    assert callable(build_gateway_channels)
    assert build_gateway_channels.__module__ == "agent.bootstrap"


def test_the_channel_layer_re_exports_it() -> None:
    """Existing importers are unaffected, which is why the name is kept there."""
    from agent.channels.base import _build_gateway_channels
    from agent.bootstrap import build_gateway_channels

    assert _build_gateway_channels({}) == build_gateway_channels({})


def test_only_the_builder_constructs_a_channel() -> None:
    """A second construction site is how "one place decides" stops being true.

    Read from the source rather than probed at runtime, because a construction
    in a branch the tests do not exercise is exactly the case this guards.
    """
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "agent").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        if rel == _BUILDER_MODULE:
            continue
        for name in _call_sites(path.read_text(encoding="utf-8")):
            offenders.append(f"{rel}: {name}(...)")
    assert not offenders, (
        "channels constructed outside the composition point:\n  "
        + "\n  ".join(offenders)
    )


def test_the_builder_itself_does_construct_channels() -> None:
    """Control: the guard above is vacuous if the construction site is not detected."""
    source = (REPO_ROOT / _BUILDER_MODULE).read_text(encoding="utf-8")
    found = set(_call_sites(source))
    assert {"WebChannel", "FeishuChannel"} <= found, found


def test_the_composition_mounts_a_channel_plugin() -> None:
    """The other half: one module may construct, and the composition must reach it.

    A guard that only forbids construction elsewhere passes just as well when nothing
    constructs a channel at all, which is why the two are asserted together.
    """
    source = (REPO_ROOT / _COMPOSITION_MODULE).read_text(encoding="utf-8")
    tree = ast.parse(source)
    mounted = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "agent.channels.plugin"
        for alias in node.names
    }
    assert {"WebChannelPlugin", "FeishuChannelPlugin"} <= mounted, (
        "build_gateway_channels must mount the channel plugins; found "
        f"{sorted(mounted)}"
    )
    assert "ChannelRegistry" in source, "and read the registry they contribute to"


def test_the_switch_reads_the_config_section() -> None:
    """Enabled/disabled is config, and an absent section means no channels."""
    from agent.bootstrap import build_gateway_channels

    assert build_gateway_channels({}) == []
    assert build_gateway_channels({"channels": {}}) == []
    assert build_gateway_channels(
        {"channels": {"web": {"enabled": False}, "feishu": {"enabled": False}}}
    ) == []


def test_unknown_channel_keys_do_not_raise() -> None:
    """A stale key in config.json is filtered, not fatal.

    Asserted for the disabled path so it needs no channel dependency; the
    enabled path is covered in the channel test modules.
    """
    from agent.bootstrap import build_gateway_channels

    assert build_gateway_channels(
        {"channels": {"web": {"enabled": False, "no_such_option": 1}}}
    ) == []


def test_the_scheduler_is_not_here_and_that_is_recorded() -> None:
    """Task 8's other half, as an assertion about the *reason* it is not here.

    ``SchedulerService`` needs an ``agent_executor`` built on ``AgentCore``,
    which bootstrap creates late in ``_build_components_async``.  So the
    scheduler cannot be constructed where the rest of composition happens, and
    that is an ordering constraint rather than a scattered decision.  This test
    fails the day that stops being true, so the note cannot quietly go stale.
    """
    from agent import bootstrap

    assert not hasattr(bootstrap, "build_scheduler_service"), (
        "the scheduler now has a composition entry point -- update the plan's "
        "Task 8 note, which says the ordering constraint prevents one"
    )


# ── A channel a plugin contributes ───────────────────────────────────────────


def test_a_plugin_can_contribute_a_channel() -> None:
    """The gap this closed, asserted directly.

    A plugin could contribute a tool, a slash command, a sub-agent role, an MCP config
    and a skill root.  It could not contribute a **channel**: the gateway's set was a
    branch per built-in in ``bootstrap``, so a third party's transport had to be added
    there.  Web and Feishu are now the first users of the registry rather than special
    cases in it, and this is a third user with no branch anywhere.
    """
    from agent.channels.registry import ChannelRegistry
    from agent.kernel.plugin import PluginHost

    class _ThirdPartyChannel:
        pass

    class _TheirPlugin:
        name = "acme.transport"
        inject = ("channels",)

        def setup(self, ctx):
            ctx.channels.add(_ThirdPartyChannel())

    registry = ChannelRegistry()
    host = PluginHost()
    host.services.provide("channels", registry)

    assert registry.all() == ()
    host.load(_TheirPlugin())
    assert len(registry.all()) == 1
    assert registry.owners() == ("acme.transport",)

    host.unload("acme.transport")
    assert registry.all() == (), "the channel went with the plugin that contributed it"


def test_a_disabled_channel_contributes_nothing_but_stays_mounted() -> None:
    """The switch stays in the config rather than moving into the composition root.

    Mounting a channel plugin only when its section says ``enabled`` would put the
    decision back in ``build_gateway_channels`` -- which is where it was, one branch per
    channel.  Mounted unconditionally, the plugin reads its own section and contributes
    nothing when it is off.
    """
    from agent.channels.plugin import WebChannelPlugin
    from agent.channels.registry import ChannelRegistry
    from agent.kernel.plugin import PluginHost

    registry = ChannelRegistry()
    host = PluginHost()
    host.services.provide("channels", registry)
    host.services.provide("cfg", {"channels": {"web": {"enabled": False}}})

    host.load(WebChannelPlugin())
    assert registry.all() == ()
    assert host.pending() == (), "mounted and inert, not pending"


def test_two_channels_from_one_plugin_both_survive() -> None:
    """A list, not a mapping keyed by name.

    A composition may run two web channels on different ports, and a registry keyed by
    name would refuse the second -- so the undo removes by identity, and removing one
    leaves the other.
    """
    from agent.channels.registry import ChannelRegistry
    from agent.kernel.plugin import PluginHost

    class _TheirPlugin:
        name = "acme.transport"
        inject = ("channels",)

        def __init__(self):
            self.made = []

        def setup(self, ctx):
            for _ in range(2):
                channel = object()
                self.made.append(channel)
                ctx.channels.add(channel)

    registry = ChannelRegistry()
    host = PluginHost()
    host.services.provide("channels", registry)

    host.load(_TheirPlugin())
    assert len(registry.all()) == 2

    # Removing one leaves the other: an undo that removed by position would take the
    # wrong one, and the symptom would be a transport stopping that nobody unloaded.
    first = registry.all()[0]
    registry.remove(first)
    assert len(registry.all()) == 1
    assert registry.all()[0] is not first


# ── hot swap: a channel plugin's contribution is undone by unloading it ───────


def _channel_host(cfg_sections: dict):
    from agent.channels.registry import ChannelRegistry
    from agent.kernel.plugin import PluginHost
    from agent.kernel.service import ServiceRegistry

    mapping = {"cfg": {"channels": cfg_sections}, "channels": ChannelRegistry()}
    return PluginHost(services=ServiceRegistry.adopt(mapping)), mapping


def test_unloading_a_channel_plugin_removes_exactly_that_channel() -> None:
    """A channel's whole effect is one registration, so it is only a plugin if that
    registration is undoable.

    ``ctx.channels.add`` returns an undo and the scoped view registers it on the plugin's
    scope, so this is the property that separates "declared in a plugin" from "hot
    swappable".  Asserted by identity of the remaining channel rather than by count: a
    count would pass if the wrong one had been removed.
    """
    from agent.channels.plugin import FeishuChannelPlugin, WebChannelPlugin

    host, mapping = _channel_host({"web": {"enabled": True}, "feishu": {"enabled": True}})
    host.load(WebChannelPlugin())
    host.load(FeishuChannelPlugin())
    registry = mapping["channels"]
    assert len(registry) == 2

    host.unload("channel.web")

    assert registry.owners() == ("channel.feishu",), registry.owners()
    assert len(registry) == 1


def test_a_disabled_channel_plugin_registers_nothing_to_undo() -> None:
    """Control: mounted-but-inert is the documented shape, so unloading it is a no-op.

    The plugins are mounted unconditionally and read their own config section.  A test that
    only exercised the enabled case would not notice if disabling started contributing
    anyway.
    """
    from agent.channels.plugin import WebChannelPlugin

    host, mapping = _channel_host({"web": {"enabled": False}})
    host.load(WebChannelPlugin())

    assert len(mapping["channels"]) == 0
    host.unload("channel.web")
    assert len(mapping["channels"]) == 0
