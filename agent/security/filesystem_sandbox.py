"""OS-level filesystem sandboxing for shell descendants.

File-tool authorization alone cannot protect the Agent repository because a
shell process can write files independently.  Shell execution therefore
consumes the same immutable :class:`FileAccessPolicy`: the sandbox exposes
the workspace according to its read/write rules, keeps ``output_dir`` (minus
internal bookkeeping) writable, and denies writes everywhere else on the
host.

Three sandbox modes link to the permission levels:

- ``restricted``: reads limited to platform runtimes plus the workspace and
  output dirs plus the user's cache/state directories.
- ``read_all`` (default): the same write boundary, but reads are allowed
  everywhere on the host (``~/.config``, home caches, miniconda, …) so
  local tooling works without granting write access.
- ``none`` (danger-full-access): no OS sandbox at all; the child sees the
  whole machine including GPU/IOKit.  Only meaningful with permission level
  ``full``; the caller enforces that linkage.

``ShellSandboxRequest.devices`` controls device/service access (Metal/IOKit
mach services) inside a sandboxed run (``restricted`` or ``read_all``): it
defaults to open, the same posture the profile already takes for network.
Seatbelt can expose the GPU services (the same mechanism App Store sandboxes
use), so local MLX/GPU workloads work without opening writes or disabling
confirmation; set it to ``False`` for the strictest posture.

Write policy is inverted, not allowlisted: the sandbox protects **user
data**, not tool behavior.  Writes are open by default, so every local tool
(npm, pip, uv, git, HuggingFace, Chrome/Electron, MCP servers, …) can
persist caches, app state and temp files without a per-tool carve-out —
enumerating what each tool needs is unmaintainable and always one tool
behind.  The only explicit write denials are protected user-data surfaces:
documents/media, keychains and credentials (``~/.ssh``, ``~/.aws``,
``~/.git-credentials``, …), the workspace unless an approved ``write_scope``
reopens it, and the agent's internal bookkeeping.  GUI/rendering workloads
also receive the generic system facilities (process-local mach bootstrap,
app-sandbox file extensions, preference reads) that App Store GUI apps get
from ``application.sb``.

One limitation is architectural, not configurable: seatbelt has no
operation for starting a *second* sandbox, so a tool that installs its own
OS sandbox (headless Chrome, Electron) cannot nest inside this one.  Such
tools must disable their own sandbox (``--no-sandbox`` for Chrome,
``ELECTRON_DISABLE_SANDBOX=1`` for Electron) or run unsandboxed via
``shell_sandbox: none`` with permission level ``full``.

On a platform where an enforcing adapter cannot be constructed, sandboxed
modes fail closed instead of running unsandboxed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import shutil
import sys
import uuid


class SandboxUnavailableError(RuntimeError):
    """Raised when no enforcing filesystem sandbox can be constructed."""


SANDBOX_MODE_RESTRICTED = "restricted"
SANDBOX_MODE_READ_ALL = "read_all"
SANDBOX_MODE_NONE = "none"
SANDBOX_MODES: tuple[str, ...] = (
    SANDBOX_MODE_RESTRICTED,
    SANDBOX_MODE_READ_ALL,
    SANDBOX_MODE_NONE,
)

_DEVICE_SANDBOX_RULES: tuple[str, ...] = (
    '(allow mach-lookup (global-name "com.apple.IOAccelerator"))',
    '(allow mach-lookup (global-name "com.apple.Metal"))',
    '(allow mach-lookup (global-name "com.apple.MTLCompilerService"))',
    "(allow iokit-open)",
)

# User-data surfaces that stay write-protected even though tool state is
# open by default: documents/media, keychains/personal library data, and
# credential files.  This is the deny side of the inverted policy — the
# list is small and stable because it names what the user owns, not what
# individual tools need.
_PROTECTED_HOME_SUBDIRS: tuple[str, ...] = (
    "Documents",
    "Desktop",
    "Downloads",
    "Movies",
    "Music",
    "Pictures",
    "Library/Keychains",
    "Library/Mail",
    "Library/Safari",
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".gitconfig",
    ".git-credentials",
    ".netrc",
)


@dataclass(frozen=True)
class ShellSandboxRequest:
    """Immutable sandbox request derived from the active file policy."""

    workspace_root: Path
    output_root: Path
    workspace_read: bool
    workspace_write: bool
    write_scope: tuple[str, ...]
    scratch_dir: Path
    mode: str = SANDBOX_MODE_READ_ALL
    devices: bool = True
    home_dir: Path = field(default_factory=lambda: Path.home())


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
    if request.mode == SANDBOX_MODE_NONE:
        return SandboxCommand(
            argv_prefix=(),
            env_updates={},
            platform="none",
        )
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
    # Use canonical paths: /var is a symlink to /private/var on macOS, and a
    # rule written for one spelling silently fails to match the other.
    workspace = str(request.workspace_root.resolve(strict=False))
    output = str(request.output_root.resolve(strict=False))
    scratch = str(request.scratch_dir.resolve(strict=False))
    internal = str(
        (request.output_root / ".simple-internal").resolve(strict=False)
    )

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
    if request.mode == SANDBOX_MODE_READ_ALL:
        # Convenience default: reads are open everywhere, writes stay scoped.
        lines.append('(allow file-read* (subpath "/"))')
    if request.workspace_read:
        lines.append(
            f'(allow file-read* (subpath "{_seatbelt_literal(workspace)}"))'
        )
    # output_dir and scratch are always readable for generated artifacts.
    lines.append(f'(allow file-read* (subpath "{_seatbelt_literal(output)}"))')
    lines.append(f'(allow file-read* (subpath "{_seatbelt_literal(scratch)}"))')
    # Restricted mode still needs to read the user's cache/state dirs so
    # tools can consume their own caches; in read_all mode reads are open
    # anyway and these rules are redundant but harmless.
    home = str(request.home_dir.resolve(strict=False))
    for state_dir in (
        home + "/.cache",
        home + "/.npm",
        home + "/.local",
        home + "/.config",
        home + "/Library/Caches",
        home + "/Library/Application Support",
    ):
        literal = _seatbelt_literal(state_dir)
        lines.append(f'(allow file-read* (subpath "{literal}"))')

    # Generic system facilities every GUI/rendering app needs (crashpad/XPC
    # handshakes use unique per-run mach names, so they cannot be
    # enumerated; app-sandbox extensions and preference reads are generic
    # app capabilities — the same set application.sb grants App Store apps).
    lines.extend(
        (
            "(allow mach-bootstrap)",
            "(allow mach-register)",
            "(allow mach-lookup)",
            "(allow file-issue-extension)",
            "(allow user-preference-read)",
        )
    )

    # Inverted write policy: open by default (tools persist caches/app state
    # anywhere), then deny only the protected user-data surfaces.  Seatbelt
    # resolves overlapping rules last-match-wins, so the denies below must
    # follow this default-open allow, and write_scope re-opens must follow
    # their denies.
    lines.append('(allow file-write* (subpath "/"))')
    # Internal bookkeeping (locks, profiles, scratch internals) stays hidden.
    lines.append(
        f'(deny file-read* (subpath "{_seatbelt_literal(internal)}"))'
    )
    lines.append(
        f'(deny file-write* (subpath "{_seatbelt_literal(internal)}"))'
    )
    # The workspace is not writable unless write_scope enables it.
    workspace_denied = (
        not request.workspace_write and "*" not in request.write_scope
    )
    if workspace_denied:
        lines.append(
            f'(deny file-write* (subpath "{_seatbelt_literal(workspace)}"))'
        )
    # Protected user data (documents, media, keychains, credentials).
    for sub in _PROTECTED_HOME_SUBDIRS:
        literal = _seatbelt_literal(f"{home}/{sub}")
        lines.append(f'(deny file-write* (subpath "{literal}"))')
    # Approved write_scope entries reopen paths after their denies.
    for scope in request.write_scope:
        if scope == "*":
            lines.append(
                f'(allow file-write* (subpath "{_seatbelt_literal(workspace)}"))'
            )
            continue
        candidate = request.workspace_root / scope
        if _path_is_within(candidate, request.workspace_root):
            lines.append(
                f'(allow file-write* (subpath "{_seatbelt_literal(str(candidate.resolve(strict=False)))}"))'
            )
    if request.devices and request.mode != SANDBOX_MODE_NONE:
        lines.extend(_DEVICE_SANDBOX_RULES)
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
