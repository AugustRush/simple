from __future__ import annotations

import asyncio
import os
import signal
import shlex
from pathlib import Path
from typing import Any, Sequence

from agent.security.shell import shell_command_check

from .models import VerificationResult, VerificationStatus


RALPH_VERIFICATION_OUTPUT_LIMIT = 64 * 1024
RALPH_DEFAULT_VERIFY_TIMEOUT_SECONDS = 60.0
RALPH_VERIFY_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "VIRTUAL_ENV",
)


class RalphVerifier:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        output_dir: str | Path,
        blocked_commands: Sequence[str] = (),
        timeout_seconds: float = RALPH_DEFAULT_VERIFY_TIMEOUT_SECONDS,
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
        try:
            argv = shlex.split(command, posix=True)
        except (TypeError, ValueError) as exc:
            return VerificationResult(
                status=VerificationStatus.REJECTED,
                error=f"Verification command is malformed: {exc}",
            )
        if not argv:
            return VerificationResult(
                status=VerificationStatus.REJECTED,
                error="Verification command cannot be empty",
            )
        inline_reason = _inline_interpreter_reason(argv)
        if inline_reason is not None:
            return VerificationResult(
                status=VerificationStatus.REJECTED,
                error=f"Verification command rejected: {inline_reason}",
            )
        safety = shell_command_check(
            command,
            extra_blocked=list(self.blocked_commands),
            allowed_roots=frozenset((self.workspace_root, self.output_dir)),
        )
        if (
            not safety.allowed
            or safety.risk_level != "low"
            or safety.requires_confirmation
            or safety.reason == "command was confirmed for this session"
        ):
            return VerificationResult(
                status=VerificationStatus.REJECTED,
                error=f"Verification command rejected: {safety.reason}",
            )

        if cancel_token is not None and bool(
            getattr(cancel_token, "is_cancelled", False)
        ):
            return VerificationResult(
                status=VerificationStatus.CANCELLED,
                error="Verification was cancelled before process creation",
            )

        env = {
            name: os.environ[name]
            for name in RALPH_VERIFY_ENV_ALLOWLIST
            if name in os.environ
        }
        env["AGENT_WORKSPACE_ROOT"] = str(self.workspace_root)
        env["AGENT_OUTPUT_DIR"] = str(self.output_dir)
        try:
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

        stdout_tail = _TailBuffer(RALPH_VERIFICATION_OUTPUT_LIMIT)
        stderr_tail = _TailBuffer(RALPH_VERIFICATION_OUTPUT_LIMIT)
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
                await self._terminate_process(process, process_wait, force=cancel_level == "force")
                status = VerificationStatus.CANCELLED
                error = "Verification was cancelled"
            elif process_wait in done:
                status = (
                    VerificationStatus.PASSED
                    if process.returncode == 0
                    else VerificationStatus.FAILED
                )
            else:
                await self._terminate_process(process, process_wait)
                status = VerificationStatus.TIMEOUT
                error = f"Verification timed out after {self.timeout_seconds:g} seconds"
            await self._finish_drains(stdout_task, stderr_task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self._terminate_process(process, process_wait))
                await asyncio.shield(self._finish_drains(stdout_task, stderr_task))
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                await asyncio.shield(self._terminate_process(process, process_wait))
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
        *,
        force: bool = False,
    ) -> None:
        if process.returncode is not None:
            await _reap(process_wait)
            return

        first_signal = signal.SIGKILL if force else signal.SIGTERM
        _signal_process_group(process, first_signal)
        if not force:
            try:
                await asyncio.wait_for(
                    asyncio.shield(process_wait),
                    timeout=self.termination_grace_seconds,
                )
            except asyncio.TimeoutError:
                _signal_process_group(process, signal.SIGKILL)
        await _reap(process_wait)

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
    sig: signal.Signals,
) -> None:
    try:
        os.killpg(process.pid, sig)
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


async def _reap(process_wait: asyncio.Task[int]) -> None:
    try:
        await asyncio.shield(process_wait)
    except (ProcessLookupError, PermissionError):
        pass


def _inline_interpreter_reason(argv: list[str]) -> str | None:
    inline_flags = {
        "python": ("-c",),
        "python2": ("-c",),
        "python3": ("-c",),
        "perl": ("-e",),
        "ruby": ("-e",),
        "node": ("-e", "--eval"),
        "php": ("-r",),
        "bash": ("-c",),
        "sh": ("-c",),
        "zsh": ("-c",),
    }
    for index, token in enumerate(argv):
        executable = Path(token).name
        flags = inline_flags.get(executable)
        if flags is None:
            continue
        for argument in argv[index + 1 :]:
            if any(argument == flag or argument.startswith(flag) for flag in flags):
                return f"inline interpreter execution via '{executable}' is not allowed"
    return None


__all__ = [
    "RALPH_DEFAULT_VERIFY_TIMEOUT_SECONDS",
    "RALPH_VERIFICATION_OUTPUT_LIMIT",
    "RALPH_VERIFY_ENV_ALLOWLIST",
    "RalphVerifier",
]
