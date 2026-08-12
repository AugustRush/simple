"""Guards for ``--name`` multi-instance isolation.

``shared.AGENT_HOME`` and its ~20 derived paths are process-global mutable
state, rewritten by ``_set_agent_home`` when the CLI parses ``--name``.  That
design makes one mistake possible everywhere: binding a derived path into a
module-level constant at import time captures whichever home was active when
the module first loaded, and never follows a later switch.

The evolution plugin did exactly that (and hardcoded ``.agent`` on top of it),
so ``simple gateway --name prod`` wrote its learned rules and failure log into
the *default* instance's home.  These tests pin the fixed behaviour and, more
usefully, catch the next module that reintroduces the pattern.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import agent as agent_module
from agent import shared

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "agent"

# Derived-path names that must never be captured at import time.
_HOME_DERIVED_NAMES = frozenset(
    {
        "AGENT_HOME",
        "MEMORY_DIR",
        "SKILLS_DIR",
        "TOOLS_DIR",
        "PROMPTS_DIR",
        "RL_DIR",
        "SCHEDULER_DIR",
        "SCHEDULER_DB_FILE",
        "CONFIG_FILE",
        "INDEX_FILE",
        "SESSIONS_FILE",
        "DEFAULT_OUTPUT_DIR",
        "USER_PLUGINS_DIR",
        "CONTEXT_DIR",
        "STAGING_DIR",
        "PALACE_DB_FILE",
    }
)

# ``shared`` defines these; ``agent/__init__`` re-exports them and refreshes
# its own mirror inside ``_set_agent_home``.  Everyone else must late-bind.
_ALLOWED_TO_BIND = {"shared.py", "__init__.py"}


@pytest.fixture
def restore_agent_home():
    original = shared.AGENT_HOME
    yield
    agent_module._set_agent_home(original)


def _module_level_assignments(path: Path) -> list[tuple[str, ast.expr]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, ast.expr]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.value is not None:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.append((target.id, node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name):
                found.append((node.target.id, node.value))
    return found


def _mentions_agent_home(expr: ast.expr) -> str:
    """Return a description when *expr* derives a path from the agent home."""
    for node in ast.walk(expr):
        # shared.RL_DIR / "x"  or  agent.AGENT_HOME / "y"
        if isinstance(node, ast.Attribute) and node.attr in _HOME_DERIVED_NAMES:
            return f"attribute {node.attr}"
        # bare RL_DIR after `from agent.shared import RL_DIR`
        if isinstance(node, ast.Name) and node.id in _HOME_DERIVED_NAMES:
            return f"name {node.id}"
        # Path.home() / ".agent"  — hardcodes the default instance too
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value == ".agent" or node.value.startswith(".agent-"):
                return f"literal {node.value!r}"
    return ""


def test_no_module_binds_an_agent_home_path_at_import_time():
    """The structural guard: this is why the evolution plugin broke.

    A module-level constant derived from the agent home is frozen at import
    time.  Plugins in particular import before ``--name`` is applied, so the
    constant is always wrong for a named instance.  Resolve inside a function.
    """
    offenders: list[str] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        if path.name in _ALLOWED_TO_BIND and path.parent.name == "agent":
            continue
        if "__pycache__" in path.parts:
            continue
        for name, value in _module_level_assignments(path):
            reason = _mentions_agent_home(value)
            if reason:
                rel = path.relative_to(_PACKAGE_ROOT.parent)
                offenders.append(f"{rel}: {name} = ... ({reason})")

    assert offenders == [], (
        "these module-level constants freeze the agent home at import time and "
        "will not follow `--name`; resolve them inside a function instead:\n  "
        + "\n  ".join(offenders)
    )


def test_evolution_plugin_paths_follow_a_named_instance(restore_agent_home, tmp_path):
    from agent._builtin.plugins.evolution import _failures_file
    from agent._builtin.plugins.evolution.rules import _rules_file

    agent_module._set_agent_home(tmp_path / ".agent-prod")

    assert _failures_file() == tmp_path / ".agent-prod" / "rl" / "failures.jsonl"
    assert _rules_file() == tmp_path / ".agent-prod" / "rl" / "rules.jsonl"
    assert shared.RL_DIR == tmp_path / ".agent-prod" / "rl"


def test_rule_store_writes_into_the_active_instance(restore_agent_home, tmp_path):
    from agent._builtin.plugins.evolution.rules import RuleStore

    agent_module._set_agent_home(tmp_path / ".agent-dev")
    store = RuleStore()

    assert store._path == tmp_path / ".agent-dev" / "rl" / "rules.jsonl"
    assert store._path.parent.exists()
    # The default instance must not have been touched.
    assert not (tmp_path / ".agent" / "rl").exists()


def test_switching_home_moves_every_derived_path(restore_agent_home, tmp_path):
    agent_module._set_agent_home(tmp_path / ".agent-alt")

    for name in sorted(_HOME_DERIVED_NAMES):
        value = getattr(shared, name)
        assert str(value).startswith(str(tmp_path / ".agent-alt")), (name, value)

    # agent/__init__ keeps its own TASKS_DIR mirror; it must follow too.
    assert agent_module.TASKS_DIR == shared.SCHEDULER_DIR
