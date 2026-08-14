"""Running a child process, as one replaceable capability.

Two call sites spawn children — the shell tool and the user-tool runner — and
both had independently grown the same five concerns: prepend the sandbox argv
prefix, merge its env, start a new session so the whole process group can be
signalled, kill that group on timeout or cancellation, and emit a heartbeat so
a quiet long-running command is not mistaken for a stalled one.

Five concerns duplicated twice is five chances to fix a bug in one copy.  They
had already diverged: the shell tool registered its child with the active
:class:`CancelToken` so ``/cancel`` could kill it, and the user-tool runner did
not — so a runaway user tool survived ``/cancel`` until its own timeout expired.
Routing both through one provider closes that gap by construction rather than
by remembering.

What stays with the callers is *policy*, not mechanism: how to interpret an
exit code, whether to move workspace artifacts, what a sandbox denial should
suggest, how to decode the payload.  The provider knows only how to run a
process and hand back what it produced.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import os
import signal
from typing import Mapping, Protocol, runtime_checkable

from agent import shared
from agent.security.sandbox.policy import SandboxCommand

#: How long a heartbeat waits between progress reports.  Comfortably shorter
#: than the executor's stale-tool timeout, so a command producing no output
#: never looks stalled.
DEFAULT_HEARTBEAT_SECONDS = 10.0

#: Grace period between SIGTERM and SIGKILL when tearing a child down.
_TERMINATE_GRACE_SECONDS = 1.0

#: Cap on combined stdout+stderr the provider buffers per child.  A runaway
#: command (``cat /dev/urandom | base64``) must not OOM the agent long before
#: its timeout; excess bytes are drained and discarded so the child never
#: blocks on a full pipe, while the timeout still governs truly runaway
#: commands.
OUTPUT_MAX_BYTES = 4 * 1024 * 1024
_READ_CHUNK = 64 * 1024


async def _communicate_bounded(
    process: "asyncio.subprocess.Process",
    input_data: bytes | None,
    *,
    max_bytes: int,
) -> tuple[bytes, bytes, bool]:
    """``communicate()`` with a cap on buffered stdout/stderr.

    Returns ``(stdout, stderr, truncated)``.  Draining (rather than stopping)
    keeps the child from blocking on a full pipe, so the caller's timeout
    still governs runaway commands while memory stays bounded.
    """
    # Test doubles (and exotic wrappers) may only implement communicate()
    # without the StreamReader plumbing.  Fall back there — the bounded path
    # is a hardening, not a protocol requirement, and only real children can
    # genuinely overflow.
    if not hasattr(process, "stdout") or not hasattr(process, "stderr"):
        stdout, stderr = await process.communicate()
        return stdout or b"", stderr or b"", False

    if process.stdin is not None:
        try:
            if input_data is not None:
                process.stdin.write(input_data)
                await process.stdin.drain()
        finally:
            process.stdin.close()

    async def _pump(
        stream: "asyncio.StreamReader | None",
    ) -> tuple[bytes, int, bool]:
        if stream is None:
            return b"", 0, False
        chunks: list[bytes] = []
        buffered = 0
        total = 0
        while True:
            chunk = await stream.read(_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            room = max_bytes - buffered
            if room > 0:
                keep = chunk[:room]
                chunks.append(keep)
                buffered += len(keep)
        return b"".join(chunks), total, total > max_bytes

    (stdout, _out_total, out_truncated), (stderr, _err_total, err_truncated) = (
        await asyncio.gather(_pump(process.stdout), _pump(process.stderr))
    )
    await process.wait()
    return stdout, stderr, out_truncated or err_truncated


@dataclass(frozen=True)
class ExecRequest:
    """One child process to run, and how to supervise it."""

    argv: tuple[str, ...]
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    #: Applied by the provider: ``argv_prefix`` is prepended and
    #: ``env_updates`` merged.  Callers therefore never assemble a sandboxed
    #: argv by hand, which is where the two copies could drift apart.
    sandbox: SandboxCommand | None = None
    #: Written to the child's stdin and closed.  Prefer this to argv for
    #: anything sensitive: argv is visible to every process on the machine
    #: via ``ps``.
    stdin: bytes | None = None
    timeout: float | None = None
    #: When set, the child is registered with the active cancel token under
    #: this label, so ``/cancel`` kills its process group instead of waiting
    #: for the command to finish.
    cancel_label: str | None = None
    #: When set, a progress heartbeat with this message runs for the child's
    #: lifetime.
    heartbeat_message: str | None = None
    heartbeat_interval: float = DEFAULT_HEARTBEAT_SECONDS


@dataclass(frozen=True)
class ExecResult:
    """What a finished child produced."""

    stdout: bytes = b""
    stderr: bytes = b""
    returncode: int | None = None
    #: True when the child was killed for exceeding ``timeout``.  Returned
    #: rather than raised because both callers turn it into their own error
    #: payload, and an exception would only be caught and translated twice.
    timed_out: bool = False
    #: True when stdout/stderr exceeded the provider's output cap and were
    #: truncated (drained, not fully buffered).
    truncated: bool = False

    def stdout_text(self, errors: str = "replace") -> str:
        return self.stdout.decode("utf-8", errors)

    def stderr_text(self, errors: str = "replace") -> str:
        return self.stderr.decode("utf-8", errors)


@runtime_checkable
class SubprocessProvider(Protocol):
    """Where child processes run.

    The seam exists so "run this command" can mean something other than
    ``fork`` on this host — a container, a remote worker — without either
    caller learning about it.
    """

    async def run(self, request: ExecRequest) -> ExecResult: ...


class LocalSubprocessProvider:
    """Run children on this machine, under the local OS sandbox."""

    async def run(self, request: ExecRequest) -> ExecResult:
        argv = tuple(request.sandbox.argv_prefix if request.sandbox else ()) + tuple(
            request.argv
        )
        env = dict(request.env) if request.env is not None else os.environ.copy()
        if request.sandbox is not None:
            env.update(request.sandbox.env_updates)

        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if request.stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=request.cwd,
            env=env,
            # The child leads its own process group, so a cancel or timeout
            # can signal everything it spawned rather than just the shell that
            # spawned them.  Every teardown path below depends on this.
            start_new_session=True,
        )

        deregister = self._register_cancel(process, request.cancel_label)
        heartbeat = self._start_heartbeat(request)
        try:
            try:
                stdout, stderr, truncated = await asyncio.wait_for(
                    _communicate_bounded(
                        process, request.stdin, max_bytes=OUTPUT_MAX_BYTES
                    ),
                    timeout=request.timeout,
                )
            except asyncio.TimeoutError:
                await self._terminate(process)
                return ExecResult(returncode=process.returncode, timed_out=True)
        except asyncio.CancelledError:
            # The caller is going away; the child must not outlive it as an
            # orphan in a detached session.
            await self._terminate(process)
            raise
        finally:
            deregister()
            await self._stop_heartbeat(heartbeat)

        return ExecResult(
            stdout=stdout or b"",
            stderr=stderr or b"",
            returncode=process.returncode,
            truncated=truncated,
        )

    @staticmethod
    def _register_cancel(process, label: str | None):
        """Let ``/cancel`` reach this child, returning a deregister callback."""
        if not label:
            return lambda: None
        token = shared._active_cancel_token.get()
        if token is None:
            return lambda: None

        def _cancel(level: str) -> None:
            sig = signal.SIGKILL if level == "force" else signal.SIGTERM
            _signal_group(process, sig)

        return token.register_cleanup(label, _cancel)

    @staticmethod
    def _start_heartbeat(request: ExecRequest):
        if not request.heartbeat_message:
            return None
        # Imported here: the executor imports this module's callers, and a
        # module-level import would close the cycle.
        from agent.tools.executor import report_tool_progress

        async def _beat() -> None:
            while True:
                await asyncio.sleep(request.heartbeat_interval)
                with contextlib.suppress(Exception):
                    report_tool_progress(
                        status="running",
                        message=request.heartbeat_message or "",
                    )

        return asyncio.create_task(_beat())

    @staticmethod
    async def _stop_heartbeat(task) -> None:
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @staticmethod
    async def _terminate(process) -> None:
        """SIGTERM the group, then SIGKILL it if that was not enough."""
        if process is None or process.returncode is not None:
            return
        _signal_group(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(
                process.communicate(), timeout=_TERMINATE_GRACE_SECONDS
            )
            return
        except asyncio.TimeoutError:
            pass
        except Exception:
            return
        _signal_group(process, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await process.communicate()


def _signal_group(process, sig: int) -> None:
    """Signal the child's whole process group, falling back to the child.

    Group-wide because the point of ``start_new_session=True`` is that a shell
    command's grandchildren die with it; signalling only the direct child
    leaves the work it spawned running.
    """
    killpg = getattr(os, "killpg", None)
    getpgid = getattr(os, "getpgid", None)
    with contextlib.suppress(
        AttributeError, ProcessLookupError, PermissionError, OSError
    ):
        if killpg is not None and getpgid is not None and process.pid:
            killpg(getpgid(process.pid), sig)
            return
    with contextlib.suppress(Exception):
        process.send_signal(sig)


#: Process-wide default.  Callers reach the provider through
#: ``registry.get_context("subprocess_provider")`` — the repo's existing
#: injection idiom — and fall back to this when nothing was injected.
_DEFAULT_PROVIDER = LocalSubprocessProvider()


def provider_from(registry) -> SubprocessProvider:
    """The injected provider, or the local one."""
    if registry is not None:
        injected = registry.get_context("subprocess_provider")
        if injected is not None:
            return injected
    return _DEFAULT_PROVIDER


__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "ExecRequest",
    "ExecResult",
    "LocalSubprocessProvider",
    "SubprocessProvider",
    "provider_from",
]
