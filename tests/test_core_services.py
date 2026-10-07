"""The core service vocabulary, checked against the source.

The container already held these objects -- ``RuntimeComponents.services`` adopts
the component mapping -- so this is a *declaration* of which names are services,
not a new mechanism.  What it buys is that "replaceable" becomes a checkable
claim: a name with an owner, consumers, and a guard that fails when either list
drifts from the code.

The owner/consumer lists are asserted against an AST scan rather than trusted.
That is deliberate: hand-written ledgers in this refactor have been wrong twice,
once missing five entries entirely, and a ledger that disagrees with the code is
worse than none because it reports a service as filled when nothing fills it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent.runtime.core_services import (
    CORE_SERVICES,
    CoreService,
    core_service_names,
    entry_for,
)
from agent.runtime.contracts import RuntimeComponents
from agent.runtime.services import MissingServiceError

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = REPO_ROOT / "agent"


def _string_key(node: ast.Call | ast.Subscript) -> str | None:
    """The literal string key of a ``.get("k")`` / ``["k"]`` expression."""
    if isinstance(node, ast.Call):
        if not node.args:
            return None
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
        return None
    slice_node = node.slice
    if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
        return slice_node.value
    return None


def _scan() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """``(writers, readers)`` per core service name, by file.

    Three write mechanisms are counted, because the source uses all three and a
    scanner that knew only one reported four services as never written:

    - ``mapping["name"] = value`` -- the obvious one;
    - a ``{"name": value}`` **dict literal**, which is how bootstrap builds the
      component mapping it hands over;
    - ``registry.set_context("name", value)`` -- the tool registry's own key-value
      seam, which is a different mechanism carrying the same names.

    A read is any ``.get`` or subscript with the name as a literal key.
    """
    names = set(core_service_names())
    writers: dict[str, set[str]] = {}
    readers: dict[str, set[str]] = {}

    def note(bucket: dict[str, set[str]], key: str | None, rel: str) -> None:
        if key in names:
            bucket.setdefault(key, set()).add(rel)

    for path in sorted(AGENT_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript):
                        note(writers, _string_key(target), rel)
                # Every dict literal in the assigned value, at any depth.
                for inner in ast.walk(node.value):
                    if isinstance(inner, ast.Dict):
                        for key_node in inner.keys:
                            if isinstance(key_node, ast.Constant):
                                note(writers, key_node.value, rel)
            elif isinstance(node, ast.Call):
                func = node.func
                attr = func.attr if isinstance(func, ast.Attribute) else ""
                if attr == "set_context":
                    note(writers, _string_key(node), rel)
                elif attr in ("get", "pop") or attr == "require":
                    note(readers, _string_key(node), rel)
            elif isinstance(node, ast.Subscript):
                note(readers, _string_key(node), rel)
    return writers, readers


# ── The ledger is well formed ────────────────────────────────────────────────


def test_every_entry_has_a_name_owners_and_summary() -> None:
    for entry in CORE_SERVICES:
        assert entry.name.strip()
        assert entry.owners, f"{entry.name}: a service nothing writes is not one"
        for owner in entry.owners:
            assert owner.startswith("agent/"), f"{entry.name}: owner must be a module"
        assert entry.summary.strip(), f"{entry.name}: a service with no purpose untold"


def test_no_name_is_declared_twice() -> None:
    names = core_service_names()
    assert len(names) == len(set(names))


def test_every_entry_has_a_consumer() -> None:
    """A declared service nothing reads is vocabulary that should be deleted."""
    for entry in CORE_SERVICES:
        assert entry.consumers, f"{entry.name!r} is declared but nothing reads it"


def test_lookup_of_an_undeclared_name_raises() -> None:
    with pytest.raises(KeyError) as caught:
        entry_for("not_a_service")
    assert "CORE_SERVICES" in str(caught.value), "it says where to add it"


def test_lookup_of_a_declared_name_returns_it() -> None:
    assert entry_for("agent_core").name == "agent_core"


# ── The container already resolves them ──────────────────────────────────────


def test_the_container_resolves_a_declared_service() -> None:
    """Not new behaviour: ``services`` adopts the mapping, so this always worked.

    Asserted because the whole point of the declaration is that these names are
    *already* resolvable through the registry -- if that were false, this ledger
    would be describing a migration nobody had done.
    """
    components = {"agent_core": object()}
    assert RuntimeComponents(components).services.get("agent_core") is not None


def test_the_container_raises_for_a_missing_service() -> None:
    """What makes a typo visible: ``None`` is a legitimate value for a composition."""
    with pytest.raises(MissingServiceError):
        RuntimeComponents({}).services.get("agent_core")


def test_require_names_the_missing_service() -> None:
    with pytest.raises(KeyError, match="agent_core"):
        RuntimeComponents({"other": 1}).require("agent_core")


def test_providing_through_the_container_reaches_the_mapping() -> None:
    """So a replacement installed through the registry is visible to both sides."""
    components: dict = {}
    services = RuntimeComponents(components).services
    services.provide("agent_core", "REPLACEMENT")
    assert components["agent_core"] == "REPLACEMENT"


# ── The ledger agrees with the source ────────────────────────────────────────


def test_every_declared_owner_really_writes_the_service() -> None:
    """A wrong owner is worse than none: it reports a service as filled.

    Both directions are checked.  A claimed owner that does not write would make
    the escape hatch look supported where it is not; an **unlisted** writer is the
    scattered-construction debt itself -- a service whose value depends on who ran
    last, which is exactly what makes it unsafely replaceable.
    """
    writers, _readers = _scan()
    wrong: list[str] = []
    for entry in CORE_SERVICES:
        actual = writers.get(entry.name, set())
        if not actual:
            wrong.append(f"{entry.name}: nothing was found writing it")
            continue
        for claimed in entry.owners:
            if claimed not in actual:
                wrong.append(f"{entry.name}: {claimed} does not write it")
        for found in sorted(actual):
            if found not in entry.owners:
                wrong.append(
                    f"{entry.name}: {found} writes it but is not listed as an owner"
                )
    assert not wrong, "the ledger and the source disagree:\n  " + "\n  ".join(wrong)


def test_every_declared_consumer_really_reads_the_service() -> None:
    _writers, readers = _scan()
    wrong: list[str] = []
    for entry in CORE_SERVICES:
        actual = readers.get(entry.name, set())
        for claimed in entry.consumers:
            if claimed not in actual:
                wrong.append(f"{entry.name}: {claimed} does not read it")
    assert not wrong, "the ledger and the source disagree:\n  " + "\n  ".join(wrong)


def test_the_scan_finds_reads_and_writes_at_all() -> None:
    """Control: a scanner that finds nothing passes every assertion above."""
    writers, readers = _scan()
    assert writers, "no writes found"
    assert readers, "no reads found"
    assert "agent_core" in writers
    assert "skill_catalog" in readers


# ── The vocabulary is closed ─────────────────────────────────────────────────


def test_declared_names_match_the_documented_set() -> None:
    """The eight names this step covers, so an accidental removal is caught."""
    assert set(core_service_names()) == {
        "agent_core",
        "context_manager",
        "mcp_client",
        "plugin_catalog",
        "registry",
        "skill_catalog",
        "turn_runner",
    }


def test_an_entry_is_frozen() -> None:
    with pytest.raises(Exception):
        CORE_SERVICES[0].name = "other"  # type: ignore[misc]


def test_a_service_can_be_replaced_through_the_container() -> None:
    """The claim this whole step exists to make true, in one assertion.

    A composition that wants a different AgentCore provides one under the same
    name; every consumer that resolves through the container gets it.
    """
    class _FakeCore:
        pass

    components = {"agent_core": object()}
    container = RuntimeComponents(components)
    original = container.services.get("agent_core")

    container.services.provide("agent_core", _FakeCore(), replace=True)

    assert container.services.get("agent_core") is not original
    assert isinstance(container.services.get("agent_core"), _FakeCore)
    assert components["agent_core"] is container.services.get("agent_core"), (
        "the mapping and the container must not diverge"
    )


def test_a_core_service_entry_is_not_blank() -> None:
    """Control for the dataclass itself."""
    entry = CoreService(
        name="x", owners=("agent/x.py",), consumers=("agent/y.py",), summary="s"
    )
    assert entry.name == "x"
