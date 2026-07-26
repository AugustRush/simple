from __future__ import annotations

import asyncio
import json
import os
import pytest
import signal
import time


def test_parser_builds_typed_start_command_from_quoted_tokens():
    from agent.ralph import RalphStartCommand, parse_ralph_command

    result = parse_ralph_command(
        'fix the "failing parser" --max 7 --verify "pytest tests/test_parser.py -q"'
    )

    assert result == RalphStartCommand(
        goal="fix the failing parser",
        max_iterations=7,
        verify_command="pytest tests/test_parser.py -q",
    )


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "empty_goal"),
        ('"unterminated', "malformed_quotes"),
        ("goal --max", "missing_option_value"),
        ("goal --max nope", "invalid_max_iterations"),
        ("goal --max 0", "max_iterations_out_of_range"),
        ("goal --max 101", "max_iterations_out_of_range"),
        ("goal --verify", "missing_option_value"),
        ("goal --unknown value", "unknown_option"),
        ("goal -x", "unknown_option"),
        ("list now", "unexpected_arguments"),
        ("resume", "invalid_resume"),
        ("resume abc extra", "invalid_resume"),
    ],
)
def test_parser_rejects_invalid_commands_with_stable_codes(text, code):
    from agent.ralph import RalphParseError, parse_ralph_command

    with pytest.raises(RalphParseError) as exc_info:
        parse_ralph_command(text)

    assert exc_info.value.code == code


def test_parser_returns_typed_list_and_resume_commands():
    from agent.ralph import RalphListCommand, RalphResumeCommand, parse_ralph_command

    assert parse_ralph_command("list") == RalphListCommand()
    assert parse_ralph_command("resume abc123") == RalphResumeCommand("abc123")


def test_models_load_legacy_task_json_with_stable_defaults_and_round_trip():
    from agent.ralph import RALPH_COMPLETION_PROMISE, RalphTask, RalphTaskStatus

    legacy = {
        "id": "abc123",
        "goal": "repair parser",
        "completion_criteria": ["tests pass"],
        "verify_command": "pytest -q",
        "completion_promise": RALPH_COMPLETION_PROMISE,
        "max_iterations": 10,
        "current_iteration": 3,
        "status": "running",
        "progress": [{"iteration": 3, "summary": "still failing"}],
        "created_at": "2026-07-26T00:00:00+00:00",
    }

    task = RalphTask.from_dict(legacy)

    assert task.status is RalphTaskStatus.RUNNING
    assert task.current_iteration == 3
    assert task.last_error is None
    assert task.iterations == []
    assert task.to_dict() == {**legacy, "last_error": None, "iterations": []}


def test_agent_legacy_ralph_exports_delegate_to_new_domain(monkeypatch, tmp_path):
    import agent
    from agent.ralph import (
        RALPH_COMPLETION_PROMISE,
        RALPH_DEFAULT_MAX_ITERATIONS,
        RalphTask,
    )

    monkeypatch.setattr(agent, "TASKS_DIR", tmp_path / "tasks")
    task = RalphTask(id="legacy123", goal="keep CLI working")

    assert agent.RalphTask is RalphTask
    assert agent.RALPH_COMPLETION_PROMISE == RALPH_COMPLETION_PROMISE
    assert agent.RALPH_DEFAULT_MAX_ITERATIONS == RALPH_DEFAULT_MAX_ITERATIONS
    agent._save_ralph_task(task)
    assert agent._load_ralph_task("legacy123") == task
    assert agent._load_ralph_task("missing") is None


def test_store_persists_cursor_and_resolves_exact_or_unique_prefix(tmp_path):
    from agent.ralph import RalphTask, RalphTaskStore

    store = RalphTaskStore(tmp_path / "tasks")
    first = RalphTask(id="abc123", goal="first")
    second = RalphTask(id="abc999", goal="second")
    store.save(second)
    store.save(first)
    first.current_iteration = 4
    store.save(first)

    assert store.load("abc123").current_iteration == 4
    assert store.load("abc1").id == "abc123"
    assert [task.id for task in store.list_tasks()] == ["abc123", "abc999"]


def test_store_reports_not_found_ambiguous_corrupt_and_symlink_entries(tmp_path):
    from agent.ralph import (
        RalphTask,
        RalphTaskAmbiguousError,
        RalphTaskCorruptError,
        RalphTaskNotFoundError,
        RalphTaskStore,
    )

    store = RalphTaskStore(tmp_path / "tasks")
    store.save(RalphTask(id="abc123", goal="first"))
    store.save(RalphTask(id="abc999", goal="second"))
    with pytest.raises(RalphTaskAmbiguousError) as ambiguous:
        store.load("abc")
    assert ambiguous.value.code == "ambiguous_task_id"
    with pytest.raises(RalphTaskNotFoundError) as missing:
        store.load("missing")
    assert missing.value.code == "task_not_found"

    (store.tasks_dir / "broken.json").write_text("{", encoding="utf-8")
    with pytest.raises(RalphTaskCorruptError) as corrupt:
        store.load("broken")
    assert corrupt.value.code == "corrupt_task"

    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (store.tasks_dir / "linked.json").symlink_to(outside)
    with pytest.raises(RalphTaskCorruptError):
        store.load("linked")


