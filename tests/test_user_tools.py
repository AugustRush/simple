"""Tests for the user-tool authoring pipeline.

The invariant under test throughout: nothing a generated module asks for —
a dependency, an import, a registration — is allowed to reach the user's
project or the running session without passing a check first.
"""

from __future__ import annotations

import asyncio
import sys

import pytest


@pytest.fixture
def tools_root(tmp_path, monkeypatch):
    from agent import shared

    root = tmp_path / "tools"
    root.mkdir()
    monkeypatch.setattr(shared, "TOOLS_DIR", root)
    return root


_GOOD_SOURCE = '''
def register(registry):
    async def echo(text: str) -> str:
        return text

    registry.register(
        "echo",
        "Echo text back.",
        {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "In."}},
            "required": ["text"],
        },
        echo,
    )
'''


# ── Source validation ────────────────────────────────────────────────────────


def test_validate_source_accepts_a_well_formed_module():
    from agent.tools import user_tools

    assert user_tools.validate_source(_GOOD_SOURCE) is None


@pytest.mark.parametrize(
    "source, expected",
    [
        ("", "empty"),
        ("def register(registry:\n", "syntax error"),
        ("x = 1", "top-level `register(registry)`"),
        ("async def register(registry):\n    registry.register()", "not async"),
        ("def register():\n    pass", "must accept the registry"),
        ("def register(registry):\n    return 1", "never calls registry.register"),
    ],
)
def test_validate_source_rejects_unusable_modules(source, expected):
    from agent.tools import user_tools

    error = user_tools.validate_source(source)
    assert error is not None
    assert expected in error


def test_validate_tool_id_rejects_traversal_and_private_names():
    from agent.tools import user_tools

    assert user_tools.validate_tool_id("good_tool_1") is None
    for bad in ("", "../escape", "_deps", "Has Spaces", "UPPER", "a" * 65):
        assert user_tools.validate_tool_id(bad) is not None, bad


# ── Import probe ─────────────────────────────────────────────────────────────


def test_probe_reports_the_tools_a_module_registers(tools_root):
    from agent.tools import user_tools

    path = tools_root / "echo.py"
    path.write_text(_GOOD_SOURCE, encoding="utf-8")

    result = asyncio.run(user_tools.probe_module(path, root=tools_root))

    assert result.ok is True
    assert [tool["name"] for tool in result.tools] == ["echo"]


def test_probe_catches_an_import_error_out_of_process(tools_root):
    """A module that explodes on import must not explode in the session."""
    from agent.tools import user_tools

    path = tools_root / "boom.py"
    path.write_text(
        "raise RuntimeError('boom')\n\n\ndef register(registry):\n"
        "    registry.register()\n",
        encoding="utf-8",
    )

    result = asyncio.run(user_tools.probe_module(path, root=tools_root))

    assert result.ok is False
    assert "boom" in result.error


def test_probe_rejects_a_module_that_registers_nothing(tools_root):
    from agent.tools import user_tools

    path = tools_root / "silent.py"
    path.write_text(
        "def register(registry):\n"
        "    if False:\n"
        "        registry.register()\n",
        encoding="utf-8",
    )

    result = asyncio.run(user_tools.probe_module(path, root=tools_root))

    assert result.ok is False
    assert "without registering any tool" in result.error


def test_probe_kills_a_module_that_blocks_on_import(tools_root):
    from agent.tools import user_tools

    path = tools_root / "hang.py"
    path.write_text(
        "import time\ntime.sleep(30)\n\n\ndef register(registry):\n"
        "    registry.register()\n",
        encoding="utf-8",
    )

    result = asyncio.run(
        user_tools.probe_module(path, root=tools_root, timeout=1.0)
    )

    assert result.ok is False
    assert "did not finish" in result.error


# ── Approval ledger ──────────────────────────────────────────────────────────


