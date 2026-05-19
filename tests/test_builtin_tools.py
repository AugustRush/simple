"""Tests for built-in tool safety and resource boundaries."""

import asyncio
import json
from pathlib import Path
import sys

import pytest


@pytest.fixture(autouse=True)
def _isolate_scheduler_state(monkeypatch, tmp_path):
    import agent.shared as shared_module
    from agent.security.shell import shell_session_allowlist_clear

    agent_home = tmp_path / ".agent"
    monkeypatch.setattr(shared_module, "AGENT_HOME", agent_home)
    monkeypatch.setattr(shared_module, "DEFAULT_OUTPUT_DIR", agent_home / "output")
    monkeypatch.setattr(shared_module, "SCHEDULER_DIR", agent_home / "tasks")
    monkeypatch.setattr(
        shared_module,
        "SCHEDULER_DB_FILE",
        agent_home / "tasks" / "scheduler.db",
    )
    shell_session_allowlist_clear()


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


def make_builtin_tools_with_output_dir(tmp_path):
    from agent import BuiltinTools, MemoryPalace, ToolRegistry

    registry = ToolRegistry()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "agent-output"
    output.mkdir()
    memory = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
    )
    tools = BuiltinTools(
        memory=memory,
        registry=registry,
        workspace_root=workspace,
        output_dir=output,
    )
    return tools, registry, workspace, output


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

    flat = tools._list_files(
        str(root), pattern="*.txt", recursive=False, max_results=10
    )
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


def test_read_file_allows_text_files_in_output_dir(tmp_path):
    tools, _, _workspace, output = make_builtin_tools_with_output_dir(tmp_path)
    artifact = output / "result.txt"
    artifact.write_text("generated", encoding="utf-8")

    result = tools._read_file(str(artifact))

    assert result["ok"] is True
    assert result["path"] == str(artifact.resolve())
    assert result["content"] == "generated"


def test_read_file_returns_binary_metadata_for_output_dir_artifacts(tmp_path):
    tools, _, _workspace, output = make_builtin_tools_with_output_dir(tmp_path)
    artifact = output / "phoenix_fire.jpeg"
    artifact.write_bytes(b"\xff\xd8\xff\x00fakejpeg")

    result = tools._read_file(str(artifact))

    assert result["ok"] is True
    assert result["path"] == str(artifact.resolve())
    assert result["binary"] is True
    assert result["content"] == ""
    assert "generated artifact" in result["message"]


def test_list_files_allows_output_dir(tmp_path):
    tools, _, _workspace, output = make_builtin_tools_with_output_dir(tmp_path)
    artifact = output / "phoenix_fire.jpeg"
    artifact.write_bytes(b"fake")

    result = tools._list_files(str(output))

    assert result["ok"] is True
    assert str(artifact.resolve()) in result["items"]


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


def test_context_retrieve_returns_conversation_history_sections(tmp_path):
    from agent import BuiltinTools, ConsolidationEngine, ContextManager, LTMStore
    from agent import LocalRetriever, MemoryPalace, ToolRegistry

    registry = ToolRegistry()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")
    ctx_mgr = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
    )
    memory = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
        store=store,
    )
    tools = BuiltinTools(
        memory=memory,
        registry=registry,
        context_manager=ctx_mgr,
        workspace_root=workspace,
    )
    ctx_mgr.record_turn(
        user_content="我们刚才确认要做 durable event history",
        assistant_content="我会先写测试再实现。",
        channel="feishu",
    )

    result = tools._context_retrieve("刚才我们聊了什么", top_k=5)

    assert result["ok"] is True
    assert result["count"] >= 1
    assert "## Conversation History" in result["content"]
    assert "durable event history" in result["content"]


