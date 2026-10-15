"""The plugin vocabulary is closed, and derived rather than trusted.

Two questions a closed vocabulary has to answer, and the tests here answer them
from the tree rather than from a list somebody maintains by hand:

* a plugin that touches a name not in ``PUBLIC_SERVICES`` fails (the vocabulary is
  closed);
* a name in ``PUBLIC_SERVICES`` that no plugin uses and none provides fails (the
  vocabulary does not grow speculatively).

Plus the two halves that make the policy real rather than decorative: reserved
names are disjoint from public ones, and a wired policy actually refuses -- at
runtime, through a real catalog, for both reading and providing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent.kernel.plugin import PluginHost
from agent.kernel.service import ReservedServiceError
from agent.runtime.service_vocabulary import (
    FOR_THIRD_PARTIES,
    PUBLIC_SERVICES,
    RESERVED_COMPONENT_KEYS,
    RESERVED_REASONS,
    plugin_service_policy,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: ``Context``'s own surface.  A ``ctx.<name>`` read of anything else is a service
#: read through ``__getattr__``, which is what the vocabulary governs.
_CONTEXT_METHODS = frozenset(
    {
        "name",
        "services",
        "provide",
        "get",
        "raw",
        "require_declared",
        "require_allowed",
        "has",
        "bus",
        "on",
        "emit",
        "emit_blocking",
        "plugin",
        "on_unwind",
        "on_unwind_async",
        "scope",
    }
)


def _string_tuple(node: ast.AST) -> set[str]:
    if not isinstance(node, (ast.Tuple, ast.List)):
        return set()
    return {
        item.value
        for item in node.elts
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }


def _is_plugin_module(tree: ast.AST) -> bool:
    """Whether *tree* is a plugin, judged structurally.

    Two signals, and both are the kernel's own vocabulary: a class declaring
    ``inject``/``optional``, or a call to ``ctx.provide``.  Deliberately **not**
    "any module that mentions ``ctx``": this codebase passes a dozen different
    things around under that name (a turn context, a pydantic validator's
    context), and a scan that confused them would fail on ``ctx.messages``.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("inject", "optional"):
                    return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "provide"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "ctx"
        ):
            return True
    return False


def _plugin_modules() -> dict[str, dict[str, set[str]]]:
    """``path -> {"declared", "read", "provided"}`` for every module that is a plugin.

    Structural rather than a hardcoded file list: a new plugin file is in scope the
    moment it is written, which is the property that stops the vocabulary drifting
    behind the code.
    """
    found: dict[str, dict[str, set[str]]] = {}
    for path in sorted((REPO_ROOT / "agent").rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not _is_plugin_module(tree):
            continue
        declared: set[str] = set()
        read: set[str] = set()
        provided: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ("inject", "optional"):
                        declared |= _string_tuple(node.value)
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            if isinstance(func.value, ast.Name) and func.value.id == "ctx":
                if func.attr in ("get", "raw"):
                    read.add(first.value)
                elif func.attr == "provide":
                    provided.add(first.value)
        # ``ctx.tools`` / ``ctx.skill_catalog``: an injected service read by
        # attribute, which ``Context.__getattr__`` routes through ``get``.
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "ctx"
                and not node.attr.startswith("_")
                and node.attr not in _CONTEXT_METHODS
            ):
                read.add(node.attr)

        found[str(path.relative_to(REPO_ROOT))] = {
            "declared": declared,
            "read": read,
            "provided": provided,
        }
    return found


def _used() -> set[str]:
    names: set[str] = set()
    for groups in _plugin_modules().values():
        names |= groups["declared"] | groups["read"] | groups["provided"]
    return names


def test_the_scan_finds_the_plugins() -> None:
    """Control: the scan reads real modules, so an empty answer fails loudly."""
    modules = _plugin_modules()

    assert "agent/tools/registry_plugin.py" in modules, "the tool registry is a plugin"
    assert modules["agent/tools/registry_plugin.py"]["provided"] == {"registry"}
    assert "cfg" in modules["agent/tools/registry_plugin.py"]["declared"]


def test_every_name_a_plugin_touches_is_in_the_vocabulary() -> None:
    """The closing direction: a plugin cannot reach a name nobody published."""
    undeclared: list[str] = []
    for path, groups in sorted(_plugin_modules().items()):
        for name in sorted(groups["declared"] | groups["read"] | groups["provided"]):
            if name not in PUBLIC_SERVICES:
                undeclared.append(f"{path}: {name}")
    assert not undeclared, (
        "these plugins touch names that are not in the plugin vocabulary. Either the "
        "name belongs to the composition (add it to RESERVED_REASONS with the reason "
        "and stop reading it), or it is a service a plugin may use (add it to "
        "PUBLIC_SERVICES):\n  " + "\n  ".join(undeclared)
    )


def test_the_vocabulary_has_no_unused_names() -> None:
    """The other direction: no speculative entries.

    A name published to plugins is a promise that it will keep working, so it has to
    be one somebody uses.  The two documented third-party entries are the exception
    and they carry a reason.
    """
    unused = sorted(PUBLIC_SERVICES - _used() - set(FOR_THIRD_PARTIES))
    assert not unused, (
        "these names are published to plugins but no plugin uses or provides them: "
        f"{unused}. Remove them, or add them to FOR_THIRD_PARTIES with a reason."
    )


def test_the_third_party_entries_say_why_they_are_unused() -> None:
    stale = sorted(name for name in FOR_THIRD_PARTIES if name in _used())
    assert not stale, (
        f"these now have an in-tree user, so remove them from FOR_THIRD_PARTIES: {stale}"
    )
    for name, reason in FOR_THIRD_PARTIES.items():
        assert len(reason) > 80, f"{name}: the reason is too short to be one"
        assert name in PUBLIC_SERVICES


