from __future__ import annotations

import asyncio
import json
import os
import pytest
import signal
import time
from types import SimpleNamespace


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


class _RalphMemoryStore:
    def __init__(self, task=None, *, fail_on_save: int | None = None):
        self.task = task
        self.saved = []
        self.fail_on_save = fail_on_save

    def save(self, task):
        from agent.ralph import RalphTaskStoreIOError

        if self.fail_on_save is not None and len(self.saved) + 1 == self.fail_on_save:
            raise RalphTaskStoreIOError("save", "disk unavailable")
        snapshot = type(task).from_dict(task.to_dict())
        self.saved.append(snapshot)
        self.task = snapshot

    def load(self, task_ref):
        return type(self.task).from_dict(self.task.to_dict())


def _ralph_state(*, token=None, model="session-model", pending=None):
    return SimpleNamespace(
        cancel_token=token,
        model_override=model,
        pending_interjections=pending if pending is not None else [],
    )


def test_ralph_service_completes_by_promise_without_verifier():
    from agent.ralph import (
        RALPH_COMPLETION_PROMISE,
        RalphIterationResult,
        RalphService,
        RalphTask,
        RalphTaskStatus,
    )

    store = _RalphMemoryStore()

    async def execute(context, prompt, *, cancel_token, model_override):
        assert cancel_token is None
        assert model_override == "session-model"
        return RalphIterationResult(1, f"done {RALPH_COMPLETION_PROMISE}", ("write",))

    task = RalphTask(id="promise", goal="finish the work", max_iterations=3)
    store.save(task)
    result = asyncio.run(
        RalphService(turn_executor=execute, store=store).run(task, _ralph_state())
    )

    assert result.durability_error is None
    assert result.task.status is RalphTaskStatus.COMPLETE
    assert result.task.current_iteration == 1
    assert result.task.iterations[-1].completed_by == "promise"


def test_ralph_service_verifier_is_authoritative_and_feeds_diagnostics_forward():
    from agent.ralph import (
        RALPH_COMPLETION_PROMISE,
        RalphIterationResult,
        RalphService,
        RalphTask,
        RalphTaskStatus,
        VerificationResult,
        VerificationStatus,
    )

    prompts = []
    verification = iter(
        [
            VerificationResult(
                VerificationStatus.FAILED,
                exit_code=7,
                stderr_tail="first failure",
            ),
            VerificationResult(VerificationStatus.PASSED, exit_code=0),
        ]
    )

    async def execute(context, prompt, **kwargs):
        prompts.append(prompt)
        return RalphIterationResult(
            len(prompts), f"claimed {RALPH_COMPLETION_PROMISE}"
        )

    class Verifier:
        async def verify(self, command, *, cancel_token=None):
            assert command == "pytest -q"
            return next(verification)

    task = RalphTask(
        id="verify",
        goal="pass tests",
        verify_command="pytest -q",
        max_iterations=3,
    )
    store = _RalphMemoryStore()
    store.save(task)
    result = asyncio.run(
        RalphService(turn_executor=execute, store=store, verifier=Verifier()).run(
            task, _ralph_state()
        )
    )

    assert result.task.status is RalphTaskStatus.COMPLETE
    assert result.task.current_iteration == 2
    assert result.task.iterations[0].completed_by is None
    assert result.task.iterations[1].completed_by == "verify_command"
    assert "first failure" in prompts[1]


@pytest.mark.parametrize("verification_status", ["timeout", "failed"])
def test_ralph_service_nonpassing_verifier_reaches_iteration_limit(
    verification_status,
):
    from agent.ralph import (
        RalphIterationResult,
        RalphService,
        RalphTask,
        RalphTaskStatus,
        VerificationResult,
        VerificationStatus,
    )

    status = VerificationStatus(verification_status)

    async def execute(context, prompt, **kwargs):
        return RalphIterationResult(1, "not yet")

    class Verifier:
        async def verify(self, command, *, cancel_token=None):
            return VerificationResult(
                status,
                exit_code=2 if status is VerificationStatus.FAILED else None,
                error="timed out" if status is VerificationStatus.TIMEOUT else None,
            )

    task = RalphTask(
        id=f"limit-{verification_status}",
        goal="work",
        verify_command="pytest",
        max_iterations=1,
    )
    store = _RalphMemoryStore()
    store.save(task)
    result = asyncio.run(
        RalphService(turn_executor=execute, store=store, verifier=Verifier()).run(
            task, _ralph_state()
        )
    )

    assert result.task.status is RalphTaskStatus.MAX_ITERATIONS_REACHED
    assert result.task.iterations[0].verification.status is status


