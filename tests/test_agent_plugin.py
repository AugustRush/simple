"""The agent as a plugin -- the composition resolves it instead of assembling it."""

from __future__ import annotations

import ast
import pathlib

import pytest

from agent.core.agent_plugin import AgentPlugin
from agent.kernel.plugin import PluginHost
from agent.kernel.service import ServiceRegistry

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Agent:
    """Stands in for ``BaseAgent`` and records how it was constructed."""

    def __init__(self, client=None, registry=None, **kwargs):
        self.client = client
        self.registry = registry
        self.constructed_with = kwargs
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.max_parallel_agents = None
        self.plugin_catalog = None
        self.context_manager = None


def _mapping(**overrides):
    mapping = {
        "cfg": {"active_provider": "custom", "providers": {"custom": {}}},
        "client": object(),
        "registry": object(),
        "model": "m",
        "max_tokens": 100,
        "api_format": "anthropic",
        "llm": object(),
        "context_manager": object(),
        "plugin_catalog": object(),
    }
    mapping.update(overrides)
    return mapping


def _load(mapping, plugin=None):
    host = PluginHost(services=ServiceRegistry.adopt(mapping))
    host.load(plugin or AgentPlugin(_Agent))
    return host, mapping


def test_it_provides_the_agent() -> None:
    host, mapping = _load(_mapping())
    assert host.pending() == ()
    assert isinstance(mapping["agent"], _Agent)
    assert mapping["agent"].model == "m"


def test_the_class_is_a_substitution_point() -> None:
    """A composition decides *which* agent, and says so by passing one.

    This is why the class is an argument rather than an import at the point of use: the
    moment the construction moved behind an import, replacing ``bootstrap.BaseAgent``
    stopped deciding anything -- and "which agent" is exactly the decision the composition
    is supposed to make.
    """
    mapping = _mapping()
    host, mapping = _load(mapping, AgentPlugin(_Agent))
    assert type(mapping["agent"]) is _Agent

    class _Other(_Agent):
        pass

    other = _mapping()
    _load(other, AgentPlugin(_Other))
    assert type(other["agent"]) is _Other


def test_bootstrap_hands_it_its_own_module_level_name() -> None:
    """So the patch target that existed before still works.

    Asserted against the source because the point is *which expression* is passed: a literal
    import inside the plugin would make this unreachable, and the failure would be a test
    double silently not being used.

    The seam moved when the composition's own plugins became a declaration -- the class is
    now passed into a ``CoreContext`` rather than into ``mount(...)`` -- so the assertion
    moved with it.  Following the seam is the point; the alternative is a test that keeps
    passing by looking somewhere the value no longer travels through.
    """
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1] / "agent/bootstrap.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    contexts = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "CoreContext"
    ]
    assert contexts, "the composition root must build a CoreContext for the declaration"
    passed = {
        kw.arg: kw.value
        for kw in contexts[0].keywords
        if isinstance(kw.value, ast.Name)
    }
    assert passed.get("agent_class") is not None, (
        "the agent class must be handed over, or replacing bootstrap.BaseAgent decides "
        "nothing"
    )
    assert passed["agent_class"].id == "BaseAgent"


class _Registry:
    """A registry shaped like the real one: a service that also offers a scoped view."""

    def __init__(self) -> None:
        self.context: dict = {}

    def set_context(self, key: str, value) -> None:
        self.context[key] = value

    def get_context(self, key: str, default=None):
        return self.context.get(key, default)

    def scoped(self, ctx):
        return _ScopedView(self)


class _ScopedView:
    """What ``ctx.get("registry")`` returns: contribution, not configuration."""

    def __init__(self, registry: _Registry) -> None:
        self._registry = registry

    def get_context(self, key: str, default=None):
        return self._registry.get_context(key, default)


def test_the_agent_is_handed_the_registry_not_a_scoped_view_of_it() -> None:
    """The view is for *contributing* tools; the agent configures the registry.

    ``ctx.get("registry")`` returns a plugin-scoped view because ``ToolRegistry`` offers
    ``scoped(ctx)`` -- which is what a plugin registering tools wants, since every
    registration then comes back out when it unloads.  The agent is not that plugin: its
    turn loop writes the registry's shared context (``turn_request``, on ``prepare_turn``)
    and the tool implementations read it back.  Handed a view, the first turn dies with
    ``'ScopedTools' object has no attribute 'set_context'`` -- before any model call, so
    what a user sees is a message that produces nothing at all.

    The stand-in offers ``scoped`` **because the real one does**.  The mapping this file
    used before did not, so ``ctx.get`` handed over the raw object and the double could not
    tell the two apart: a stand-in simpler than the thing it stands in for, in exactly the
    way that hides the defect.
    """
    registry = _Registry()
    mapping = _mapping(registry=registry)
    _load(mapping)

    agent = mapping["agent"]
    assert agent.registry is registry, (
        "the agent must hold the composition's registry, not a view of it"
    )
    assert type(agent.registry) is _Registry, "and specifically not the scoped view"

    # The exact call the turn loop makes on every turn -- the one that failed.
    agent.registry.set_context("turn_request", "hello")
    assert registry.get_context("turn_request") == "hello"
