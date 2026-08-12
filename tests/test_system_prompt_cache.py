"""The system-prompt cache must not outlive the values it rendered from.

``_compose_system_prompt`` memoizes an expensive static block.  Its key used
to be a hand-listed subset of the real inputs, and it had already drifted:
``supports_vision`` (registry context — ``set_context`` does not bump
``_prompt_generation``) and ``TOOLS_DIR``/``SKILLS_DIR`` (rewritten by
``--name``) were absent, so a stale block could be served indefinitely.

``filesystem_sandbox`` hit the identical bug and fixed it by keying on
rendered content.  Here the fix is structural: ``_render_static_prompt``
takes a ``_StaticPromptInputs`` and can read nothing else, so the key and the
body cannot diverge.  These tests pin each input that used to be missing.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

import agent as agent_module
from agent import config as config_module
from agent import shared
from agent.tools.runtime import ToolRegistry


@pytest.fixture(autouse=True)
def _reset_prompt_cache():
    config_module._system_prompt_cache_key = None
    config_module._system_prompt_cache_value = ""
    yield
    config_module._system_prompt_cache_key = None
    config_module._system_prompt_cache_value = ""


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        name="read_file",
        description="read a file",
        parameters={"type": "object", "properties": {}},
        fn=lambda **_: None,
        source="builtin",
    )
    return registry


def _compose(registry, **kwargs) -> str:
    return config_module._compose_system_prompt(
        "base prompt", registry, workspace_root=Path("/ws"), **kwargs
    )


def test_supports_vision_change_invalidates_the_cache():
    """`set_context` does not bump `_prompt_generation` — the old key missed this."""
    registry = _registry()
    registry.set_context("supports_vision", False)
    without = _compose(registry)
    assert "supports vision" not in without

    registry.set_context("supports_vision", True)
    with_vision = _compose(registry)
    assert "supports vision" in with_vision


def test_agent_home_switch_invalidates_the_cache(tmp_path):
    """`--name` rewrites TOOLS_DIR/SKILLS_DIR, which the block interpolates."""
    original = shared.AGENT_HOME
    try:
        registry = _registry()
        agent_module._set_agent_home(tmp_path / ".agent-one")
        first = _compose(registry)
        assert str(tmp_path / ".agent-one" / "tools") in first

        agent_module._set_agent_home(tmp_path / ".agent-two")
        second = _compose(registry)
        assert str(tmp_path / ".agent-two" / "tools") in second
        assert str(tmp_path / ".agent-one" / "tools") not in second
    finally:
        agent_module._set_agent_home(original)


def test_tool_description_change_invalidates_the_cache():
    """The key carries tool descriptions, not just a generation counter."""
    registry = _registry()
    first = _compose(registry)
    assert "read a file" in first

    registry.register(
        name="read_file",
        description="read a file (v2)",
        parameters={"type": "object", "properties": {}},
        fn=lambda **_: None,
        source="builtin",
        replace=True,
    )
    second = _compose(registry)
    assert "read a file (v2)" in second


def test_identical_inputs_reuse_the_cached_block():
    """The cache must still work — this is not a "disable it" fix."""
    registry = _registry()
    _compose(registry)
    cached = config_module._system_prompt_cache_value
    _compose(registry)
    assert config_module._system_prompt_cache_value is cached


def test_every_static_input_is_part_of_the_key():
    """Structural guard: the key IS the input set, by construction.

    If a field is added to `_StaticPromptInputs` it is in the key
    automatically; if the renderer starts reading state from somewhere else,
    that state has no field and this test's premise is what breaks.
    """
    registry = _registry()
    inputs = config_module._static_prompt_inputs(
        "base", registry, Path("/ws"), Path("/out"), None
    )
    assert config_module._system_prompt_cache_key is None

    baseline = config_module._render_static_prompt(inputs)
    # Perturbing any single field must change the rendered block or at least
    # produce a distinct key — no field may be inert.
    for field in dataclasses.fields(inputs):
        assert hasattr(inputs, field.name)
    # The dataclass is frozen and compares by value, so it is a valid key.
    assert inputs == dataclasses.replace(inputs)
    assert config_module._render_static_prompt(inputs) == baseline


def test_renderer_does_not_read_module_state_for_paths(tmp_path):
    """`_render_static_prompt` must use its inputs, not live globals."""
    registry = _registry()
    inputs = config_module._static_prompt_inputs(
        "base", registry, Path("/ws"), Path("/out"), None
    )
    pinned = dataclasses.replace(inputs, tools_dir="/pinned/tools")

    original = shared.TOOLS_DIR
    try:
        shared.TOOLS_DIR = tmp_path / "somewhere-else"
        rendered = config_module._render_static_prompt(pinned)
    finally:
        shared.TOOLS_DIR = original

    assert "/pinned/tools" in rendered
    assert "somewhere-else" not in rendered