@pytest.mark.parametrize("failure_source", ["return", "raise", "verifier"])
def test_ralph_service_persists_infrastructure_failures(failure_source):
    from agent.ralph import (
        RalphIterationResult,
        RalphService,
        RalphTask,
        RalphTaskStatus,
        VerificationResult,
        VerificationStatus,
    )

    async def execute(context, prompt, **kwargs):
        if failure_source == "raise":
            raise RuntimeError("transport exploded")
        return RalphIterationResult(
            1,
            "partial",
            error="provider failed" if failure_source == "return" else None,
        )

    class Verifier:
        async def verify(self, command, *, cancel_token=None):
            return VerificationResult(
                VerificationStatus.SETUP_ERROR,
                error="spawn failed",
            )

    task = RalphTask(
        id=f"failure-{failure_source}",
        goal="work",
        verify_command="pytest" if failure_source == "verifier" else None,
    )
    store = _RalphMemoryStore()
    store.save(task)
    result = asyncio.run(
        RalphService(
            turn_executor=execute,
            store=store,
            verifier=Verifier() if failure_source == "verifier" else None,
        ).run(task, _ralph_state())
    )

    assert result.task.status is RalphTaskStatus.FAILED
    assert store.task.status is RalphTaskStatus.FAILED
    assert result.task.last_error


def test_ralph_service_cancellation_is_interrupted_and_durable():
    from agent.ralph import RalphService, RalphTask, RalphTaskStatus
    from agent.shared import CancelToken

    token = CancelToken()
    token.cancel("graceful")

    async def forbidden(*args, **kwargs):
        raise AssertionError("cancelled run executed a turn")

    task = RalphTask(id="cancelled", goal="work")
    store = _RalphMemoryStore()
    store.save(task)
    result = asyncio.run(
        RalphService(turn_executor=forbidden, store=store).run(
            task, _ralph_state(token=token)
        )
    )

    assert result.task.status is RalphTaskStatus.INTERRUPTED
    assert store.task.status is RalphTaskStatus.INTERRUPTED
    assert result.task.current_iteration == 0


