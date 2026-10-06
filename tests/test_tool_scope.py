"""One API for the three tool-set scopes the tree already had.

Before this, restricting which tools a registry exposes was expressed three
different ways, in three different places:

1. **whole-process** -- one registry built in ``bootstrap`` (``:510``);
2. **per web session** -- ``ToolRegistry.fork(exclude_source_prefixes=...)``
   (``:566-593``), called from ``bootstrap`` with ``("runtime:spawn",)``;
3. **per sub-agent** -- ``BaseAgent._create_sub_registry(profile, write_scope)``
   (``agent/core/agent.py:4030-4077``), which built a *new* registry, filtered
   by capability subset, excluded every orchestration tool, and cloned two
   context lists by hand.

All three are "copy the tools, copy the context, drop what does not belong",
so they are now one call.  These tests pin the shared behaviour plus each
scope's own rule, because the risk in collapsing them is that one case's rule
quietly becomes the default for another's.
"""

from __future__ import annotations

import pytest

from agent.tools.runtime import ToolDef, ToolRegistry, ToolView


def _tool(name: str, source: str, capabilities=()) -> ToolDef:
    return ToolDef(
        name=name,
        description=f"{name} tool",
        parameters={"type": "object", "properties": {}},
        fn=lambda **kwargs: "ok",
        source=source,
        capabilities=frozenset(capabilities),
    )


def _registry(*tools: ToolDef) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry._tools[tool.name] = tool
    registry.set_context("workspace_root", "/tmp/ws")
    return registry


def _names(registry: ToolRegistry) -> set[str]:
    return set(registry._tools)


# ── The shared mechanics ─────────────────────────────────────────────────────


def test_a_view_copies_the_tools_it_accepts() -> None:
    parent = _registry(_tool("read_file", "builtin"), _tool("shell", "builtin"))
    view = parent.derive(ToolView(accepts=lambda tool: tool.name != "shell"))
    assert _names(view) == {"read_file"}


def test_a_view_copies_the_parent_context() -> None:
    parent = _registry(_tool("read_file", "builtin"))
    view = parent.derive(ToolView(accepts=lambda tool: True))
    assert view.get_context("workspace_root") == "/tmp/ws"


def test_a_view_override_wins_over_the_parent_context() -> None:
    parent = _registry(_tool("read_file", "builtin"))
    view = parent.derive(
        ToolView(accepts=lambda tool: True), context={"workspace_root": "/tmp/other"}
    )
    assert view.get_context("workspace_root") == "/tmp/other"
    # And the parent is untouched.
    assert parent.get_context("workspace_root") == "/tmp/ws"


def test_a_view_clones_mutable_context_values_it_overrides() -> None:
    """A child that mutates a list must not rewrite the parent's list.

    The sub-agent path cloned two named lists by hand for exactly this reason;
    doing it generically is what makes the collapse safe rather than lucky.
    """
    parent = _registry(_tool("shell", "builtin"))
    blocked = ["rm", "dd"]
    parent.set_context("shell_blocked_commands", blocked)
    view = parent.derive(
        ToolView(accepts=lambda tool: True), context={"shell_blocked_commands": blocked}
    )
    view.get_context("shell_blocked_commands").append("mkfs")
    assert parent.get_context("shell_blocked_commands") == ["rm", "dd"]


def test_a_view_does_not_leak_tools_back_to_the_parent() -> None:
    parent = _registry(_tool("read_file", "builtin"))
    view = parent.derive(ToolView(accepts=lambda tool: True))
    view._tools["injected"] = _tool("injected", "runtime")
    assert "injected" not in _names(parent)


def test_a_view_shares_tool_definitions_rather_than_copying_them() -> None:
    """Definitions are immutable once registered, so a shallow share is right.

    Copying them would break the bound-method trick that tool callables
    rely on to find their owning registry.
    """
    parent = _registry(_tool("read_file", "builtin"))
    view = parent.derive(ToolView(accepts=lambda tool: True))
    assert view._tools["read_file"] is parent._tools["read_file"]


def test_an_empty_view_is_legal() -> None:
    """Excluding everything is a real configuration, not an error."""
    parent = _registry(_tool("read_file", "builtin"))
    view = parent.derive(ToolView(accepts=lambda tool: False))
    assert _names(view) == set()


