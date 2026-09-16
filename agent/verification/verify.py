"""Run one command and read its exit code, under an unattended envelope.

The safety posture is inherited unchanged from the Ralph design: verification
runs *less* privileged than an interactive shell, not more.  Only commands the
shell safety gate classifies as low risk, never through an inline interpreter,
with a curated environment rather than the process's own.

The distinction this module exists to preserve is between the two halves of a
refusal.  ``command_rejection_reason`` answers "could this command ever be used
as a check?" *without running it*, which is what a caller needs at the moment
somebody writes a task down.  :meth:`CommandVerifier.verify` asks the same
question again before running, because the answer can change between writing a
task and its 3am run, and returns ``REJECTED`` rather than raising.

One rule, two moments, no second implementation to drift.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import shlex
from pathlib import Path
from typing import Any, Callable, Sequence

from agent.security.shell import shell_command_check

from .models import VerificationResult, VerificationStatus


VERIFICATION_OUTPUT_LIMIT = 64 * 1024
DEFAULT_VERIFY_TIMEOUT_SECONDS = 60.0
VERIFY_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "VIRTUAL_ENV",
)


def command_rejection_reason(
    command: str,
    *,
    workspace_root: str | Path,
    output_dir: str | Path,
    blocked_commands: Sequence[str] = (),
) -> str | None:
    """Why *command* can never serve as an acceptance check, or ``None``.

    Decidable without running anything, which is the point: a caller writing a
    task down can refuse a check that would never have been allowed to run,
    while somebody is still looking at the screen, instead of discovering it at
    3am in a run that then has no verdict at all.
    """
    try:
        argv = shlex.split(str(command or ""), posix=True)
    except (TypeError, ValueError) as exc:
        return f"验收命令无法解析：{exc}"
    if not argv:
        return "验收命令不能为空"
    inline_reason = _inline_interpreter_reason(argv)
    if inline_reason is not None:
        return f"验收命令被拒绝：{inline_reason}"
    safety = shell_command_check(
        str(command or ""),
        extra_blocked=list(blocked_commands),
        allowed_roots=frozenset(
            (
                Path(workspace_root).expanduser().resolve(strict=False),
                Path(output_dir).expanduser().resolve(strict=False),
            )
        ),
    )
    return _refusal_reason(safety)


#: ``shell_command_check`` fills ``reason`` with a *refusal* explanation only on
#: the branches that refuse.  The path that falls through to the end returns
#: this string whatever the risk level turned out to be, so quoting it back
#: would produce "refused: command is allowed" -- a message that names the
#: opposite of what happened.  Recognised, and replaced by a description of
#: what actually stopped the command.
_GENERIC_ALLOWED_REASON = "command is allowed"


def _refusal_reason(safety: Any) -> str | None:
    """The reason this command cannot be a check, in the reader's terms.

    Composed from whichever condition actually refused it rather than from
    ``safety.reason``, because that field is populated on some refusing
    branches and misleading on the rest.  The reader here is whoever wrote the
    criterion and now has to change it, so every branch has to say what to
    change.
    """
    if not safety.allowed:
        return f"验收命令被拒绝：{safety.reason}"
    if safety.reason == "command was confirmed for this session":
        # A confirmation is a fact about one interactive use, not a property
        # of the command itself.  Nobody will be there to confirm it at 3am,
        # and a check that only runs when somebody is watching is not a check.
        return (
            "验收命令被拒绝：该命令只是本次会话中被人工确认过一次，"
            "本身并不在允许范围内"
        )
    detail = "" if safety.reason == _GENERIC_ALLOWED_REASON else f"（{safety.reason}）"
    if safety.requires_confirmation:
        return f"验收命令被拒绝：该命令需要人工确认{detail}，而验收时没有人在场"
    if safety.risk_level != "low":
        return (
            f"验收命令被拒绝：该命令的风险等级是 {safety.risk_level}{detail}，"
            "验收只接受低风险命令"
        )
    return None


class CommandVerifier:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        output_dir: str | Path,
        blocked_commands: Sequence[str] = (),
        timeout_seconds: float = DEFAULT_VERIFY_TIMEOUT_SECONDS,
        termination_grace_seconds: float = 2.0,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        self.output_dir = Path(output_dir).expanduser().resolve(strict=False)
        self.blocked_commands = tuple(str(item) for item in blocked_commands)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if termination_grace_seconds < 0:
            raise ValueError("termination_grace_seconds cannot be negative")
        self.timeout_seconds = float(timeout_seconds)
        self.termination_grace_seconds = float(termination_grace_seconds)

    async def verify(
        self,
        command: str,
        *,
        cancel_token: Any = None,
    ) -> VerificationResult:
        reason = command_rejection_reason(
            command,
            workspace_root=self.workspace_root,
            output_dir=self.output_dir,
            blocked_commands=self.blocked_commands,
        )
        if reason is not None:
            return VerificationResult(
                status=VerificationStatus.REJECTED,
                error=reason,
            )

        if cancel_token is not None and bool(
            getattr(cancel_token, "is_cancelled", False)
        ):
            return VerificationResult(
                status=VerificationStatus.CANCELLED,
                error="Verification was cancelled before process creation",
            )

        # Already parsed once inside ``command_rejection_reason`` and found
        # well formed, so this cannot raise -- re-splitting rather than
        # threading the argv out keeps that function's signature about the
        # question it answers ("may this run?") instead of its by-products.
        argv = shlex.split(command, posix=True)

        env = {
            name: os.environ[name]
            for name in VERIFY_ENV_ALLOWLIST
            if name in os.environ
        }
        env["AGENT_WORKSPACE_ROOT"] = str(self.workspace_root)
        env["AGENT_OUTPUT_DIR"] = str(self.output_dir)
        try:
            # Not yet on the `agent.exec` provider seam. It would fit — this
            # wants the same group teardown — but the verifier deliberately
            # runs unsandboxed with a curated env allowlist, so moving it is a
            # separate change with its own security argument to make.
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.workspace_root,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            return VerificationResult(
                status=VerificationStatus.SETUP_ERROR,
                error=f"Unable to start verification command: {exc}",
            )
        try:
            process_group_id = os.getpgid(process.pid)
        except (ProcessLookupError, PermissionError):
            process_group_id = process.pid

        stdout_tail = _TailBuffer(VERIFICATION_OUTPUT_LIMIT)
        stderr_tail = _TailBuffer(VERIFICATION_OUTPUT_LIMIT)
        stdout_task = asyncio.create_task(_drain_stream(process.stdout, stdout_tail))
        stderr_task = asyncio.create_task(_drain_stream(process.stderr, stderr_tail))
        process_wait = asyncio.create_task(process.wait())
        cancel_event = asyncio.Event()
        cancel_level = "graceful"
        loop = asyncio.get_running_loop()

        def _token_cancelled(level: str) -> None:
            nonlocal cancel_level
            cancel_level = level
            loop.call_soon_threadsafe(cancel_event.set)

        def deregister() -> None:
            return None

        cancel_wait: asyncio.Task[bool] | None = None
        register_cleanup = getattr(cancel_token, "register_cleanup", None)

        status: VerificationStatus
        error: str | None = None
        try:
            if callable(register_cleanup):
                deregister = register_cleanup("ralph-verifier", _token_cancelled)
                cancel_wait = asyncio.create_task(cancel_event.wait())
            waiters: set[asyncio.Task[Any]] = {process_wait}
            if cancel_wait is not None:
                waiters.add(cancel_wait)
            done, _pending = await asyncio.wait(
                waiters,
                timeout=self.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_wait is not None and cancel_wait in done:
                await self._terminate_process(
                    process,
                    process_wait,
                    process_group_id,
                    force=cancel_level == "force",
                    force_requested=lambda: cancel_level == "force",
                )
                status = VerificationStatus.CANCELLED
                error = "Verification was cancelled"
            elif process_wait in done:
                status = (
                    VerificationStatus.PASSED
                    if process.returncode == 0
                    else VerificationStatus.FAILED
                )
            else:
                await self._terminate_process(process, process_wait, process_group_id)
                status = VerificationStatus.TIMEOUT
                error = f"Verification timed out after {self.timeout_seconds:g} seconds"
            await self._finish_drains(stdout_task, stderr_task)
        except asyncio.CancelledError:
            try:
                await _await_cleanup_despite_cancellation(
                    self._cleanup_process(
                        process,
                        process_wait,
                        process_group_id,
                        stdout_task,
                        stderr_task,
                        force=cancel_level == "force",
                        force_requested=lambda: cancel_level == "force",
                    )
                )
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                await asyncio.shield(
                    self._terminate_process(process, process_wait, process_group_id)
                )
                await asyncio.shield(self._finish_drains(stdout_task, stderr_task))
            except Exception:
                for task in (stdout_task, stderr_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            status = VerificationStatus.SETUP_ERROR
            error = f"Verification infrastructure failed: {exc}"
        finally:
            deregister()
            if cancel_wait is not None and not cancel_wait.done():
                cancel_wait.cancel()
            if not process_wait.done():
                process_wait.cancel()

        return VerificationResult(
            status=status,
            exit_code=(
                process.returncode
                if status in (VerificationStatus.PASSED, VerificationStatus.FAILED)
                else None
            ),
            stdout_tail=stdout_tail.text(),
            stderr_tail=stderr_tail.text(),
            error=error,
        )

    async def _terminate_process(
        self,
        process: asyncio.subprocess.Process,
        process_wait: asyncio.Task[int],
        process_group_id: int,
        *,
        force: bool = False,
        force_requested: Callable[[], bool] | None = None,
    ) -> None:
        first_signal = signal.SIGKILL if force else signal.SIGTERM
        _signal_process_group(process, process_group_id, first_signal)
        if not force:
            group_exited = await _wait_for_process_group_exit(
                process_group_id,
                self.termination_grace_seconds,
                force_requested=force_requested,
            )
            if not group_exited:
                _signal_process_group(process, process_group_id, signal.SIGKILL)
        await _reap(process_wait)

    async def _cleanup_process(
        self,
        process: asyncio.subprocess.Process,
        process_wait: asyncio.Task[int],
        process_group_id: int,
        stdout_task: asyncio.Task[None],
        stderr_task: asyncio.Task[None],
        *,
        force: bool = False,
        force_requested: Callable[[], bool] | None = None,
    ) -> None:
        await self._terminate_process(
            process,
            process_wait,
            process_group_id,
            force=force,
            force_requested=force_requested,
        )
        await self._finish_drains(stdout_task, stderr_task)

    async def _finish_drains(self, *tasks: asyncio.Task[None]) -> None:
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=max(self.termination_grace_seconds, 0.1),
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise RuntimeError(f"verification output read failed: {result}")
        except asyncio.TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class _TailBuffer:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()

    def append(self, chunk: bytes) -> None:
        self.data.extend(chunk)
        excess = len(self.data) - self.limit
        if excess > 0:
            del self.data[:excess]

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")


async def _drain_stream(
    stream: asyncio.StreamReader | None,
    tail: _TailBuffer,
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return
        tail.append(chunk)


def _signal_process_group(
    process: asyncio.subprocess.Process,
    process_group_id: int,
    sig: signal.Signals,
) -> None:
    try:
        os.killpg(process_group_id, sig)
        return
    except ProcessLookupError:
        return
    except PermissionError:
        pass
    try:
        if sig == signal.SIGKILL:
            process.kill()
        else:
            process.terminate()
    except (ProcessLookupError, PermissionError):
        pass


def _process_group_is_alive(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def _wait_for_process_group_exit(
    process_group_id: int,
    timeout: float,
    *,
    force_requested: Callable[[], bool] | None = None,
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while _process_group_is_alive(process_group_id):
        if force_requested is not None and force_requested():
            return False
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.01, remaining))
    return True


async def _reap(process_wait: asyncio.Task[int]) -> None:
    try:
        await asyncio.shield(process_wait)
    except (ProcessLookupError, PermissionError):
        pass


async def _await_cleanup_despite_cancellation(cleanup: Any) -> None:
    cleanup_task = asyncio.create_task(cleanup)
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            continue
    await cleanup_task


def _inline_interpreter_reason(argv: list[str]) -> str | None:
    effective_argv = _effective_command_argv(argv)
    if not effective_argv:
        return None
    executable = Path(effective_argv[0]).name
    inline_flags = {
        "perl": ("-e",),
        "ruby": ("-e",),
        "node": ("-e", "--eval"),
        "php": ("-r",),
        "bash": ("-c",),
        "sh": ("-c",),
        "zsh": ("-c",),
    }
    flags = (
        ("-c",)
        if re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?", executable)
        else inline_flags.get(executable)
    )
    if flags is None:
        return None
    for argument in effective_argv[1:]:
        if any(argument == flag or argument.startswith(flag) for flag in flags):
            return f"inline interpreter execution via '{executable}' is not allowed"
    return None


def _effective_command_argv(argv: list[str]) -> list[str]:
    effective = argv
    while effective and Path(effective[0]).name == "env":
        env_executable = effective[0]
        index = 1
        split_command: list[str] | None = None
        while index < len(effective):
            token = effective[index]
            if token == "--":
                index += 1
                break
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
                index += 1
                continue
            if token in (
                "--ignore-environment",
                "--null",
                "--debug",
                "--list-signal-handling",
            ):
                index += 1
                continue
            if token in (
                "--unset",
                "--chdir",
                "--argv0",
                "--block-signal",
                "--default-signal",
                "--ignore-signal",
            ):
                index += 2
                continue
            if token == "--split-string":
                if index + 1 >= len(effective):
                    return []
                try:
                    split_command = shlex.split(effective[index + 1], posix=True)
                except ValueError:
                    return []
                index += 2
                break
            if token.startswith("--split-string="):
                try:
                    split_command = shlex.split(token.partition("=")[2], posix=True)
                except ValueError:
                    return []
                index += 1
                break
            short_options = _parse_env_short_options(effective, index)
            if short_options is not None:
                index, split_source = short_options
                if split_source is None:
                    continue
                try:
                    split_command = shlex.split(split_source, posix=True)
                except ValueError:
                    return []
                break
            if token.startswith("-") and "=" in token:
                index += 1
                continue
            break
        suffix = effective[index:]
        effective = (
            [env_executable, *split_command, *suffix]
            if split_command is not None
            else suffix
        )
    return effective


def _parse_env_short_options(
    argv: list[str],
    index: int,
) -> tuple[int, str | None] | None:
    token = argv[index]
    if token == "-":
        return index + 1, None
    if not token.startswith("-") or token.startswith("--"):
        return None
    cluster = token[1:]
    position = 0
    while position < len(cluster):
        option = cluster[position]
        if option in "i0v":
            position += 1
            continue
        if option in "uPCa":
            if position + 1 < len(cluster):
                return index + 1, None
            return min(index + 2, len(argv)), None
        if option == "S":
            if position + 1 < len(cluster):
                return index + 1, cluster[position + 1 :]
            if index + 1 < len(argv):
                return index + 2, argv[index + 1]
            return len(argv), None
        return None
    return index + 1, None


__all__ = [
    "DEFAULT_VERIFY_TIMEOUT_SECONDS",
    "VERIFICATION_OUTPUT_LIMIT",
    "VERIFY_ENV_ALLOWLIST",
    "CommandVerifier",
    "command_rejection_reason",
]
