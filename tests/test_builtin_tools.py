"""Tests for built-in tool safety and resource boundaries."""

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


def _soon(hours: int = 2) -> str:
    """A moment in the future, in the zone these tests schedule in.

    A clock reading rather than a date, on purpose.  ``2026-04-20T10:00:00``
    was a few hours away when it was written and quietly became five months
    *behind* -- at which point every create below was refused for being in the
    past, so thirty tests reported a failure that had nothing to do with what
    they are about.  Creating a one-off that has already passed is a task that
    can never run, which is why the tools refuse it; a test that wants a valid
    task has to ask for a valid moment.
    """
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    return (
        datetime.now(timezone.utc) + timedelta(hours=hours)
    ).astimezone(ZoneInfo("Asia/Shanghai")).isoformat()


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


def test_memory_clear_uses_human_confirmation_and_storage_api(tmp_path):
    from agent.core.agent import AgentContext, _active_agent_context
    from agent.core.output import _active_sink

    tools, registry, _ = make_builtin_tools(tmp_path)
    tools.memory.write("identity", "user", "Prefers concise responses")

    class ApprovingSink:
        interactive_confirmation = True

        def __init__(self):
            self.calls = []

        async def on_tool_confirmation(self, name, **kwargs):
            self.calls.append((name, kwargs))
            return True

    sink = ApprovingSink()
    cleared = []
    context_manager = SimpleNamespace(
        on_memory_cleared=lambda: cleared.append(True)
    )
    context_token = _active_agent_context.set(
        AgentContext(metadata={"context_manager": context_manager})
    )
    sink_token = _active_sink.set(sink)
    try:
        result = json.loads(asyncio.run(registry.call("memory_clear", {})))
    finally:
        _active_sink.reset(sink_token)
        _active_agent_context.reset(context_token)

    assert result["ok"] is True
    assert result["deleted_by_store"]["memory_items"] == 1
    assert tools.memory.store.all_entries() == []
    assert sink.calls[0][0] == "memory_clear"
    assert "不可恢复" in sink.calls[0][1]["reason"]
    assert cleared == [True]
    assert registry.tool_capabilities("memory_clear") == frozenset({"state_write"})


def test_file_tool_schemas_are_rooted_and_closed(tmp_path):
    tools, registry, _ = make_builtin_tools(tmp_path)

    for name in ("read_file", "write_file", "edit_file", "list_files"):
        schema = registry._tools[name].parameters
        assert schema["additionalProperties"] is False, name
        assert schema["properties"]["root"]["enum"] == ["workspace", "output_dir"]

    read_schema = registry._tools["read_file"].parameters
    assert "max_bytes" not in read_schema["properties"]
    assert "path" in read_schema["required"]
    assert "edit_file" in registry.list_tools()

    write_schema = registry._tools["write_file"].parameters
    assert set(write_schema["required"]) == {"root", "path", "mode", "content"}
    assert write_schema["properties"]["mode"]["enum"] == ["create", "overwrite"]

    edit_schema = registry._tools["edit_file"].parameters
    assert set(edit_schema["required"]) == {
        "root",
        "path",
        "expected_revision",
        "replacements",
    }


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


def test_list_installed_plugins_counts_only_listed_user_plugins(tmp_path, monkeypatch):
    import agent.shared as shared_module

    tools, registry, _ = make_builtin_tools(tmp_path)
    user_plugins = tmp_path / "user-plugins"
    user_plugins.mkdir()
    (user_plugins / "webwright").mkdir()

    monkeypatch.setattr(shared_module, "USER_PLUGINS_DIR", user_plugins)

    class _Meta:
        def __init__(self, name):
            self.name = name

    class _Catalog:
        def list_plugins(self):
            # Simulate one loaded builtin + one loaded user plugin.
            return [_Meta("builtin_only"), _Meta("webwright")]

    registry.set_context("plugin_catalog", _Catalog())

    result = tools._list_installed_plugins()

    assert result["ok"] is True
    assert result["loaded_count"] == 1
    assert result["global_loaded_count"] == 2
    assert result["plugins"] == [
        {
            "name": "webwright",
            "path": str(user_plugins / "webwright"),
            "loaded": True,
        }
    ]


def test_read_file_returns_bounded_window_and_revision(tmp_path):
    tools, _, workspace = make_builtin_tools(tmp_path)
    path = workspace / "large.txt"
    path.write_text("\n".join(f"line{i}" for i in range(10)) + "\n", encoding="utf-8")

    result = tools._read_file(
        root="workspace", path="large.txt", start_line=3, line_count=2
    )

    assert result["ok"] is True
    assert result["path"] == "large.txt"
    assert result["content"] == "line2\nline3\n"
    assert result["start_line"] == 3
    assert result["end_line"] == 4
    assert result["next_start_line"] == 5
    assert result["total_lines"] == 10
    assert result["revision"].startswith("sha256:")


def test_read_file_rejects_invalid_utf8_content(tmp_path):
    tools, _, workspace = make_builtin_tools(tmp_path)
    (workspace / "binary.bin").write_bytes(b"\x00\x01\x02abc")

    result = tools._read_file(root="workspace", path="binary.bin")

    assert result["ok"] is False
    assert result["error"]["code"] == "unsupported_encoding"


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
        root="workspace",
        path="files",
        pattern="*.txt",
        recursive=False,
        max_results=10,
    )
    recursive = tools._list_files(
        root="workspace",
        path="files",
        pattern="*.txt",
        recursive=True,
        max_results=2,
    )

    assert flat["ok"] is True
    assert all(item["path"] != "files/nested/c.txt" for item in flat["items"])
    assert recursive["ok"] is True
    assert len(recursive["items"]) == 2
    assert recursive["truncated"] is True
    assert recursive["next_cursor"] is not None

    resumed = tools._list_files(
        root="workspace",
        path="files",
        pattern="*.txt",
        recursive=True,
        max_results=2,
        cursor=recursive["next_cursor"],
    )
    assert resumed["ok"] is True
    assert [item["path"] for item in resumed["items"]] == ["files/nested/c.txt"]