def test_ralph_service_caller_cancellation_persists_before_returning():
    from agent.ralph import RalphService, RalphTask, RalphTaskStatus

    entered = asyncio.Event()

    async def execute(context, prompt, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    task = RalphTask(id="caller-cancel", goal="work")
    store = _RalphMemoryStore()
    store.save(task)

    async def scenario():
        pending = asyncio.create_task(
            RalphService(turn_executor=execute, store=store).run(
                task, _ralph_state()
            )
        )
        await entered.wait()
        pending.cancel()
        asyncio.get_running_loop().call_soon(pending.cancel)
        return await pending

    result = asyncio.run(scenario())

    assert result.task.status is RalphTaskStatus.INTERRUPTED
    assert store.task.status is RalphTaskStatus.INTERRUPTED


def test_ralph_service_internal_setup_exception_becomes_durable_failure():
    from agent.ralph import RalphService, RalphTask, RalphTaskStatus

    async def forbidden(*args, **kwargs):
        raise AssertionError("turn executor should not be reached")

    def broken_context():
        raise RuntimeError("context setup failed")

    task = RalphTask(id="setup-failure", goal="work")
    store = _RalphMemoryStore()
    store.save(task)
    result = asyncio.run(
        RalphService(
            turn_executor=forbidden,
            store=store,
            context_factory=broken_context,
        ).run(task, _ralph_state())
    )

    assert result.task.status is RalphTaskStatus.FAILED
    assert store.task.status is RalphTaskStatus.FAILED
    assert "context setup failed" in result.task.last_error


def test_ralph_service_persists_before_observer_and_ignores_observer_errors():
    from agent.ralph import RalphIterationResult, RalphService, RalphTask

    store = _RalphMemoryStore()
    observed = []

    async def execute(context, prompt, **kwargs):
        return RalphIterationResult(1, "continue")

    def observer(event):
        observed.append((event.kind, store.task.current_iteration, store.task.status))
        raise RuntimeError("delivery failed")

    task = RalphTask(id="observer", goal="work", max_iterations=1)
    store.save(task)
    result = asyncio.run(
        RalphService(turn_executor=execute, store=store, observer=observer).run(
            task, _ralph_state()
        )
    )

    assert result.durability_error is None
    assert observed[0][1] == 1
    assert observed[-1][2].value == "max_iterations_reached"


def test_ralph_service_store_failure_returns_last_durable_truth():
    from agent.ralph import RalphIterationResult, RalphService, RalphTask, RalphTaskStatus

    async def execute(context, prompt, **kwargs):
        return RalphIterationResult(1, "<promise>COMPLETE</promise>")

    task = RalphTask(id="durability", goal="work")
    store = _RalphMemoryStore(fail_on_save=2)
    store.save(task)
    result = asyncio.run(
        RalphService(turn_executor=execute, store=store).run(task, _ralph_state())
    )

    assert result.durability_error and "disk unavailable" in result.durability_error
    assert result.task.status is RalphTaskStatus.RUNNING
    assert result.task.current_iteration == 0


def test_ralph_service_resume_starts_after_durable_cursor_and_rejects_terminal():
    from agent.ralph import (
        RalphIterationResult,
        RalphService,
        RalphTask,
        RalphTaskStatus,
        RalphValidationError,
    )

    seen = []

    async def execute(context, prompt, **kwargs):
        seen.append(prompt)
        return RalphIterationResult(3, "done <promise>COMPLETE</promise>")

    task = RalphTask(
        id="resume",
        goal="work",
        current_iteration=2,
        max_iterations=4,
    )
    store = _RalphMemoryStore(task)
    result = asyncio.run(
        RalphService(turn_executor=execute, store=store).resume(
            "resume", _ralph_state()
        )
    )
    assert result.task.current_iteration == 3
    assert "iteration 3 of 4" in seen[0].lower()

    for status in (RalphTaskStatus.COMPLETE, RalphTaskStatus.MAX_ITERATIONS_REACHED):
        store.task.status = status
        with pytest.raises(RalphValidationError) as exc_info:
            asyncio.run(
                RalphService(turn_executor=execute, store=store).resume(
                    "resume", _ralph_state()
                )
            )
        assert exc_info.value.code == "task_not_resumable"


def test_ralph_service_interjections_keep_shared_queue_identity_and_order():
    from agent.ralph import RalphIterationResult, RalphService, RalphTask

    pending = [
        {"text": "first", "urgency": "normal"},
        {"text": "second", "urgency": "urgent"},
    ]
    queue_id = id(pending)
    prompts = []

    async def execute(context, prompt, **kwargs):
        prompts.append(prompt)
        assert context.metadata["pending_messages"] is pending
        assert id(context.metadata["pending_messages"]) == queue_id
        if len(prompts) == 1:
            pending.append({"text": "during turn", "urgency": "normal"})
        return RalphIterationResult(len(prompts), "continue")

    task = RalphTask(id="mailbox", goal="work", max_iterations=2)
    store = _RalphMemoryStore()
    store.save(task)
    asyncio.run(
        RalphService(turn_executor=execute, store=store).run(
            task, _ralph_state(pending=pending)
        )
    )

    assert "first" in prompts[0] and "second" in prompts[0]
    assert prompts[0].index("first") < prompts[0].index("second")
    assert "urgent" in prompts[0]
    assert "during turn" in prompts[1]
    assert pending == []


def test_ralph_service_stages_memory_once_after_run():
    from agent.ralph import RalphIterationResult, RalphService, RalphTask

    class Staging:
        def __init__(self):
            self.entries = []

        def append(self, role, content):
            self.entries.append((role, content))

    manager = SimpleNamespace(
        staging=Staging(),
        mark_activity=lambda: None,
        should_enqueue_consolidation=lambda: True,
        enqueue_consolidation=lambda reason: manager.jobs.append(reason),
        jobs=[],
    )

    async def execute(context, prompt, **kwargs):
        return RalphIterationResult(1, "done <promise>COMPLETE</promise>")

    task = RalphTask(id="memory", goal="work")
    store = _RalphMemoryStore()
    store.save(task)
    asyncio.run(
        RalphService(
            turn_executor=execute,
            store=store,
            context_manager=manager,
        ).run(task, _ralph_state())
    )

    assert [role for role, _ in manager.staging.entries] == ["user", "assistant"]
    assert manager.jobs == ["ralph_task_end"]
