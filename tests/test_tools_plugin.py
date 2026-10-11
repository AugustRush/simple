"""The built-in tool set as a plugin: the last thing the composition built directly.

``BuiltinTools`` is the largest implementation in the project, and until now ``bootstrap``
constructed it inline -- ``builtin_tools_cls(memory, registry, ...)`` -- so every tool it
registers lived for the life of the process and no composition could replace or remove the
set.  These tests assert the three properties that makes a plugin:

* it can be built from services rather than from a mapping,
* the class is a substitution point,
* **unloading it removes the tools**, which is what "hot-swappable" means for a
  contribution whose whole effect is registrations.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from agent.kernel.plugin import PluginHost
from agent.kernel.service import ServiceRegistry

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    from agent import shared
    from agent.tools.runtime import ToolRegistry

    monkeypatch.setattr(shared, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(shared, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(shared, "LEGACY_MEMORY_ALIASES", {})
    (tmp_path / "context").mkdir()
    (tmp_path / "memory").mkdir()
    (tmp_path / "out").mkdir()

    registry = ToolRegistry()
    mapping = {
        "cfg": {},
        "registry": registry,
        "memory": object(),
        "context_manager": object(),
        "workspace_root": tmp_path,
        "output_dir": tmp_path / "out",
        "file_service": None,
    }
    host = PluginHost(services=ServiceRegistry.adopt(mapping))
    return host, mapping, registry


def test_it_registers_the_built_in_tools(wired) -> None:
    from agent.tools.plugin import ToolsPlugin

    host, mapping, registry = wired
    host.load(ToolsPlugin())

    assert host.pending() == ()
    names = registry.list_tools()
    assert len(names) > 20, f"expected the built-in set, got {sorted(names)}"
    assert "read_file" in names and "shell" in names


def test_it_keeps_the_builtin_source_tag(wired) -> None:
    """The source is load-bearing, not decoration.

    ``ToolRegistry`` keys a capability table on ``(source, name)`` --
    ``("builtin", "read_file"): frozenset({"read"})``.  Registering through a scoped view
    stamps the plugin's name by default, which would have emptied that table silently; the
    view honours an explicit ``source`` instead, so this asserts both halves.
    """
    from agent.tools.plugin import ToolsPlugin

    host, mapping, registry = wired
    host.load(ToolsPlugin())

    assert registry._tools["read_file"].source == "builtin"
    assert registry.tool_capabilities("read_file") == frozenset({"read"})


def test_unloading_it_removes_every_tool_it_registered(wired) -> None:
    """This is the property that makes it hot-swappable rather than merely configurable.

    A plugin whose whole effect is registrations is only a plugin if the registrations are
    *effects on its scope*.  Registering into the raw registry would leave the tools behind
    after the unload, and the composition would hold a tool set whose owner is gone.
    """
    from agent.tools.plugin import ToolsPlugin

    host, mapping, registry = wired
    host.load(ToolsPlugin())
    assert registry.list_tools()

    host.unload("tools")

    assert registry.list_tools() == [], "the tools must go with the plugin"


def test_it_takes_the_class_as_an_argument(wired) -> None:
    """So ``bootstrap.BuiltinTools`` stays the substitution point."""

    class _Fake:
        calls: list = []

        def __init__(self, memory, registry, **kwargs):
            _Fake.calls.append((memory, registry, kwargs))

    from agent.tools.plugin import ToolsPlugin

    host, mapping, registry = wired
    host.load(ToolsPlugin(_Fake))

    assert len(_Fake.calls) == 1
    _, passed_registry, kwargs = _Fake.calls[0]
    assert passed_registry is not registry, (
        "the tool set must be handed the scoped view, or its registrations are permanent"
    )
    assert kwargs["workspace_root"] == mapping["workspace_root"]


def test_bootstrap_hands_it_its_own_module_level_name() -> None:
    """Asserted on the source because the point is *which expression* is passed.

    Moved with the seam: the tool class is handed into the ``CoreContext`` the declaration
    builds, not into a literal ``mount`` call.
    """
    import ast
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1] / "agent/bootstrap.py"
    ).read_text(encoding="utf-8")
    contexts = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "CoreContext"
    ]
    assert contexts, "the composition root must build a CoreContext for the declaration"
    # Every CoreContext the root builds, not the first: the no-composition channel
    # builder builds one too, and its plugins need nothing.
    handovers = [
        {kw.arg: kw.value for kw in node.keywords if isinstance(kw.value, ast.Name)}
        for node in contexts
    ]
    tool_handovers = [
        call.get("tools_class") for call in handovers if call.get("tools_class")
    ]
    assert tool_handovers, "the tool class must be handed over"
    assert all(handover.id == "builtin_tools_cls" for handover in tool_handovers)