# ── Rule 1: the per-session source exclusion ─────────────────────────────────


def test_excluded_source_prefixes_are_dropped() -> None:
    parent = _registry(
        _tool("read_file", "builtin"),
        _tool("spawn_agent", "runtime:spawn"),
        _tool("skill_read", "runtime:skill"),
    )
    view = parent.derive(ToolView(exclude_source_prefixes=("runtime:spawn",)))
    assert _names(view) == {"read_file", "skill_read"}


def test_several_excluded_prefixes_all_apply() -> None:
    parent = _registry(
        _tool("read_file", "builtin"),
        _tool("spawn_agent", "runtime:spawn"),
        _tool("skill_read", "runtime:skill"),
    )
    view = parent.derive(
        ToolView(exclude_source_prefixes=("runtime:spawn", "runtime:skill"))
    )
    assert _names(view) == {"read_file"}


# ── Rule 2: the sub-agent capability subset ──────────────────────────────────


def test_capabilities_include_keeps_only_tools_within_the_bound() -> None:
    """A tool with a capability outside the bound is dropped; so is a bare one.

    An unclassified tool is excluded rather than assumed safe -- the empty set
    is unknown, not unrestricted.
    """
    parent = _registry(
        _tool("read_file", "builtin", {"read"}),
        _tool("write_file", "builtin", {"output_write"}),
        _tool("shell", "builtin", {"shell"}),
        _tool("mystery", "builtin"),
    )
    view = parent.derive(
        ToolView(capabilities_include=frozenset({"read", "output_write"}))
    )
    assert _names(view) == {"read_file", "write_file"}


def test_an_empty_include_bound_is_not_a_wildcard() -> None:
    """``None`` means unconstrained; an empty frozenset admits nothing.

    ``issubset(())`` is trivially true, so an empty bound judged by subset alone
    would be a wildcard that reads as the strictest setting.
    """
    parent = _registry(_tool("read_file", "builtin", {"read"}))
    unconstrained = parent.derive(ToolView(capabilities_include=None))
    assert _names(unconstrained) == {"read_file"}
    empty = parent.derive(ToolView(capabilities_include=frozenset()))
    assert _names(empty) == set()


def test_a_bounded_view_drops_a_tool_that_declares_nothing() -> None:
    """Unknown capability is not the same as safe.

    The sub-agent profiles relied on this: an unclassified tool was excluded
    rather than admitted to a read-only view.
    """
    parent = _registry(
        _tool("read_file", "builtin", {"read"}),
        _tool("mystery", "builtin"),
    )
    view = parent.derive(ToolView(capabilities_include=frozenset({"read"})))
    assert _names(view) == {"read_file"}


def test_capabilities_exclude_drops_any_tool_carrying_one() -> None:
    """The orchestration rule: never spawn from a sub-agent, by capability."""
    parent = _registry(
        _tool("read_file", "builtin", {"read"}),
        _tool("spawn_agent", "runtime:spawn", {"orchestration"}),
        _tool("mixed", "runtime", {"orchestration", "read"}),
    )
    view = parent.derive(ToolView(capabilities_exclude=frozenset({"orchestration"})))
    assert _names(view) == {"read_file"}


def test_include_and_exclude_compose() -> None:
    """Exclusion is applied after inclusion, so the narrower rule wins."""
    parent = _registry(
        _tool("read_file", "builtin", {"read"}),
        _tool("spawn_agent", "runtime:spawn", {"read", "orchestration"}),
    )
    view = parent.derive(
        ToolView(
            capabilities_include=frozenset({"read", "orchestration"}),
            capabilities_exclude=frozenset({"orchestration"}),
        )
    )
    assert _names(view) == {"read_file"}


def test_filters_compose_with_source_exclusion() -> None:
    parent = _registry(
        _tool("read_file", "builtin", {"read"}),
        _tool("skill_read", "runtime:skill", {"read"}),
    )
    view = parent.derive(
        ToolView(
            exclude_source_prefixes=("runtime:skill",),
            capabilities_include=frozenset({"read"}),
        )
    )
    assert _names(view) == {"read_file"}


# ── The old entry points still work, expressed through the new one ───────────