def test_store_rejects_schema_coercion_in_task_json(tmp_path):
    from agent.ralph import RalphTaskCorruptError, RalphTaskStore

    store = RalphTaskStore(tmp_path / "tasks")
    store.tasks_dir.mkdir()
    (store.tasks_dir / "schema123.json").write_text(
        json.dumps(
            {
                "id": "schema123",
                "goal": "do work",
                "completion_criteria": "tests",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RalphTaskCorruptError) as exc_info:
        store.load("schema123")

    assert exc_info.value.code == "corrupt_task"


def test_verifier_rejects_unsafe_commands_without_spawning(monkeypatch, tmp_path):
    from agent.ralph import RalphVerifier, VerificationStatus

    async def forbidden_spawn(*args, **kwargs):
        raise AssertionError("unsafe command reached process creation")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    verifier = RalphVerifier(
        workspace_root=tmp_path,
        output_dir=tmp_path / "output",
        blocked_commands=[],
    )

    result = asyncio.run(verifier.verify("python -c 'print(1)'"))

    assert result.status is VerificationStatus.REJECTED
    assert result.error


@pytest.mark.parametrize(
    "command",
    [
        "python3.11 -c 'print(1)'",
        "/opt/tools/python3.11 -c 'print(1)'",
        "pypy3.10 -c 'print(1)'",
        "env python3.11 -c 'print(1)'",
        "env -iv python3.11 -c 'print(1)'",
        "env -uHOME pypy3.10 -c 'print(1)'",
        "env -iuHOME python3.11 -c 'print(1)'",
        "env -iu HOME pypy3.10 -c 'print(1)'",
        "env CHECK=1 python3.12 -c 'print(1)'",
        "env -S \"python3.11 -c 'print(1)'\"",
        "env -S \"-i python3.11 -c 'print(1)'\"",
        "env --split-string=\"python3.11 -c 'print(1)'\"",
        "/usr/bin/env -i pypy3.10 -c 'print(1)'",
    ],
)
def test_verifier_rejects_versioned_inline_python_without_spawning(
    monkeypatch, tmp_path, command
):
    from agent.ralph import RalphVerifier, VerificationStatus

    async def forbidden_spawn(*args, **kwargs):
        raise AssertionError("inline Python reached process creation")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    verifier = RalphVerifier(
        workspace_root=tmp_path,
        output_dir=tmp_path / "output",
    )

    result = asyncio.run(verifier.verify(command))

    assert result.status is VerificationStatus.REJECTED
    assert result.error and "inline interpreter" in result.error


def test_verifier_does_not_scan_innocent_interpreter_arguments(tmp_path):
    from agent.ralph import RalphVerifier, VerificationStatus

    verifier = RalphVerifier(
        workspace_root=tmp_path,
        output_dir=tmp_path / "output",
    )

    result = asyncio.run(verifier.verify("echo python3.11 -c"))

    assert result.status is VerificationStatus.PASSED
    assert result.stdout_tail == "python3.11 -c\n"


@pytest.mark.parametrize(
    "command",
    [
        "",
        '"unterminated',
        "printf ok | cat",
        "rm old.txt",
        "mkfs disk.img",
        "pytest -q",
        "python -I -c 'print(1)'",
    ],
)
def test_verifier_rejection_matrix_never_spawns(monkeypatch, tmp_path, command):
    from agent.ralph import RalphVerifier, VerificationStatus

    async def forbidden_spawn(*args, **kwargs):
        raise AssertionError("rejected command reached process creation")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    verifier = RalphVerifier(
        workspace_root=tmp_path,
        output_dir=tmp_path / "output",
        blocked_commands=["pytest"],
    )

    result = asyncio.run(verifier.verify(command))

    assert result.status is VerificationStatus.REJECTED


def test_verifier_executes_argv_with_controlled_env_and_bounded_tails(
    monkeypatch, tmp_path
):
    from agent.ralph import (
        RALPH_VERIFICATION_OUTPUT_LIMIT,
        RalphVerifier,
        VerificationStatus,
    )

    workspace = tmp_path / "workspace"
    output_dir = tmp_path / "output"
    workspace.mkdir()
    probe = workspace / "probe"
    probe.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "sys.stdout.write('A' * 70000)\n"
        "print(json.dumps({\n"
        "  'workspace': os.environ.get('AGENT_WORKSPACE_ROOT'),\n"
        "  'output': os.environ.get('AGENT_OUTPUT_DIR'),\n"
        "  'secret': os.environ.get('OPENAI_API_KEY'),\n"
        "}))\n"
        "sys.stderr.write('B' * 70000)\n",
        encoding="utf-8",
    )
    probe.chmod(0o755)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    captured = {}
    real_spawn = asyncio.create_subprocess_exec

    async def recording_spawn(*args, **kwargs):
        captured.update(kwargs)
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_spawn)
    verifier = RalphVerifier(
        workspace_root=workspace,
        output_dir=output_dir,
        timeout_seconds=5,
    )

    result = asyncio.run(verifier.verify("./probe"))

    assert result.status is VerificationStatus.PASSED
    assert result.exit_code == 0
    assert len(result.stdout_tail.encode()) <= RALPH_VERIFICATION_OUTPUT_LIMIT
    assert len(result.stderr_tail.encode()) <= RALPH_VERIFICATION_OUTPUT_LIMIT
    assert result.stderr_tail == "B" * RALPH_VERIFICATION_OUTPUT_LIMIT
    metadata = json.loads(result.stdout_tail[result.stdout_tail.index("{") :])
    assert metadata == {
        "workspace": str(workspace.resolve()),
        "output": str(output_dir.resolve()),
        "secret": None,
    }
    inherited = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "VIRTUAL_ENV")
        if name in os.environ
    }
    assert captured["env"] == {
        **inherited,
        "AGENT_WORKSPACE_ROOT": str(workspace.resolve()),
        "AGENT_OUTPUT_DIR": str(output_dir.resolve()),
    }


