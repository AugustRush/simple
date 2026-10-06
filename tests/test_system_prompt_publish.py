"""Publishing a refreshed system prompt: one meaning, one implementation.

``components["system_prompt"]`` has five readers and was written from five
places in four modules, each repeating the same three lines: assign the mapping,
fetch ``ctx``, assign the context.  Copies of a write drift, and the symptom is a
stale prompt in whichever reader the author forgot -- so the write is named here.

What is deliberately *not* unified: how the prompt is composed.  One caller falls
back to the working directory when no workspace is configured, and folding that
into a shared composer would silently change the others.  Each caller keeps its
own composition and publishes through one function.
"""

from __future__ import annotations

from types import MappingProxyType, SimpleNamespace

import pytest

from agent.runtime.system_prompt import (
    publish_system_prompt,
    refresh_system_prompt,
)


def _registry():
    """A real ToolRegistry: ``_compose_system_prompt`` reads its tool list."""
    import agent as agent_module

    return agent_module.ToolRegistry()


class _Ctx:
    def __init__(self) -> None:
        self.system_prompt = "old"


# ── publish ──────────────────────────────────────────────────────────────────


def test_the_prompt_lands_in_the_container() -> None:
    components: dict = {}
    returned = publish_system_prompt(components, "new")
    assert components["system_prompt"] == "new"
    assert returned == "new"


def test_a_live_context_is_updated_too() -> None:
    """The two are read by different consumers; refreshing one is the bug."""
    ctx = _Ctx()
    components: dict = {"ctx": ctx}
    publish_system_prompt(components, "new")
    assert components["system_prompt"] == "new"
    assert ctx.system_prompt == "new", "the live context must not go stale"


def test_no_context_is_fine() -> None:
    """Most compositions have no live context yet."""
    components: dict = {}
    publish_system_prompt(components, "new")
    assert components["system_prompt"] == "new"


def test_a_read_only_mapping_is_not_written_but_still_returns() -> None:
    frozen = MappingProxyType({})
    assert publish_system_prompt(frozen, "composed") == "composed"


def test_a_duck_typed_context_holder_works() -> None:
    """``components`` is often a bespoke holder, not a dict."""

    class _Holder:
        def __init__(self) -> None:
            self._d = {"ctx": _Ctx()}

        def get(self, key, default=None):
            return self._d.get(key, default)

        def __setitem__(self, key, value):
            self._d[key] = value

    holder = _Holder()
    publish_system_prompt(holder, "new")
    assert holder.get("system_prompt") == "new"
    assert holder.get("ctx").system_prompt == "new"


def test_publishing_twice_leaves_the_last_value() -> None:
    components: dict = {}
    publish_system_prompt(components, "first")
    publish_system_prompt(components, "second")
    assert components["system_prompt"] == "second"


# ── refresh ──────────────────────────────────────────────────────────────────


def test_refresh_composes_from_the_container_and_publishes() -> None:
    """The composition is the real one, so a registry is needed to see it work."""
    registry = _registry()
    components: dict = {
        "base_system_prompt": "BASE",
        "registry": registry,
        "workspace_root": "/tmp/ws",
        "output_dir": "/tmp/out",
    }
    composed = refresh_system_prompt(components)
    assert isinstance(composed, str) and composed
    assert components["system_prompt"] == composed


def test_refresh_uses_an_explicit_base_prompt() -> None:
    """A caller that just produced a new base prompt passes it in."""
    registry = _registry()
    components: dict = {
        "base_system_prompt": "OLD-BASE",
        "registry": registry,
        "workspace_root": "/tmp/ws",
        "output_dir": "/tmp/out",
    }
    with_new = refresh_system_prompt(components, base_prompt="NEW-BASE")
    implicit = refresh_system_prompt(
        {
            "base_system_prompt": "NEW-BASE",
            "registry": registry,
            "workspace_root": "/tmp/ws",
            "output_dir": "/tmp/out",
        }
    )
    assert with_new == implicit, "an explicit base must equal a stored one"


def test_refresh_does_not_change_the_base_prompt() -> None:
    """Storing a new base is a separate fact, and not every refresh wants it."""
    registry = _registry()
    components: dict = {
        "base_system_prompt": "KEEP",
        "registry": registry,
        "workspace_root": "/tmp/ws",
        "output_dir": "/tmp/out",
    }
    refresh_system_prompt(components, base_prompt="TRANSIENT")
    assert components["base_system_prompt"] == "KEEP"


def test_refresh_defaults_a_missing_base_prompt_to_empty() -> None:
    registry = _registry()
    components: dict = {
        "registry": registry,
        "workspace_root": "/tmp/ws",
        "output_dir": "/tmp/out",
    }
    assert refresh_system_prompt(components)


# ── The callers must go through it ───────────────────────────────────────────


def test_the_bootstrap_refresh_reaches_the_live_context() -> None:
    """Behaviour, not source text: the old bootstrap did update ``ctx``.

    Asserted through the real bootstrap helper rather than by matching its
    source, because matching source text pins the implementation and would pass
    for a rewrite that dropped the context update -- the exact drift the
    publisher exists to prevent.
    """
    from agent import bootstrap

    ctx = _Ctx()
    components: dict = {
        "base_system_prompt": "BASE",
        "registry": _registry(),
        "workspace_root": "/tmp/ws",
        "output_dir": "/tmp/out",
        "ctx": ctx,
    }
    refreshed = bootstrap._refresh_components_system_prompt(components)
    assert components["system_prompt"] == refreshed
    assert ctx.system_prompt == refreshed, "the live context must follow"


# ── The guard ────────────────────────────────────────────────────────────────


def test_only_the_publisher_assigns_the_composed_prompt() -> None:
    """A sixth copy is how the write drifts again.

    Scans the source because a duplicated assignment in a rarely taken branch is
    exactly the case to catch, and there is no runtime probe for a dead path.

    One exemption, and it is a different operation: the evolution plugin stores a
    brand-new *base* prompt alongside the composed one, which is not the same
    fact as publishing a composed prompt and has a fallback the others do not.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "agent"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(root.parent))
        if rel.endswith("runtime/system_prompt.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Subscript):
                    continue
                key = target.slice
                if isinstance(key, ast.Constant) and key.value == "system_prompt":
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "the composed system prompt is assigned outside its publisher:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse agent.runtime.system_prompt.publish_system_prompt(...)."
    )
