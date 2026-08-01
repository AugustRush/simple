"""Interactive CLI consent gate and input-history tests."""

import asyncio
from io import StringIO

import pytest

from rich.console import Console


@pytest.fixture(autouse=True)
def _clear_shell_allowlist():
    from agent.security.shell import shell_session_allowlist_clear

    shell_session_allowlist_clear()
    yield
    shell_session_allowlist_clear()


def _make_tools(tmp_path):
    from agent import BuiltinTools, MemoryPalace, ToolRegistry

    registry = ToolRegistry()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "output"
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


class _FakeSink:
    """Duck-typed OutputSink recording confirmation asks."""

    def __init__(self, answer: bool = True):
        self.answer = answer
        self.asked: list[dict] = []

    async def on_tool_confirmation(
        self,
        name: str,
        *,
        command: str,
        risk_level: str,
        reason: str,
        confirmation_token: str,
        scope,
    ) -> bool:
        self.asked.append(
            {
                "name": name,
                "command": command,
                "risk_level": risk_level,
                "reason": reason,
                "token": confirmation_token,
                "scope": str(getattr(scope, "session_id", "")),
            }
        )
        return self.answer


def _run_with_context(active_sink, *, cancelled: bool = False, **kwargs):
    import agent.shared as shared
    from agent.core.agent import AgentContext, _active_agent_context
    from agent.core.output import _active_sink

    ctx = AgentContext(
        metadata={
            "session_id": "cli",
            "channel_name": "cli",
            "user_id": "",
        }
    )
    agent_token = _active_agent_context.set(ctx)
    sink_token = _active_sink.set(active_sink)
    cancel_token = shared.CancelToken()
    if cancelled:
        cancel_token.cancel()
    cancel_var_token = shared._active_cancel_token.set(cancel_token)
    try:
        return asyncio.run(kwargs["tools"]._shell(kwargs["command"], timeout=1))
    finally:
        shared._active_cancel_token.reset(cancel_var_token)
        _active_sink.reset(sink_token)
        _active_agent_context.reset(agent_token)


def test_shell_confirmation_gate_executes_after_human_approval(
    tmp_path, monkeypatch
):
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_session_allowlist_contains,
    )

    tools, _, _, _ = _make_tools(tmp_path)
    sink = _FakeSink(answer=True)
    spawned = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"ok", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        spawned["argv"] = args
        return FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    result = _run_with_context(sink, tools=tools, command="mkfs /dev/disk0")

    assert result["ok"] is True
    assert spawned["argv"][-3:] == ("/bin/sh", "-c", "mkfs /dev/disk0")
    assert len(sink.asked) == 1
    ask = sink.asked[0]
    assert ask["command"] == "mkfs /dev/disk0"
    assert ask["risk_level"] == "high"
    assert ask["name"] == "shell"
    assert ask["token"]
    scope = ShellAuthorizationScope("cli", "cli", "")
    assert shell_session_allowlist_contains("mkfs /dev/disk0", scope=scope) is True


def test_shell_confirmation_gate_decline_keeps_structured_error(
    tmp_path, monkeypatch
):
    tools, _, _, _ = _make_tools(tmp_path)
    sink = _FakeSink(answer=False)
    spawned = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        spawned["argv"] = args
        raise AssertionError("must not spawn after decline")

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    result = _run_with_context(sink, tools=tools, command="mkfs /dev/disk0")

    assert result["ok"] is False
    assert result["requires_confirmation"] is True
    assert result["confirmation_token"]
    assert spawned == {}
    assert len(sink.asked) == 1


def test_shell_confirmation_gate_declines_after_turn_cancelled(
    tmp_path, monkeypatch
):
    tools, _, _, _ = _make_tools(tmp_path)
    sink = _FakeSink(answer=True)
    spawned = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        spawned["argv"] = args
        raise AssertionError("must not spawn on a cancelled turn")

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    result = _run_with_context(
        sink, tools=tools, command="mkfs /dev/disk0", cancelled=True
    )

    assert result["ok"] is False
    assert result["requires_confirmation"] is True
    assert spawned == {}


def test_cli_output_sink_declines_without_terminal():
    from agent.core.output import CliOutputSink

    console = Console(file=StringIO())
    sink = CliOutputSink(console)

    approved = asyncio.run(
        sink.on_tool_confirmation(
            "shell",
            command="curl example.com",
            risk_level="medium",
            reason="network request or download",
            confirmation_token="tok",
            scope=None,
        )
    )

    assert approved is False