def test_memory_search_returns_structured_results(tmp_path):
    tools, registry, workspace = make_builtin_tools(tmp_path)
    tools.memory.write("identity", "user", "Prefers concise responses")

    result = asyncio.run(
        registry.call("memory_search", {"query": "concise", "top_k": 3})
    )
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

    monkeypatch.setattr(
        asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(
        BuiltinTools, "_terminate_process", fake_terminate, raising=False
    )

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
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )

    result = asyncio.run(tools._shell("echo ok", timeout=1))

    assert result["ok"] is True
    assert captured["env"]["AGENT_OUTPUT_DIR"] == str(tmp_path / "output")
    assert captured["env"]["AGENT_WORKSPACE_ROOT"]
    assert captured["cwd"] == str((tmp_path / "output" / "sandbox").resolve())
    assert captured["env"]["AGENT_SANDBOX_DIR"] == str((tmp_path / "output" / "sandbox").resolve())


def test_shell_defaults_to_agent_output_dir_not_workspace(tmp_path, monkeypatch):
    import agent.shared as shared_module

    tools, _reg, workspace = make_builtin_tools(tmp_path)
    captured = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"ok", b"")

    async def fake_create_subprocess_shell(*args, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )

    result = asyncio.run(tools._shell("echo ok", timeout=1))

    assert result["ok"] is True
    assert captured["cwd"] == str((shared_module.DEFAULT_OUTPUT_DIR / "sandbox").resolve())
    assert captured["cwd"] != str(workspace.resolve())
    assert captured["env"]["AGENT_OUTPUT_DIR"] == str(shared_module.DEFAULT_OUTPUT_DIR.resolve())


def test_shell_passes_validated_cwd_to_subprocess(tmp_path, monkeypatch):
    tools, reg, workspace, output_dir = make_builtin_tools_with_output_dir(tmp_path)
    captured = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"ok", b"")

    async def fake_create_subprocess_shell(*args, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create_subprocess_shell)

    result = asyncio.run(
        tools._shell("echo ok", timeout=1, cwd=str(output_dir))
    )

    assert result["ok"] is True
    assert captured["cwd"] == str(output_dir.resolve())


def test_shell_returns_confirmation_request_for_restricted_command(tmp_path):
    tools, _, _ = make_builtin_tools(tmp_path)

    result = asyncio.run(tools._shell("mv a b", timeout=1))

    assert result["ok"] is False
    assert result["requires_confirmation"] is True
    assert result["risk_level"] == "medium"
    assert result["confirmation_token"]
    assert "requires confirmation" in result["error"].lower()


def test_shell_runs_restricted_command_after_matching_confirmation(
    tmp_path, monkeypatch
):
    from agent.security.shell import shell_command_confirm

    tools, _, _ = make_builtin_tools(tmp_path)
    first = asyncio.run(tools._shell("mv a b", timeout=1))
    captured = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"ok", b"")

    async def fake_create_subprocess_shell(*args, **kwargs):
        captured["command"] = args[0]
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create_subprocess_shell)

    assert shell_command_confirm(first["confirmation_token"], "mv a b") is True
    result = asyncio.run(tools._shell("mv a b", timeout=1))

    assert result["ok"] is True
    assert captured["command"] == "mv a b"


def test_shell_runs_restricted_command_with_confirmation_token(
    tmp_path, monkeypatch
):
    tools, _, _ = make_builtin_tools(tmp_path)
    first = asyncio.run(tools._shell("mv a b", timeout=1))
    captured = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"ok", b"")

    async def fake_create_subprocess_shell(*args, **kwargs):
        captured["command"] = args[0]
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create_subprocess_shell)

    result = asyncio.run(
        tools._shell(
            "mv a b",
            timeout=1,
            confirmation_token=first["confirmation_token"],
        )
    )

    assert result["ok"] is True
    assert captured["command"] == "mv a b"


def test_shell_rejects_mismatched_confirmation_token(tmp_path):
    tools, _, _ = make_builtin_tools(tmp_path)
    first = asyncio.run(tools._shell("mv a b", timeout=1))

    result = asyncio.run(
        tools._shell(
            "mv c d",
            timeout=1,
            confirmation_token=first["confirmation_token"],
        )
    )

    assert result["ok"] is False
    assert result["requires_confirmation"] is True


