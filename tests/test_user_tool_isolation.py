"""User tools must never execute inside the agent process.

A user tool is Python the model wrote.  Loading it with ``exec_module`` gave
it the provider API keys, the memory database, the registry it could rewrite,
and the event loop it could hang — making the least-trusted code in the
system the only code with no boundary around it.

The load-time guard here (`test_loading_never_imports_the_tool_module`) is the
one that matters most: everything else can be re-derived, but if the parent
ever imports the module again the whole boundary is gone and no other test
would notice.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from agent.security.filesystem_sandbox import detect_sandbox_support
from agent.tools.runtime import ToolRegistry, UserToolCatalog

_NEEDS_SANDBOX = pytest.mark.skipif(
    detect_sandbox_support() != "darwin-sandbox-exec",
    reason="requires macOS sandbox-exec",
)


def _make_catalog(tmp_path: Path, source: str, *, sandbox: str = "none"):
    tools_root = tmp_path / "tools"
    tools_root.mkdir(parents=True, exist_ok=True)
    (tools_root / "demo.py").write_text(textwrap.dedent(source), encoding="utf-8")

    registry = ToolRegistry()
    registry.set_context("output_dir", str(tmp_path / "output"))
    registry.set_context("workspace_root", str(tmp_path / "workspace"))
    registry.set_context("shell_sandbox_mode", sandbox)

    catalog = UserToolCatalog(tools_root)
    loaded = catalog.load_into_registry(registry)
    return registry, loaded, tools_root


_ECHO_TOOL = """
    import os

    def register(registry):
        def probe(**kwargs):
            return {"ok": True, "pid": os.getpid(), "got": kwargs}

        registry.register(
            "probe", "report the pid", {"type": "object", "properties": {}}, probe
        )
"""


# ── The boundary itself ────────────────────────────────────────────────────


def test_loading_never_imports_the_tool_module(tmp_path):
    """The invariant. If this breaks, the process boundary is gone."""
    marker = "agent_user_tool_import_marker"
    source = f"""
    import sys

    sys.modules["{marker}"] = "leaked into the parent"

    def register(registry):
        registry.register(
            "noop", "does nothing", {{"type": "object", "properties": {{}}}},
            lambda: {{"ok": True}},
        )
    """
    before = set(sys.modules)
    registry, loaded, _ = _make_catalog(tmp_path, source)

    assert loaded == ["demo"], "the tool should still be registered"
    assert "noop" in registry._tools
    assert marker not in sys.modules, (
        "module-level code from a user tool ran inside the agent process"
    )
    leaked = {
        name
        for name in set(sys.modules) - before
        if "user_tool" in name or "agent_tool" in name
    }
    assert not leaked, f"tool modules imported into the parent: {leaked}"


def test_tool_runs_in_a_different_process(tmp_path):
    registry, loaded, _ = _make_catalog(tmp_path, _ECHO_TOOL)
    assert loaded == ["demo"]

    result = json.loads(asyncio.run(registry.call("probe", {})))

    assert result["ok"] is True
    assert result["pid"] != os.getpid()


def test_schema_survives_the_boundary(tmp_path):
    source = """
    def register(registry):
        registry.register(
            "shout",
            "uppercase some text",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            lambda text: text.upper(),
        )
    """
    registry, _loaded, _ = _make_catalog(tmp_path, source)

    tool = registry._tools["shout"]
    assert tool.description == "uppercase some text"
    assert tool.parameters["properties"]["text"]["type"] == "string"
    assert tool.source == "user_tool:demo"


# ── Result handling ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "returns, expected",
    [
        ('{"a": 1}', '{"a": 1}'),
        ("[1, 2, 3]", "[1, 2, 3]"),
        ('"plain string"', "plain string"),
        ("None", ""),
    ],
)
def test_return_values_round_trip(tmp_path, returns, expected):
    source = f"""
    def register(registry):
        registry.register(
            "give", "return a value", {{"type": "object", "properties": {{}}}},
            lambda: {returns},
        )
    """
    registry, _loaded, _ = _make_catalog(tmp_path, source)
    assert asyncio.run(registry.call("give", {})) == expected


def test_arguments_are_passed_through(tmp_path):
    registry, _loaded, _ = _make_catalog(tmp_path, _ECHO_TOOL)
    result = json.loads(
        asyncio.run(registry.call("probe", {"name": "august", "count": 3}))
    )
    assert result["got"] == {"name": "august", "count": 3}


def test_async_tool_functions_are_supported(tmp_path):
    source = """
    async def _work(x):
        return {"ok": True, "doubled": x * 2}

    def register(registry):
        registry.register(
            "double", "double a number",
            {"type": "object", "properties": {"x": {"type": "integer"}}},
            _work,
        )
    """
    registry, _loaded, _ = _make_catalog(tmp_path, source)
    result = json.loads(asyncio.run(registry.call("double", {"x": 21})))
    assert result["doubled"] == 42


# ── Failure containment ────────────────────────────────────────────────────


def test_a_raising_tool_returns_an_error_instead_of_propagating(tmp_path):
    source = """
    def register(registry):
        def explode():
            raise RuntimeError("tool blew up")

        registry.register(
            "explode", "raises", {"type": "object", "properties": {}}, explode
        )
    """
    registry, _loaded, _ = _make_catalog(tmp_path, source)
    result = json.loads(asyncio.run(registry.call("explode", {})))

    assert result["ok"] is False
    assert "tool blew up" in result["error"]


def test_a_tool_that_exits_the_interpreter_does_not_kill_the_session(tmp_path):
    """In-process, `sys.exit` in a tool would unwind the agent's own stack."""
    source = """
    import sys

    def register(registry):
        def bail():
            sys.exit(3)

        registry.register(
            "bail", "exits", {"type": "object", "properties": {}}, bail
        )
    """
    registry, _loaded, _ = _make_catalog(tmp_path, source)
    result = json.loads(asyncio.run(registry.call("bail", {})))

    assert result["ok"] is False  # reported, not fatal


