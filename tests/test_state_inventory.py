"""Guard rail for the state ledger.

The refactor's central promise is that a registration unwinds when its provider
unloads.  Module-level mutable state is what makes that promise false: it
outlives the provider that created it and cannot be scoped to it.  These tests
do not *fix* that state -- they make adding more of it a deliberate act.

The scanner below is the authority, not the ledger: it rediscovers every shape
in the tree and then asserts the ledger accounts for all of it.  A new global
shows up as a test failure naming the symbol, which is the only cheap moment to
ask whether it should exist.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent.runtime.state_ledger import BY_SYMBOL, LEDGER, unconverged

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = REPO_ROOT / "agent"

#: Method names that mutate a container in place.
MUTATORS = frozenset(
    {
        "append",
        "add",
        "update",
        "pop",
        "setdefault",
        "discard",
        "clear",
        "extend",
        "remove",
        "insert",
        "popitem",
    }
)

#: Names that are part of the module protocol rather than application state.
IGNORED = frozenset({"__all__", "__path__", "__version__", "__doc__"})


@dataclass(frozen=True)
class Discovered:
    """One piece of module-level mutable state found by scanning."""

    symbol: str
    path: str
    line: int
    shape: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.symbol, self.path, self.shape)


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _contextvars(tree: ast.Module) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name == "ContextVar":
            found.append((target.id, node.lineno))
    return found


def _own_scope(func: ast.AST) -> list[ast.AST]:
    """Walk *func*'s body without descending into nested scopes.

    ``ast.walk`` crosses into nested functions and classes, which attributes a
    nested ``global`` statement to its enclosing function.  That produced
    phantom "rebound" entries in files that contain no ``global`` at all, so
    the traversal stops at every function and class boundary.
    """
    collected: list[ast.AST] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        collected.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return collected


def _functions(tree: ast.Module) -> list[ast.AST]:
    """Every function and method defined anywhere in the module."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _rebound(tree: ast.Module, module_names: set[str]) -> list[tuple[str, int]]:
    """Module-level names reassigned through ``global`` inside a function."""
    found: dict[str, int] = {}
    for func in _functions(tree):
        for node in _own_scope(func):
            if not isinstance(node, ast.Global):
                continue
            for name in node.names:
                if name in module_names and name not in IGNORED:
                    found.setdefault(name, node.lineno)
    return sorted(found.items(), key=lambda item: item[1])


def _containers_mutated(tree: ast.Module, module_names: set[str]) -> list[tuple[str, int]]:
    """Module-level containers mutated in place without being rebound.

    A name that is also assigned as a simple target inside the same function is
    treated as local and skipped: without a full scope resolver this is the one
    false-positive source, and it is cheap to keep quiet.
    """
    found: dict[str, int] = {}
    for func in _functions(tree):
        nodes = _own_scope(func)
        declared: set[str] = set()
        local_assigns: set[str] = set()
        for node in nodes:
            if isinstance(node, ast.Global):
                declared.update(node.names)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        local_assigns.add(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                local_assigns.add(node.target.id)
        for node in nodes:
            name: str | None = None
            if (
                isinstance(node, ast.Assign)
                and node.targets
                and isinstance(node.targets[0], ast.Subscript)
                and isinstance(node.targets[0].value, ast.Name)
            ):
                name = node.targets[0].value.id
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in MUTATORS
                and isinstance(node.func.value, ast.Name)
            ):
                name = node.func.value.id
            if (
                name
                and name in module_names
                and name not in IGNORED
                and name not in declared
                and name not in local_assigns
            ):
                found.setdefault(name, node.lineno)
    return sorted(found.items(), key=lambda item: item[1])