def test_the_two_vocabularies_are_disjoint() -> None:
    overlap = sorted(PUBLIC_SERVICES & RESERVED_COMPONENT_KEYS)
    assert not overlap, (
        f"a name cannot be both a plugin service and composition-internal: {overlap}"
    )


def test_a_reserved_name_is_a_real_component_key() -> None:
    """A reason for a key that does not exist is a comment, not a decision.

    Keeps the reserved list from filling with names somebody imagined: each one has
    to be written somewhere in the tree.
    """
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "agent").rglob("*.py")
        if "__pycache__" not in str(path)
    )
    missing = sorted(name for name in RESERVED_REASONS if f'"{name}"' not in sources)
    assert not missing, (
        f"these are reserved with a reason but appear nowhere in agent/: {missing}"
    )


# ── The policy is enforced, not decorative ───────────────────────────────────


class _Reader:
    def __init__(self, name: str, *, optional: tuple[str, ...]) -> None:
        self.name = "reader"
        self.inject = ()
        self.optional = optional
        self._read = name
        self.seen = "not read"

    def setup(self, ctx):
        self.seen = ctx.get(self._read, None)


def test_a_reserved_name_cannot_be_read_through_a_wired_policy() -> None:
    host = PluginHost()
    host.service_policy = plugin_service_policy
    host.services.provide("mcp_task", object())
    host.load(_Reader("mcp_task", optional=("mcp_task",)))

    assert host.loaded() == (), "the read was refused, so setup raised"
    assert "reserved to the composition" in host.failed()["reader"]


def test_a_reserved_name_cannot_be_provided_either() -> None:
    """Otherwise a plugin could shadow a component the composition resolves."""
    host = PluginHost()
    host.service_policy = plugin_service_policy

    class Squatter:
        name = "squatter"
        inject: tuple[str, ...] = ()

        def setup(self, ctx):
            ctx.provide("mcp_task", object())

    host.load(Squatter())

    assert host.loaded() == ()
    assert "reserved to the composition" in host.failed()["squatter"]


def test_a_public_name_is_allowed() -> None:
    """Control: the policy refuses the reserved set, not everything."""
    host = PluginHost()
    host.service_policy = plugin_service_policy
    host.services.provide("workspace_root", "/tmp/ws")
    host.load(_Reader("workspace_root", optional=("workspace_root",)))

    assert host.loaded() == ("reader",)
    assert host.installs["reader"].plugin.seen == "/tmp/ws"


def test_no_policy_means_no_vocabulary() -> None:
    """Control for the kernel's own tests, which invent names like ``greeter``.

    The vocabulary is the composition's; a bare ``PluginHost`` is an embedder that
    published nothing, and the kernel must not invent a policy on its behalf.
    """
    host = PluginHost()
    host.services.provide("anything.at.all", 1)
    host.load(_Reader("anything.at.all", optional=("anything.at.all",)))

    assert host.loaded() == ("reader",)


def test_the_installed_composition_wires_the_policy() -> None:
    """The ledger says the vocabulary exists; this says something installs it."""
    source = (REPO_ROOT / "agent" / "bootstrap.py").read_text(encoding="utf-8")
    assert "plugin_service_policy" in source, (
        "bootstrap must hand the vocabulary to the catalog, or the closed vocabulary "
        "is a module nobody reads"
    )


def test_the_reserved_error_is_not_a_missing_service() -> None:
    """Two different facts, two different types: absence vs. not yours to ask for."""
    from agent.kernel.service import MissingServiceError

    assert not issubclass(ReservedServiceError, MissingServiceError)
    error = ReservedServiceError("p", "mcp_task", action="read")
    assert "reserved" in str(error) and error.name == "mcp_task"


@pytest.mark.parametrize("name", sorted(RESERVED_REASONS))
def test_every_reserved_reason_is_usable_prose(name: str) -> None:
    assert len(RESERVED_REASONS[name]) > 20, f"{name}: say why, not just what"


# ── The two documented third-party views actually work ───────────────────────


def test_a_plugin_can_contribute_a_command_through_the_view() -> None:
    """``FOR_THIRD_PARTIES["commands"]`` claims this path; this is the claim.

    Without it the entry would be a published name nobody had ever exercised, which
    is the shape the guard exists to refuse.
    """
    from agent.commands.registry import CommandRegistry

    registry = CommandRegistry()
    host = PluginHost()
    host.service_policy = plugin_service_policy
    host.services.provide("commands", registry)

    class AddsACommand:
        name = "adder"
        inject = ("commands",)

        def setup(self, ctx):
            ctx.commands.add("mine:hello", lambda: "hi")

    host.load(AddsACommand())

    assert registry.has("mine:hello")
    assert registry.owner("mine:hello") == "adder"

    host.unload("adder")

    assert not registry.has("mine:hello"), "the contribution unwound with its plugin"


def test_a_plugin_can_contribute_a_role_through_the_view() -> None:
    from agent.core.agent_registry import AgentRegistry, role_key

    registry = AgentRegistry()
    host = PluginHost()
    host.service_policy = plugin_service_policy
    host.services.provide("agents", registry)

    class AddsARole:
        name = "adder"
        inject = ("agents",)

        def setup(self, ctx):
            ctx.agents.add("reviewer", {"description": "reviews"})

    host.load(AddsARole())

    key = role_key("adder", "reviewer")
    assert key in registry.keys()
    assert registry.get(key)["plugin"] == "adder"

    host.unload("adder")

    assert key not in registry.keys()