def test_shell_confirmation_is_exact_command_only(tmp_path):
    from agent.security.shell import shell_command_confirm

    tools, _, _ = make_builtin_tools(tmp_path)
    first = asyncio.run(tools._shell("mv a b", timeout=1))

    assert shell_command_confirm(first["confirmation_token"], "mv a b") is True
    result = asyncio.run(tools._shell("mv c d", timeout=1))

    assert result["ok"] is False
    assert result["requires_confirmation"] is True


def test_shell_rejects_inline_cwd_escape(tmp_path):
    tools, _, _ = make_builtin_tools(tmp_path)

    result = asyncio.run(tools._shell("cd /tmp && echo ok", timeout=1))

    assert result["ok"] is False
    assert result["risk_level"] == "high"
    assert "cwd" in result["error"].lower()


def test_transcribe_audio_rejects_shell_control_in_template(tmp_path):
    tools, reg, workspace = make_builtin_tools(tmp_path)
    audio = workspace / "sample.wav"
    audio.write_bytes(b"RIFF")
    reg.set_context(
        "audio_transcription_command",
        "python transcribe.py {path}; touch /tmp/pwned",
    )

    result = asyncio.run(tools._transcribe_audio("sample.wav", timeout=1))

    assert result["ok"] is False
    assert "unsafe audio transcription command" in result["error"].lower()


@pytest.mark.parametrize(
    "command",
    [
        "sudo rm -rf tmp",
        "FOO=1 rm -rf tmp",
        "env rm -rf tmp",
        "shutdown now",
    ],
)
def test_shell_blocks_wrapped_dangerous_commands(tmp_path, command):
    tools, _, _ = make_builtin_tools(tmp_path)

    result = asyncio.run(tools._shell(command, timeout=1))

    assert result["ok"] is False
    assert "rejected" in result["error"].lower() or result.get(
        "requires_confirmation"
    )


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

    monkeypatch.setattr(
        BuiltinTools, "_make_tavily_request", staticmethod(fake_request)
    )

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

    async def fake_tavily(
        self, query, max_results=5, search_depth="basic", include_answer=False
    ):
        assert query == "latest ai news"
        assert max_results == 3
        return {
            "ok": True,
            "query": query,
            "count": 1,
            "results": [
                {"title": "Example", "url": "https://example.com", "snippet": "news"}
            ],
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


def test_web_fetch_reports_download_progress(tmp_path, monkeypatch):
    import urllib.request

    from agent.core.output import EventCollector, _active_event_collector
    from agent.tools.executor import RegularToolExecutor

    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    class _FakeResponse:
        headers = {"Content-Length": "11"}

        def __init__(self):
            self._chunks = [b"<p>hello ", b"world</p>", b""]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _size):
            return self._chunks.pop(0)

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: _FakeResponse(),
    )

    async def run():
        collector = EventCollector()
        token = _active_event_collector.set(collector)
        try:
            result = await RegularToolExecutor(registry, timeout_seconds=1).run(
                {"name": "web_fetch", "input": {"url": "https://example.com"}}
            )
        finally:
            _active_event_collector.reset(token)
        return json.loads(result), collector.drain()

    result, events = asyncio.run(run())

    assert result["ok"] is True
    assert "hello world" in result["content"]
    progress = [
        event for event in events
        if event.name == "tool_progress"
        and event.fields.get("status") == "downloading"
    ]
    assert progress
    assert progress[-1].fields["bytes_done"] == 18
    assert progress[-1].fields["operation_id"] == events[0].fields["operation_id"]


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


def test_builtin_tools_register_scheduler_runtime_tools(tmp_path):
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    tool_names = registry.list_tools()

    assert "schedule_create" in tool_names
    assert "schedule_list" in tool_names
    assert "schedule_delete" in tool_names
    assert "send_file" in tool_names


