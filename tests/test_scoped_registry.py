"""One rule, one place: the scoped view is not the registry.

The bug class this file is about
--------------------------------
A plugin reaches ``ctx.get("registry")`` and gets a **scoped view** (``ScopedTools``),
which records each registration on the plugin's lifetime so it can be undone on unload.
The view *contributes*; it is not the registry.  Machinery that has to act on the
registry itself -- install a call's merged context, replace a module's tools by source
prefix, own a server's tools -- therefore needs the object behind it.

Four separate defects in one session had exactly this shape, and all four were found by
tests rather than by review:

* ``create_tool`` wrote the file, recorded its approval, then raised, because the view
  has no ``unregister_by_source_prefix``;
* an MCP client disconnected a server whose process really had stopped, while its tools
  stayed registered against the closed session;
* the per-call context merge was installed on nothing, so a sub-agent's narrowed write
  scope and a session's own workspace were silently not applied;
* user tools that had loaded fine failed every call on their own module (a sandbox
  policy ordering bug, but reached by the same investigation).

Each site used to re-derive the rule inline as ``getattr(x, "registry", x)``, each with a
paragraph of justification.  That is four copies of one rule -- so the fifth caller that
*did not* derive it was inevitable.  ``as_registry`` is now the only place the rule is
written down, and ``test_nothing_else_derives_the_rule_inline`` keeps it that way.
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
RULE_OWNER = REPO_ROOT / "agent" / "tools" / "scoped.py"


class _Scope:
    """The kernel's ``ServiceScope``, as far as the view needs it."""

    name = "probe"

    def __init__(self) -> None:
        self.undos: list = []

    def on_unwind(self, fn) -> None:
        self.undos.append(fn)


# ── the rule, behaviourally ─────────────────────────────────────────────────


def test_a_view_unwraps_to_the_registry_it_wraps() -> None:
    from agent.tools.runtime import ToolRegistry
    from agent.tools.scoped import as_registry

    registry = ToolRegistry()
    view = registry.scoped(_Scope())

    assert as_registry(view) is registry
    # ...and the object is not a view of itself.
    assert as_registry(as_registry(view)) is registry


def test_a_plain_registry_is_returned_unchanged() -> None:
    """The composition passes the registry itself in most paths; unwrapping must be a no-op."""
    from agent.tools.runtime import ToolRegistry
    from agent.tools.scoped import as_registry

    registry = ToolRegistry()
    assert as_registry(registry) is registry


def test_none_passes_through() -> None:
    """Call sites read ``getattr(owner, "registry", None)`` first; the unwrap must accept it."""
    from agent.tools.scoped import as_registry

    assert as_registry(None) is None


def test_the_rule_is_what_makes_a_prefix_removal_work() -> None:
    """The concrete failure, at the smallest size that shows it.

    A view has no ``unregister_by_source_prefix``; the registry does.  Before the unwrap,
    the caller's defensive ``getattr`` found nothing and removal silently did not happen.
    """
    from agent.tools.runtime import ToolRegistry
    from agent.tools.scoped import as_registry

    registry = ToolRegistry()
    view = registry.scoped(_Scope())
    registry.register(
        "mcp_alpha_echo",
        "echo",
        {"type": "object", "properties": {}},
        lambda **_: "hi",
        source="mcp:alpha",
    )

    assert not hasattr(view, "unregister_by_source_prefix"), (
        "if the view grew this method, the unwrap is no longer load-bearing -- and this "
        "test is the place that should notice"
    )
    as_registry(view).unregister_by_source_prefix("mcp:alpha")
    assert "mcp_alpha_echo" not in registry.list_tools()


# ── the rule, structurally: it stays in one place ───────────────────────────


def _registry_attr_defaults(path: pathlib.Path) -> list[int]:
    """Line numbers of ``getattr(x, "registry", ...)`` in *path*."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    lines: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "getattr" or len(node.args) < 2:
            continue
        key = node.args[1]
        if isinstance(key, ast.Constant) and key.value == "registry":
            lines.append(node.lineno)
    return lines


def test_nothing_else_derives_the_rule_inline() -> None:
    """A fifth copy of the rule is how the fifth bug happens.

    This is a source check, not a behavioural one, on purpose: the failure mode is a
    caller *writing the idiom again*, which no runtime test can see until it is wrong.
    """
    offenders: list[str] = []
    for path in (REPO_ROOT / "agent").rglob("*.py"):
        if path.resolve() == RULE_OWNER.resolve():
            continue
        for line in _registry_attr_defaults(path):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}")

    assert not offenders, (
        "these lines re-derive `getattr(x, \"registry\", x)` instead of calling "
        "`agent.tools.scoped.as_registry`:\n  " + "\n  ".join(sorted(offenders))
    )


