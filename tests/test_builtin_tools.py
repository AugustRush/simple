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
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
    )
    tools = BuiltinTools(memory=memory, registry=registry, workspace_root=workspace)
    return tools, registry, workspace


def test_registry_rejects_duplicate_tool_names():
    from agent import ToolRegistry

    registry = ToolRegistry()
    registry.register("dup", "first", {"type": "object"}, lambda: "ok")

    with pytest.raises(ValueError):
        registry.register("dup", "second", {"type": "object"}, lambda: "nope")


def test_registry_rejects_cross_source_replace():
    from agent import ToolRegistry

    registry = ToolRegistry()
    registry.register(
        "dup",
        "first",
        {"type": "object"},
        lambda: "ok",
        source="builtin",
    )

    with pytest.raises(ValueError):
        registry.register(
            "dup",
            "second",
            {"type": "object"},
            lambda: "nope",
            replace=True,
            source="user_tool:demo",
        )


def test_registry_call_sanitizes_exceptions():
    from agent import ToolRegistry

    registry = ToolRegistry()

    def boom():
        raise RuntimeError("boom")

    registry.register("explode", "fails", {"type": "object"}, boom)

    result = asyncio.run(registry.call("explode", {}))
    payload = json.loads(result)

    assert payload["ok"] is False
    assert payload["tool"] == "explode"
    assert "boom" in payload["error"]
    assert "Traceback" not in payload["error"]
    assert "tests/test_builtin_tools.py" not in payload["error"]


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
    tools, _, workspace = make_builtin_tools(tmp_path)
    path = workspace / "large.txt"
    path.write_text("abcdefghij", encoding="utf-8")

    result = tools._read_file(str(path), max_bytes=4)

    assert result["ok"] is True
    assert result["content"] == "abcd"
    assert result["truncated"] is True


def test_read_file_rejects_binary_content(tmp_path):
    tools, _, workspace = make_builtin_tools(tmp_path)
    path = workspace / "binary.bin"
    path.write_bytes(b"\x00\x01\x02abc")

    result = tools._read_file(str(path))

    assert result["ok"] is False
    assert "binary" in result["error"].lower()


def test_list_files_respects_recursive_and_max_results(tmp_path):
    tools, _, workspace = make_builtin_tools(tmp_path)
    root = workspace / "files"
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

    assert flat["ok"] is True
    assert all("nested/c.txt" not in item for item in flat["items"])
    assert recursive["ok"] is True
    assert len(recursive["items"]) == 2
    assert recursive["truncated"] is True


def test_read_file_rejects_paths_outside_workspace(tmp_path):
    tools, _, workspace = make_builtin_tools(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    result = tools._read_file(str(outside))

    assert result["ok"] is False
    assert "outside the workspace" in result["error"].lower()


def test_write_file_rejects_paths_outside_workspace(tmp_path):
    tools, _, workspace = make_builtin_tools(tmp_path)
    outside = tmp_path / "outside.txt"

    result = tools._write_file(str(outside), "secret")

    assert result["ok"] is False
    assert "outside the workspace" in result["error"].lower()


def test_registry_call_returns_structured_builtin_payloads(tmp_path):
    tools, registry, workspace = make_builtin_tools(tmp_path)
    path = workspace / "note.txt"
    path.write_text("hello", encoding="utf-8")

    result = asyncio.run(registry.call("read_file", {"path": str(path)}))
    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["path"] == str(path.resolve())
    assert payload["content"] == "hello"


def test_current_time_returns_structured_timestamps(tmp_path):
    tools, registry, _ = make_builtin_tools(tmp_path)

    result = asyncio.run(registry.call("current_time", {}))
    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["timezone"] == "local"
    assert "local_time" in payload
    assert "utc_time" in payload
    assert "unix_timestamp" in payload


def test_memory_search_returns_structured_results(tmp_path):
    tools, registry, workspace = make_builtin_tools(tmp_path)
    tools.memory.write("identity", "user", "Prefers concise responses")

    result = asyncio.run(registry.call("memory_search", {"query": "concise", "top_k": 3}))
    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["query"] == "concise"
    assert payload["count"] >= 1
    assert payload["items"][0]["path"] == "identity/user"


def test_shell_timeout_terminates_process(tmp_path, monkeypatch):
    from agent import BuiltinTools

    tools, _, _ = make_builtin_tools(tmp_path)
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
    assert result["ok"] is False
    assert "timed out" in result["error"].lower()


def test_shell_passes_output_dir_env_to_subprocess(tmp_path, monkeypatch):
    tools, reg, _ = make_builtin_tools(tmp_path)
    reg.set_context("output_dir", str(tmp_path / "output"))
    captured = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"ok", b"")

    async def fake_create_subprocess_shell(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create_subprocess_shell)

    result = asyncio.run(tools._shell("echo ok", timeout=1))

    assert result["ok"] is True
    assert captured["env"]["AGENT_OUTPUT_DIR"] == str(tmp_path / "output")


