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

    from agent.tui import _OutputPane

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
    assert rendered == per_thread * thread_count, "writes were lost"
    assert pane._line_count == rendered, "counter desynced from rendered content"
    assert pane.cursor_position().y <= pane._line_count


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
