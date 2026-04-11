"""Tests for built-in tool safety and resource boundaries."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def make_builtin_tools(tmp_path):
    from agent import BuiltinTools, MemoryPalace, ToolRegistry

    registry = ToolRegistry()
    memory = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
    )
    tools = BuiltinTools(memory=memory, registry=registry)
    return tools, registry


def test_registry_rejects_duplicate_tool_names():
    from agent import ToolRegistry

    registry = ToolRegistry()
    registry.register("dup", "first", {"type": "object"}, lambda: "ok")

    with pytest.raises(ValueError):
        registry.register("dup", "second", {"type": "object"}, lambda: "nope")


def test_registry_call_sanitizes_exceptions():
    from agent import ToolRegistry

    registry = ToolRegistry()

    def boom():
        raise RuntimeError("boom")

    registry.register("explode", "fails", {"type": "object"}, boom)

    result = asyncio.run(registry.call("explode", {}))

    assert "boom" in result
    assert "Traceback" not in result
    assert "tests/test_builtin_tools.py" not in result


def test_registry_call_json_encodes_structured_results():
    from agent import ToolRegistry

    registry = ToolRegistry()
    registry.register(
        "structured",
        "returns json",
        {"type": "object"},
        lambda: {"ok": True, "items": ["a", "b"]},
    )

    result = asyncio.run(registry.call("structured", {}))

    assert json.loads(result) == {"ok": True, "items": ["a", "b"]}


def test_read_file_truncates_large_content(tmp_path):
    tools, _ = make_builtin_tools(tmp_path)
    path = tmp_path / "large.txt"
    path.write_text("abcdefghij", encoding="utf-8")

    result = tools._read_file(str(path), max_bytes=4)

    assert "abcd" in result
    assert "truncated" in result.lower()


def test_read_file_rejects_binary_content(tmp_path):
    tools, _ = make_builtin_tools(tmp_path)
    path = tmp_path / "binary.bin"
    path.write_bytes(b"\x00\x01\x02abc")

    result = tools._read_file(str(path))

    assert "binary" in result.lower()


def test_list_files_respects_recursive_and_max_results(tmp_path):
    tools, _ = make_builtin_tools(tmp_path)
    root = tmp_path / "files"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "b.txt").write_text("b", encoding="utf-8")
    nested = root / "nested"
    nested.mkdir()
    (nested / "c.txt").write_text("c", encoding="utf-8")

    flat = tools._list_files(str(root), pattern="*.txt", recursive=False, max_results=10)
    recursive = tools._list_files(
        str(root), pattern="*.txt", recursive=True, max_results=2
    )
    recursive_lines = recursive.splitlines()
    listed = [line for line in recursive_lines if not line.startswith("...")]

    assert "nested/c.txt" not in flat
    assert len(listed) == 2
    assert "truncated" in recursive.lower()


def test_shell_timeout_terminates_process(tmp_path, monkeypatch):
    from agent import BuiltinTools

    tools, _ = make_builtin_tools(tmp_path)
    called = {"terminated": False}

    class FakeProc:
        pid = 123
        returncode = None

        async def communicate(self):
            return (b"", b"")

    async def fake_create_subprocess_shell(*args, **kwargs):
        return FakeProc()

    async def fake_terminate(self, proc):
        called["terminated"] = True

    async def fake_wait_for(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create_subprocess_shell)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(BuiltinTools, "_terminate_process", fake_terminate, raising=False)

    result = asyncio.run(tools._shell("sleep 10", timeout=1))

    assert called["terminated"] is True
    assert "timed out" in result.lower()
