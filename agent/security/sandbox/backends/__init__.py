"""Backend registry: pick the enforcing mechanism this host actually has.

Adding a backend means adding an entry to :data:`_BACKENDS` and nothing else.
The dispatch below is the only place that knows more than one exists, and it
fails closed — a sandboxed mode on a host with no enforcing backend raises
rather than silently running the command unsandboxed.

Why there is only one backend
-----------------------------
macOS is the only platform this project supports, and Linux support was
considered and declined rather than left undone.  Recorded here because the
reasoning is not obvious from the code, and "just add a Landlock backend"
looks easy until you try it.

The obstacle is not this seam — it is the write policy.  Both sandboxed modes
resolve to ``write_everywhere=True`` followed by several dozen nested write
denials; ``mode`` selects the *read* posture only (``restricted`` differs from
``read_all`` in ``read_everywhere``, nothing else).  Linux Landlock is a purely
additive allowlist with no deny rule, and a nested path may not carry fewer
rights than its parent, so "open writes, then carve out secrets /
later-executed code / user data" is inexpressible there — in *both* modes.

A Landlock backend could therefore not transcribe the policy.  It would have
to pick one of:

* Grant only workspace, output and scratch.  Expressible and safe, but it
  breaks the npm/pip/uv/HuggingFace cache writes that the inverted policy
  exists to permit, and it fails the conformance assertions that check a
  sandboxed child *can* write ordinary paths.
* Enumerate the non-secret entries under home at build time and grant those.
  A point-in-time snapshot: a secret directory created later is missed.  That
  is an open-ended failure, contrary to this module's fail-closed posture.

Worth knowing if that decision is ever revisited: fail-closed is not free on
an unsupported platform.  ``build_sandbox_command`` raises there, and both
callers turn that into a refusal — so the shell tool and every user tool stop
working, and the only configuration that runs at all is ``sandbox none`` at
permission level ``full``.  Declining to support a platform pushes anyone on
it toward zero protection, not toward less.
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