def test_verifier_distinguishes_nonzero_setup_and_timeout(tmp_path):
    from agent.ralph import RalphVerifier, VerificationStatus

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    failure = workspace / "failure"
    failure.write_text("#!/bin/sh\nprintf failure\nexit 7\n", encoding="utf-8")
    failure.chmod(0o755)
    sleeper = workspace / "sleeper"
    sleeper.write_text("#!/bin/sh\nprintf started\nsleep 30\n", encoding="utf-8")
    sleeper.chmod(0o755)
    verifier = RalphVerifier(
        workspace_root=workspace,
        output_dir=tmp_path / "output",
        timeout_seconds=5,
        termination_grace_seconds=0.1,
    )
    timeout_verifier = RalphVerifier(
        workspace_root=workspace,
        output_dir=tmp_path / "output",
        timeout_seconds=0.1,
        termination_grace_seconds=0.1,
    )

    failed = asyncio.run(verifier.verify("./failure"))
    setup = asyncio.run(verifier.verify("./does-not-exist"))
    started = time.monotonic()
    timed_out = asyncio.run(timeout_verifier.verify("./sleeper"))

    assert failed.status is VerificationStatus.FAILED
    assert failed.exit_code == 7
    assert failed.stdout_tail == "failure"
    assert setup.status is VerificationStatus.SETUP_ERROR
    assert setup.infrastructure_error is True
    assert timed_out.status is VerificationStatus.TIMEOUT
    assert timed_out.exit_code is None
    assert time.monotonic() - started < 2


def test_verifier_token_cancellation_returns_typed_result(tmp_path):
    from agent.ralph import RalphVerifier, VerificationStatus
    from agent.shared import CancelToken

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sleeper = workspace / "sleeper"
    sleeper.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    sleeper.chmod(0o755)
    verifier = RalphVerifier(
        workspace_root=workspace,
        output_dir=tmp_path / "output",
        timeout_seconds=5,
        termination_grace_seconds=0.1,
    )
    token = CancelToken()

    async def scenario():
        pending = asyncio.create_task(verifier.verify("./sleeper", cancel_token=token))
        await asyncio.sleep(0.05)
        token.cancel()
        return await pending

    result = asyncio.run(scenario())

    assert result.status is VerificationStatus.CANCELLED
    assert result.exit_code is None


