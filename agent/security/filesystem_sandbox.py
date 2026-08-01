"""OS-level filesystem sandboxing for shell descendants.

File-tool authorization alone cannot protect the Agent repository because a
shell process can write files independently.  Shell execution therefore
consumes the same immutable :class:`FileAccessPolicy`: the sandbox exposes
the workspace according to its read/write rules, keeps ``output_dir`` (minus
internal bookkeeping) writable, and denies writes everywhere else on the
host.

On a platform where an enforcing adapter cannot be constructed, restricted
shell capability fails closed instead of running unsandboxed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import sys
import uuid


class SandboxUnavailableError(RuntimeError):
    """Raised when no enforcing filesystem sandbox can be constructed."""


@dataclass(frozen=True)
class ShellSandboxRequest:
    """Immutable sandbox request derived from the active file policy."""

    workspace_root: Path
    output_root: Path
    workspace_read: bool
    workspace_write: bool
    write_scope: tuple[str, ...]
    scratch_dir: Path


@dataclass(frozen=True)
class SandboxCommand:
    """An argv prefix plus environment updates that enforce the request."""

    argv_prefix: tuple[str, ...]
    env_updates: dict[str, str]
    platform: str


_MACOS_SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def detect_sandbox_support() -> str | None:
    """Return the platform adapter name when an enforcing sandbox exists."""
    if sys.platform == "darwin" and os.path.exists(_MACOS_SANDBOX_EXEC):
        return "darwin-sandbox-exec"
    return None


def build_sandbox_command(
    request: ShellSandboxRequest,
) -> SandboxCommand:
    """Build the command wrapper for ``request`` or fail closed."""
    support = detect_sandbox_support()
    if support == "darwin-sandbox-exec":
        return _build_macos_sandbox_command(request)
    raise SandboxUnavailableError(
        "no enforcing filesystem sandbox is available on this platform "
        f"({sys.platform}); restricted shell execution is disabled"
    )


def _build_macos_sandbox_command(request: ShellSandboxRequest) -> SandboxCommand:
    profile_dir = request.output_root / ".simple-internal" / "sandbox"
    profile_dir.mkdir(parents=True, exist_ok=True)
    request_key = hashlib.sha256(
        (
            f"{request.workspace_root}\0{request.output_root}\0"
            f"{request.workspace_read}\0{request.workspace_write}\0"
            f"{','.join(request.write_scope)}\0{request.scratch_dir}"
        ).encode("utf-8")
    ).hexdigest()[:32]
    profile_path = profile_dir / f"shell-{request_key}.sb"
    if not profile_path.exists():
        profile_path.write_text(
            _macos_seatbelt_profile(request), encoding="utf-8"
        )
    request.scratch_dir.mkdir(parents=True, exist_ok=True)
    return SandboxCommand(
        argv_prefix=(_MACOS_SANDBOX_EXEC, "-f", str(profile_path)),
        env_updates={
            "TMPDIR": str(request.scratch_dir),
            "TMP": str(request.scratch_dir),
            "TEMP": str(request.scratch_dir),
        },
        platform="darwin-sandbox-exec",
    )


def _seatbelt_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _macos_seatbelt_profile(request: ShellSandboxRequest) -> str:
    workspace = str(request.workspace_root)
    output = str(request.output_root)
    scratch = str(request.scratch_dir)
    internal = str(request.output_root / ".simple-internal")

    lines = [
        "(version 1)",
        '(import "system.sb")',
        "(deny default)",
        # Process/runtime primitives required to launch and manage commands.
        "(allow process*)",
        "(allow sysctl-read)",
        "(allow network*)",
        "(allow ipc-posix-shm*)",
        "(allow ipc-posix-sem*)",
        # Read-only platform runtime allowlist.  The child sees only the
        # minimum executables/libraries needed to launch commands.
        "(allow file-read-metadata)",
        "(allow file-read* (subpath \"/System\"))",
        "(allow file-read* (subpath \"/usr/lib\"))",
        "(allow file-read* (subpath \"/usr/bin\"))",
        "(allow file-read* (subpath \"/usr/sbin\"))",
        "(allow file-read* (subpath \"/bin\"))",
        "(allow file-read* (subpath \"/sbin\"))",
        "(allow file-read* (subpath \"/usr/share\"))",
        "(allow file-read* (subpath \"/Library/Apple\"))",
        "(allow file-read* (subpath \"/private/etc\"))",
        "(allow file-read* (subpath \"/private/var/db\"))",
        "(allow file-read* (subpath \"/private/var/run\"))",
        "(allow file-read* (subpath \"/private/var/select\"))",
        "(allow file-read* (subpath \"/dev\"))",
    ]
    if request.workspace_read:
        lines.append(
            f'(allow file-read* (subpath "{_seatbelt_literal(workspace)}"))'
        )
    # output_dir is always readable and writable for generated artifacts.
    lines.append(f'(allow file-read* (subpath "{_seatbelt_literal(output)}"))')
    lines.append(f'(allow file-write* (subpath "{_seatbelt_literal(output)}"))')
    # Internal bookkeeping (locks, profiles, scratch internals) stays hidden.
    lines.append(
        f'(deny file-read* (subpath "{_seatbelt_literal(internal)}"))'
    )
    lines.append(
        f'(deny file-write* (subpath "{_seatbelt_literal(internal)}"))'
    )
    # Private scratch used for TMPDIR/TMP/TEMP.
    lines.append(f'(allow file-read* (subpath "{_seatbelt_literal(scratch)}"))')
    lines.append(f'(allow file-write* (subpath "{_seatbelt_literal(scratch)}"))')
    # Approved write_scope entries become writable; the rest of the workspace
    # stays read-only (or hidden when workspace reads are disabled).
    for scope in request.write_scope:
        if scope == "*":
            lines.append(
                f'(allow file-write* (subpath "{_seatbelt_literal(workspace)}"))'
            )
            continue
        candidate = request.workspace_root / scope
        if _path_is_within(candidate, request.workspace_root):
            lines.append(
                f'(allow file-write* (subpath "{_seatbelt_literal(str(candidate))}"))'
            )
            continue
        candidate = request.output_root / scope
        if _path_is_within(candidate, request.output_root):
            lines.append(
                f'(allow file-write* (subpath "{_seatbelt_literal(str(candidate))}"))'
            )
    lines.append("(allow mach-lookup (global-name \"com.apple.system.logger\"))")
    lines.append(
        "(allow mach-lookup "
        "(global-name \"com.apple.system.opendirectoryd.libinfo\"))"
    )
    return "\n".join(lines) + "\n"


def _path_is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def new_scratch_dir(output_root: Path) -> Path:
    """Create a private public scratch directory under ``output_root``."""
    scratch_root = output_root / "sandbox"
    scratch_root.mkdir(parents=True, exist_ok=True)
    scratch = scratch_root / f"tmp-{uuid.uuid4().hex[:12]}"
    scratch.mkdir()
    return scratch


__all__ = [
    "SandboxCommand",
    "SandboxUnavailableError",
    "ShellSandboxRequest",
    "build_sandbox_command",
    "detect_sandbox_support",
    "new_scratch_dir",
]
