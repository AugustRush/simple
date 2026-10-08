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