@pytest.mark.parametrize(
    "command",
    [
        "sudo rm -rf tmp",
        "FOO=1 rm -rf tmp",
        "env rm -rf tmp",
    ],
)
def test_shell_blocks_wrapped_dangerous_commands(tmp_path, command):
    tools, _, _ = make_builtin_tools(tmp_path)

    result = asyncio.run(tools._shell(command, timeout=1))

    assert result["ok"] is False
    assert "rejected" in result["error"].lower()


def test_tavily_search_requires_api_key(tmp_path):
    tools, registry, _ = make_builtin_tools(tmp_path)

    result = asyncio.run(registry.call("tavily_search", {"query": "latest ai news"}))
    payload = json.loads(result)

    assert payload["ok"] is False
    assert "tavily api key" in payload["error"].lower()


def test_tavily_search_returns_normalized_results(tmp_path, monkeypatch):
    from agent import BuiltinTools

    tools, registry, _ = make_builtin_tools(tmp_path)
    registry.set_context("tavily_api_key", "test-key")

    def fake_request(api_key, query, max_results, search_depth, include_answer):
        assert api_key == "test-key"
        assert query == "latest ai news"
        assert max_results == 3
        assert search_depth == "advanced"
        assert include_answer is True
        return {
            "answer": "A concise answer",
            "results": [
                {
                    "title": "Example result",
                    "url": "https://example.com/news",
                    "content": "Example snippet",
                    "score": 0.91,
                }
            ],
        }

    monkeypatch.setattr(BuiltinTools, "_make_tavily_request", staticmethod(fake_request))

    result = asyncio.run(
        registry.call(
            "tavily_search",
            {
                "query": "latest ai news",
                "max_results": 3,
                "search_depth": "advanced",
                "include_answer": True,
            },
        )
    )
    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["query"] == "latest ai news"
    assert payload["count"] == 1
    assert payload["answer"] == "A concise answer"
    assert payload["results"] == [
        {
            "title": "Example result",
            "url": "https://example.com/news",
            "snippet": "Example snippet",
            "score": 0.91,
        }
    ]


def test_web_search_delegates_to_tavily_backend(tmp_path, monkeypatch):
    from agent import BuiltinTools

    tools, registry, _ = make_builtin_tools(tmp_path)

    async def fake_tavily(self, query, max_results=5, search_depth="basic", include_answer=False):
        assert query == "latest ai news"
        assert max_results == 3
        return {
            "ok": True,
            "query": query,
            "count": 1,
            "results": [{"title": "Example", "url": "https://example.com", "snippet": "news"}],
        }

    monkeypatch.setattr(BuiltinTools, "_tavily_search", fake_tavily)

    result = asyncio.run(
        registry.call("web_search", {"query": "latest ai news", "max_results": 3})
    )
    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["results"][0]["title"] == "Example"


def test_web_fetch_uses_asyncio_to_thread(tmp_path, monkeypatch):
    from agent import BuiltinTools

    tools, _, _ = make_builtin_tools(tmp_path)
    called = {}

    async def fake_to_thread(fn, *args, **kwargs):
        called["fn"] = fn
        called["args"] = args
        return b"<html><body>hello</body></html>"

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    result = asyncio.run(tools._web_fetch("https://example.com"))

    assert result["ok"] is True
    assert called["fn"] == tools._make_urllib_request
    assert called["args"] == ("https://example.com",)


def test_tavily_search_uses_asyncio_to_thread(tmp_path, monkeypatch):
    from agent import BuiltinTools

    tools, registry, _ = make_builtin_tools(tmp_path)
    registry.set_context("tavily_api_key", "test-key")
    called = {}

    async def fake_to_thread(fn, *args, **kwargs):
        called["fn"] = fn
        called["args"] = args
        return {"results": []}

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    result = asyncio.run(registry.call("tavily_search", {"query": "latest ai news"}))
    payload = json.loads(result)

    assert payload["ok"] is True
    assert called["fn"] == tools._make_tavily_request
    assert called["args"] == ("test-key", "latest ai news", 5, "basic", False)


def test_registry_call_classifies_value_errors():
    from agent import ToolRegistry

    registry = ToolRegistry()

    def bad_input():
        raise ValueError("invalid input")

    registry.register("explode", "fails", {"type": "object"}, bad_input)

    result = asyncio.run(registry.call("explode", {}))
    payload = json.loads(result)

    assert payload == {
        "ok": False,
        "tool": "explode",
        "error": "Invalid input for tool 'explode': invalid input",
    }


def test_registry_call_returns_structured_error_for_missing_tool():
    from agent import ToolRegistry

    registry = ToolRegistry()

    result = asyncio.run(registry.call("missing", {}))

    assert json.loads(result) == {
        "ok": False,
        "tool": "missing",
        "error": "tool 'missing' not found",
    }


def test_registry_call_returns_structured_error_for_timeout():
    from agent import ToolRegistry

    registry = ToolRegistry()

    async def slow():
        raise asyncio.TimeoutError()

    registry.register("slow", "slow", {"type": "object"}, slow)

    result = asyncio.run(registry.call("slow", {}))

    assert json.loads(result) == {
        "ok": False,
        "tool": "slow",
        "error": "Timeout calling tool 'slow'",
    }