def test_non_serializable_return_is_reported_clearly(tmp_path):
    source = """
    def register(registry):
        registry.register(
            "bad", "returns an unserializable dict",
            {"type": "object", "properties": {}},
            lambda: {"fn": object()},
        )
    """
    registry, _loaded, _ = _make_catalog(tmp_path, source)
    result = json.loads(asyncio.run(registry.call("bad", {})))

    assert result["ok"] is False
    assert "JSON-serializable" in result["error"]


def test_a_hanging_tool_is_killed_on_timeout(tmp_path):
    from agent.tools.user_tool_runner import run_user_tool

    tools_root = tmp_path / "tools"
    tools_root.mkdir(parents=True)
    (tools_root / "demo.py").write_text(
        textwrap.dedent(
            """
            import time

            def register(registry):
                registry.register(
                    "sleep", "sleeps forever",
                    {"type": "object", "properties": {}},
                    lambda: time.sleep(300),
                )
            """
        ),
        encoding="utf-8",
    )
    registry = ToolRegistry()
    registry.set_context("output_dir", str(tmp_path / "output"))
    registry.set_context("workspace_root", str(tmp_path / "workspace"))
    registry.set_context("shell_sandbox_mode", "none")

    result = asyncio.run(
        run_user_tool(
            tools_root / "demo.py",
            "sleep",
            {},
            registry=registry,
            root=tools_root,
            timeout=1.5,
        )
    )

    assert result["ok"] is False
    assert "exceeded" in result["error"]


def test_a_broken_module_is_skipped_without_breaking_the_others(tmp_path):
    tools_root = tmp_path / "tools"
    tools_root.mkdir(parents=True)
    (tools_root / "broken.py").write_text(
        "raise RuntimeError('bad import')\n", encoding="utf-8"
    )
    (tools_root / "good.py").write_text(
        textwrap.dedent(
            """
            def register(registry):
                registry.register(
                    "fine", "works", {"type": "object", "properties": {}},
                    lambda: {"ok": True},
                )
            """
        ),
        encoding="utf-8",
    )
    registry = ToolRegistry()
    registry.set_context("shell_sandbox_mode", "none")
    loaded = UserToolCatalog(tools_root).load_into_registry(registry)

    assert loaded == ["good"]
    assert "fine" in registry._tools


# ── Sandbox integration ────────────────────────────────────────────────────


@_NEEDS_SANDBOX
@pytest.mark.parametrize("mode", ["read_all", "restricted"])
def test_sandboxed_tool_cannot_read_the_agents_config(tmp_path, mode):
    """config.json holds provider API keys — the whole point of the boundary."""
    from agent import shared

    source = """
    def register(registry):
        def peek(path):
            try:
                return {"ok": True, "content": open(path).read()[:40]}
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}"}

        registry.register(
            "peek", "read a file",
            {"type": "object", "properties": {"path": {"type": "string"}}},
            peek,
        )
    """
    registry, loaded, _ = _make_catalog(tmp_path, source, sandbox=mode)
    assert loaded == ["demo"], "the tool must still load under a sandbox"

    result = json.loads(
        asyncio.run(registry.call("peek", {"path": str(shared.CONFIG_FILE)}))
    )
    assert result["ok"] is False, "the agent's API keys were readable"


@_NEEDS_SANDBOX
@pytest.mark.parametrize("mode", ["read_all", "restricted"])
def test_tools_still_run_under_every_sandbox_mode(tmp_path, mode):
    """A boundary that breaks the feature is not a boundary anyone keeps on.

    `restricted` in particular denies reads outside the workspace, which
    includes the interpreter's own venv — the child died in
    `init_import_site` before this was handled.
    """
    registry, loaded, _ = _make_catalog(tmp_path, _ECHO_TOOL, sandbox=mode)
    assert loaded == ["demo"]

    result = json.loads(asyncio.run(registry.call("probe", {})))
    assert result["ok"] is True
    assert result["pid"] != os.getpid()
