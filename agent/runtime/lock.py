"""Single-owner enforcement for one agent home directory.

Several subsystems assume they are the only writer of ``AGENT_HOME``:
``staging_turns`` rows are partitioned by ``session_id`` and the CLI hardcodes
``session_id="cli"``, so two interactive processes share one staging partition
and ``StagingBuffer.clear_all()`` in either one deletes turns the other has not
consolidated yet.  Memory index files, prompts, and plugin approval state are
read-modify-write with no cross-process coordination either.

Rather than audit every such site, enforce the invariant at the entry points:
one live owner per agent home.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover - exercised implicitly on POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None  # type: ignore[assignment]

from agent import shared

LOCK_FILENAME = ".owner.lock"
BYPASS_ENV_VAR = "SIMPLE_ALLOW_MULTI_INSTANCE"


def multi_instance_allowed() -> bool:
    """Whether the operator has explicitly opted out of the single-owner rule."""
    return str(os.environ.get(BYPASS_ENV_VAR, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True)
class LockHolder:
    """Best-effort identity of the process currently owning an agent home."""

    pid: int = 0
    mode: str = ""
    started_at: str = ""

    @classmethod
    def from_payload(cls, payload: Any) -> Optional["LockHolder"]:
        if not isinstance(payload, dict):
            return None
        try:
            pid = int(payload.get("pid", 0))
        except (TypeError, ValueError):
            pid = 0
        mode = str(payload.get("mode", "") or "")
        started_at = str(payload.get("started_at", "") or "")
        if not pid and not mode:
            return None
        return cls(pid=pid, mode=mode, started_at=started_at)

    def describe(self) -> str:
        parts = []
        if self.pid:
            parts.append(f"pid {self.pid}")
        if self.mode:
            parts.append(f"模式 {self.mode}")
        if self.started_at:
            parts.append(f"启动于 {self.started_at}")
        return ", ".join(parts) if parts else "身份未知"


class AgentHomeBusyError(RuntimeError):
    """Raised when another live process already owns the agent home."""

    def __init__(self, home: Path, holder: Optional[LockHolder] = None) -> None:
        self.home = Path(home)
        self.holder = holder
        super().__init__(self._message())

    def _message(self) -> str:
        who = self.holder.describe() if self.holder else "身份未知"
        return (
            f"另一个 simple 实例正在使用 {self.home} ({who})。\n"
            "同一个 agent home 同一时刻只能有一个实例：CLI 与 gateway 共享 staging、\n"
            "记忆库和索引文件，并发运行会互相覆盖未固化的对话。\n"
            "\n"
            "可选做法:\n"
            "  • 结束那个实例后重试\n"
            "  • 换独立实例: SIMPLE_AGENT_HOME=~/.agent-dev simple\n"
            "    (gateway 用 simple gateway --name dev)\n"
            f"  • 明确知道风险时: {BYPASS_ENV_VAR}=1 simple"
        )


class AgentHomeLock:
    """Exclusive advisory lock over one agent home directory.

    Backed by ``flock`` rather than a PID file because the kernel drops the
    lock when the owning process dies for any reason — including SIGKILL and
    power loss — so there is no stale-lock state to detect, age out, or clean
    up by hand.  The file's JSON body is metadata for the error message only;
    it never participates in deciding whether the lock is held.
    """

    def __init__(self, home: Optional[Path] = None, mode: str = "") -> None:
        self._home = Path(home) if home is not None else None
        self.mode = str(mode or "")
        self._fd: Optional[int] = None

    @property
    def home(self) -> Path:
        """Resolve lazily: ``--name`` rewrites AGENT_HOME after import time."""
        return self._home if self._home is not None else shared.AGENT_HOME

    @property
    def path(self) -> Path:
        return self.home / LOCK_FILENAME

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "AgentHomeLock":
        """Take the lock, or raise :class:`AgentHomeBusyError`.

        A no-op when the platform lacks ``flock`` or the operator set
        ``SIMPLE_ALLOW_MULTI_INSTANCE``; both cases keep the object usable so
        callers never need to branch on whether locking is active.
        """
        if self.held or fcntl is None or multi_instance_allowed():
            return self
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise AgentHomeBusyError(self.home, read_lock_holder(path)) from exc
        self._fd = fd
        self._write_metadata()
        return self

    def release(self) -> None:
        """Drop the lock and blank the metadata so no stale holder is reported."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            # The lock file is never unlinked: deleting it would let a waiter
            # that already opened the old inode hold a lock nobody else sees.
            try:
                os.close(fd)
            except OSError:
                pass

    def _write_metadata(self) -> None:
        if self._fd is None:
            return
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "mode": self.mode,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "home": str(self.home),
            }
        ).encode("utf-8")
        try:
            os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, payload)
            os.fsync(self._fd)
        except OSError:
            # Metadata is a diagnostic nicety; losing it must not cost the lock.
            pass

    def __enter__(self) -> "AgentHomeLock":
        return self.acquire()

    def __exit__(self, *_exc_info: Any) -> None:
        self.release()


def read_lock_holder(path: Path) -> Optional[LockHolder]:
    """Read holder metadata without taking the lock (advisory locks allow it)."""
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return LockHolder.from_payload(json.loads(raw))
    except (ValueError, TypeError):
        return None


def acquire_agent_home_lock(
    mode: str, home: Optional[Path] = None
) -> AgentHomeLock:
    """Convenience wrapper used by the CLI entry points."""
    return AgentHomeLock(home=home, mode=mode).acquire()
