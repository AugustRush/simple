"""The subprocess seam: one provider, both spawn points.

The point of the seam is not that a provider *exists* — it is that no call
site can quietly keep its own copy of "spawn a child".  So the load-bearing
assertion here is that injecting one accounting fake intercepts *both* the
shell tool and the user-tool runner.  If someone later reintroduces an inline
``create_subprocess_exec``, that test fails rather than the drift going
unnoticed until the two copies disagree about cancellation again.
"""

from __future__ import annotations

import asyncio
import json
import textwrap

from agent.exec import ExecRequest, ExecResult, provider_from


class RecordingProvider:
    """Records every request, runs nothing."""

    def __init__(self, *, stdout: bytes = b"", returncode: int = 0):
        self.requests: list[ExecRequest] = []
        self._stdout = stdout
        self._returncode = returncode

    async def run(self, request: ExecRequest) -> ExecResult:
        self.requests.append(request)
        return ExecResult(stdout=self._stdout, returncode=self._returncode)


def _tools(tmp_path, registry):
    from agent import BuiltinTools, MemoryPalace

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    output = tmp_path / "output"
    output.mkdir(exist_ok=True)
    return BuiltinTools(
        memory=MemoryPalace(
            base_dir=tmp_path / "memory",
            context_dir=tmp_path / "context",
        ),
        registry=registry,
        workspace_root=workspace,
        output_dir=output,
    )


def test_both_spawn_points_share_one_injected_provider(tmp_path):
    from agent import ToolRegistry
    from agent.tools.user_tool_runner import run_user_tool

    provider = RecordingProvider(
        stdout=json.dumps({"ok": True, "result": "hi"}).encode("utf-8")
    )
    registry = ToolRegistry()
    registry.set_context("subprocess_provider", provider)
    registry.set_context("output_dir", str(tmp_path / "output"))
    registry.set_context("workspace_root", str(tmp_path / "workspace"))
    registry.set_context("shell_sandbox_mode", "none")

    tools = _tools(tmp_path, registry)
    asyncio.run(tools._shell("echo hi", intent="exercise the seam"))

    tools_root = tmp_path / "tools"
    tools_root.mkdir()
    (tools_root / "demo.py").write_text(
        textwrap.dedent(
            """
            def register(registry):
                registry.register(
                    "greet", "greets",
                    {"type": "object", "properties": {}},
                    lambda: "hi",
                )
            """
        ),
        encoding="utf-8",
    )
    asyncio.run(
        run_user_tool(
            tools_root / "demo.py",
            "greet",
            {},
            registry=registry,
            root=tools_root,
        )
    )

    assert len(provider.requests) == 2, "one spawn point bypassed the provider"
    shell_request, tool_request = provider.requests
    assert shell_request.argv[:2] == ("/bin/sh", "-c")
    assert "demo.py" in " ".join(tool_request.argv)


def test_the_user_tool_runner_now_registers_for_cancellation(tmp_path):
    """The gap the seam closed.

    Before both call sites shared a provider, only the shell tool registered
    its child with the active cancel token; a runaway user tool survived
    ``/cancel`` until its own timeout expired.
    """
    from agent import ToolRegistry
    from agent.tools.user_tool_runner import run_user_tool

    provider = RecordingProvider(
        stdout=json.dumps({"ok": True, "result": "hi"}).encode("utf-8")
    )
    registry = ToolRegistry()
    registry.set_context("subprocess_provider", provider)
    registry.set_context("output_dir", str(tmp_path / "output"))
    registry.set_context("workspace_root", str(tmp_path / "workspace"))
    registry.set_context("shell_sandbox_mode", "none")

    tools_root = tmp_path / "tools"
    tools_root.mkdir()
    (tools_root / "demo.py").write_text(
        textwrap.dedent(
            """
            def register(registry):
                registry.register(
                    "greet", "greets",
                    {"type": "object", "properties": {}},
                    lambda: "hi",
                )
            """
        ),
        encoding="utf-8",
    )
    asyncio.run(
        run_user_tool(
            tools_root / "demo.py",
            "greet",
            {},
            registry=registry,
            root=tools_root,
        )
    )

    (request,) = provider.requests
    assert request.cancel_label
    assert request.heartbeat_message


def test_tool_input_travels_over_stdin_not_argv(tmp_path):
    """argv is world-readable via `ps`; tool inputs routinely are not."""
    from agent import ToolRegistry
    from agent.tools.user_tool_runner import run_user_tool

    provider = RecordingProvider(
        stdout=json.dumps({"ok": True, "result": "ok"}).encode("utf-8")
    )
    registry = ToolRegistry()
    registry.set_context("subprocess_provider", provider)
    registry.set_context("output_dir", str(tmp_path / "output"))
    registry.set_context("workspace_root", str(tmp_path / "workspace"))
    registry.set_context("shell_sandbox_mode", "none")

    tools_root = tmp_path / "tools"
    tools_root.mkdir()
    (tools_root / "demo.py").write_text(
        textwrap.dedent(
            """
            def register(registry):
                registry.register(
                    "echo", "echoes",
                    {"type": "object", "properties": {"secret": {"type": "string"}}},
                    lambda secret: secret,
                )
            """
        ),
        encoding="utf-8",
    )
    asyncio.run(
        run_user_tool(
            tools_root / "demo.py",
            "echo",
            {"secret": "hunter2"},
            registry=registry,
            root=tools_root,
        )
    )

    (request,) = provider.requests
    assert b"hunter2" in (request.stdin or b"")
    assert "hunter2" not in " ".join(request.argv)


def test_provider_falls_back_to_local_when_nothing_is_injected():
    from agent import ToolRegistry
    from agent.exec import LocalSubprocessProvider

    assert isinstance(provider_from(None), LocalSubprocessProvider)
    assert isinstance(provider_from(ToolRegistry()), LocalSubprocessProvider)


def test_a_timeout_is_reported_not_raised():
    """Both callers translate `timed_out` into their own error payload.

    Returning it rather than raising is why neither call site needs a
    `TimeoutError` handler of its own.
    """
    from agent.exec import LocalSubprocessProvider

    result = asyncio.run(
        LocalSubprocessProvider().run(
            ExecRequest(argv=("/bin/sh", "-c", "sleep 30"), timeout=0.5)
        )
    )

    assert result.timed_out is True


def test_the_whole_process_group_dies_with_a_timed_out_command(tmp_path):
    """A shell command's grandchildren must not outlive it.

    `start_new_session=True` plus a group-wide signal is what makes this
    true; signalling only the direct child leaves the real work running.
    """
    from agent.exec import LocalSubprocessProvider

    marker = tmp_path / "still-alive"
    script = (
        f"(sleep 5; touch {marker}) & "
        "sleep 30"
    )
    result = asyncio.run(
        LocalSubprocessProvider().run(
            ExecRequest(argv=("/bin/sh", "-c", script), timeout=0.5)
        )
    )
    assert result.timed_out is True

    async def _wait_past_the_grandchild() -> None:
        await asyncio.sleep(6)

    asyncio.run(_wait_past_the_grandchild())
    assert not marker.exists(), "a grandchild survived the group kill"
