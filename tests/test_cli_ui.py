"""Interactive CLI consent gate and input-history tests."""

import asyncio
import contextlib
import re
from io import StringIO
from types import SimpleNamespace

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

        async def communicate(self, stdin=None):
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

    class _TTYStringIO(StringIO):
        def isatty(self) -> bool:
            return True

    console = Console(file=_TTYStringIO(), force_terminal=True)
    return CliOutputSink(console), console


_ANSI_RE = re.compile(r"\x1b\[[0-9;:]*[a-zA-Z]")


def _rendered_text(console) -> str:
    """Console output with styling removed.

    Rich emits an SGR sequence around every syntax-highlighted token, so a code
    span never appears as a contiguous substring of the raw output.  Assertions
    about *content* must therefore compare the rendered text.
    """
    return _ANSI_RE.sub("", console.file.getvalue())


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

    out = _rendered_text(console)
    assert "print(1)" in out
    assert "```" not in out


def test_stream_markdown_flushes_truncated_fence_on_turn_end():
    sink, console = _tty_sink()

    sink.on_stream_chunk("```python\nprint(1)\n")
    sink.on_turn_complete("", [])

    assert "print(1)" in _rendered_text(console)
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

        async def communicate(self, stdin=None):
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


def _builtin_cli_router():
    from agent.commands import CommandRouter, register_builtin_commands

    router = CommandRouter()
    register_builtin_commands(router)
    return router


async def _run_command_menu(monkeypatch, answers):
    import agent.cli as cli

    iterator = iter(answers)

    async def fake_prompt(state, title):
        answer = next(iterator)
        return state.pick(answer) if answer is not None else None

    monkeypatch.setattr(cli, "_menu_prompt_async", fake_prompt)
    return await cli._command_menu(_builtin_cli_router())


def test_command_menu_selects_argument_by_name(monkeypatch):
    import asyncio

    result = asyncio.run(
        _run_command_menu(monkeypatch, ["permissions", "high"])
    )
    assert result == "/permissions high"


def test_command_menu_auto_approve_argument_menu(monkeypatch):
    import asyncio

    result = asyncio.run(
        _run_command_menu(monkeypatch, ["auto-approve", "on"])
    )
    assert result == "/auto-approve on"


def test_command_menu_filter_auto_selects_single_match(monkeypatch):
    import asyncio

    result = asyncio.run(
        _run_command_menu(monkeypatch, ["perm", "sandbox read_all"])
    )
    assert result == "/permissions sandbox read_all"


def test_command_menu_plain_command_returns_immediately(monkeypatch):
    import asyncio

    result = asyncio.run(_run_command_menu(monkeypatch, ["tools"]))
    assert result == "/tools"


def test_command_menu_cancel_returns_none(monkeypatch):
    import asyncio

    assert asyncio.run(_run_command_menu(monkeypatch, [None])) is None
    assert (
        asyncio.run(_run_command_menu(monkeypatch, ["permissions", None]))
        is None
    )


def test_menu_state_filters_live():
    from agent.cli import _MenuState

    state = _MenuState(
        [
            ("alpha", "Alpha", "first option"),
            ("beta", "Beta", "second option"),
            ("gamma", "Gamma", "third option"),
        ]
    )

    assert [item[0] for item in state.filtered("")] == ["alpha", "beta", "gamma"]
    assert [item[0] for item in state.filtered("g")] == ["gamma"]
    assert [item[0] for item in state.filtered("/b")] == ["beta"]
    assert [item[0] for item in state.filtered("2")] == ["beta"]
    assert state.filtered("zzz") == []
    assert state.pick("") == "alpha"
    assert state.pick("2") == "beta"
    assert state.pick("third") == "gamma"
    assert state.pick("zzz") is None


def test_selection_menu_accepts_numbers_names_and_filters(monkeypatch):
    import asyncio

    import agent.cli as cli

    items = [
        ("alpha", "Alpha", "first option"),
        ("beta", "Beta", "second option"),
        ("gamma", "Gamma", "third option"),
    ]

    async def run(answers):
        iterator = iter(answers)

        async def fake_prompt(state, title):
            answer = next(iterator)
            return state.pick(answer) if answer is not None else None

        monkeypatch.setattr(cli, "_menu_prompt_async", fake_prompt)
        return await cli._select_from_menu("测试菜单", items)

    assert asyncio.run(run(["2"])) == "beta"
    assert asyncio.run(run(["gamma"])) == "gamma"
    assert asyncio.run(run(["/alpha"])) == "alpha"
    assert asyncio.run(run(["third"])) == "gamma"
    assert asyncio.run(run([None])) is None


def test_cli_command_completer_filters_live_and_respects_scope(monkeypatch):
    from prompt_toolkit.document import Document

    import agent.cli as cli

    monkeypatch.setattr(cli, "_cli_router", _builtin_cli_router())
    completer = cli._cli_command_completer()

    assert completer is not None

    def suggested(text):
        return [
            completion.text
            for completion in completer.get_completions(Document(text), None)
        ]

    assert "/permissions" in suggested("/")
    assert "/allow" in suggested("/")
    assert "/permissions" in suggested("/p")
    assert "/plugins" in suggested("/p")
    assert "/permissions" in suggested("/perm")
    assert "/export" in suggested("/x")
    assert suggested("/zzz") == []
    assert "/send" not in suggested("/s")
    assert suggested("/Users/shike") == []


class _FakeMenuBuffer:
    def __init__(self, text):
        self.text = text
        self.complete_state = SimpleNamespace(
            current_completion=None,
            complete_index=None,
            completions=[object()],
        )
        self.applied = False
        self.handled = False

    def apply_completion(self, completion):
        self.applied = True

    def go_to_completion(self, index):
        self.complete_state.complete_index = index
        self.complete_state.current_completion = self.complete_state.completions[index]

    def validate_and_handle(self):
        self.handled = True


class _FakeMenuEvent:
    def __init__(self, text):
        self.current_buffer = _FakeMenuBuffer(text)