def test_fork_is_the_source_exclusion_view() -> None:
    parent = _registry(
        _tool("read_file", "builtin"),
        _tool("spawn_agent", "runtime:spawn"),
    )
    forked = parent.fork(exclude_source_prefixes=("runtime:spawn",))
    assert _names(forked) == {"read_file"}
    assert forked.get_context("workspace_root") == "/tmp/ws"


def test_fork_still_accepts_a_context_override() -> None:
    parent = _registry(_tool("read_file", "builtin"))
    forked = parent.fork(
        {"workspace_root": "/tmp/session"},
        exclude_source_prefixes=(),
    )
    assert forked.get_context("workspace_root") == "/tmp/session"
    assert parent.get_context("workspace_root") == "/tmp/ws"


def test_and_none_is_a_real_view_of_everything() -> None:
    """``ToolView()`` accepts all: the base case each rule narrows."""
    parent = _registry(_tool("read_file", "builtin"), _tool("shell", "builtin"))
    view = parent.derive(ToolView())
    assert _names(view) == {"read_file", "shell"}


# ── The sub-agent scope, through the real method ─────────────────────────────


def _spawn_agent_with_tools(*tools: ToolDef):
    """A BaseAgent whose registry holds *tools*, with no other moving parts."""
    import agent as agent_module

    registry = ToolRegistry()
    for tool in tools:
        registry._tools[tool.name] = tool
    registry.set_context("workspace_root", "/tmp/ws")
    registry.set_context("shell_blocked_commands", ["rm"])
    return agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    ), registry


def test_sub_registry_excludes_orchestration_by_capability() -> None:
    """A sub-agent must never spawn, and the rule is by capability, not name."""
    agent, _ = _spawn_agent_with_tools(
        _tool("read_file", "builtin", {"read"}),
        _tool("spawn_agent", "runtime:spawn", {"orchestration"}),
        _tool("sneaky_spawner", "runtime:custom", {"orchestration"}),
    )
    sub = agent._create_sub_registry("full")
    assert _names(sub) == {"read_file"}


def test_read_only_profile_bounds_capabilities() -> None:
    agent, _ = _spawn_agent_with_tools(
        _tool("read_file", "builtin", {"read"}),
        _tool("write_file", "builtin", {"output_write"}),
        _tool("shell", "builtin", {"shell", "requires_intent"}),
    )
    sub = agent._create_sub_registry("read_only")
    assert _names(sub) == {"read_file", "write_file"}


def test_implementation_profile_adds_workspace_write_only_with_a_scope() -> None:
    agent, _ = _spawn_agent_with_tools(
        _tool("read_file", "builtin", {"read"}),
        _tool("edit_in_workspace", "builtin", {"workspace_write"}),
    )
    without = agent._create_sub_registry("implementation")
    assert _names(without) == {"read_file"}
    with_scope = agent._create_sub_registry("implementation", write_scope=["src"])
    assert _names(with_scope) == {"read_file", "edit_in_workspace"}


def test_sub_registry_records_its_profile_and_write_scope() -> None:
    agent, _ = _spawn_agent_with_tools(_tool("read_file", "builtin", {"read"}))
    sub = agent._create_sub_registry("implementation", write_scope=["src", "docs"])
    assert sub.get_context("capability_profile") == "implementation"
    assert sub.get_context("write_scope") == ["src", "docs"]


def test_sub_registry_clones_the_lists_it_inherits() -> None:
    """The parent's blocked-command list must not follow the child's edits."""
    agent, parent = _spawn_agent_with_tools(_tool("read_file", "builtin", {"read"}))
    sub = agent._create_sub_registry("full")
    sub.get_context("shell_blocked_commands").append("dd")
    assert parent.get_context("shell_blocked_commands") == ["rm"]


def test_sub_registry_still_rejects_an_unknown_profile() -> None:
    agent, _ = _spawn_agent_with_tools(_tool("read_file", "builtin", {"read"}))
    with pytest.raises(ValueError, match="unsupported capability_profile"):
        agent._create_sub_registry("read-only")


def test_sub_registry_tools_do_not_leak_back() -> None:
    agent, parent = _spawn_agent_with_tools(_tool("read_file", "builtin", {"read"}))
    sub = agent._create_sub_registry("full")
    sub._tools["extra"] = _tool("extra", "runtime")
    assert "extra" not in _names(parent)
