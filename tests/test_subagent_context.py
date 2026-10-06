"""Composing a sub-agent's system prompt and write scope.

The group this came from is not uniformly coupled: ``_execute_agent`` reaches
sixteen attributes over 288 lines, while these two touch two and three.  They
are extracted first because the property they protect is the one a sub-agent
gets wrong silently -- being told about tools it does not have, or inheriting
the parent's turn.

``compose_sub_system_prompt`` takes a ``compose`` callable and the real
``SkillCatalog`` class rather than importing either, so these tests drive it
with a recorder and a stub class.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.core.subagent_context import (
    canonicalize_write_scope,
    compose_sub_system_prompt,
)


class _Catalog:
    """Stand-in for the real ``SkillCatalog`` class in isinstance checks."""


def _registry(**context) -> SimpleNamespace:
    return SimpleNamespace(_context=dict(context))


def _compose_recorder() -> tuple[list, object]:
    calls: list = []

    def compose(base, registry, workspace_root, *, output_dir=None, skill_catalog=None):
        calls.append(
            {
                "base": base,
                "registry": registry,
                "workspace_root": workspace_root,
                "output_dir": output_dir,
                "skill_catalog": skill_catalog,
            }
        )
        return f"prompt({base})"

    return calls, compose


def _ctx(metadata: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(metadata=metadata or {})


# ── Write scope ──────────────────────────────────────────────────────────────


def test_a_relative_scope_resolves_under_the_workspace(tmp_path: Path) -> None:
    resolved = canonicalize_write_scope(
        "src", workspace_root=tmp_path, output_dir=None
    )
    assert str(resolved).startswith(str(tmp_path.resolve()))
    assert str(resolved).endswith("src")


def test_a_scope_outside_the_workspace_is_refused(tmp_path: Path) -> None:
    """The shared path policy, not a local rule: the refusal is the point."""
    outside = tmp_path.parent / "elsewhere"
    with pytest.raises(ValueError):
        canonicalize_write_scope(
            str(outside), workspace_root=tmp_path, output_dir=None
        )


def test_without_a_workspace_root_the_path_is_taken_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No project to be relative to, so the user's own path is used."""
    monkeypatch.chdir(tmp_path)
    resolved = canonicalize_write_scope(
        "notes.md", workspace_root=None, output_dir=None
    )
    assert resolved == (tmp_path / "notes.md").resolve()


def test_an_absolute_scope_inside_the_workspace_is_accepted(tmp_path: Path) -> None:
    target = tmp_path / "src"
    resolved = canonicalize_write_scope(
        str(target), workspace_root=tmp_path, output_dir=None
    )
    assert resolved == target.resolve()


# ── System prompt ────────────────────────────────────────────────────────────


def test_the_prompt_is_built_from_the_sub_registry_not_the_parent() -> None:
    """The property that fails silently: a prompt describing the wrong tools."""
    calls, compose = _compose_recorder()
    sub = _registry(output_dir="/tmp/out")
    parent = _registry(output_dir="/tmp/parent")

    compose_sub_system_prompt(
        sub,
        base_system_prompt="base",
        workspace_root=Path("/ws"),
        active_context=_ctx(),
        compose=compose,
        skill_catalog_type=_Catalog,
    )

    assert calls[0]["registry"] is sub
    assert calls[0]["registry"] is not parent
    assert calls[0]["base"] == "base"
    assert calls[0]["workspace_root"] == Path("/ws")


def test_the_output_dir_comes_from_the_sub_registry() -> None:
    calls, compose = _compose_recorder()
    compose_sub_system_prompt(
        _registry(output_dir="/tmp/sub-out"),
        base_system_prompt="b",
        workspace_root=None,
        active_context=_ctx(),
        compose=compose,
        skill_catalog_type=_Catalog,
    )
    assert calls[0]["output_dir"] == Path("/tmp/sub-out")


def test_no_output_dir_means_none_reaches_the_prompt() -> None:
    calls, compose = _compose_recorder()
    compose_sub_system_prompt(
        _registry(),
        base_system_prompt="b",
        workspace_root=None,
        active_context=_ctx(),
        compose=compose,
        skill_catalog_type=_Catalog,
    )
    assert calls[0]["output_dir"] is None


def test_a_real_skill_catalog_is_passed_through() -> None:
    calls, compose = _compose_recorder()
    catalog = _Catalog()
    compose_sub_system_prompt(
        _registry(),
        base_system_prompt="b",
        workspace_root=None,
        active_context=_ctx({"skill_catalog": catalog}),
        compose=compose,
        skill_catalog_type=_Catalog,
    )
    assert calls[0]["skill_catalog"] is catalog


def test_a_stub_skill_catalog_is_rejected() -> None:
    """Metadata may hold a test stub with no ``summary_lines``.

    Passing it would fail inside a sub-agent -- far from where the stub was
    put -- so the type is checked here instead.
    """
    calls, compose = _compose_recorder()
    compose_sub_system_prompt(
        _registry(),
        base_system_prompt="b",
        workspace_root=None,
        active_context=_ctx({"skill_catalog": object()}),
        compose=compose,
        skill_catalog_type=_Catalog,
    )
    assert calls[0]["skill_catalog"] is None