def test_enter_on_bare_slash_skips_completion_and_submits():
    from agent.cli import _accept_completion_or_submit

    event = _FakeMenuEvent("/")
    _accept_completion_or_submit(event)

    assert event.current_buffer.applied is False
    assert event.current_buffer.handled is True


def test_enter_on_partial_command_applies_highlighted_completion():
    from agent.cli import _accept_completion_or_submit

    event = _FakeMenuEvent("/p")
    _accept_completion_or_submit(event)

    assert event.current_buffer.applied is True
    assert event.current_buffer.handled is True


def test_enter_with_empty_completions_submits_raw_text():
    from agent.cli import _accept_completion_or_submit

    event = _FakeMenuEvent("/zzz")
    event.current_buffer.complete_state.completions = []
    _accept_completion_or_submit(event)

    assert event.current_buffer.applied is False
    assert event.current_buffer.handled is True


def test_interactive_loop_bare_slash_opens_command_menu(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    import agent as agent_module
    import agent.cli as cli_module

    class Agent:
        api_format = "openai"
        model = "fake-model"
        max_tokens = 1024
        context_window = 4096

    class PluginCatalog:
        def fire_session_start(self, components):
            return None

        async def fire_session_end(self, event):
            return None

    class Coordinator:
        def __init__(self):
            self.calls = []

        async def handle(self, turn_input, state, sink):
            self.calls.append(turn_input.text)
            return "exit_cli" if turn_input.text == "/quit" else None

    answers = iter(["/", "/quit"])

    async def _fake_input():
        return next(answers)

    async def _fake_command_menu(router):
        return "/permissions ask"

    monkeypatch.setattr(cli_module, "_ask_user_input", _fake_input)
    monkeypatch.setattr(cli_module, "_command_menu", _fake_command_menu)
    monkeypatch.setattr(cli_module, "_cli_router", None)

    coordinator = Coordinator()
    components = {
        "agent": Agent(),
        "memory": SimpleNamespace(read_index=lambda: ""),
        "system_prompt": "system",
        "skill_catalog": object(),
        "user_tool_catalog": object(),
        "registry": agent_module.ToolRegistry(),
        "output_dir": tmp_path / "output",
        "plugin_catalog": PluginCatalog(),
        "command_router": _builtin_cli_router(),
        "command_coordinator_factory": lambda **kwargs: coordinator,
    }
    cfg = {
        "active_provider": "fake",
        "providers": {
            "fake": {
                "api_format": "openai",
                "default_model": "fake-model",
                "max_tokens": 1024,
            }
        },
        "memory": {},
        "orchestration": {},
        "context": {},
        "mcp_servers": [],
    }

    asyncio.run(agent_module._interactive_loop(components, cfg))

    assert coordinator.calls == ["/permissions ask", "/quit"]


def test_tui_output_pane_captures_ansi_and_tracks_last_line():
    from agent.tui import _OutputPane

    pane = _OutputPane()
    payload = "\x1b[1mbold\x1b[0m\nsecond line\n"
    assert pane.write(payload) == len(payload)

    fragments = pane.fragments()
    assert any(str(style) == "bold" for style, _text in fragments)
    assert pane.cursor_position().y == 2
    assert pane.cursor_position().x == 0


def test_tui_output_pane_parses_split_escape_sequences():
    from agent.tui import _OutputPane

    pane = _OutputPane(view_height=4)
    pane.write("\x1b[3")
    pane.write("1mred text\x1b[0m\n")

    fragments = pane.fragments()
    joined = "".join(text for _style, text in fragments)
    assert "red text" in joined
    assert "\x1b" not in joined
    assert pane.cursor_position().y == 1


def test_tui_output_pane_partial_escape_before_newline_keeps_counts_aligned():
    from prompt_toolkit.layout.controls import FormattedTextControl

    from agent.tui import _OutputPane

    pane = _OutputPane(view_height=4)
    pane.write("hello\x1b[")
    pane.write("\nworld")

    # The abandoned partial escape must not swallow the newline, and the
    # line counter must match what prompt_toolkit will actually render.
    joined = "".join(text for _style, text in pane.fragments())
    assert joined == "hello\nworld"
    assert pane.cursor_position().y == 1

    control = FormattedTextControl(
        pane.fragments,
        get_cursor_position=pane.cursor_position,
    )
    content = control.create_content(80, None)
    assert content.cursor_position.y < content.line_count


def test_tui_output_pane_collapses_live_redraw_stream():
    """Rich live displays rewrite the line with \\r; the pane must not
    accumulate every frame as separate garbage text."""
    from agent.tui import _OutputPane

    frame = "Starting 5 sub-agents via pipeline (limit 3): a, b, c, d, e"
    pane = _OutputPane(view_height=4)
    pane.write("\x1b[?25l" + frame)
    for _ in range(9):
        pane.write("\r\x1b[2K" + frame)
    pane.write("\r\x1b[2K" + frame + "\n\x1b[?25h")

    joined = "".join(text for _style, text in pane.fragments())
    assert joined == frame + "\n"
    assert "\r" not in joined
    assert "25l" not in joined and "25h" not in joined
    assert joined.count(frame) == 1
    assert pane.cursor_position().y == 1


def test_tui_output_pane_redraw_replaces_current_line():
    from agent.tui import _OutputPane

    pane = _OutputPane(view_height=4)
    pane.write("old long line\n")
    pane.write("prefix\r\x1b[2Knew short")
    pane.write("\r\x1b[2Kreplaced")

    joined = "".join(text for _style, text in pane.fragments())
    assert joined == "old long line\nreplaced"
    assert pane.cursor_position().y == 1


def test_tui_output_pane_handles_crlf_as_plain_newline():
    from agent.tui import _OutputPane

    pane = _OutputPane(view_height=4)
    pane.write("line one\r\nline two\r\n")

    joined = "".join(text for _style, text in pane.fragments())
    assert joined == "line one\nline two\n"
    assert pane.cursor_position().y == 2


def test_tui_output_pane_strips_osc8_hyperlinks_keeps_path_text():
    from agent.tui import _OutputPane

    pane = _OutputPane(view_height=4)
    pane.write(
        "\x1b]8;id=1;file:///tmp/app.html\x1b\\"
        "path=/tmp/app.html"
        "\x1b]8;;\x1b\\\n"
    )

    joined = "".join(text for _style, text in pane.fragments())
    assert joined == "path=/tmp/app.html\n"
    assert "\x1b" not in joined
    assert "8;;" not in joined
    assert pane.cursor_position().y == 1


def test_tui_output_pane_tracks_lines_incrementally():
    from agent.tui import _OutputPane

    pane = _OutputPane(view_height=4)
    pane.write("a\n")
    pane.write("b\n")
    pane.write("c")
    assert pane.cursor_position().y == 2
    pane.write("d\n")
    assert pane.cursor_position().y == 3


def test_tui_output_pane_scrolls_through_history():
    from agent.tui import _OutputPane

    pane = _OutputPane(view_height=4)
    pane.write("a\nb\nc\nd\ne")
    assert pane.cursor_position().y == 4

    pane.scroll_up(lines=1)
    assert pane.cursor_position().y == 3
    pane.scroll_up(lines=10)
    assert pane.cursor_position().y == 3

    pane.scroll_down(lines=1)
    assert pane.cursor_position().y == 4

    pane.scroll_top()
    assert pane.cursor_position().y == 3
    pane.scroll_bottom()
    assert pane.cursor_position().y == 4


def test_tui_output_pane_keeps_manual_scroll_when_new_output_arrives():
    from agent.tui import _OutputPane

    pane = _OutputPane(view_height=4)
    pane.write("a\nb\nc\nd\ne")
    pane.scroll_up(lines=10)  # manual scroll to the top
    assert pane.cursor_position().y == 3

    pane.write("f\n")
    assert pane.cursor_position().y == 3  # view stays put while scrolled up

    pane.scroll_bottom()
    assert pane.cursor_position().y == 5
    pane.write("g\n")
    assert pane.cursor_position().y == 6  # follows the newest output again


def test_tui_mouse_wheel_scrolls_output_pane():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.mouse_events import MouseEvent, MouseEventType

    from agent.tui import _OutputPane, _ScrollableOutputWindow

    pane = _OutputPane(view_height=4)
    pane.write("1\n2\n3\n4\n5")
    window = _ScrollableOutputWindow(pane, FormattedTextControl(pane.fragments))

    window._mouse_handler(
        MouseEvent(
            position=Point(x=0, y=0),
            event_type=MouseEventType.SCROLL_UP,
            button=0,
            modifiers=frozenset(),
        )
    )
    assert pane.cursor_position().y == 3

    window._mouse_handler(
        MouseEvent(
            position=Point(x=0, y=0),
            event_type=MouseEventType.SCROLL_DOWN,
            button=0,
            modifiers=frozenset(),
        )
    )
    assert pane.cursor_position().y == 4


def test_output_pane_logical_line_mapping_and_line_text():
    from agent.tui import _OutputPane

    pane = _OutputPane(view_height=3)
    for index in range(6):
        pane.write(f"line{index}\n")

    pane.scroll_top()
    assert pane.logical_line_at(0) == 0
    assert pane.logical_line_at(2) == 2
    assert pane.line_text(2) == "line2"

    pane.scroll_bottom()
    assert pane.logical_line_at(0) == 4
    assert pane.line_text(4) == "line4"
    assert pane.line_text(99) == ""


def test_paths_from_line_extracts_existing_paths(tmp_path):
    from pathlib import Path

    from agent.tui import _paths_from_line

    real = tmp_path / "real.txt"
    real.write_text("x", encoding="utf-8")

    paths = _paths_from_line(f"✓ path={real} · items=3")
    assert [str(path) for _display, path in paths] == [str(real)]

    spaced = tmp_path / "a b.txt"
    spaced.write_text("x", encoding="utf-8")
    assert _paths_from_line(f"path='{spaced}'")[0][0] == str(spaced)

    deduped = _paths_from_line(f"see {real} and {real}")
    assert len(deduped) == 1

    missing = _paths_from_line(f"path={tmp_path / 'nope.txt'}")
    assert missing == []


def test_output_window_right_click_invokes_context_menu():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    from agent.tui import _OutputPane, _ScrollableOutputWindow

    pane = _OutputPane(view_height=4)
    pane.write("first\nsecond path=/tmp/app.html\nthird")
    calls: list[tuple[int, str, Point]] = []

    window = _ScrollableOutputWindow(
        pane,
        FormattedTextControl(pane.fragments),
        context_menu=lambda line_index, text, position: calls.append(
            (line_index, text, position)
        ),
    )

    window._mouse_handler(
        MouseEvent(
            position=Point(x=4, y=1),
            event_type=MouseEventType.MOUSE_DOWN,
            button=MouseButton.RIGHT,
            modifiers=frozenset(),
        )
    )
    assert len(calls) == 1
    assert calls[0][0] == 1
    assert "path=/tmp/app.html" in calls[0][1]

    window._mouse_handler(
        MouseEvent(
            position=Point(x=4, y=1),
            event_type=MouseEventType.MOUSE_UP,
            button=MouseButton.RIGHT,
            modifiers=frozenset(),
        )
    )
    assert len(calls) == 1  # release must not open the menu a second time

    window._mouse_handler(
        MouseEvent(
            position=Point(x=4, y=1),
            event_type=MouseEventType.MOUSE_DOWN,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
    )
    assert len(calls) == 1  # left click must not open the menu


def test_copy_to_clipboard_pipes_path(monkeypatch):
    import sys
    from pathlib import Path

    from agent.tui import _copy_to_clipboard

    captured: dict = {}

    class _FakeProcess:
        returncode = 0

        async def communicate(self, data):
            captured["data"] = data

    async def _fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(sys, "platform", "darwin")

    assert asyncio.run(_copy_to_clipboard(Path("/tmp/x.txt"))) is True
    assert captured["args"][0] == "pbcopy"
    assert captured["data"] == b"/tmp/x.txt"


def test_tui_session_submits_input_through_queue(tmp_path):
    import asyncio

    from agent.tui import TuiSession

    tui = TuiSession(
        history_path=tmp_path / "cli_history",
        completer_factory=lambda: None,
        cancel_callback=lambda: None,
        console_width=80,
    )
    tui._submit("hello")
    tui._submit(None)

    async def scenario():
        assert await tui.ask_async() == "hello"
        assert await tui.ask_async() is None

    asyncio.run(scenario())


def test_tui_submit_echoes_queued_input_when_busy(tmp_path):
    from agent.tui import TuiSession

    busy_tui = TuiSession(
        history_path=tmp_path / "h1",
        completer_factory=lambda: None,
        cancel_callback=lambda: None,
        busy=lambda: True,
        console_width=80,
    )
    busy_tui._submit("hello")
    busy_text = "".join(
        text for _style, text in busy_tui._pane.fragments()
    )
    assert "已排队" in busy_text
    assert "hello" in busy_text

    idle_tui = TuiSession(
        history_path=tmp_path / "h2",
        completer_factory=lambda: None,
        cancel_callback=lambda: None,
        busy=lambda: False,
        console_width=80,
    )
    idle_tui._submit("hello")
    idle_text = "".join(
        text for _style, text in idle_tui._pane.fragments()
    )
    assert "已排队" not in idle_text


def test_tui_does_not_mark_confirmation_input_as_queued(tmp_path):
    from agent.tui import TuiSession

    tui = TuiSession(
        history_path=tmp_path / "confirmation_history",
        completer_factory=lambda: None,
        cancel_callback=lambda: None,
        busy=lambda: True,
        console_width=80,
    )

    async def scenario():
        pending = asyncio.create_task(tui.ask_async(during_turn=True))
        await asyncio.sleep(0)
        tui._submit("1")
        assert await pending == "1"

    asyncio.run(scenario())
    rendered = "".join(text for _style, text in tui._pane.fragments())
    assert "已排队" not in rendered


def test_tui_request_exit_queues_none_and_is_idempotent(tmp_path):
    import asyncio

    from agent.tui import TuiSession

    tui = TuiSession(
        history_path=tmp_path / "cli_history",
        completer_factory=lambda: None,
        cancel_callback=lambda: None,
        console_width=80,
    )

    tui.request_exit()
    tui.request_exit()

    async def scenario():
        assert await tui.ask_async() is None

    asyncio.run(scenario())


def test_ask_user_input_uses_active_tui(monkeypatch):
    import asyncio

    import agent.cli as cli

    class FakeTui:
        def __init__(self, text):
            self.text = text

        async def ask_async(self):
            return self.text

    monkeypatch.setattr(cli, "_ACTIVE_TUI", FakeTui("hello"))
    assert asyncio.run(cli._ask_user_input()) == "hello"

    monkeypatch.setattr(cli, "_ACTIVE_TUI", FakeTui(None))
    with pytest.raises(EOFError):
        asyncio.run(cli._ask_user_input())


def test_line_mode_wraps_background_output_with_prompt_toolkit_proxy(monkeypatch):
    """Async MCP status output must render above, not inside, the input line."""
    import agent.cli as cli

    events: list[str] = []

    class _PatchStdout:
        def __enter__(self):
            events.append("enter")

        def __exit__(self, exc_type, exc, tb):
            events.append("exit")

    async def fake_body(*args, **kwargs):
        events.append("body")

    monkeypatch.setattr(
        cli,
        "patch_stdout",
        lambda *, raw: _PatchStdout() if raw else None,
    )
    monkeypatch.setattr(cli, "_interactive_loop_body", fake_body)

    asyncio.run(cli._interactive_loop_coro({}, {}, None))

    assert events == ["enter", "body", "exit"]


def test_sink_disables_live_status_but_keeps_stream_markdown():
    from agent.core.output import CliOutputSink

    console = Console(file=StringIO(), force_terminal=True)
    sink = CliOutputSink(console, live_status=False)

    assert sink._supports_live_status() is False
    assert sink._supports_stream_markdown() is True


def test_sink_interactive_confirmation_requires_live_terminal():
    from agent.core.output import CliOutputSink, OutputSink

    assert OutputSink().interactive_confirmation is False

    console = Console(file=StringIO(), force_terminal=True)
    assert CliOutputSink(console, live_status=False).interactive_confirmation is False
    assert CliOutputSink(console, live_status=True).interactive_confirmation is False


# ── Consent capability is independent of spinner capability ─────────────────


class _ConsentConsole:
    is_terminal = True

    def status(self, *args, **kwargs):
        raise AssertionError("consent must not depend on the live spinner")

    def print(self, *args, **kwargs):
        return None


def test_tui_sink_can_prompt_for_consent_without_a_live_spinner(monkeypatch):
    """The TUI hosts no spinner but prompts fine.

    Regression: interactive_confirmation was derived from the spinner flag, so
    full-screen TUI mode — the default on any real terminal — silently had no
    approval menu at all, leaving phrase matching as the only consent channel.
    """
    from agent.core.output import CliOutputSink

    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    tui_sink = CliOutputSink(_ConsentConsole(), live_status=False, can_prompt=True)
    assert tui_sink.interactive_confirmation is True
    assert tui_sink._supports_live_status() is False


def test_tui_consent_reuses_existing_input_queue(monkeypatch):
    """A full-screen TUI must not start a nested prompt_toolkit session."""
    from agent.core import output as output_module
    from agent.core.output import CliOutputSink

    answers = iter(["invalid", "1"])
    received: list[str] = []

    async def tui_ask():
        answer = next(answers)
        received.append(answer)
        return answer

    async def nested_prompt_must_not_run(*args, **kwargs):
        raise AssertionError("nested PromptSession must not run in TUI mode")

    monkeypatch.setattr(
        output_module._APPROVAL_PROMPT,
        "prompt_async",
        nested_prompt_must_not_run,
    )
    sink = CliOutputSink(
        _ConsentConsole(),
        live_status=False,
        can_prompt=True,
        confirmation_prompt=tui_ask,
    )

    approved = asyncio.run(
        sink.on_tool_confirmation(
            "shell",
            command="sqlite3 palace.db 'DELETE FROM memory_items'",
            risk_level="high",
            reason="destructive memory reset",
            confirmation_token="token",
            scope=None,
        )
    )

    assert approved is True
    assert received == ["invalid", "1"]


def test_line_mode_sink_still_prompts(monkeypatch):
    from agent.core.output import CliOutputSink

    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    assert CliOutputSink(_ConsentConsole(), live_status=True).interactive_confirmation


def test_non_interactive_sink_refuses_to_prompt(monkeypatch):
    from agent.core.output import CliOutputSink

    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    piped = CliOutputSink(_ConsentConsole(), live_status=False, can_prompt=False)
    assert piped.interactive_confirmation is False

    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    no_tty = CliOutputSink(_ConsentConsole(), live_status=True, can_prompt=True)
    assert no_tty.interactive_confirmation is False


def test_consent_prompt_shows_the_command_untruncated():
    """The approval allowlists the whole command, so the human must see it all.

    Regression: the command was clipped to 200 chars, letting a long benign
    prefix push the destructive tail out of view.
    """
    from agent.core.output import CliOutputSink

    printed: list[str] = []

    class _Recorder(_ConsentConsole):
        def print(self, *args, **kwargs):
            printed.append(str(args[0]) if args else "")

    command = "echo " + "A" * 400 + " ; rm -rf /tmp/victim"
    sink = CliOutputSink(_Recorder(), live_status=False, can_prompt=True)
    with contextlib.suppress(Exception):
        asyncio.run(
            sink.on_tool_confirmation(
                "shell",
                command=command,
                risk_level="medium",
                reason="inline execution",
                confirmation_token="t",
                scope=None,
            )
        )
    rendered = "\n".join(printed)
    if rendered:
        assert "rm -rf /tmp/victim" in rendered, "destructive tail was hidden"


# ── One Ctrl+C must be counted exactly once ─────────────────────────────────


def test_ctrl_c_has_exactly_one_owner_per_mode():
    """Ctrl+C must be counted once.

    Regression: TUI mode installed the process-level SIGINT handler *and* relied
    on prompt_toolkit's key binding. Both fired for one keypress, so
    _sigint_count reached 2 immediately and the first Ctrl+C escalated straight
    to force-cancel (SIGKILL) — the advertised graceful-then-force contract was
    unreachable.
    """
    from agent.cli import _owns_process_sigint

    assert _owns_process_sigint(tui_active=False) is True
    assert _owns_process_sigint(tui_active=True) is False


def test_both_sigint_owners_would_double_count_a_single_press(monkeypatch):
    """Documents why the guard above is required."""
    import agent.cli as cli

    class _Token:
        def __init__(self):
            self.levels: list[str] = []
            self.is_cancelled = False

        def cancel(self, level: str = "graceful") -> None:
            self.levels.append(level)

    token = _Token()
    monkeypatch.setattr(cli, "_current_cancel_token", token)
    monkeypatch.setattr(cli, "_sigint_count", 0)
    monkeypatch.setattr(cli, "_ACTIVE_TUI", SimpleNamespace(request_exit=lambda: None))
    monkeypatch.setattr(cli.shared, "CONSOLE", SimpleNamespace(print=lambda *a, **k: None))

    # Simulate the pre-fix world: both owners react to one keypress.
    cli._cli_sigint_handler(2, None)
    cli._tui_cancel_callback()
    assert token.levels == ["graceful", "force"], (
        "two owners escalate a single press to force-cancel"
    )

    # A single owner gives the documented two-stage contract.
    token2 = _Token()
    monkeypatch.setattr(cli, "_current_cancel_token", token2)
    monkeypatch.setattr(cli, "_sigint_count", 0)
    cli._tui_cancel_callback()
    assert token2.levels == ["graceful"]
    cli._tui_cancel_callback()
    assert token2.levels == ["graceful", "force"]


# ── One channel owns the screen ─────────────────────────────────────────────


def test_log_records_route_through_the_console_not_raw_stderr(monkeypatch):
    """The interactive CLI configures no logging, so lastResort wrote full
    tracebacks to stderr: invisible to the sink's redaction and, while the TUI
    owns the screen, straight past the output pane onto the raw terminal.
    """
    import logging
    import sys

    from agent.cli import _SinkLogHandler

    printed: list[str] = []

    class _Console:
        def print(self, *args, **kwargs):
            printed.append(str(args[0]) if args else "")

    root = logging.getLogger()
    handler = _SinkLogHandler(_Console())
    root.addHandler(handler)
    previous_last_resort = logging.lastResort
    logging.lastResort = None
    captured = StringIO()
    monkeypatch.setattr(sys, "stderr", captured)
    try:
        logger = logging.getLogger("agent.commands.router")
        try:
            raise RuntimeError("api_key=sk-SECRET at /Users/x/.agent/config.json")
        except RuntimeError:
            logger.exception("command /x failed")
    finally:
        root.removeHandler(handler)
        logging.lastResort = previous_last_resort

    console_text = "\n".join(printed)
    assert captured.getvalue() == "", "log output escaped to raw stderr"
    assert "command /x failed" in console_text
    assert "Traceback" in console_text, "debuggability must be preserved"
    assert "sk-SECRET" not in console_text, "key=value secret was not redacted"


def test_sink_log_handler_ignores_routine_chatter():
    import logging

    from agent.cli import _SinkLogHandler

    printed: list[str] = []

    class _Console:
        def print(self, *args, **kwargs):
            printed.append(str(args[0]) if args else "")

    root = logging.getLogger()
    handler = _SinkLogHandler(_Console())
    root.addHandler(handler)
    try:
        logging.getLogger("agent").info("routine chatter")
    finally:
        root.removeHandler(handler)
    assert printed == [], "INFO-level records must not reach the screen"


def test_output_pane_counter_survives_concurrent_writes():
    """The background memory worker is a daemon thread printing to
    shared.CONSOLE, which is rebound to the TUI console — so pane writes race
    the event loop.  A desynced _line_count pins the view to the wrong line and
    breaks auto-follow.
    """
    import threading

    from agent.tui import (
        _MAX_SCROLLBACK_LINES,
        _SCROLLBACK_TRIM_SLACK,
        _OutputPane,
    )

    pane = _OutputPane(view_height=10)
    per_thread, thread_count = 2000, 8

    def worker(index: int) -> None:
        for i in range(per_thread):
            pane.write(f"thread{index} line{i}\n")

    threads = [
        threading.Thread(target=worker, args=(n,)) for n in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    rendered = sum(
        item[1].count("\n") for item in pane.fragments() if len(item) >= 2
    )
    # The writes far exceed the scrollback cap, so the pane keeps a trailing
    # window of them.  What has to hold is that the counter still describes
    # exactly what is in the fragment list, trimming included.
    assert _MAX_SCROLLBACK_LINES <= rendered <= _MAX_SCROLLBACK_LINES + _SCROLLBACK_TRIM_SLACK
    assert pane._line_count == rendered, "counter desynced from rendered content"
    assert pane.cursor_position().y <= pane._line_count


# ── Scrollback cap ──────────────────────────────────────────────────────────


def _pane_text(pane) -> str:
    return "".join(item[1] for item in pane.fragments() if len(item) >= 2)


def test_write_coalesces_same_style_runs():
    """The ANSI parser emits one fragment per character.

    Everything downstream is per-fragment — prompt_toolkit splits, copies and
    hashes the whole list on every redraw — so 26 characters must not become
    26 tuples.
    """
    from agent.tui import _OutputPane

    pane = _OutputPane(view_height=10)
    pane.write("plain \x1b[31mred text\x1b[0m plain again\n")

    assert pane.fragments() == [
        ("", "plain "),
        ("ansired", "red text"),
        ("", " plain again\n"),
    ]


def test_scrollback_is_capped_and_keeps_the_newest_lines():
    """An uncapped pane makes every redraw cost grow with the whole session.

    prompt_toolkit re-splits, re-copies and hashes the entire fragment list on
    each render, so the pane keeps a trailing window instead of the transcript.
    """
    from agent.tui import (
        _MAX_SCROLLBACK_LINES,
        _SCROLLBACK_TRIM_SLACK,
        _OutputPane,
    )

    pane = _OutputPane(view_height=10)
    total = _MAX_SCROLLBACK_LINES + _SCROLLBACK_TRIM_SLACK + 700
    for i in range(total):
        pane.write(f"line {i}\n")

    text = _pane_text(pane)
    assert pane._line_count == text.count("\n")
    assert pane._line_count <= _MAX_SCROLLBACK_LINES + _SCROLLBACK_TRIM_SLACK

    assert f"line {total - 1}\n" in text, "the newest output must survive"
    assert "line 0\n" not in text, "the oldest output must be the part dropped"
    # No half-line left at the front: the cut lands on a line boundary.
    assert text.startswith("line ")


def test_trimming_shifts_the_scroll_position_with_the_content():
    """Logical indices are relative to the fragment list, so they must shift.

    A reader parked in the middle of the transcript would otherwise silently
    jump backwards through the conversation every time the pane trimmed.
    """
    from agent.tui import (
        _MAX_SCROLLBACK_LINES,
        _SCROLLBACK_TRIM_SLACK,
        _OutputPane,
    )

    pane = _OutputPane(view_height=10)
    for i in range(_MAX_SCROLLBACK_LINES + _SCROLLBACK_TRIM_SLACK):
        pane.write(f"line {i}\n")

    pane.scroll_up(lines=200)
    parked = pane.line_text(pane._scroll_top)
    assert parked.startswith("line ")

    for i in range(400):
        pane.write(f"tail {i}\n")

    assert pane._line_count <= _MAX_SCROLLBACK_LINES + _SCROLLBACK_TRIM_SLACK
    assert pane.line_text(pane._scroll_top) == parked, "the view slid off its line"


def test_line_text_reads_one_line_without_copying_the_pane():
    from agent.tui import _OutputPane

    pane = _OutputPane(view_height=10)
    pane.write("first\nsecond\n")
    pane.write("thi")
    pane.write("rd tail\nfourth\n")

    assert pane.line_text(0) == "first"
    assert pane.line_text(1) == "second"
    # A line split across two write() calls spans several fragments.
    assert pane.line_text(2) == "third tail"
    assert pane.line_text(3) == "fourth"
    # The unterminated line after the last newline, and out-of-range lookups.
    assert pane.line_text(4) == ""
    assert pane.line_text(99) == ""
    assert pane.line_text(-1) == ""


# ── Scroll accounting must use display rows, not logical lines ───────────────


class _WrapInfo:
    """Stands in for WindowRenderInfo with a fixed wrap factor."""

    def __init__(self, window_height: int, rows_per_line: int) -> None:
        self.window_height = window_height
        self._rows = rows_per_line

    def get_height_for_line(self, line: int) -> int:
        return self._rows


class _WrapWindow:
    def __init__(self, info) -> None:
        self.render_info = info
        self.vertical_scroll = 0


def _wrapped_pane(lines: int = 12):
    from agent.tui import _OutputPane

    pane = _OutputPane(view_height=10)
    for _ in range(lines):
        pane.write("x" * 400 + "\n")
    return pane


def test_follow_bottom_uses_wrapped_rows_so_newest_output_stays_visible():
    """One logical line can occupy several display rows under wrap_lines=True.

    Regression: the scroll bound was computed from the logical line count, so
    for output wider than the terminal the view pinned far above the newest
    line — auto-follow silently stopped showing the newest output.
    """
    pane = _wrapped_pane(12)
    logical_only = pane.vertical_scroll(_WrapWindow(None))
    wrapped = pane.vertical_scroll(_WrapWindow(_WrapInfo(10, 5)))

    # 5 rows per line into a 10-row window fits 2 logical lines, not 10.
    assert wrapped > logical_only
    assert wrapped >= pane._line_count - 2


def test_unwrapped_output_matches_logical_accounting():
    pane = _wrapped_pane(12)
    logical_only = pane.vertical_scroll(_WrapWindow(None))
    assert pane.vertical_scroll(_WrapWindow(_WrapInfo(10, 1))) == logical_only


def test_manual_scrolling_is_symmetric_under_wrapping():
    """scroll_up/scroll_down must step evenly and restore follow at the bottom."""
    pane = _wrapped_pane(12)
    window = _WrapWindow(_WrapInfo(10, 5))
    bottom = pane.vertical_scroll(window)

    pane.scroll_up(3)
    first = pane.vertical_scroll(window)
    assert first == bottom - 3, "scroll_up jumped instead of stepping"
    assert pane._follow_bottom is False

    pane.scroll_up(3)
    assert pane.vertical_scroll(window) == bottom - 6

    pane.scroll_down(3)
    assert pane.vertical_scroll(window) == bottom - 3, "scroll_down was asymmetric"
    pane.scroll_down(3)
    assert pane.vertical_scroll(window) == bottom
    assert pane._follow_bottom is True, "reaching the bottom must resume follow"


def test_scroll_accounting_tolerates_a_failing_renderer():
    pane = _wrapped_pane(12)
    logical_only = pane.vertical_scroll(_WrapWindow(None))

    class _Broken:
        window_height = 10

        def get_height_for_line(self, line):
            raise RuntimeError("renderer not ready")

    assert pane.vertical_scroll(_WrapWindow(_Broken())) == logical_only


# ── Terminal resize follow-through ──────────────────────────────────────────


def _tui(tmp_path, **kwargs):
    from agent.tui import TuiSession

    return TuiSession(
        history_path=tmp_path / "cli_history",
        completer_factory=lambda: None,
        cancel_callback=lambda: None,
        **kwargs,
    )


def test_console_width_follows_terminal_resize(tmp_path, monkeypatch):
    """Rich wraps at print time, so a stale width mis-wraps every later line."""
    import os
    import agent.tui as tui_module

    size = os.terminal_size((100, 40))
    monkeypatch.setattr(tui_module.shutil, "get_terminal_size", lambda *a: size)
    tui = _tui(tmp_path)
    assert tui.console.width == 100

    size = os.terminal_size((60, 40))
    # The width check is throttled; a resize between checks is not missed, it
    # is just applied at the next one.
    tui._width_checked_at = 0.0
    tui._output_fragments()
    assert tui.console.width == 60

    size = os.terminal_size((140, 40))
    tui._width_checked_at = 0.0
    tui._output_fragments()
    assert tui.console.width == 140


def test_console_width_check_is_throttled(tmp_path, monkeypatch):
    import os
    import agent.tui as tui_module

    size = os.terminal_size((100, 40))
    monkeypatch.setattr(tui_module.shutil, "get_terminal_size", lambda *a: size)
    tui = _tui(tmp_path)
    tui._output_fragments()  # arms the throttle

    size = os.terminal_size((60, 40))
    tui._output_fragments()
    assert tui.console.width == 100, "width was re-polled on every render"


def test_explicit_console_width_is_pinned(tmp_path, monkeypatch):
    import os
    import agent.tui as tui_module

    monkeypatch.setattr(
        tui_module.shutil, "get_terminal_size", lambda *a: os.terminal_size((60, 40))
    )
    tui = _tui(tmp_path, console_width=80)
    tui._width_checked_at = 0.0
    tui._output_fragments()

    assert tui.console.width == 80


def test_console_width_survives_a_failing_terminal_probe(tmp_path, monkeypatch):
    import agent.tui as tui_module

    def _boom(*args):
        raise OSError("no tty")

    monkeypatch.setattr(tui_module.shutil, "get_terminal_size", lambda *a: _FakeSize())
    tui = _tui(tmp_path)
    monkeypatch.setattr(tui_module.shutil, "get_terminal_size", _boom)
    tui._width_checked_at = 0.0
    tui._output_fragments()

    assert tui.console.width == 100


class _FakeSize:
    columns = 100
    lines = 40


# ── Multi-line input ────────────────────────────────────────────────────────


def _containers(node):
    """Every container in a layout tree, parents before children."""
    yield node
    for child in node.get_children():
        yield from _containers(child)


def _ruled_input(container):
    """The HSplit bracketing the input window between two rules."""
    from prompt_toolkit.layout.containers import HSplit
    from prompt_toolkit.layout.controls import BufferControl

    for node in _containers(container):
        if not isinstance(node, HSplit) or len(node.children) != 3:
            continue
        if isinstance(getattr(node.children[1], "content", None), BufferControl):
            return node
    raise AssertionError("the input window is not bracketed by rules")


def test_input_is_bracketed_by_rules(tmp_path):
    from prompt_toolkit.layout.controls import BufferControl

    from agent.tui import _RULE_CHAR

    tui = _tui(tmp_path, console_width=80)
    top, body, bottom = _ruled_input(tui._build_layout()).children

    assert isinstance(body.content, BufferControl)
    assert top.char == bottom.char == _RULE_CHAR
    assert top.height == bottom.height == 1, "chrome must cost two rows, no more"
    assert top.width is None and bottom.width is None, "rules span the full width"


def _key_handlers(tui):
    """Key-binding handlers by function name (key reprs are not stable)."""
    return {
        binding.handler.__name__: binding.handler
        for binding in tui._build_key_bindings().bindings
    }


def test_input_window_grows_with_the_buffer(tmp_path):
    import asyncio

    from agent.tui import _INPUT_MAX_ROWS

    async def scenario():
        tui = _tui(tmp_path, console_width=80)
        assert tui._input_height() == 1

        tui._buffer.insert_text("one\ntwo\nthree")
        assert tui._input_height() == 3, "a pasted block must be visible, not hidden"

        tui._buffer.insert_text("\n" * 20)
        assert tui._input_height() == _INPUT_MAX_ROWS, "input must not eat the transcript"

    asyncio.run(scenario())


def test_alt_enter_composes_a_newline_and_enter_submits(tmp_path):
    import asyncio

    async def scenario():
        tui = _tui(tmp_path, console_width=80)
        handlers = _key_handlers(tui)
        event = SimpleNamespace(app=SimpleNamespace(current_buffer=tui._buffer))

        handlers["_newline"](event)
        tui._buffer.insert_text("second")
        assert tui._buffer.text == "\nsecond"
        assert tui._buffer.document.line_count == 2

        handlers["_enter"](event)
        assert tui._buffer.text == ""
        assert await tui.ask_async() == "\nsecond"

    asyncio.run(scenario())


def test_arrows_move_the_cursor_inside_a_multiline_draft(tmp_path):
    import asyncio

    async def scenario():
        tui = _tui(tmp_path, console_width=80)
        handlers = _key_handlers(tui)
        event = SimpleNamespace(app=SimpleNamespace(current_buffer=tui._buffer))
        scrolled = []
        tui._pane.scroll_up = lambda **kwargs: scrolled.append("up")
        tui._pane.scroll_down = lambda **kwargs: scrolled.append("down")

        tui._buffer.insert_text("one\ntwo")
        assert tui._buffer.document.cursor_position_row == 1

        handlers["_up"](event)
        assert tui._buffer.document.cursor_position_row == 0
        handlers["_down"](event)
        assert tui._buffer.document.cursor_position_row == 1
        assert scrolled == [], "a draft in the buffer must not scroll the transcript"

        tui._buffer.reset()
        handlers["_up"](event)
        handlers["_down"](event)
        assert scrolled == ["up", "down"], "an empty buffer still scrolls the transcript"

    asyncio.run(scenario())


# ── Status row ──────────────────────────────────────────────────────────────


def test_status_row_is_collapsed_while_idle(tmp_path):
    tui = _tui(tmp_path, console_width=80)

    assert tui._status_height() == 0
    assert tui._status_fragments() == []


def test_status_row_renders_a_spinner_and_the_text(tmp_path):
    from agent.tui import _SPINNER_FRAMES

    tui = _tui(tmp_path, console_width=80)
    tui.set_status("模型正在生成 (3s)")

    assert tui._status_height() == 1
    fragments = tui._status_fragments()
    assert fragments[0][1].strip() in _SPINNER_FRAMES
    assert fragments[1][1] == "模型正在生成 (3s)"

    tui.set_status("")
    assert tui._status_height() == 0


def test_status_spinner_animates_then_stops_when_cleared(tmp_path):
    import asyncio

    from agent.tui import _STATUS_TICK_SECONDS

    async def scenario():
        tui = _tui(tmp_path, console_width=80)
        tui.set_status("running")
        first = tui._status_fragments()[0][1]
        await asyncio.sleep(_STATUS_TICK_SECONDS * 2.5)
        assert tui._status_fragments()[0][1] != first, "spinner never advanced"
        assert tui._status_task is not None

        tui.set_status("")
        await asyncio.sleep(_STATUS_TICK_SECONDS * 2)
        assert tui._status_task is None, "an idle session must cost no redraws"

    asyncio.run(scenario())


def test_status_row_normalizes_multiline_text(tmp_path):
    """The row is one line high; embedded newlines would corrupt the layout."""
    tui = _tui(tmp_path, console_width=80)
    tui.set_status("running\nsecond line   spaced")

    assert tui._status_fragments()[1][1] == "running second line spaced"


def test_status_row_survives_being_set_outside_an_event_loop(tmp_path):
    tui = _tui(tmp_path, console_width=80)
    tui.set_status("no loop here")

    assert tui._status_text == "no loop here"
    assert tui._status_task is None


# ── Sink → status row wiring ────────────────────────────────────────────────


def _status_sink():
    from agent.core.output import CliOutputSink

    statuses = []
    console = Console(file=StringIO(), force_terminal=True)
    sink = CliOutputSink(
        console,
        live_status=False,
        can_prompt=True,
        status_callback=statuses.append,
    )
    return sink, statuses, console


def test_heartbeat_updates_the_status_row_instead_of_the_transcript():
    sink, statuses, console = _status_sink()

    sink.on_heartbeat(elapsed_seconds=3.0, current_op="LLM")

    assert statuses == ["模型正在生成 (3s)"]
    assert console.file.getvalue() == "", "progress must not enter the scrollback"


def test_heartbeat_keeps_the_elapsed_counter_live_on_a_status_row():
    """Without a status row each tick costs a printed line, so it is throttled."""
    sink, statuses, _ = _status_sink()

    sink.on_heartbeat(elapsed_seconds=3.0, current_op="LLM")
    sink._last_heartbeat_at -= 1.5
    sink.on_heartbeat(elapsed_seconds=5.0, current_op="LLM")

    assert statuses == ["模型正在生成 (3s)", "模型正在生成 (5s)"]


def test_heartbeat_without_a_status_surface_still_throttles_hard():
    from agent.core.output import CliOutputSink

    console = Console(file=StringIO(), force_terminal=True)
    sink = CliOutputSink(console, live_status=False)

    sink.on_heartbeat(elapsed_seconds=3.0, current_op="LLM")
    sink._last_heartbeat_at -= 1.5
    sink.on_heartbeat(elapsed_seconds=5.0, current_op="LLM")

    assert console.file.getvalue().count("模型正在生成") == 1


def test_tool_lifecycle_drives_the_status_row():
    sink, statuses, _ = _status_sink()

    sink.begin_turn()
    sink.on_tool_start("bash", {"command": "ls"})
    sink.on_tool_end("bash", '{"ok": true}')
    sink.on_turn_complete("done", [])

    assert statuses[0] == "Preparing response…"
    assert "Running bash…" in statuses
    assert statuses[-1] == "", "the row must clear when the turn ends"


def test_tool_progress_updates_the_status_row():
    sink, statuses, console = _status_sink()

    sink.on_tool_progress("fetch", {"status": "running", "current": 2, "total": 10})

    assert statuses and "fetch" in statuses[-1]
    assert console.file.getvalue() == ""


def test_status_callback_is_optional_and_line_mode_is_unchanged():
    from agent.core.output import CliOutputSink

    console = Console(file=StringIO(), force_terminal=True)
    sink = CliOutputSink(console, live_status=False)

    assert sink._has_status_surface() is False
    sink.on_heartbeat(elapsed_seconds=3.0, current_op="LLM")
    assert "模型正在生成" in console.file.getvalue()