def test_schedule_create_uses_active_delivery_target_for_channel_messages(tmp_path):
    import agent.tools.runtime as runtime_module
    from agent.scheduler import SchedulerStore

    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    token = runtime_module._active_schedule_target.set(
        {
            "delivery_mode": "channel",
            "target_type": "feishu_chat",
            "chat_id": "oc_test_chat",
            "chat_type": "group",
        }
    )
    try:
        result = asyncio.run(
            registry.call(
                "schedule_create",
                {
                    "name": "reminder",
                    "trigger_type": "once",
                    "prompt": "测试一下",
                    "at": "2026-04-20T10:00:00+08:00",
                    "timezone_name": "Asia/Shanghai",
                },
            )
        )
    finally:
        runtime_module._active_schedule_target.reset(token)

    payload = json.loads(result)
    store = SchedulerStore(db_path=Path(payload["task"]["db_path"]))
    try:
        task = store.get_task(payload["task"]["id"])
    finally:
        store.close()

    assert payload["ok"] is True
    assert task is not None
    assert task.kind == "message"
    assert task.delivery_mode == "channel"
    assert task.payload["message_text"] == "测试一下"
    assert task.delivery_target.target_type == "feishu_chat"
    assert task.delivery_target.payload["chat_id"] == "oc_test_chat"
    assert "summary_text" in payload


def test_schedule_create_defaults_to_standalone_without_active_target(tmp_path):
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    result = asyncio.run(
        registry.call(
            "schedule_create",
            {
                "name": "reminder",
                "trigger_type": "once",
                "prompt": "测试一下",
                "at": "2026-04-20T10:00:00+08:00",
                "timezone_name": "Asia/Shanghai",
            },
        )
    )
    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["task"]["kind"] == "message"
    assert payload["task"]["delivery_mode"] == "standalone"


def test_schedule_create_uses_isolated_scheduler_db_in_tests(tmp_path):
    import agent.shared as shared_module

    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    result = asyncio.run(
        registry.call(
            "schedule_create",
            {
                "name": "isolated-reminder",
                "trigger_type": "once",
                "prompt": "测试隔离",
                "at": "2026-04-20T10:00:00+08:00",
                "timezone_name": "Asia/Shanghai",
            },
        )
    )
    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["task"]["db_path"] == str(shared_module.SCHEDULER_DB_FILE)


def test_send_file_queues_attachment_on_active_sink(tmp_path):
    import agent.tools.runtime as runtime_module

    _tools, registry, workspace = make_builtin_tools(tmp_path)
    target = workspace / "clip.mp4"
    target.write_bytes(b"video")

    class _Sink:
        def __init__(self):
            self.paths: list[Path] = []

        def queue_attachment(self, path: Path) -> None:
            self.paths.append(path)

    sink = _Sink()
    token = runtime_module._active_sink.set(sink)
    try:
        result = asyncio.run(
            registry.call("send_file", {"path": str(target)})
        )
    finally:
        runtime_module._active_sink.reset(token)

    payload = json.loads(result)

    assert payload["ok"] is True
    assert sink.paths == [target.resolve()]


def test_transcribe_audio_requires_configured_command(monkeypatch, tmp_path):
    monkeypatch.delenv("SIMPLE_AUDIO_TRANSCRIBE_COMMAND", raising=False)
    _tools, registry, workspace = make_builtin_tools(tmp_path)
    target = workspace / "voice.mp3"
    target.write_bytes(b"audio")

    result = asyncio.run(registry.call("transcribe_audio", {"path": str(target)}))
    payload = json.loads(result)

    assert payload["ok"] is False
    assert "not configured" in payload["error"]