def test_cli_history_path_is_under_agent_home(monkeypatch, tmp_path):
    import agent.cli as cli

    monkeypatch.setattr(cli.shared, "AGENT_HOME", tmp_path)

    path = cli._cli_history_path()

    assert path == tmp_path / "cli_history"
    assert path.parent.is_dir()


def test_cli_prompt_session_persists_history(monkeypatch, tmp_path):
    import agent.cli as cli

    monkeypatch.setattr(cli.shared, "AGENT_HOME", tmp_path)
    cli._cli_prompt_session = None
    try:
        session = cli._cli_prompt()
        session.history.append_string("第一条输入")
        session.history.append_string("第二条输入")

        loaded = list(session.history.load_history_strings())
        assert set(loaded) == {"第一条输入", "第二条输入"}

        persisted = (tmp_path / "cli_history").read_text(encoding="utf-8")
        assert "第一条输入" in persisted
        assert "第二条输入" in persisted
    finally:
        cli._cli_prompt_session = None


def _tty_sink():
    from agent.core.output import CliOutputSink

    console = Console(file=StringIO(), force_terminal=True)
    return CliOutputSink(console), console


def test_render_markdown_line_styles_inline_tokens():
    from agent.core.output import _render_markdown_line

    line = _render_markdown_line("## 标题 with **bold** and `code`")

    assert line.plain == "## 标题 with bold and code"
    styles = " ".join(str(span.style) for span in line.spans)
    assert "bold" in styles
    assert "cyan" in styles

    bullet = _render_markdown_line("- 项目符号")
    assert bullet.plain == "- 项目符号"

    quote = _render_markdown_line("> 引用")
    assert "italic" in " ".join(str(span.style) for span in quote.spans)


def test_stream_markdown_buffers_code_fence():
    sink, console = _tty_sink()

    sink.on_stream_chunk("```python\nprint(1)\n```\n")

    out = console.file.getvalue()
    assert "print(1)" in out
    assert "```" not in out


def test_stream_markdown_flushes_truncated_fence_on_turn_end():
    sink, console = _tty_sink()

    sink.on_stream_chunk("```python\nprint(1)\n")
    sink.on_turn_complete("", [])

    assert "print(1)" in console.file.getvalue()
    assert sink._stream_md is None


def test_stream_chunk_renders_completed_lines_in_terminal():
    sink, console = _tty_sink()

    sink.on_stream_chunk("第一行\n第二行")

    out = console.file.getvalue()
    assert "第一行" in out
    assert "第二行" not in out  # partial line stays buffered until newline
    sink.on_turn_complete("", [])
    assert "第二行" in console.file.getvalue()


def test_stream_chunk_stays_raw_without_terminal():
    from agent.core.output import CliOutputSink

    console = Console(file=StringIO())
    sink = CliOutputSink(console)

    sink.on_stream_chunk("hello **world**")

    assert console.file.getvalue() == "hello **world**"
    assert sink._stream_md is None


def test_tool_progress_renders_bar_in_terminal():
    sink, console = _tty_sink()

    sink.on_tool_start("shell", {"command": "long run"})
    sink.on_tool_progress(
        "shell", {"status": "running", "current": 3, "total": 10}
    )
    assert sink._tool_progress is not None
    assert sink._tool_progress_task is not None

    sink.on_tool_end("shell", '{"ok": true, "exit_code": 0, "output": "ok"}')
    assert sink._tool_progress is None


def test_parallel_identical_medium_risk_commands_ask_for_consent_once(
    tmp_path, monkeypatch
):
    import agent.shared as shared
    from agent.core.agent import AgentContext, _active_agent_context
    from agent.core.output import _active_sink
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_session_allowlist_contains,
    )

    tools, _, _, _ = _make_tools(tmp_path)
    sink = _FakeSink(answer=True)
    spawned: list = []

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"ok", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        spawned.append(args)
        return FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    ctx = AgentContext(
        metadata={"session_id": "cli", "channel_name": "cli", "user_id": ""}
    )
    agent_token = _active_agent_context.set(ctx)
    sink_token = _active_sink.set(sink)
    cancel_token = shared.CancelToken()
    cancel_var_token = shared._active_cancel_token.set(cancel_token)
    try:

        async def scenario():
            return await asyncio.gather(
                tools._shell("mkfs /dev/disk0", timeout=1),
                tools._shell("mkfs /dev/disk0", timeout=1),
            )

        results = asyncio.run(scenario())
    finally:
        shared._active_cancel_token.reset(cancel_var_token)
        _active_sink.reset(sink_token)
        _active_agent_context.reset(agent_token)

    assert all(result["ok"] is True for result in results)
    assert len(spawned) == 2
    assert len(sink.asked) == 1
    scope = ShellAuthorizationScope("cli", "cli", "")
    assert shell_session_allowlist_contains("mkfs /dev/disk0", scope=scope) is True


