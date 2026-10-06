"""The command router is constructed in one place.

``components["command_router"]`` was written from four sites -- the bootstrap, the
CLI, the base channel and the web channel -- and each built the wire itself:

    router = CommandRouter(skill_catalog=...)
    register_builtin_commands(router)
    router.register_plugin_catalog(plugin_catalog)
    components["command_router"] = router

That is the same shape as the twenty-two ``SchedulerStore`` constructions: one
decision expressed four times, so "which router does this process use?" was
answered by whoever ran last.  It is also the P8 debt -- construction authority
not collected into one place.

``agent/commands/composition.py`` is that place now.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The module that owns the construction.
_OWNER = "agent/commands/composition.py"


def _router_constructions(source: str) -> list[int]:
    """Lines that call ``CommandRouter(...)``."""
    tree = ast.parse(source)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "CommandRouter")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "CommandRouter")
        )
    ]


def _assigns_router_key(source: str) -> list[int]:
    """Lines that assign the ``command_router`` component key."""
    tree = ast.parse(source)
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AugAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Subscript):
                key = target.slice
                if isinstance(key, ast.Constant) and key.value == "command_router":
                    found.append(node.lineno)
    return found


# ── The seam ─────────────────────────────────────────────────────────────────


def test_the_router_is_built_once_and_cached(tmp_path: Path) -> None:
    from agent.commands.composition import ensure_command_router

    components: dict = {}
    first = ensure_command_router(components)
    second = ensure_command_router(components)
    assert first is second, "a second call must not build a second router"
    assert components["command_router"] is first


def test_the_router_has_the_builtin_commands(tmp_path: Path) -> None:
    from agent.commands.composition import ensure_command_router

    router = ensure_command_router({})
    names = {d.name for d in router.visible_descriptors("web")}
    assert "help" in names, "the builtin registration is the point of the seam"


def test_a_supplied_plugin_catalog_is_registered() -> None:
    """The one difference between the four old call sites had to be preserved."""
    from agent.commands.composition import ensure_command_router

    class _Catalog:
        def __init__(self) -> None:
            self.asked = False

        def get_slash_commands(self):
            self.asked = True
            return {}

    catalog = _Catalog()
    ensure_command_router({"plugin_catalog": catalog})
    assert catalog.asked is True


def test_a_plugin_catalog_without_the_method_is_tolerated() -> None:
    """The old sites disagreed on how to check; the seam does it once."""
    from agent.commands.composition import ensure_command_router

    ensure_command_router({"plugin_catalog": object()})  # must not raise


def test_a_read_only_mapping_is_not_written_to() -> None:
    """A caller that wants no cache still gets a working router."""
    from types import MappingProxyType

    from agent.commands.composition import ensure_command_router

    frozen = MappingProxyType({})
    router = ensure_command_router(frozen)
    assert router is not None
    assert "command_router" not in frozen


def test_a_duck_typed_mapping_is_cached_into() -> None:
    """Not every mutable holder registers as ``MutableMapping``.

    The web channel's component holder supports ``__setitem__`` without being an
    instance of the ABC, and testing for the ABC skipped its cache -- so it built
    a fresh router per request and listed no commands.  This is the regression
    test for a duck-typing mistake that a type check made invisible.
    """

    class _Holder:
        def __init__(self) -> None:
            self._d: dict = {}

        def get(self, key, default=None):
            return self._d.get(key, default)

        def __setitem__(self, key, value):
            self._d[key] = value

    from agent.commands.composition import ensure_command_router

    holder = _Holder()
    first = ensure_command_router(holder)
    assert holder.get("command_router") is first
    assert ensure_command_router(holder) is first


# ── The guard ────────────────────────────────────────────────────────────────


def test_no_module_builds_a_command_router_outside_the_owner() -> None:
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "agent").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        if rel == _OWNER:
            continue
        if _router_constructions(path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert not offenders, (
        "CommandRouter constructed outside agent/commands/composition.py:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse agent.commands.composition.ensure_command_router(...) instead."
    )


def test_the_owner_does_construct_it() -> None:
    """Control: the guard is vacuous if construction is not detected at all."""
    assert _router_constructions((REPO_ROOT / _OWNER).read_text(encoding="utf-8"))


def test_no_module_assigns_the_component_key_outside_the_owner() -> None:
    """A second writer is how "one place decides" stops being true.

    Even a caller that builds no router can undo the seam by assigning the key,
    because the next reader takes whatever it finds.
    """
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "agent").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        if rel == _OWNER:
            continue
        if _assigns_router_key(path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert not offenders, (
        "the command_router component is assigned outside its owner:\n  "
        + "\n  ".join(offenders)
    )