def test_approval_is_bound_to_file_contents(tools_root):
    from agent.tools import user_tools

    digest = user_tools.source_digest(_GOOD_SOURCE)
    user_tools.record_approval("echo", digest, tools_root)

    assert user_tools.is_approved("echo", digest, tools_root) is True
    # An edit is new code, so the old approval does not cover it.
    edited = user_tools.source_digest(_GOOD_SOURCE + "\n# changed\n")
    assert user_tools.is_approved("echo", edited, tools_root) is False


# ── Dependency isolation ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "requirement",
    [
        "markdown; rm -rf /",
        "https://example.com/evil.tar.gz",
        "../../../etc/passwd",
        "-e .",
        "markdown --target /etc",
        "",
    ],
)
def test_install_rejects_anything_that_is_not_a_plain_requirement(requirement):
    from agent.tools import user_tools

    assert user_tools.validate_requirement(requirement) is not None


def test_install_commands_always_target_the_isolated_directory(tools_root):
    from agent.tools import user_tools

    target = user_tools.deps_dir(tools_root)
    commands = user_tools._install_commands("markdown", target)

    assert commands, "no installer available in the test environment"
    for argv in commands:
        assert "--target" in argv
        assert argv[argv.index("--target") + 1] == str(target)
        assert "markdown" in argv