def test_transcribe_audio_uses_configured_command(tmp_path):
    _tools, registry, workspace = make_builtin_tools(tmp_path)
    target = workspace / "voice.mp3"
    target.write_bytes(b"audio")
    script = tmp_path / "transcribe.py"
    script.write_text(
        "import pathlib, sys\n"
        "print('TRANSCRIPT:' + pathlib.Path(sys.argv[1]).name)\n",
        encoding="utf-8",
    )
    registry.set_context(
        "audio_transcription_command",
        f"{sys.executable} {script} {{path}}",
    )

    result = asyncio.run(registry.call("transcribe_audio", {"path": str(target)}))
    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["transcript"].strip() == "TRANSCRIPT:voice.mp3"
    assert payload["path"] == str(target.resolve())


def test_transcribe_audio_runs_in_agent_output_dir(tmp_path, monkeypatch):
    import agent.shared as shared_module

    _tools, registry, workspace = make_builtin_tools(tmp_path)
    target = workspace / "voice.mp3"
    target.write_bytes(b"audio")
    registry.set_context("audio_transcription_command", "fake-transcriber {path}")
    captured = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"transcript", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["argv"] = args
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    result = asyncio.run(registry.call("transcribe_audio", {"path": str(target)}))
    payload = json.loads(result)

    assert payload["ok"] is True
    assert captured["argv"][0] == "fake-transcriber"
    assert captured["cwd"] == str(shared_module.DEFAULT_OUTPUT_DIR.resolve())
    assert captured["env"]["AGENT_OUTPUT_DIR"] == captured["cwd"]
    assert captured["env"]["AGENT_WORKSPACE_ROOT"] == str(workspace.resolve())


def test_schedule_create_supports_agent_task_action_type(tmp_path):
    from agent.scheduler import SchedulerStore

    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    result = asyncio.run(
        registry.call(
            "schedule_create",
            {
                "name": "summary-task",
                "trigger_type": "once",
                "action_type": "agent_task",
                "instruction": "总结今天的群消息",
                "at": "2026-04-20T10:00:00+08:00",
                "timezone_name": "Asia/Shanghai",
            },
        )
    )
    payload = json.loads(result)
    store = SchedulerStore(db_path=Path(payload["task"]["db_path"]))
    try:
        task = store.get_task(payload["task"]["id"])
    finally:
        store.close()

    assert payload["ok"] is True
    assert task is not None
    assert task.kind == "agent_prompt"
    assert task.payload["prompt"] == "总结今天的群消息"


def test_schedule_create_supports_system_job_action_type(tmp_path):
    from agent.scheduler import SchedulerStore

    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    result = asyncio.run(
        registry.call(
            "schedule_create",
            {
                "name": "memory-tidy",
                "trigger_type": "daily",
                "action_type": "system_job",
                "job_name": "memory_tidy",
                "time_of_day": "03:00",
                "timezone_name": "Asia/Shanghai",
            },
        )
    )
    payload = json.loads(result)
    store = SchedulerStore(db_path=Path(payload["task"]["db_path"]))
    try:
        task = store.get_task(payload["task"]["id"])
    finally:
        store.close()

    assert payload["ok"] is True
    assert task is not None
    assert task.kind == "system_job"
    assert task.payload["job_name"] == "memory_tidy"


def test_schedule_create_is_idempotent_for_same_task_signature(tmp_path):
    from agent.scheduler import SchedulerStore

    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    args = {
        "name": "memory-tidy",
        "trigger_type": "daily",
        "action_type": "system_job",
        "job_name": "memory_tidy",
        "time_of_day": "03:00",
        "timezone_name": "Asia/Shanghai",
    }

    first = json.loads(asyncio.run(registry.call("schedule_create", args)))
    second = json.loads(asyncio.run(registry.call("schedule_create", args)))

    store = SchedulerStore(db_path=Path(first["task"]["db_path"]))
    try:
        tasks = store.list_tasks()
    finally:
        store.close()

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["task"]["id"] == first["task"]["id"]
    assert second["task"]["existing"] is True
    assert [task.id for task in tasks] == [first["task"]["id"]]
