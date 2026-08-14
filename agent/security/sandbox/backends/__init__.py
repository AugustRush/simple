"""Backend registry: pick the enforcing mechanism this host actually has.

Adding a backend means adding an entry to :data:`_BACKENDS` and nothing else.
The dispatch below is the only place that knows more than one exists, and it
fails closed — a sandboxed mode on a host with no enforcing backend raises
rather than silently running the command unsandboxed.
"""

from __future__ import annotations

import sys
from typing import Protocol

from agent.security.sandbox.policy import (
    SANDBOX_MODE_NONE,
    SandboxCommand,
    SandboxUnavailableError,
    ShellSandboxRequest,
)
from agent.security.sandbox.backends import seatbelt


class SandboxBackend(Protocol):
    """What a backend must provide to be dispatchable.

    Deliberately narrow: availability, a name, and request-to-command.  How
    the backend enforces the policy — a profile file, a set of syscalls, a
    remote API — is entirely its own business.
    """

    BACKEND_NAME: str

    def is_available(self) -> bool: ...

    def build_command(self, request: ShellSandboxRequest) -> SandboxCommand: ...


#: Ordered by preference.  Each entry is checked with ``is_available()``, so
#: importing a backend for a foreign platform is harmless.
_BACKENDS: tuple[object, ...] = (seatbelt,)


def detect_sandbox_support() -> str | None:
    """Return the platform adapter name when an enforcing sandbox exists.

    The single authority on "can this host enforce a sandbox".  Both the
    status display and :func:`build_sandbox_command` route through it, so
    what the user is told and what is actually enforced cannot diverge.
    """
    for backend in _BACKENDS:
        if backend.is_available():  # type: ignore[attr-defined]
            return backend.BACKEND_NAME  # type: ignore[attr-defined]
    return None


def _backend_named(name: str) -> object | None:
    for backend in _BACKENDS:
        if backend.BACKEND_NAME == name:  # type: ignore[attr-defined]
            return backend
    return None


def build_sandbox_command(request: ShellSandboxRequest) -> SandboxCommand:
    """Build the command wrapper for ``request`` or fail closed."""
    if request.mode == SANDBOX_MODE_NONE:
        return SandboxCommand(
            argv_prefix=(),
            env_updates={},
            platform="none",
        )
    support = detect_sandbox_support()
    backend = _backend_named(support) if support else None
    if backend is not None:
        return backend.build_command(request)  # type: ignore[attr-defined]
    raise SandboxUnavailableError(
        "no enforcing filesystem sandbox is available on this platform "
        f"({sys.platform}); restricted shell execution is disabled"
    )


__all__ = [
    "SandboxBackend",
    "build_sandbox_command",
    "detect_sandbox_support",
    "seatbelt",
]