def test_install_falls_back_when_the_interpreter_has_no_pip(monkeypatch, tools_root):
    """A uv-created venv has no pip; that must not break tool dependencies."""
    from agent.tools import user_tools

    monkeypatch.setattr(user_tools.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(user_tools.shutil, "which", lambda name: "/usr/bin/uv")

    commands = user_tools._install_commands("markdown", user_tools.deps_dir(tools_root))

    assert len(commands) == 1
    assert commands[0][:3] == ["/usr/bin/uv", "pip", "install"]


def test_install_reports_clearly_when_no_installer_exists(monkeypatch, tools_root):
    from agent.tools import user_tools

    monkeypatch.setattr(user_tools.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(user_tools.shutil, "which", lambda name: None)

    result = asyncio.run(user_tools.install_dependency("markdown", root=tools_root))

    assert result["ok"] is False
    assert "No installer is available" in result["error"]


def test_deps_dir_is_on_sys_path_but_is_not_a_tool(tools_root):
    from agent.tools import user_tools

    target = user_tools.ensure_deps_on_path(tools_root)

    assert sys.path[0] == str(target)
    # The catalog must never try to import something out of _deps.
    assert (
        user_tools.is_tool_module(target / "markdown" / "__init__.py", tools_root)
        is False
    )


# ── End-to-end authoring ─────────────────────────────────────────────────────


def _approve_everything(monkeypatch):
    from agent.security import tool_approval

    async def _approve(**kwargs):
        return True, False

    monkeypatch.setattr(tool_approval, "confirm_tool_activation", _approve)


def _decline_everything(monkeypatch, *, interactive: bool):
    from agent.security import tool_approval

    async def _decline(**kwargs):
        return False, not interactive

    monkeypatch.setattr(tool_approval, "confirm_tool_activation", _decline)


def test_author_tool_activates_an_approved_tool(tools_root, monkeypatch):
    from agent.tools import user_tools
    from agent.tools.runtime import ToolRegistry

    _approve_everything(monkeypatch)
    registry = ToolRegistry()

    result = asyncio.run(
        user_tools.author_tool("echo", _GOOD_SOURCE, registry=registry)
    )

    assert result["ok"] is True
    assert result["registered_tools"] == ["echo"]
    assert result["activated"] is True
    assert (tools_root / "echo.py").is_file()
    assert "echo" in registry.list_tools()
    assert registry.tool_source("echo") == "user_tool:echo"


def test_declined_tool_leaves_nothing_behind(tools_root, monkeypatch):
    from agent.tools import user_tools
    from agent.tools.runtime import ToolRegistry

    _decline_everything(monkeypatch, interactive=True)
    registry = ToolRegistry()

    result = asyncio.run(
        user_tools.author_tool("echo", _GOOD_SOURCE, registry=registry)
    )

    assert result["ok"] is False
    assert result["cancelled"] is True
    assert list(tools_root.glob("*.py")) == []
    assert list(tools_root.glob(".candidate_*")) == []
    assert "echo" not in registry.list_tools()


def test_non_interactive_decline_asks_for_confirmation_instead(
    tools_root, monkeypatch
):
    """In chat there is nobody to answer synchronously — so ask, don't fail."""
    from agent.tools import user_tools
    from agent.tools.runtime import ToolRegistry

    _decline_everything(monkeypatch, interactive=False)

    result = asyncio.run(
        user_tools.author_tool("echo", _GOOD_SOURCE, registry=ToolRegistry())
    )

    assert result["requires_confirmation"] is True
    assert "echo" in result["confirmation_guidance"]
    assert result["would_register"] == ["echo"]


def test_a_broken_tool_never_reaches_the_tools_directory(tools_root, monkeypatch):
    from agent.tools import user_tools
    from agent.tools.runtime import ToolRegistry

    _approve_everything(monkeypatch)

    result = asyncio.run(
        user_tools.author_tool(
            "broken",
            "import definitely_not_a_real_package\n\n\n"
            "def register(registry):\n    registry.register()\n",
            registry=ToolRegistry(),
        )
    )

    assert result["ok"] is False
    assert result["stage"] == "probe"
    assert "install_tool_dependency" in result["recovery_hint"]
    assert list(tools_root.glob("*.py")) == []


def test_create_refuses_to_clobber_and_update_refuses_to_invent(
    tools_root, monkeypatch
):
    from agent.tools import user_tools
    from agent.tools.runtime import ToolRegistry

    _approve_everything(monkeypatch)
    registry = ToolRegistry()
    asyncio.run(user_tools.author_tool("echo", _GOOD_SOURCE, registry=registry))

    clobber = asyncio.run(
        user_tools.author_tool("echo", _GOOD_SOURCE, registry=registry)
    )
    assert clobber["ok"] is False
    assert "already exists" in clobber["error"]

    missing = asyncio.run(
        user_tools.author_tool(
            "nope", _GOOD_SOURCE, registry=registry, replace=True
        )
    )
    assert missing["ok"] is False
    assert "does not exist" in missing["error"]


def test_remove_tool_unloads_and_revokes_approval(tools_root, monkeypatch):
    from agent.tools import user_tools
    from agent.tools.runtime import ToolRegistry

    _approve_everything(monkeypatch)
    registry = ToolRegistry()
    asyncio.run(user_tools.author_tool("echo", _GOOD_SOURCE, registry=registry))

    result = user_tools.remove_tool("echo", registry=registry)

    assert result["ok"] is True
    assert "echo" not in registry.list_tools()
    assert not (tools_root / "echo.py").exists()
    assert user_tools.approved_ids(tools_root) == set()


def test_approved_tools_survive_restart_without_trusting_the_directory(
    tools_root, monkeypatch
):
    """The reason approvals are recorded on disk at all."""
    from agent.tools import user_tools
    from agent.tools.runtime import ToolRegistry, UserToolCatalog

    _approve_everything(monkeypatch)
    asyncio.run(
        user_tools.author_tool("echo", _GOOD_SOURCE, registry=ToolRegistry())
    )

    # A fresh session with user_tools still disabled.
    fresh = ToolRegistry()
    loaded = UserToolCatalog(tools_root).load_into_registry(
        fresh, require_approval=True
    )

    assert loaded == ["echo"]
    assert "echo" in fresh.list_tools()


def test_edited_tool_stops_loading_until_reapproved(tools_root, monkeypatch):
    from agent.tools import user_tools
    from agent.tools.runtime import ToolRegistry, UserToolCatalog

    _approve_everything(monkeypatch)
    asyncio.run(
        user_tools.author_tool("echo", _GOOD_SOURCE, registry=ToolRegistry())
    )

    # Someone edits the file outside the agent.
    (tools_root / "echo.py").write_text(
        _GOOD_SOURCE + "\n# tampered\n", encoding="utf-8"
    )

    fresh = ToolRegistry()
    loaded = UserToolCatalog(tools_root).load_into_registry(
        fresh, require_approval=True
    )

    assert loaded == []
    assert "echo" not in fresh.list_tools()