def test_parallel_consent_flows_are_serialized(tmp_path):
    import agent.shared as shared
    from agent.core.agent import AgentContext, _active_agent_context
    from agent.core.output import _active_sink
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_command_check,
    )

    tools, _, _, _ = _make_tools(tmp_path)
    scope = ShellAuthorizationScope("cli", "cli", "")

    class SlowSink(_FakeSink):
        def __init__(self):
            super().__init__(answer=True)
            self.active = 0
            self.max_active = 0

        async def on_tool_confirmation(self, *args, **kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.05)
                return self.answer
            finally:
                self.active -= 1

    sink = SlowSink()
    ctx = AgentContext(
        metadata={"session_id": "cli", "channel_name": "cli", "user_id": ""}
    )
    agent_token = _active_agent_context.set(ctx)
    sink_token = _active_sink.set(sink)
    cancel_token = shared.CancelToken()
    cancel_var_token = shared._active_cancel_token.set(cancel_token)
    try:
        safety_a = shell_command_check("mkfs /dev/disk0", scope=scope)
        safety_b = shell_command_check("dd if=/dev/zero of=/dev/disk1", scope=scope)

        async def scenario():
            return await asyncio.gather(
                tools._try_interactive_confirmation(
                    safety=safety_a,
                    command="mkfs /dev/disk0",
                    extra_blocked=[],
                    authorization_scope=scope,
                ),
                tools._try_interactive_confirmation(
                    safety=safety_b,
                    command="dd if=/dev/zero of=/dev/disk1",
                    extra_blocked=[],
                    authorization_scope=scope,
                ),
            )

        results = asyncio.run(scenario())
    finally:
        shared._active_cancel_token.reset(cancel_var_token)
        _active_sink.reset(sink_token)
        _active_agent_context.reset(agent_token)

    assert results == [True, True]
    assert sink.max_active == 1


@pytest.mark.parametrize(
    "answer, expected",
    [
        ("1", True),
        ("y", True),
        ("yes", True),
        ("同意", True),
        ("批准", True),
        ("2", False),
        ("n", False),
        ("no", False),
        ("拒绝", False),
    ],
)
def test_approval_menu_choice_maps_answers(answer, expected):
    from agent.core.output import _approval_choice_accepted

    assert _approval_choice_accepted(answer) is expected


@pytest.mark.parametrize("answer", ["1", "2", "y", "n", "同意", "拒绝", " 1 "])
def test_approval_menu_validator_accepts_known_choices(answer):
    from prompt_toolkit.document import Document

    from agent.core.output import _APPROVAL_VALIDATOR

    _APPROVAL_VALIDATOR.validate(Document(answer))


@pytest.mark.parametrize("answer", ["3", "", "yes please", "0", "同意执行"])
def test_approval_menu_validator_rejects_unknown_choices(answer):
    from prompt_toolkit.document import Document
    from prompt_toolkit.validation import ValidationError

    from agent.core.output import _APPROVAL_VALIDATOR

    with pytest.raises(ValidationError):
        _APPROVAL_VALIDATOR.validate(Document(answer))


def test_consent_prompt_buffers_parallel_tool_rendering(tmp_path):
    from agent.core.output import CliOutputSink

    console = Console(file=StringIO())
    sink = CliOutputSink(console)

    sink._approval_active = True
    sink.on_tool_end("shell", '{"ok": false, "error": "requires confirmation"}')
    sink.on_tool_progress(
        "shell", {"status": "running", "elapsed_ms": 9000}
    )
    sink.on_heartbeat(elapsed_seconds=9, current_op="tools", op_detail="shell")
    sink.on_status("模型继续中", level="info")

    assert console.file.getvalue() == ""

    sink._flush_consent_buffer()

    out = console.file.getvalue()
    assert "requires confirmation" in out
    assert "模型继续中" in out
    assert "工具正在执行" in out


def test_consent_pending_tracks_approval_lock():
    import asyncio

    from agent.core.output import _APPROVAL_LOCK, _consent_pending

    async def scenario():
        assert _consent_pending() is False
        await _APPROVAL_LOCK.acquire()
        try:
            assert _consent_pending() is True
        finally:
            _APPROVAL_LOCK.release()
        assert _consent_pending() is False

    asyncio.run(scenario())