def test_read_file_rejects_paths_outside_workspace(tmp_path):
    tools, _, workspace = make_builtin_tools(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    result = tools._read_file(root="workspace", path=str(outside))

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_path"


def test_read_file_allows_text_files_in_output_dir(tmp_path):
    tools, _, _workspace, output = make_builtin_tools_with_output_dir(tmp_path)
    artifact = output / "result.txt"
    artifact.write_text("generated", encoding="utf-8")

    result = tools._read_file(root="output_dir", path="result.txt")

    assert result["ok"] is True
    assert result["path"] == "result.txt"
    assert result["content"] == "generated"


def test_list_files_allows_output_dir(tmp_path):
    tools, _, _workspace, output = make_builtin_tools_with_output_dir(tmp_path)
    artifact = output / "phoenix_fire.jpeg"
    artifact.write_bytes(b"fake")

    result = tools._list_files(root="output_dir", path=".")

    assert result["ok"] is True
    assert [item["path"] for item in result["items"]] == ["phoenix_fire.jpeg"]


def test_write_file_rejects_paths_outside_workspace(tmp_path):
    tools, _, _workspace = make_builtin_tools(tmp_path)
    outside = tmp_path / "outside.txt"

    result = tools._write_file(
        root="workspace", path=str(outside), mode="create", content="secret"
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_path"


def test_write_file_explicit_output_dir(tmp_path):
    tools, _reg, workspace, output_dir = make_builtin_tools_with_output_dir(tmp_path)

    result = tools._write_file(
        root="output_dir",
        path="report.md",
        mode="create",
        content="generated",
    )

    assert result["ok"] is True
    assert result["path"] == "report.md"
    assert not (workspace / "report.md").exists()
    assert (output_dir / "report.md").read_text(encoding="utf-8") == "generated"


def test_write_file_allows_scoped_workspace_write(tmp_path):
    from agent import BuiltinTools, MemoryPalace, ToolRegistry
    from agent.tools.files import FileAccessPolicy, FileService

    registry = ToolRegistry()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "agent-output"
    output.mkdir()
    memory = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
    )
    policy = FileAccessPolicy.from_config(
        {"workspace": {"read": True, "write": True}},
        workspace_root=workspace,
        output_root=output,
    )
    service = FileService(policy, write_scope=["src/app.py"])
    tools = BuiltinTools(
        memory=memory,
        registry=registry,
        workspace_root=workspace,
        output_dir=output,
        file_service=service,
    )

    result = tools._write_file(
        root="workspace",
        path="src/app.py",
        mode="create",
        content="print('ok')\n",
    )

    assert result["ok"] is True
    assert result["path"] == "src/app.py"
    assert (workspace / "src" / "app.py").read_text(encoding="utf-8") == "print('ok')\n"

    forbidden = tools._write_file(
        root="workspace",
        path="other.txt",
        mode="create",
        content="x",
    )
    assert forbidden["error"]["code"] == "access_denied"


def test_registry_round_trip_create_edit_read(tmp_path):
    tools, registry, _workspace, output = make_builtin_tools_with_output_dir(tmp_path)

    created = json.loads(
        asyncio.run(
            registry.call(
                "write_file",
                {
                    "root": "output_dir",
                    "path": "note.txt",
                    "mode": "create",
                    "content": "alpha\nbeta\n",
                },
            )
        )
    )
    assert created["ok"] is True

    read = json.loads(
        asyncio.run(
            registry.call(
                "read_file",
                {"root": "output_dir", "path": "note.txt"},
            )
        )
    )
    assert read["content"] == "alpha\nbeta\n"

    edited = json.loads(
        asyncio.run(
            registry.call(
                "edit_file",
                {
                    "root": "output_dir",
                    "path": "note.txt",
                    "expected_revision": read["revision"],
                    "replacements": [
                        {"old_text": "beta", "new_text": "gamma", "expected_count": 1}
                    ],
                },
            )
        )
    )
    assert edited["ok"] is True
    assert (output / "note.txt").read_text(encoding="utf-8") == "alpha\ngamma\n"


def test_tool_schema_validation_rejects_invalid_inputs(tmp_path):
    tools, registry, workspace = make_builtin_tools(tmp_path)
    (workspace / "a.txt").write_text("x", encoding="utf-8")

    missing = json.loads(
        asyncio.run(registry.call("read_file", {"root": "workspace"}))
    )
    assert missing["error"]["code"] == "invalid_request"
    assert "missing required field: path" in missing["error"]["message"]

    unknown = json.loads(
        asyncio.run(
            registry.call(
                "read_file",
                {"root": "workspace", "path": "a.txt", "bogus": 1},
            )
        )
    )
    assert unknown["error"]["code"] == "invalid_request"
    assert "unknown field" in unknown["error"]["message"]

    wrong_type = json.loads(
        asyncio.run(
            registry.call(
                "read_file",
                {"root": "workspace", "path": "a.txt", "start_line": "1"},
            )
        )
    )
    assert wrong_type["error"]["code"] == "invalid_request"

    bad_enum = json.loads(
        asyncio.run(
            registry.call(
                "write_file",
                {"root": "bogus", "path": "a.txt", "mode": "create", "content": "x"},
            )
        )
    )
    assert bad_enum["error"]["code"] == "invalid_request"


def test_tool_schema_validation_no_coercion_and_integer_bounds():
    from agent import ToolRegistry

    registry = ToolRegistry()
    registry.register(
        "bounded",
        "bounded tool",
        {
            "type": "object",
            "properties": {"n": {"type": "integer", "minimum": 1}},
            "required": ["n"],
        },
        lambda n: {"ok": True, "n": n},
    )

    coerced = json.loads(asyncio.run(registry.call("bounded", {"n": "5"})))
    assert coerced["error"]["code"] == "invalid_request"

    below = json.loads(asyncio.run(registry.call("bounded", {"n": 0})))
    assert below["error"]["code"] == "invalid_request"

    ok = json.loads(asyncio.run(registry.call("bounded", {"n": 5})))
    assert ok == {"ok": True, "n": 5}


def test_schema_validation_runs_before_authorizer_and_handler():
    from agent import ToolRegistry

    calls = []

    def authorizer(tool_input, registry):
        calls.append(("authorizer", tool_input))
        return None

    registry = ToolRegistry()
    registry.register(
        "guarded",
        "guarded tool",
        {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
            "additionalProperties": False,
        },
        lambda n: calls.append(("handler", n)) or {"ok": True},
        authorizer=authorizer,
    )

    invalid = json.loads(asyncio.run(registry.call("guarded", {"n": "bad"})))
    assert invalid["error"]["code"] == "invalid_request"
    assert calls == []

    valid = json.loads(asyncio.run(registry.call("guarded", {"n": 3})))
    assert valid == {"ok": True}
    assert calls == [("authorizer", {"n": 3}), ("handler", 3)]


def test_authorizer_denial_is_returned_unchanged():
    from agent import ToolRegistry

    denial = {
        "ok": False,
        "error": {
            "code": "access_denied",
            "message": "blocked by authorizer",
            "details": {"reason": "test"},
            "retryable": False,
        },
    }

    def authorizer(tool_input, registry):
        return denial

    registry = ToolRegistry()
    registry.register(
        "guarded",
        "guarded tool",
        {"type": "object", "properties": {}},
        lambda: {"ok": True},
        authorizer=authorizer,
    )

    result = json.loads(asyncio.run(registry.call("guarded", {})))
    assert result == denial


def test_file_mutation_authorizer_allows_output_dir_for_read_only(tmp_path):
    tools, registry, _ = make_builtin_tools(tmp_path)
    registry.set_context("capability_profile", "read_only")

    output_write = json.loads(
        asyncio.run(
            registry.call(
                "write_file",
                {"root": "output_dir", "path": "a.txt", "mode": "create", "content": "x"},
            )
        )
    )
    assert output_write["ok"] is True

    workspace_write = json.loads(
        asyncio.run(
            registry.call(
                "write_file",
                {"root": "workspace", "path": "a.txt", "mode": "create", "content": "x"},
            )
        )
    )
    assert workspace_write["error"]["code"] == "access_denied"


def test_file_mutation_authorizer_enforces_implementation_scope(tmp_path):
    from agent import BuiltinTools, MemoryPalace, ToolRegistry
    from agent.tools.files import FileAccessPolicy, FileService

    registry = ToolRegistry()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    policy = FileAccessPolicy.from_config(
        {"workspace": {"read": True, "write": True}},
        workspace_root=workspace,
        output_root=output,
    )
    service = FileService(
        policy,
        write_scope=lambda: registry.get_context("write_scope") or (),
    )
    memory = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
    )
    BuiltinTools(
        memory,
        registry,
        workspace_root=workspace,
        output_dir=output,
        file_service=service,
    )
    registry.set_context("file_access_policy", policy)
    registry.set_context("capability_profile", "implementation")
    registry.set_context("write_scope", ["src/app.py"])

    inside = json.loads(
        asyncio.run(
            registry.call(
                "write_file",
                {
                    "root": "workspace",
                    "path": "src/app.py",
                    "mode": "create",
                    "content": "print('x')\n",
                },
            )
        )
    )
    assert inside["ok"] is True
    assert (workspace / "src" / "app.py").read_text() == "print('x')\n"

    outside = json.loads(
        asyncio.run(
            registry.call(
                "write_file",
                {"root": "workspace", "path": "other.txt", "mode": "create", "content": "x"},
            )
        )
    )
    assert outside["error"]["code"] == "access_denied"
    assert "write scope" in outside["error"]["message"]


def test_registry_call_returns_structured_builtin_payloads(tmp_path):
    tools, registry, workspace = make_builtin_tools(tmp_path)
    path = workspace / "note.txt"
    path.write_text("hello", encoding="utf-8")

    result = asyncio.run(
        registry.call("read_file", {"root": "workspace", "path": "note.txt"})
    )
    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["path"] == "note.txt"
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


def test_context_retrieve_uses_active_session_manager(tmp_path):
    from agent import AgentContext, BuiltinTools, ConsolidationEngine, ContextManager
    from agent import LocalRetriever, LTMStore, MemoryPalace, ToolRegistry
    from agent.core.agent import _active_agent_context

    registry = ToolRegistry()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")
    base = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
    )
    cli_manager = base.spawn_session("cli")
    memory = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
        store=store,
    )
    tools = BuiltinTools(
        memory=memory,
        registry=registry,
        context_manager=base,
        workspace_root=workspace,
    )
    base.record_turn(
        user_content="base session topic",
        assistant_content="base reply",
        channel="internal",
        message_id="base-turn",
    )
    cli_manager.record_turn(
        user_content="cli session topic",
        assistant_content="cli reply",
        channel="cli",
        message_id="cli-turn",
    )
    ctx = AgentContext(metadata={"context_manager": cli_manager})
    token = _active_agent_context.set(ctx)
    try:
        result = tools._context_retrieve("刚才我们聊了什么", top_k=5)
    finally:
        _active_agent_context.reset(token)

    assert "cli session topic" in result["content"]
    assert "base session topic" not in result["content"]


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