def test_no_active_context_still_produces_a_prompt() -> None:
    calls, compose = _compose_recorder()
    prompt, ctx = compose_sub_system_prompt(
        _registry(),
        base_system_prompt="b",
        workspace_root=None,
        active_context=None,
        compose=compose,
        skill_catalog_type=_Catalog,
    )
    assert prompt == "prompt(b)"
    assert ctx is None
    assert calls[0]["skill_catalog"] is None


def test_the_suffix_is_appended_as_its_own_block() -> None:
    _calls, compose = _compose_recorder()
    prompt, _ctx_out = compose_sub_system_prompt(
        _registry(),
        base_system_prompt="b",
        workspace_root=None,
        active_context=_ctx(),
        system_suffix="Be terse.",
        compose=compose,
        skill_catalog_type=_Catalog,
    )
    assert prompt == "prompt(b)\n\nBe terse."


def test_no_suffix_leaves_the_prompt_alone() -> None:
    _calls, compose = _compose_recorder()
    prompt, _ctx_out = compose_sub_system_prompt(
        _registry(),
        base_system_prompt="b",
        workspace_root=None,
        active_context=_ctx(),
        compose=compose,
        skill_catalog_type=_Catalog,
    )
    assert prompt == "prompt(b)"


def test_the_active_context_is_returned_alongside_the_prompt() -> None:
    """Callers need it back: the sub-agent inherits that context's metadata."""
    _calls, compose = _compose_recorder()
    ctx = _ctx()
    _prompt, returned = compose_sub_system_prompt(
        _registry(),
        base_system_prompt="b",
        workspace_root=None,
        active_context=ctx,
        compose=compose,
        skill_catalog_type=_Catalog,
    )
    assert returned is ctx


# ── The agent still delegates ────────────────────────────────────────────────


def test_the_agent_methods_agree_with_the_functions(tmp_path: Path) -> None:
    """A wrapper that silently stops wrapping is the failure mode to catch."""
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    registry.set_context("output_dir", str(tmp_path))
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    agent.workspace_root = tmp_path

    assert agent._canonicalize_write_scope("src") == canonicalize_write_scope(
        "src", workspace_root=tmp_path, output_dir=tmp_path
    )

    prompt, ctx = agent._compose_sub_system_prompt(registry, "suffix")
    assert isinstance(prompt, str)
    assert prompt.endswith("suffix")


# ── Metadata inheritance ─────────────────────────────────────────────────────
#
# The whitelist is the contract.  Copying ``metadata`` wholesale would hand a
# sub-agent its parent's turn: several run at once, so they would share and
# overwrite each other's per-turn entries.


def _meta(**values) -> SimpleNamespace:
    return SimpleNamespace(metadata=dict(values))


def test_the_inherited_keys_travel() -> None:
    from agent.core.subagent_context import propagate_sub_metadata

    catalog, token, manager = object(), object(), object()
    sub = _meta()
    propagate_sub_metadata(
        sub,
        _meta(
            skill_catalog=catalog,
            required_skills=["a", "b"],
            cancel_token=token,
            context_manager=manager,
            session_id="s-1",
        ),
    )
    assert sub.metadata["skill_catalog"] is catalog
    assert sub.metadata["required_skills"] == ["a", "b"]
    assert sub.metadata["cancel_token"] is token
    assert sub.metadata["context_manager"] is manager
    assert sub.metadata["session_id"] == "s-1"


def test_a_required_skills_list_is_copied_not_shared() -> None:
    """A child appending to it must not rewrite the parent's list."""
    from agent.core.subagent_context import propagate_sub_metadata

    parent = _meta(required_skills=["a"])
    sub = _meta()
    propagate_sub_metadata(sub, parent)
    sub.metadata["required_skills"].append("b")
    assert parent.metadata["required_skills"] == ["a"]


def test_per_turn_state_does_not_travel() -> None:
    """The reason this is a whitelist rather than a copy."""
    from agent.core.subagent_context import propagate_sub_metadata

    sub = _meta()
    propagate_sub_metadata(
        sub,
        _meta(
            model_override="other-model",
            required_skills_preset=["x"],
            _provider_step=7,
            scheduler_run_id="run-1",
            session_id="s-1",
        ),
    )
    assert sub.metadata == {"session_id": "s-1"}


def test_absorbing_keys_are_skipped_when_absent() -> None:
    from agent.core.subagent_context import propagate_sub_metadata

    sub = _meta()
    propagate_sub_metadata(sub, _meta(session_id="s-1"))
    assert sub.metadata == {"session_id": "s-1"}


def test_no_active_context_is_a_no_op() -> None:
    from agent.core.subagent_context import propagate_sub_metadata

    sub = _meta()
    propagate_sub_metadata(sub, None)
    assert sub.metadata == {}


def test_the_agent_still_delegates_metadata_propagation() -> None:
    import agent as agent_module

    agent = agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="fake-model", api_format="openai"
    )
    parent = _meta(session_id="s-9", model_override="m")
    child = _meta()
    agent._propagate_sub_metadata(child, parent)
    assert child.metadata == {"session_id": "s-9"}
    assert "model_override" not in child.metadata