def scan() -> list[Discovered]:
    """Every piece of module-level mutable state in ``agent/``."""
    results: list[Discovered] = []
    for path in sorted(AGENT_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = str(path.relative_to(REPO_ROOT))
        module_names = _module_level_names(tree)
        for symbol, line in _contextvars(tree):
            results.append(Discovered(symbol, rel, line, "CONTEXTVAR"))
        for symbol, line in _rebound(tree, module_names):
            results.append(Discovered(symbol, rel, line, "REBOUND"))
        for symbol, line in _containers_mutated(tree, module_names):
            results.append(Discovered(symbol, rel, line, "CONTAINER"))
    return results


@pytest.fixture(scope="module")
def discovered() -> list[Discovered]:
    return scan()


def test_ledger_is_not_empty() -> None:
    """A silently empty ledger would make every other assertion vacuous."""
    assert len(LEDGER) > 0


def test_every_discovered_symbol_is_in_the_ledger(discovered: list[Discovered]) -> None:
    """New module-level mutable state must be registered deliberately.

    When this fails, decide which it is: converge it, or add it to
    ``agent/runtime/state_ledger.py`` with a scope and a convergence target.
    """
    unregistered = [d for d in discovered if d.key not in {
        (e.symbol, e.path, e.shape) for e in LEDGER
    }]
    assert not unregistered, (
        "module-level mutable state outside the state ledger:\n"
        + "\n".join(
            f"  {d.symbol} ({d.shape}) at {d.path}:{d.line}" for d in unregistered
        )
        + "\n\nAdd it to agent/runtime/state_ledger.py with a scope and "
        "convergence target, or remove it."
    )


def test_ledger_has_no_stale_entries(discovered: list[Discovered]) -> None:
    """A converged entry must be deleted, not left behind describing nothing."""
    live = {d.key for d in discovered}
    stale = [e for e in LEDGER if (e.symbol, e.path, e.shape) not in live]
    assert not stale, (
        "state ledger entries that no longer exist:\n"
        + "\n".join(f"  {e.symbol} ({e.shape}) at {e.path}:{e.line}" for e in stale)
    )


def test_ledger_symbols_are_unique() -> None:
    """Duplicate symbols make ``BY_SYMBOL`` lossy and lookups ambiguous."""
    seen: dict[str, int] = {}
    for entry in LEDGER:
        seen[entry.symbol] = seen.get(entry.symbol, 0) + 1
    duplicates = {name: count for name, count in seen.items() if count > 1}
    assert not duplicates, f"duplicate ledger symbols: {duplicates}"
    assert len(BY_SYMBOL) == len(LEDGER)


def test_ledger_records_where_state_is_owned() -> None:
    """Every entry must say what it is and how it will stop being a problem."""
    for entry in LEDGER:
        assert entry.path.startswith("agent/"), entry
        assert entry.line > 0, entry
        if entry.convergence == "stays":
            assert entry.reason, (
                f"{entry.symbol} is marked 'stays' without a reason; "
                "keeping process-level state needs an argument"
            )


def test_process_scoped_state_is_either_justified_or_scheduled() -> None:
    """Process scope is the shape that blocks per-agent isolation.

    Every process-scoped entry is either scheduled for convergence (a task
    number) or carries a written reason.  This is the assertion that keeps the
    ledger from becoming a list of excuses.
    """
    offenders = [
        entry
        for entry in LEDGER
        if entry.scope == "process"
        and entry.convergence != "stays"
        and entry.task is None
    ]
    assert not offenders, (
        "process-scoped state with no task and no justification:\n"
        + "\n".join(f"  {e.symbol} at {e.path}:{e.line}" for e in offenders)
    )


def test_critical_entries_are_accounted_for() -> None:
    """Spot-check the state that motivated the ledger.

    If one of these disappears the ledger must be updated in the same change,
    so the test names them explicitly rather than trusting the count.
    """
    for symbol in (
        "_active_sink",
        "_active_event_collector",
        "_active_session_id",
        "_PENDING",
        "_APPROVED",
        "_session_allowlist",
        "_session_sandbox_modes",
        "AGENT_HOME",
        "TASKS_DIR",
        "_SYNC_TOOL_EXECUTOR",
        "_system_prompt_cache_key",
        "_PLUGIN_INSTALL_LOCK",
    ):
        assert symbol in BY_SYMBOL, f"{symbol} vanished from the ledger"


def test_no_unconverged_entries_without_reason() -> None:
    """``unconverged()`` is the ledger's own debt list; keep it honest."""
    for entry in unconverged():
        assert entry.convergence in {"service", "explicit", "scope"}, entry