def test_shell_timeout_is_reported_as_an_error_not_a_hang(tmp_path):
    """The shell tool's half of a timeout: turn it into a legible failure.

    Killing the process group is the provider's half, covered end to end in
    ``tests/test_subprocess_provider.py`` — including the negative case where
    a grandchild would otherwise survive.  Asserting it here too would only
    re-test the provider through a longer path.
    """
    from agent.exec import ExecResult

    tools, reg, _ = make_builtin_tools(tmp_path)

    class TimingOutProvider:
        async def run(self, request):
            assert request.timeout == 1
            return ExecResult(timed_out=True)

    reg.set_context("subprocess_provider", TimingOutProvider())

    result = asyncio.run(tools._shell("sleep 10", timeout=1))

    assert result["ok"] is False
    assert "timed out" in result["error"].lower()
    assert result["timed_out"] is True


def test_shell_passes_roots_to_subprocess_environment(tmp_path, monkeypatch):
    tools, reg, workspace = make_builtin_tools(tmp_path)
    reg.set_context("output_dir", str(tmp_path / "output"))
    captured = {}

    class FakeProc:
        returncode = 0

        async def communicate(self, stdin=None):
            return (b"ok", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    result = asyncio.run(tools._shell("echo ok", timeout=1))

    assert result["ok"] is True
    assert captured["env"]["AGENT_OUTPUT_DIR"] == str(tmp_path / "output")
    assert captured["env"]["AGENT_WORKSPACE_ROOT"]
    assert captured["cwd"] == str(workspace.resolve())
    assert captured["env"]["AGENT_SANDBOX_DIR"] == str((tmp_path / "output" / "sandbox").resolve())


def test_shell_defaults_to_selected_workspace(tmp_path, monkeypatch):
    import agent.shared as shared_module

    tools, _reg, workspace = make_builtin_tools(tmp_path)
    captured = {}

    class FakeProc:
        returncode = 0

        async def communicate(self, stdin=None):
            return (b"ok", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    result = asyncio.run(tools._shell("echo ok", timeout=1))

    assert result["ok"] is True
    assert captured["cwd"] == str(workspace.resolve())
    assert captured["env"]["AGENT_OUTPUT_DIR"] == str(shared_module.DEFAULT_OUTPUT_DIR.resolve())


def test_shell_passes_validated_cwd_to_subprocess(tmp_path, monkeypatch):
    tools, reg, workspace, output_dir = make_builtin_tools_with_output_dir(tmp_path)
    captured = {}

    class FakeProc:
        returncode = 0

        async def communicate(self, stdin=None):
            return (b"ok", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    result = asyncio.run(
        tools._shell("echo ok", timeout=1, cwd=str(output_dir))
    )

    assert result["ok"] is True
    assert captured["cwd"] == str(output_dir.resolve())


def test_shell_resolves_relative_cwd_inside_declared_root(tmp_path, monkeypatch):
    tools, _reg, workspace, output_dir = make_builtin_tools_with_output_dir(tmp_path)
    captured = {}

    class FakeProc:
        returncode = 0

        async def communicate(self, stdin=None):
            return (b"ok", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    asyncio.run(tools._shell("echo ok", timeout=1, root="workspace", cwd="sub"))
    assert captured["cwd"] == str((workspace / "sub").resolve())

    asyncio.run(tools._shell("echo ok", timeout=1, root="output_dir", cwd="sub"))
    assert captured["cwd"] == str((output_dir / "sub").resolve())


def test_shell_rejects_relative_cwd_escaping_declared_root(tmp_path):
    tools, _reg, _workspace, output_dir = make_builtin_tools_with_output_dir(tmp_path)

    result = asyncio.run(
        tools._shell(
            "echo ok",
            timeout=1,
            root="output_dir",
            cwd="../workspace",
        )
    )

    assert result["ok"] is False
    assert "escapes root" in result["error"]


def test_shell_rejects_unknown_root(tmp_path):
    tools, _reg, _workspace = make_builtin_tools(tmp_path)

    result = asyncio.run(tools._shell("echo ok", timeout=1, root="bogus"))

    assert result["ok"] is False
    assert "root" in result["error"]


def test_shell_output_domain_relocates_workspace_files_without_sandbox(tmp_path):
    tools, registry, workspace, output_dir = make_builtin_tools_with_output_dir(
        tmp_path
    )
    registry.set_context("shell_permission_level", "full")
    registry.set_context("shell_sandbox_mode", "none")

    result = asyncio.run(
        tools._shell(
            f"mkdir -p {workspace}/polluted && "
            f"touch {workspace}/polluted/new.txt",
            timeout=10,
            root="output_dir",
            cwd=".",
        )
    )

    assert result["ok"] is True
    assert not (workspace / "polluted" / "new.txt").exists()
    assert (output_dir / "workspace-artifacts" / "polluted" / "new.txt").exists()
    assert result["moved_artifacts"]


def test_shell_workspace_domain_keeps_files_without_sandbox(tmp_path):
    tools, registry, workspace, _output_dir = make_builtin_tools_with_output_dir(
        tmp_path
    )
    registry.set_context("shell_permission_level", "full")
    registry.set_context("shell_sandbox_mode", "none")

    result = asyncio.run(
        tools._shell(
            "mkdir -p newdir && touch newdir/new.txt",
            timeout=10,
            root="workspace",
            cwd=".",
        )
    )

    assert result["ok"] is True
    assert (workspace / "newdir" / "new.txt").exists()
    assert result["moved_artifacts"] == []


def test_workspace_artifact_relocation_does_not_follow_symlinks(tmp_path):
    tools, _registry, workspace, _output_dir = make_builtin_tools_with_output_dir(
        tmp_path
    )
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "secret-link"
    link.symlink_to(outside)

    moved = tools._move_new_workspace_files_to_output_dir(
        before=set(), cwd=workspace
    )

    assert moved == []
    assert outside.read_text(encoding="utf-8") == "secret"
    assert link.is_symlink()


def test_shell_blocks_workspace_write_by_default(tmp_path):
    from agent.security.filesystem_sandbox import detect_sandbox_support

    tools, _reg, workspace, output_dir = make_builtin_tools_with_output_dir(tmp_path)
    (workspace / "a.txt").write_text("keep", encoding="utf-8")

    result = asyncio.run(
        tools._shell(f"touch {workspace}/new.txt", timeout=10)
    )

    assert not (workspace / "new.txt").exists()
    if detect_sandbox_support() is not None:
        # On an enforcing platform the sandbox denies the write inside the
        # child process; on unsupported platforms shell fails closed earlier.
        assert result["exit_code"] != 0
        assert not result["ok"] or "Operation not permitted" in result["output"]


@pytest.mark.skipif(
    not hasattr(os, "fork"),
    reason="requires POSIX process groups",
)
def test_shell_allows_output_dir_writes(tmp_path):
    from agent.security.filesystem_sandbox import detect_sandbox_support

    if detect_sandbox_support() is None:
        pytest.skip("requires an enforcing filesystem sandbox")
    tools, _reg, workspace, output_dir = make_builtin_tools_with_output_dir(tmp_path)

    result = asyncio.run(
        tools._shell(f"touch {output_dir}/made.txt", timeout=10)
    )

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert (output_dir / "made.txt").exists()


def test_shell_returns_confirmation_request_for_restricted_command(tmp_path):
    tools, _, _ = make_builtin_tools(tmp_path)

    result = asyncio.run(tools._shell("mkfs /dev/disk0", timeout=1))

    assert result["ok"] is False
    assert result["requires_confirmation"] is True
    assert result["risk_level"] == "high"
    assert result["confirmation_token"]
    assert "requires confirmation" in result["error"].lower()


def test_shell_tool_schema_does_not_expose_confirmation_token(tmp_path):
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    shell = next(item for item in registry.to_anthropic_format() if item["name"] == "shell")

    assert "confirmation_token" not in shell["input_schema"]["properties"]
    root_schema = shell["input_schema"]["properties"]["root"]
    assert root_schema["enum"] == ["output_dir", "workspace"]
    assert root_schema["default"] == "workspace"


def test_shell_runs_restricted_command_after_user_scoped_confirmation(
    tmp_path, monkeypatch
):
    from agent.core.agent import AgentContext, _active_agent_context
    from agent.security.shell import ShellAuthorizationScope, shell_command_confirm

    tools, _, _ = make_builtin_tools(tmp_path)
    ctx = AgentContext(
        metadata={
            "session_id": "session-1",
            "channel_name": "feishu",
            "user_id": "user-1",
        }
    )
    active = _active_agent_context.set(ctx)
    try:
        first = asyncio.run(tools._shell("mkfs /dev/disk0", timeout=1))
    finally:
        _active_agent_context.reset(active)
    captured = {}

    class FakeProc:
        returncode = 0

        async def communicate(self, stdin=None):
            return (b"ok", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["argv"] = args
        return FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    assert shell_command_confirm(first["confirmation_token"], scope=scope) is True
    active = _active_agent_context.set(ctx)
    try:
        result = asyncio.run(tools._shell("mkfs /dev/disk0", timeout=1))
    finally:
        _active_agent_context.reset(active)

    assert result["ok"] is True
    assert captured["argv"][-3:] == ("/bin/sh", "-c", "mkfs /dev/disk0")
    assert captured["argv"][0].endswith("sandbox-exec")


def test_shell_rejects_model_supplied_confirmation_token(tmp_path):
    tools, _, _ = make_builtin_tools(tmp_path)

    with pytest.raises(TypeError, match="confirmation_token"):
        asyncio.run(
            tools._shell(
                "mkfs /dev/disk0",
                timeout=1,
                confirmation_token="model-controlled",
            )
        )


def test_shell_rejects_inline_cwd_escape(tmp_path):
    tools, _, _ = make_builtin_tools(tmp_path)

    result = asyncio.run(tools._shell("cd /tmp && echo ok", timeout=1))

    assert result["ok"] is False
    assert result["risk_level"] == "high"
    assert "cwd" in result["error"].lower()


def test_shell_allowed_commands_context_skips_confirmation(tmp_path, monkeypatch):
    tools, registry, _ = make_builtin_tools(tmp_path)
    registry.set_context("shell_allowed_commands", ["mkfs /dev/disk0"])
    spawned = []

    class FakeProc:
        returncode = 0

        async def communicate(self, stdin=None):
            return (b"ok", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        spawned.append(args)
        return FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    result = asyncio.run(tools._shell("mkfs /dev/disk0", timeout=1))

    assert result["ok"] is True
    assert spawned


def test_shell_permission_level_context_skips_confirmation(tmp_path, monkeypatch):
    tools, registry, _ = make_builtin_tools(tmp_path)
    registry.set_context("shell_permission_level", "medium")
    spawned = []

    class FakeProc:
        returncode = 0

        async def communicate(self, stdin=None):
            return (b"ok", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        spawned.append(args)
        return FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    result = asyncio.run(tools._shell("mkfs /dev/disk0", timeout=1))

    assert result["ok"] is True
    assert spawned


def _fake_proc_spawn(monkeypatch):
    spawned: list = []

    class FakeProc:
        returncode = 0

        async def communicate(self, stdin=None):
            return (b"ok", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        spawned.append(args)
        return FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    return spawned


def test_unsandboxed_full_access_skips_sandbox_exec(tmp_path, monkeypatch):
    tools, registry, _ = make_builtin_tools(tmp_path)
    registry.set_context("shell_permission_level", "full")
    registry.set_context("shell_sandbox_mode", "none")
    spawned = _fake_proc_spawn(monkeypatch)

    result = asyncio.run(tools._shell("mkfs /dev/disk0", timeout=1))

    assert result["ok"] is True
    assert spawned
    assert spawned[0][0] == "/bin/sh"
    assert "sandbox-exec" not in spawned[0][0]


def test_unsandboxed_without_full_level_falls_back_to_sandbox(
    tmp_path, monkeypatch
):
    tools, registry, _ = make_builtin_tools(tmp_path)
    registry.set_context("shell_permission_level", "ask")
    registry.set_context("shell_sandbox_mode", "none")
    spawned = _fake_proc_spawn(monkeypatch)

    result = asyncio.run(tools._shell("mv a b", timeout=1))

    assert result["ok"] is True
    assert spawned
    assert spawned[0][0].endswith("sandbox-exec")


def test_session_sandbox_override_unsandboxes(tmp_path, monkeypatch):
    from agent.core.agent import AgentContext, _active_agent_context
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_session_allowlist_clear,
        shell_session_sandbox_set,
    )

    tools, registry, _ = make_builtin_tools(tmp_path)
    registry.set_context("shell_permission_level", "full")
    scope = ShellAuthorizationScope("sandbox-override-test", "cli", "")
    shell_session_sandbox_set(scope, "none")
    spawned = _fake_proc_spawn(monkeypatch)
    ctx = AgentContext(
        metadata={
            "session_id": "sandbox-override-test",
            "channel_name": "cli",
            "user_id": "",
        }
    )
    active = _active_agent_context.set(ctx)
    try:
        result = asyncio.run(tools._shell("mkfs /dev/disk0", timeout=1))
    finally:
        _active_agent_context.reset(active)
        shell_session_allowlist_clear()

    assert result["ok"] is True
    assert spawned
    assert spawned[0][0] == "/bin/sh"


def test_shell_devices_defaults_open_and_can_be_disabled(tmp_path, monkeypatch):
    tools, registry, _ = make_builtin_tools(tmp_path)
    spawned = _fake_proc_spawn(monkeypatch)

    result = asyncio.run(tools._shell("mv a b", timeout=1))

    assert result["ok"] is True
    assert spawned
    profile_path = spawned[0][2]
    profile = Path(profile_path).read_text(encoding="utf-8")
    assert '(allow iokit-open)' in profile
    assert '(global-name "com.apple.Metal")' in profile

    registry.set_context("shell_devices", False)
    spawned.clear()
    result = asyncio.run(tools._shell("mv a b", timeout=1))
    assert result["ok"] is True
    assert spawned
    profile = Path(spawned[0][2]).read_text(encoding="utf-8")
    assert '(allow iokit-open)' not in profile


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
        "sudo mkfs /dev/disk0",
        "FOO=1 mkfs /dev/disk0",
        "env mkfs /dev/disk0",
        "shutdown now",
    ],
)
def test_shell_wrapped_high_risk_commands_require_confirmation(
    tmp_path, command
):
    tools, _, _ = make_builtin_tools(tmp_path)

    result = asyncio.run(tools._shell(command, timeout=1))

    assert result["ok"] is False
    assert result.get("requires_confirmation") is True


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


def test_web_proxy_config_overrides_the_environment(tmp_path, monkeypatch):
    """`web_proxy` decides: a URL pins a proxy, a word disables proxying.

    With no config value the environment decides, so a machine behind Clash
    starts working without any config edit.
    """
    tools, registry, _ = make_builtin_tools(tmp_path)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")

    # No config: defer to the environment.
    proxy, trust_env = tools._web_proxy_for("https://example.com/")
    assert (proxy, trust_env) == (None, True)

    # An explicit URL wins, and the environment is then not consulted.
    registry.set_context("web_proxy", "http://127.0.0.1:7897")
    proxy, trust_env = tools._web_proxy_for("https://example.com/")
    assert (proxy.host, proxy.port, trust_env) == ("127.0.0.1", 7897, False)

    # "none" forces direct connections even when the environment asks.
    registry.set_context("web_proxy", "none")
    assert tools._web_proxy_for("https://example.com/") == (None, False)

    # An unusable value is not silently treated as "no proxy": the environment
    # still applies, so a typo degrades to the previous behaviour.
    registry.set_context("web_proxy", "socks5://127.0.0.1:1080")
    proxy, trust_env = tools._web_proxy_for("https://example.com/")
    assert (proxy, trust_env) == (None, True)


def test_web_fetch_reports_download_progress(tmp_path, monkeypatch):
    from agent.core.output import EventCollector, _active_event_collector
    from agent.security.network import FetchResponse
    from agent.tools.executor import RegularToolExecutor

    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    def fake_fetch(url, **kwargs):
        kwargs["on_progress"](9, 18)
        kwargs["on_progress"](18, 18)
        return FetchResponse(
            body=b"<p>hello world</p>",
            final_url=url,
            status=200,
            headers={"Content-Length": "18"},
        )

    monkeypatch.setattr(
        "agent.tools.builtin_tools.fetch_public_http_url",
        fake_fetch,
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
                    "at": _soon(),
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
                "at": _soon(),
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
                "at": _soon(),
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

        async def communicate(self, stdin=None):
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
                "at": _soon(),
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


def test_set_identity_records_a_setting_the_prompt_reads_back(tmp_path):
    tools, registry, _workspace = make_builtin_tools(tmp_path)

    result = json.loads(
        asyncio.run(
            registry.call(
                "set_identity",
                {"name": "小八", "persona": "一位可爱的小女孩"},
            )
        )
    )

    assert result["ok"] is True
    assert result["applied"] == {"name": "小八", "identity_note": "一位可爱的小女孩"}
    resolved = {
        fact.predicate: fact.value
        for fact in tools.memory.store.read_resolved_facts(subject="assistant")
    }
    assert resolved["name"] == "小八"
    assert resolved["identity_note"] == "一位可爱的小女孩"


def test_set_identity_needs_something_to_set(tmp_path):
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    result = json.loads(asyncio.run(registry.call("set_identity", {})))

    assert result["ok"] is False
    assert "at least one" in result["error"]


def test_set_identity_clears_a_field_with_an_empty_string(tmp_path):
    tools, registry, _workspace = make_builtin_tools(tmp_path)

    asyncio.run(registry.call("set_identity", {"role": "编程助手"}))
    asyncio.run(registry.call("set_identity", {"role": ""}))

    assert (
        tools.memory.store.read_resolved_facts(subject="assistant", predicate="role")
        == []
    )


# ---------------------------------------------------------------------------
# The permission envelope of a task the *agent* created.
#
# A scheduled run has nobody attached to approve anything, so the envelope it
# executes inside is the whole of its authority.  The UI asks the person to
# choose one; the tool path was the way around that question -- it built the
# task with the dataclass defaults, so every agent-created task inherited the
# global config and was pinned to whatever directory the gateway process
# happened to be started from.  These tests pin the four answers that matter:
# the directory is the one the user opened, an unknown profile is refused
# rather than substituted, a write-granting profile refuses to guess, and the
# reply says which envelope was stored.
# ---------------------------------------------------------------------------


def _create_scheduled_task(registry, **overrides):
    payload = {
        "name": "automation",
        "trigger_type": "once",
        "prompt": "跑一下测试",
        "at": _soon(),
        "timezone_name": "Asia/Shanghai",
    }
    payload.update(overrides)
    return json.loads(asyncio.run(registry.call("schedule_create", payload)))


def _stored_task(payload):
    from agent.scheduler import SchedulerStore

    store = SchedulerStore(db_path=Path(payload["task"]["db_path"]))
    try:
        return store.get_task(payload["task"]["id"])
    finally:
        store.close()


def test_schedule_create_pins_the_task_to_the_chosen_workspace(tmp_path):
    _tools, registry, workspace = make_builtin_tools(tmp_path)
    chosen = tmp_path / "chosen-project"
    chosen.mkdir()
    registry.set_context("workspace_root", chosen)

    payload = _create_scheduled_task(registry)
    task = _stored_task(payload)

    assert payload["ok"] is True
    assert task.workspace_root == str(chosen.resolve())
    assert task.workspace_root != str(Path.cwd().resolve())
    # No profile asked for means the historical posture, not a widened one.
    assert task.permission_profile == "inherit"


def test_schedule_create_falls_back_to_the_workspace_it_was_built_with(tmp_path):
    """With no session context the constructor argument is the chosen one."""
    _tools, registry, workspace = make_builtin_tools(tmp_path)

    payload = _create_scheduled_task(registry)
    task = _stored_task(payload)

    assert payload["ok"] is True
    assert task.workspace_root == str(workspace.resolve())


def test_schedule_create_refuses_a_write_profile_with_no_chosen_directory(tmp_path):
    """The cwd fallback is exactly what a write-granting profile must not get.

    ``workspace_write`` runs shell commands without asking, so aiming it at
    the process working directory would be an unattended write task pointed
    at a place nobody can predict from its definition.
    """
    from agent import BuiltinTools, MemoryPalace, ToolRegistry

    registry = ToolRegistry()
    memory = MemoryPalace(base_dir=tmp_path / "memory", context_dir=tmp_path / "ctx")
    BuiltinTools(memory=memory, registry=registry)  # no workspace_root, no context

    payload = _create_scheduled_task(registry, permission_profile="workspace_write")

    assert payload["ok"] is False
    assert "需要显式指定项目文件夹" in payload["error"]


def test_schedule_create_stores_a_write_profile_when_a_directory_is_given(tmp_path):
    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    payload = _create_scheduled_task(
        registry,
        action_type="agent_task",
        instruction="跑一遍测试",
        permission_profile="workspace_write",
        workspace_root=str(project),
    )
    task = _stored_task(payload)

    assert payload["ok"] is True
    assert task.permission_profile == "workspace_write"
    assert task.workspace_root == str(project.resolve())
    assert task.kind == "agent_prompt"


def test_schedule_create_refuses_an_unknown_profile_and_names_the_valid_ones(tmp_path):
    """A typo must not become a silent substitution.

    Resolving it to ``read_only`` would be safe but mute: the task would exist
    under an envelope nobody chose and the caller would never learn its name
    was wrong.  Listing the valid keys is what lets it retry correctly.
    """
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    payload = _create_scheduled_task(registry, permission_profile="workspac_write")

    assert payload["ok"] is False
    assert "workspac_write" in payload["error"]
    for key in ("inherit", "read_only", "workspace_write"):
        assert key in payload["error"]


def test_schedule_create_refuses_a_workspace_that_does_not_exist(tmp_path):
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    payload = _create_scheduled_task(
        registry,
        action_type="agent_task",
        instruction="跑一遍测试",
        workspace_root=str(tmp_path / "does-not-exist"),
    )

    assert payload["ok"] is False
    assert "项目文件夹不存在" in payload["error"]


def test_schedule_create_reports_the_envelope_it_stored(tmp_path):
    _tools, registry, workspace = make_builtin_tools(tmp_path)

    payload = _create_scheduled_task(registry, permission_profile="read_only")

    assert payload["task"]["permission_profile"] == "read_only"
    assert payload["task"]["workspace_root"] == str(workspace.resolve())
    assert "强制只读" in payload["summary_text"]


def test_two_envelopes_are_two_tasks(tmp_path):
    """Dedup identity includes the envelope.

    Re-asking for the same task with a wider profile must create a task with
    *that* profile, not hand back the earlier read-only one and quietly ignore
    the request.
    """
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    first = _create_scheduled_task(registry, permission_profile="read_only")
    second = _create_scheduled_task(registry, permission_profile="inherit")

    assert first["task"]["existing"] is False
    assert second["task"]["existing"] is False
    assert first["task"]["id"] != second["task"]["id"]


def test_schedule_create_accepts_a_signal_trigger(tmp_path):
    """A subscription is a trigger like any other, and stored as one."""
    from agent.scheduler import task_signal_name

    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    emitter = _create_scheduled_task(registry, name="emitter", prompt="先跑这个")
    signal = task_signal_name(emitter["task"]["id"], "succeeded")

    created = _create_scheduled_task(
        registry,
        name="follower",
        trigger_type="signal",
        signal_name=signal,
        prompt="再跑这个",
    )

    assert created["task"]["existing"] is False
    task = _stored_task(created)
    assert task.trigger.trigger_type == "signal"
    assert task.trigger.payload["name"] == signal
    # No calendar, so nothing for the clock to claim and nothing to fall
    # behind on.
    assert task.next_run_at is None
    assert "收到信号" in created["summary_text"]


def test_schedule_create_refuses_a_signal_for_a_task_that_does_not_exist(tmp_path):
    """A mistyped id is a task that never runs, so it is worth catching now."""
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    payload = _create_scheduled_task(
        registry,
        name="follower",
        trigger_type="signal",
        signal_name="task:no-such-task:succeeded",
    )

    assert "error" in payload
    assert "no-such-task" in json.dumps(payload, ensure_ascii=False)


def test_schedule_create_refuses_a_status_a_run_cannot_reach(tmp_path):
    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    emitter = _create_scheduled_task(registry, name="emitter", prompt="先跑这个")

    payload = _create_scheduled_task(
        registry,
        name="follower",
        trigger_type="signal",
        signal_name=f"task:{emitter['task']['id']}:exploded",
    )

    assert "error" in payload
    assert "exploded" in json.dumps(payload, ensure_ascii=False)


def test_schedule_create_allows_a_signal_nobody_has_emitted_yet(tmp_path):
    """Otherwise the two halves would have to be created in a fixed order."""
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    payload = _create_scheduled_task(
        registry,
        name="follower",
        trigger_type="signal",
        signal_name="report.ready",
        prompt="等报告就绪",
    )

    assert payload["task"]["existing"] is False
    assert _stored_task(payload).trigger.payload["name"] == "report.ready"


def test_schedule_create_requires_a_signal_name(tmp_path):
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    payload = _create_scheduled_task(registry, name="follower", trigger_type="signal")

    assert "error" in payload


# ─── The words that asked, checked by the executor ──────────────────────────
#
# ``registry.call`` goes straight to the tool, so these drive the executor the
# way the agent loop does -- which is the only path where the guard lives.


def _run_through_executor(registry, name, inputs):
    from agent.tools.executor import RegularToolExecutor

    return json.loads(
        asyncio.run(
            RegularToolExecutor(registry).run({"name": name, "input": inputs})
        )
    )


def test_a_question_cannot_leave_a_task_behind(tmp_path):
    """The bug this was written for, reproduced and refused.

    A turn that asked how a process works offers no sentence that asks for a
    task, so the call cannot quote its way past the guard and nothing is
    written.  This is the whole point: no stray schedule outliving the turn.
    """
    from agent.scheduler import SchedulerStore

    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    registry.set_context("turn_request", "订单都是如何接的，具体流程是什么")

    payload = _run_through_executor(
        registry,
        "schedule_create",
        {
            "name": "订单流程",
            "trigger_type": "once",
            "at": _soon(),
            "message_text": "去核对一遍订单流程",
            "intent": "用户想了解订单流程，所以建个任务",
        },
    )

    assert payload["ok"] is False
    assert "does not quote this turn's request" in payload["error"]
    store = SchedulerStore()
    try:
        assert store.list_tasks() == []
    finally:
        store.close()


def test_the_words_that_asked_are_kept_on_the_task(tmp_path):
    """A task created from a request records that request on its own row."""
    from agent.scheduler import SchedulerStore
    import agent.shared as shared_module

    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    registry.set_context("turn_request", "每天早上九点提醒我看盘，谢谢")

    payload = _run_through_executor(
        registry,
        "schedule_create",
        {
            "name": "看盘",
            "trigger_type": "daily",
            "time_of_day": "09:00",
            "message_text": "看盘",
            "intent": "每天早上九点提醒我看盘",
        },
    )

    assert payload["ok"] is True
    store = SchedulerStore(db_path=Path(shared_module.SCHEDULER_DB_FILE))
    try:
        task = store.get_task(payload["task"]["id"])
        assert task is not None
        assert task.request_quote == "每天早上九点提醒我看盘"
    finally:
        store.close()


def test_an_emission_nobody_asked_for_is_refused(tmp_path):
    """Emitting wakes subscribers, so it is an action and needs an ask too."""
    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    registry.set_context("turn_request", "report.ready 这个信号是干什么用的")

    refused = _run_through_executor(
        registry,
        "emit_signal",
        {"name": "report.ready", "intent": "用户问了这个信号，顺便发一下"},
    )

    assert refused["ok"] is False
    assert "does not quote this turn's request" in refused["error"]

    registry.set_context("turn_request", "把 report.ready 这个信号发出去")
    allowed = _run_through_executor(
        registry,
        "emit_signal",
        {"name": "report.ready", "intent": "把 report.ready 这个信号发出去"},
    )

    assert allowed["ok"] is True
    assert allowed["name"] == "report.ready"


def test_emit_signal_records_an_emission_and_names_its_subscribers(tmp_path):
    from agent.scheduler import SchedulerStore
    import agent.shared as shared_module

    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    _create_scheduled_task(
        registry,
        name="follower",
        trigger_type="signal",
        signal_name="report.ready",
        prompt="等报告就绪",
    )

    payload = json.loads(
        asyncio.run(registry.call("emit_signal", {"name": "report.ready", "payload": {"rows": 2}}))
    )

    assert payload["name"] == "report.ready"
    assert payload["subscribers"] == ["follower"]
    assert "1 个任务" in payload["summary_text"]
    store = SchedulerStore(db_path=Path(shared_module.SCHEDULER_DB_FILE))
    try:
        emissions = store.list_emissions(name="report.ready")
        assert len(emissions) == 1
        assert emissions[0].state == "pending"
        assert emissions[0].payload == {"rows": 2}
        # Recorded, not delivered: emitting and running are separate steps so
        # that an emission survives a process that stops right after it.
        assert store.list_runs(payload and store.list_tasks()[0].id) == []
    finally:
        store.close()


def test_emit_signal_says_so_when_nobody_is_waiting(tmp_path):
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    payload = json.loads(asyncio.run(registry.call("emit_signal", {"name": "nobody.listens"})))

    assert payload["subscribers"] == []
    assert "没有任务订阅" in payload["summary_text"]


def test_list_signals_reports_what_has_been_emitted_and_who_is_waiting(tmp_path):
    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    _create_scheduled_task(
        registry,
        name="follower",
        trigger_type="signal",
        signal_name="report.ready",
        prompt="等报告就绪",
    )
    asyncio.run(registry.call("emit_signal", {"name": "report.ready"}))

    payload = json.loads(asyncio.run(registry.call("list_signals", {})))

    emitted = {item["name"]: item for item in payload["items"]}
    assert emitted["report.ready"]["subscriber_count"] == 1
    assert emitted["report.ready"]["emission_count"] == 1


def test_list_signals_shows_a_subscription_nobody_has_emitted_yet(tmp_path):
    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    _create_scheduled_task(
        registry,
        name="follower",
        trigger_type="signal",
        signal_name="never.emitted",
        prompt="等那个信号",
    )

    payload = json.loads(asyncio.run(registry.call("list_signals", {})))

    assert payload["items"] == []
    assert payload["waiting_on_unemitted"] == [
        {"name": "never.emitted", "subscriber_count": 1}
    ]


# --- Reading back what a scheduled run did -----------------------------------
#
# ``schedule_create`` and ``workflow_create`` are how a chain gets built, and
# an unattended run happens with nobody watching: the only record of where it
# ended up is the run history.  A tool that creates work but cannot observe it
# leaves the agent building chains it can never find out about -- it cannot
# tell a task that has been succeeding every night from one that has failed
# every night since the day it was made, and it cannot say why a step stopped.
# These tests pin the answering half of that pair.


def _record_run(
    db_path,
    task_id,
    *,
    status="failed",
    summary="",
    error="",
    output_path="",
    verdict="",
    verification=None,
    products=None,
    at=None,
):
    """Drive one run through the store's real claim/finish path."""
    from datetime import datetime, timedelta, timezone

    from agent.scheduler import SchedulerStore

    now = at or datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    store = SchedulerStore(db_path=Path(db_path))
    try:
        claimed = store.claim_task_now(task_id, now=now)
        assert claimed is not None
        finished = store.complete_run(
            task_id,
            claimed.run.id,
            finished_at=now + timedelta(minutes=1),
            status=status,
            summary=summary,
            error=error,
            output_path=output_path,
            verdict=verdict,
            verification=verification,
            products=products,
        )
        assert finished is True
        return claimed.run.id
    finally:
        store.close()


def _create_workflow(registry, steps, **overrides):
    payload = {"name": "chain", "steps": steps, "intent": "建一条链"}
    payload.update(overrides)
    return json.loads(asyncio.run(registry.call("workflow_create", payload)))


def test_schedule_list_carries_how_the_last_run_ended(tmp_path):
    """A task that has never run and a task that failed are not the same answer.

    Both read as "the task exists" from the task row alone, and that is the
    one thing the caller already knew.
    """
    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    never = _create_scheduled_task(registry, name="never-ran")
    broken = _create_scheduled_task(registry, name="broken")
    _record_run(
        broken["task"]["db_path"], broken["task"]["id"], status="failed", error="炸了"
    )

    payload = json.loads(asyncio.run(registry.call("schedule_list", {})))
    items = {item["name"]: item for item in payload["items"]}

    assert items["never-ran"]["last_run"] is None
    assert items["broken"]["last_run"]["status"] == "failed"


def test_schedule_list_says_which_tasks_are_workflow_steps(tmp_path):
    """From the task table a step and a standalone task look alike.

    The difference is what says who owns the definition -- and a step left
    behind by a deleted workflow has to still be recognisable as one.
    """
    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    standalone = _create_scheduled_task(registry, name="standalone")
    created = _create_workflow(
        registry,
        [{"key": "collect", "name": "收集", "trigger_type": "once",
          "at": _soon(), "timezone_name": "Asia/Shanghai",
          "instruction": "收集数据"}],
    )
    workflow_id = created["workflow"]["id"]
    step_task_id = created["workflow"]["steps"][0]["task_id"]

    payload = json.loads(asyncio.run(registry.call("schedule_list", {})))
    items = {item["id"]: item for item in payload["items"]}

    assert items[standalone["task"]["id"]]["workflow_id"] == ""
    assert items[standalone["task"]["id"]]["step_key"] == ""
    assert items[step_task_id]["workflow_id"] == workflow_id
    assert items[step_task_id]["step_key"] == "collect"


def test_schedule_runs_reports_the_reason_a_run_failed(tmp_path):
    """The tool's whole job: say how it ended and, when it went wrong, why."""
    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    created = _create_scheduled_task(registry, name="nightly")
    _record_run(
        created["task"]["db_path"],
        created["task"]["id"],
        status="failed",
        verdict="failed",
        error="第 3 行：找不到文件 report.md",
        summary="写了一半",
    )

    payload = json.loads(
        asyncio.run(
            registry.call("schedule_runs", {"task_id": created["task"]["id"]})
        )
    )

    assert payload["ok"] is True
    assert payload["run_count"] == 1
    run = payload["runs"][0]
    assert run["status"] == "failed"
    assert run["verdict"] == "failed"
    assert "找不到文件 report.md" in run["error"]
    assert run["summary"] == "写了一半"


def test_schedule_runs_carries_the_checks_own_result(tmp_path):
    """A verify command's exit code and stderr are the evidence, not a story."""
    from agent.verification import VerificationResult, VerificationStatus

    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    created = _create_scheduled_task(registry, name="verified")
    _record_run(
        created["task"]["db_path"],
        created["task"]["id"],
        status="failed",
        verdict="failed",
        verification=VerificationResult(
            status=VerificationStatus.FAILED,
            exit_code=1,
            stderr_tail="grep: report.md: No such file or directory",
        ),
    )

    payload = json.loads(
        asyncio.run(
            registry.call("schedule_runs", {"task_id": created["task"]["id"]})
        )
    )

    verification = payload["runs"][0]["verification"]
    assert verification["status"] == "failed"
    assert verification["exit_code"] == 1
    assert "No such file or directory" in verification["stderr_tail"]


def test_schedule_runs_leaves_a_check_that_never_ran_as_no_check(tmp_path):
    """None rather than an empty object: "no check" is itself the answer."""
    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    created = _create_scheduled_task(registry, name="unchecked")
    _record_run(created["task"]["db_path"], created["task"]["id"], status="succeeded")

    payload = json.loads(
        asyncio.run(
            registry.call("schedule_runs", {"task_id": created["task"]["id"]})
        )
    )

    assert payload["runs"][0]["verification"] is None


def test_schedule_runs_hands_back_the_acceptance_it_was_judged_against(tmp_path):
    """Echoed because it is usually the answer.

    The commonest way a run fails is that the criterion it was given cannot be
    satisfied -- a verify command naming a file the step never writes -- which
    is a defect in the definition, fixable by editing the task and not by
    retrying it.  Reading the reason without the bar it was measured against
    leaves the caller unable to tell the two apart.
    """
    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    created = _create_scheduled_task(
        registry,
        name="judged",
        criteria=["报告里要有结论一章"],
        verify_command="grep -q 结论 report.md",
    )

    payload = json.loads(
        asyncio.run(
            registry.call("schedule_runs", {"task_id": created["task"]["id"]})
        )
    )

    # Flat, under the names ``schedule_update`` takes, and that is the point:
    # the reply has to be sendable straight back, and the writer reads
    # ``criteria`` and ``verify_command`` rather than a nested object.  A
    # definition that can be read but not written back is a description.
    assert payload["task"]["criteria"] == ["报告里要有结论一章"]
    assert payload["task"]["verify_command"] == "grep -q 结论 report.md"


def test_schedule_runs_says_whether_the_output_file_is_still_there(tmp_path):
    """The path is a promise; ``output_available`` is whether it still holds.

    A tool that only names the file sends the caller to a path that may not
    exist, and "the file is gone" reads as "I could not read it".
    """
    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    created = _create_scheduled_task(registry, name="produced")
    kept = tmp_path / "kept.md"
    kept.write_text("内容", encoding="utf-8")

    payload_db = created["task"]["db_path"]
    task_id = created["task"]["id"]
    _record_run(payload_db, task_id, status="succeeded", output_path=str(kept))
    _record_run(
        payload_db, task_id, status="succeeded", output_path=str(tmp_path / "gone.md")
    )

    payload = json.loads(
        asyncio.run(registry.call("schedule_runs", {"task_id": task_id}))
    )
    by_path = {run["output_path"]: run for run in payload["runs"]}

    assert by_path[str(kept)]["output_available"] is True
    assert by_path[str(tmp_path / "gone.md")]["output_available"] is False


def test_schedule_runs_clips_a_reason_that_would_flood_the_window(tmp_path):
    """A truncated reason must say it was truncated.

    A reason that ends mid-sentence reads as the whole reason, and the caller
    stops looking -- with the rest of it sitting on disk unread.
    """
    from agent.tools.builtin_tools import RUN_REASON_CHARS

    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    created = _create_scheduled_task(registry, name="chatty")
    long_error = "啊" * (RUN_REASON_CHARS + 500)
    _record_run(
        created["task"]["db_path"], created["task"]["id"], error=long_error
    )

    payload = json.loads(
        asyncio.run(
            registry.call("schedule_runs", {"task_id": created["task"]["id"]})
        )
    )
    error = payload["runs"][0]["error"]

    assert error.startswith("啊" * RUN_REASON_CHARS)
    assert "截断" in error
    assert str(len(long_error)) in error


def test_schedule_runs_keeps_the_newest_runs_and_says_what_it_left_out(tmp_path):
    """A capped list has to admit it is capped, or it reads as the whole story."""
    from datetime import datetime, timedelta, timezone

    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    created = _create_scheduled_task(registry, name="busy")
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    for index in range(3):
        _record_run(
            created["task"]["db_path"],
            created["task"]["id"],
            status="succeeded",
            summary=f"第 {index} 次",
            at=base + timedelta(hours=index),
        )

    payload = json.loads(
        asyncio.run(
            registry.call(
                "schedule_runs", {"task_id": created["task"]["id"], "limit": 2}
            )
        )
    )

    assert payload["run_count"] == 3
    assert payload["returned"] == 2
    assert payload["runs"][0]["summary"] == "第 2 次"
    assert "最近 2 次" in payload["note"]


def test_schedule_runs_refuses_an_id_that_is_not_a_task_and_says_where_to_look(tmp_path):
    """An id from the wrong place is the likeliest way to call this wrong."""
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    payload = json.loads(
        asyncio.run(registry.call("schedule_runs", {"task_id": "nope"}))
    )

    assert payload["ok"] is False
    assert "schedule_list" in payload["error"]


def test_schedule_runs_needs_a_task_id(tmp_path):
    """The id is the addressing scheme; without one there is nothing to read."""
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    payload = json.loads(asyncio.run(registry.call("schedule_runs", {"task_id": ""})))

    assert payload["ok"] is False
    assert "task_id" in payload["error"]


def test_workflow_list_names_the_task_behind_each_step_and_how_it_ended(tmp_path):
    """The graph alone is a drawing.

    Which step is where it is supposed to be -- never run, failed, waiting on
    an upstream -- is invisible in the shape of the graph, and it is the whole
    answer to "is this chain working".  ``task_id`` travels with each step
    because the run history is keyed by it: without it, "why did step three
    stop" is a question with no way to ask it.
    """
    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    created = _create_workflow(
        registry,
        [
            {"key": "collect", "name": "收集", "trigger_type": "once",
             "at": _soon(), "timezone_name": "Asia/Shanghai",
             "instruction": "收集"},
            {"key": "publish", "name": "发布", "depends_on": ["collect"],
             "instruction": "发布"},
        ],
    )
    workflow = created["workflow"]
    step_tasks = {step["key"]: step["task_id"] for step in workflow["steps"]}
    _record_run(
        workflow["db_path"], step_tasks["collect"], status="failed", error="上游炸了"
    )
    # A second chain, made but never run, so that all three states are in one
    # answer: failed, skipped, and not-yet.
    idle = _create_workflow(
        registry,
        [{"key": "alone", "name": "没人跑过", "trigger_type": "once",
          "at": _soon(hours=3), "timezone_name": "Asia/Shanghai",
          "instruction": "独自跑"}],
        name="idle",
    )["workflow"]

    payload = json.loads(asyncio.run(registry.call("workflow_list", {})))
    item = next(row for row in payload["items"] if row["id"] == workflow["id"])
    steps = {step["key"]: step for step in item["steps"]}

    assert steps["collect"]["task_id"] == step_tasks["collect"]
    assert steps["collect"]["last_run"]["status"] == "failed"
    # And the step below it, which is the part the graph alone cannot show:
    # a failed step settles its descendants in the same transaction, so they
    # read as skipped -- not as "waiting its turn", which is what a step with
    # no run at all looks like and is the wrong thing to go and debug.
    assert steps["publish"]["last_run"]["status"] == "skipped"
    idle_item = next(row for row in payload["items"] if row["id"] == idle["id"])
    assert idle_item["steps"][0]["last_run"] is None


def test_schedule_runs_is_registered_as_a_read_tool(tmp_path):
    """Reading history is not an action, so it must not need a turn request.

    Classified as an action it would be refused in exactly the turn where the
    agent has just found out something broke and needs to look -- which is the
    turn this tool exists for.
    """
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    assert registry.tool_capabilities("schedule_runs") == frozenset({"read"})


def test_a_workflow_step_written_with_an_instruction_is_an_agent_task(tmp_path):
    """The obvious spelling of a step, versus the whole chain being refused.

    Unlike ``schedule_create`` a step has no ``prompt`` to fall back on, so a
    caller who filled in ``instruction`` -- the field the schema documents for
    agent steps -- and left ``action_type`` to its default was told that a
    field it had never used was missing, and the chain was never built at all.
    The content field names the action; the default only decides the case
    where nothing was supplied.
    """
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    created = _create_workflow(
        registry,
        [{"key": "collect", "name": "收集", "trigger_type": "once",
          "at": _soon(), "timezone_name": "Asia/Shanghai",
          "instruction": "收集数据"}],
    )

    assert created["ok"] is True
    assert created["workflow"]["steps"][0]["kind"] == "agent_prompt"


def test_a_step_that_gave_a_literal_message_is_still_a_message(tmp_path):
    """Inference reads the field that was supplied, not the one that was not."""
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    created = _create_workflow(
        registry,
        [{"key": "ping", "name": "提醒", "trigger_type": "once",
          "at": _soon(), "timezone_name": "Asia/Shanghai",
          "message_text": "该开会了"}],
    )

    assert created["ok"] is True
    assert created["workflow"]["steps"][0]["kind"] == "message"

# ---------------------------------------------------------------------------
# The other half of the contract: what a task promises to leave behind.
#
# A task could say what *done* meant but not what *work product* it owed, so
# the path a run had to write existed only in prose -- or, worse, only inside
# the ``verify_command`` that checked a file the run had never been told
# about.  ``produces`` is the declaration; these tests pin the four places a
# caller meets it: creation echoes it, the lists carry it, the run history
# says whether it was met, and a path outside the workspace is refused at the
# moment it is written rather than at 3am.
# ---------------------------------------------------------------------------


def test_schedule_create_stores_the_declared_products_and_says_them_out_loud(tmp_path):
    """Echoed for the same reason the acceptance envelope is: the list is what
    the run will be *told* to write, so a declaration the caller cannot see is
    a run writing files somewhere nobody asked for."""
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    payload = _create_scheduled_task(registry, produces=["out/report.md", "notes.md"])
    task = _stored_task(payload)

    assert payload["task"]["produces"] == ["out/report.md", "notes.md"]
    assert task.produces == ["out/report.md", "notes.md"]
    assert "out/report.md" in payload["summary_text"]
    # And the *stored* list, not the raw argument -- so a caller that passed a
    # path twice sees one entry, which is what the run will be held to.
    again = _create_scheduled_task(registry, name="other", produces=["a.md", " a.md "])
    assert again["task"]["produces"] == ["a.md"]


def test_schedule_create_refuses_a_product_outside_the_workspace(tmp_path):
    """Refused while somebody is looking at it, not discovered at 3am."""
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    for bad in ("/etc/passwd", "../elsewhere.md", "out/"):
        payload = _create_scheduled_task(registry, name=f"bad-{len(bad)}", produces=[bad])
        assert payload["ok"] is False, bad
        assert "产物" in payload["error"], bad


def test_schedule_create_without_products_does_not_grow_an_empty_section(tmp_path):
    """The majority case must read exactly as it did before this existed."""
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    payload = _create_scheduled_task(registry)

    assert payload["task"]["produces"] == []
    assert "产出" not in payload["summary_text"]


def test_schedule_list_carries_what_each_task_promises_to_produce(tmp_path):
    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    _create_scheduled_task(registry, name="makes-a-file", produces=["out/report.md"])
    _create_scheduled_task(registry, name="makes-nothing")

    payload = json.loads(asyncio.run(registry.call("schedule_list", {})))
    items = {item["name"]: item for item in payload["items"]}

    assert items["makes-a-file"]["produces"] == ["out/report.md"]
    assert items["makes-nothing"]["produces"] == []


def test_schedule_runs_says_which_declared_products_were_actually_produced(tmp_path):
    """The declaration and the measurement, both, because neither answers the
    question alone: an empty list with an empty declaration means the task
    never promised anything, and an empty list with a declaration means the
    work did not produce it -- and "declared nothing" must not be able to read
    as "produced nothing"."""
    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    created = _create_scheduled_task(registry, name="nightly", produces=["out/report.md"])
    _record_run(
        created["task"]["db_path"],
        created["task"]["id"],
        status="failed",
        verdict="failed",
        error="声明的产物没有产出：out/report.md",
        products=[{"path": "out/report.md", "absolute": "/w/out/report.md", "exists": False, "bytes": 0}],
    )

    payload = json.loads(
        asyncio.run(registry.call("schedule_runs", {"task_id": created["task"]["id"]}))
    )

    # The task block carries the promise...
    assert payload["task"]["produces"] == ["out/report.md"]
    # ...and the run carries what was measured against it.  A promised path
    # with no file behind it is *missing*, not an entry in ``products``: one
    # question, one answer, in the same shape the step below is handed.
    run = payload["runs"][0]
    assert run["products"] == []
    assert run["products_missing"] == ["out/report.md"]


def test_schedule_runs_reports_a_produced_file_with_its_address_and_size(tmp_path):
    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    created = _create_scheduled_task(registry, name="nightly", produces=["out/report.md"])
    _record_run(
        created["task"]["db_path"],
        created["task"]["id"],
        status="succeeded",
        verdict="passed",
        products=[
            {
                "path": "out/report.md",
                "absolute": "/w/out/report.md",
                "exists": True,
                "bytes": 64,
            }
        ],
    )

    payload = json.loads(
        asyncio.run(registry.call("schedule_runs", {"task_id": created["task"]["id"]}))
    )

    assert payload["runs"][0]["products"] == [
        {"path": "out/report.md", "absolute": "/w/out/report.md", "bytes": 64}
    ]
    assert payload["runs"][0]["products_missing"] == []


def test_schedule_runs_separates_declaring_nothing_from_producing_nothing(tmp_path):
    """Two runs, one task that promised a file and did not write it, one that
    never promised anything.  Both have an empty product list; only one has a
    missing entry, and that is the difference a reader acts on."""
    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    promised = _create_scheduled_task(registry, name="promised", produces=["a.md"])
    silent = _create_scheduled_task(registry, name="silent")
    for created in (promised, silent):
        _record_run(created["task"]["db_path"], created["task"]["id"], status="succeeded")

    def runs_of(created):
        return json.loads(
            asyncio.run(registry.call("schedule_runs", {"task_id": created["task"]["id"]}))
        )

    promised_run = runs_of(promised)["runs"][0]
    silent_run = runs_of(silent)["runs"][0]

    assert promised_run["products"] == [] and silent_run["products"] == []
    assert promised_run["products_missing"] == ["a.md"]
    assert silent_run["products_missing"] == []


def test_a_step_declares_its_products_and_the_chain_says_so(tmp_path):
    """A step's products are the same declaration as a task's, because a step
    *is* a task -- a second dialect of the same language is how two callers
    come to disagree about what "done" means."""
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    created = _create_workflow(
        registry,
        [
            {"key": "collect", "name": "收集", "trigger_type": "once",
             "at": _soon(), "timezone_name": "Asia/Shanghai",
             "instruction": "收集数据", "produces": ["raw.json"]},
            {"key": "analyze", "name": "分析", "depends_on": ["collect"],
             "instruction": "分析", "criteria": ["the summary exists"],
             "produces": ["summary.md"]},
        ],
    )

    assert created["ok"] is True
    steps = {step["key"]: step for step in created["workflow"]["steps"]}
    assert steps["collect"]["produces"] == ["raw.json"]
    assert steps["analyze"]["produces"] == ["summary.md"]
    # Named per step, because a chain has many and "a product is missing" does
    # not say which step to go and look at.
    assert "产出文件：raw.json" in created["summary_text"]
    assert "产出文件：summary.md" in created["summary_text"]


def test_workflow_list_carries_each_step_s_products(tmp_path):
    _tools, registry, _workspace = make_builtin_tools(tmp_path)
    _create_workflow(
        registry,
        [
            {"key": "collect", "name": "收集", "trigger_type": "once",
             "at": _soon(), "timezone_name": "Asia/Shanghai",
             "instruction": "收集数据", "produces": ["raw.json"]},
        ],
    )

    payload = json.loads(asyncio.run(registry.call("workflow_list", {})))
    step = payload["items"][0]["steps"][0]

    assert step["produces"] == ["raw.json"]


def test_workflow_create_refuses_a_step_product_outside_the_workspace(tmp_path):
    _tools, registry, _workspace = make_builtin_tools(tmp_path)

    created = _create_workflow(
        registry,
        [
            {"key": "collect", "name": "收集", "trigger_type": "once",
             "at": _soon(), "timezone_name": "Asia/Shanghai",
             "instruction": "收集数据", "produces": ["/etc/passwd"]},
        ],
    )

    assert created["ok"] is False
    assert "产物" in created["error"]