def test_verifier_caller_cancellation_propagates_after_cleanup(tmp_path):
    from agent.ralph import RalphVerifier

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sleeper = workspace / "sleeper"
    sleeper.write_text(
        "#!/usr/bin/env python3\n"
        "import signal, time\n"
        "from pathlib import Path\n"
        "def on_term(_signum, _frame):\n"
        "    Path('term-seen').write_text('yes')\n"
        "signal.signal(signal.SIGTERM, on_term)\n"
        "Path('ready').write_text('yes')\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    sleeper.chmod(0o755)
    verifier = RalphVerifier(
        workspace_root=workspace,
        output_dir=tmp_path / "output",
        timeout_seconds=5,
        termination_grace_seconds=0.05,
    )

    async def scenario():
        pending = asyncio.create_task(verifier.verify("./sleeper"))
        for _ in range(200):
            if (workspace / "ready").exists():
                break
            await asyncio.sleep(0.01)
        assert (workspace / "ready").exists()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    started = time.monotonic()
    asyncio.run(scenario())

    assert (workspace / "term-seen").read_text() == "yes"
    assert time.monotonic() - started < 2


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX process groups")
@pytest.mark.parametrize("trigger", ["timeout", "token", "caller"])
def test_verifier_escalates_for_live_process_group_after_leader_exits(
    tmp_path, trigger
):
    from agent.ralph import RalphVerifier, VerificationStatus
    from agent.shared import CancelToken

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    group_parent = workspace / "group-parent"
    group_parent.write_text(
        "#!/usr/bin/env python3\n"
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    os.close(1)\n"
        "    os.close(2)\n"
        "    Path('child-pid').write_text(str(os.getpid()))\n"
        "    while True:\n"
        "        time.sleep(1)\n"
        "Path('ready').write_text('yes')\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    group_parent.chmod(0o755)
    verifier = RalphVerifier(
        workspace_root=workspace,
        output_dir=tmp_path / "output",
        timeout_seconds=2 if trigger == "timeout" else 5,
        termination_grace_seconds=0.15,
    )
    token = CancelToken()
    child_pid: int | None = None
    repeated_cancellations: list[bool] = []

    async def wait_for_file(path):
        for _ in range(200):
            if path.exists():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"process did not create {path.name}")

    async def scenario():
        pending = asyncio.create_task(
            verifier.verify(
                "./group-parent",
                cancel_token=token if trigger == "token" else None,
            )
        )
        await wait_for_file(workspace / "ready")
        await wait_for_file(workspace / "child-pid")
        if trigger == "token":
            token.cancel("graceful")
        elif trigger == "caller":
            pending.cancel()

            def cancel_again():
                repeated_cancellations.append(pending.cancel())

            asyncio.get_running_loop().call_later(0.03, cancel_again)
            with pytest.raises(asyncio.CancelledError):
                await pending
            return None
        return await pending

    try:
        result = asyncio.run(scenario())
        child_pid = int((workspace / "child-pid").read_text())
        if trigger == "timeout":
            assert result is not None and result.status is VerificationStatus.TIMEOUT
        elif trigger == "token":
            assert result is not None and result.status is VerificationStatus.CANCELLED
        else:
            assert repeated_cancellations == [True]
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if child_pid is None and (workspace / "child-pid").exists():
            child_pid = int((workspace / "child-pid").read_text())
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX process groups")
@pytest.mark.parametrize(
    "cancellation_path",
    ["force", "upgrade-from-graceful", "upgrade-and-caller-cancel"],
)
def test_verifier_force_cancellation_kills_process_group_without_grace(
    tmp_path, cancellation_path
):
    from agent.ralph import RalphVerifier, VerificationStatus
    from agent.shared import CancelToken

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    group_parent = workspace / "group-parent"
    group_parent.write_text(
        "#!/usr/bin/env python3\n"
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    os.close(1)\n"
        "    os.close(2)\n"
        "    Path('child-pid').write_text(str(os.getpid()))\n"
        "    while True:\n"
        "        time.sleep(1)\n"
        "Path('ready').write_text('yes')\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    group_parent.chmod(0o755)
    verifier = RalphVerifier(
        workspace_root=workspace,
        output_dir=tmp_path / "output",
        timeout_seconds=5,
        termination_grace_seconds=2,
    )
    token = CancelToken()
    child_pid: int | None = None

    async def scenario():
        pending = asyncio.create_task(verifier.verify("./group-parent", cancel_token=token))
        for _ in range(200):
            if (workspace / "ready").exists() and (workspace / "child-pid").exists():
                break
            await asyncio.sleep(0.01)
        assert (workspace / "ready").exists()
        started = time.monotonic()
        if cancellation_path != "force":
            token.cancel("graceful")
            await asyncio.sleep(0.05)
        token.cancel("force")
        if cancellation_path == "upgrade-and-caller-cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            return None, time.monotonic() - started
        return await pending, time.monotonic() - started

    try:
        result, elapsed = asyncio.run(scenario())
        child_pid = int((workspace / "child-pid").read_text())
        if result is not None:
            assert result.status is VerificationStatus.CANCELLED
        assert elapsed < 0.5
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if child_pid is None and (workspace / "child-pid").exists():
            child_pid = int((workspace / "child-pid").read_text())
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
