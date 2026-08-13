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
  whole machine including GPU/IOKit.  Honoured only at permission level
  ``full`` — see :func:`effective_sandbox_mode`, which is the single
  definition of that linkage so the status display cannot disagree with
  enforcement.

``ShellSandboxRequest.devices`` controls device/service access (Metal/IOKit
mach services) inside a sandboxed run (``restricted`` or ``read_all``): it
defaults to open, the same posture the profile already takes for network.
Seatbelt can expose the GPU services (the same mechanism App Store sandboxes
use), so local MLX/GPU workloads work without opening writes or disabling
confirmation; set it to ``False`` for the strictest posture.

This matters more than it looks: "I needed GPU" is the most common reason a
sandbox gets switched off wholesale, and it is not a reason — measured on
macOS, PyTorch MPS and MLX both run under ``read_all`` with ``devices=True``
exactly as they do unsandboxed, and both fail with ``devices=False``.  When a
sandboxed command is denied, :func:`narrow_alternatives_hint` names this and
the other narrow knobs, because ``none`` becomes the obvious fix only when
nothing else is visible at the moment something breaks.

Write policy is inverted, not allowlisted: the sandbox protects **user
data**, not tool behavior.  Writes are open by default, so every local tool
(npm, pip, uv, git, HuggingFace, Chrome/Electron, MCP servers, …) can
persist caches, app state and temp files without a per-tool carve-out —
enumerating what each tool needs is unmaintainable and always one tool
behind.  The explicit denials name three asset classes, chosen by what an
attacker gains rather than by what the user filed where:

- **secrets** (``_SECRET_HOME_SUBDIRS``) — denied for *read* as well as
  write.  A write boundary does nothing for a credential; the damaging act
  is reading it and shipping it out, and this profile allows unrestricted
  network.  ``permissions.shell_secret_paths`` extends the set.
- **later-executed code** (``_AUTOSTART_HOME_SUBDIRS``,
  ``_AUTOSTART_SYSTEM_DIRS``) — shell rc files, launchd drop points and PATH
  directories.  Escaping a write sandbox never means defeating the sandbox;
  it means leaving a line for the user's next login shell to run.
- **user data** (``_PROTECTED_HOME_SUBDIRS``) — documents and media, plus
  the workspace unless an approved ``write_scope`` reopens it, plus the
  agent's own home (its config file holds provider API keys) and internal
  bookkeeping.

GUI/rendering workloads also receive the generic system facilities
(process-local mach bootstrap, app-sandbox file extensions, preference
reads) that App Store GUI apps get from ``application.sb``.

Reads outside the secret set stay open in ``read_all`` mode.  That is a
deliberate boundary, not an oversight: this sandbox contains *accidents and
injected instructions*, not a determined attacker with code execution, who
can always read something interesting that no list anticipated.

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

import contextlib
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Iterable
import uuid

from agent import shared


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

# Secrets: denied for **read** as well as write.  Write protection alone does
# nothing for a credential — the damaging act is reading `id_rsa` or an API
# key and shipping it somewhere, and this profile allows unrestricted network.
# So the asset class that needs a read boundary is credentials, not documents.
#
# The default set is chosen for near-zero collateral: no build, test or
# package-manager workflow reads these, so denying them breaks nothing.
# ``~/.ssh``, ``~/.docker`` and ``~/.kube`` are deliberately NOT here even
# though they hold credentials — ``git push`` over SSH, ``docker`` and
# ``kubectl`` all need to read them, and a default that breaks ``git push``
# would just get switched off wholesale.  Add them via
# ``permissions.shell_secret_paths`` when the session does not need those
# tools; that config extends this tuple rather than replacing it.
_SECRET_HOME_SUBDIRS: tuple[str, ...] = (
    "Library/Keychains",
    ".gnupg",
    ".aws",
    ".azure",
    ".git-credentials",
    ".netrc",
    ".config/gh",
    ".config/gcloud",
    ".claude.json",
)

# Write-denied because the file is *executed later*, outside this sandbox.
# The escape from a write-sandbox is never the sandbox itself: it is dropping
# a line into something the user's next login shell, launchd, or PATH lookup
# will run with full privileges.  Protecting `~/Documents` while leaving
# `~/.zshrc` writable protects the wrong asset.
_AUTOSTART_HOME_SUBDIRS: tuple[str, ...] = (
    ".zshrc",
    ".zshenv",
    ".zprofile",
    ".zlogin",
    ".zlogout",
    ".bashrc",
    ".bash_profile",
    ".bash_login",
    ".bash_logout",
    ".profile",
    ".config/fish",
    ".config/zsh",
    ".local/bin",
    "bin",
    "Library/LaunchAgents",
    "Library/LaunchDaemons",
)

# Same rationale, host-wide: PATH directories and launchd drop points.
_AUTOSTART_SYSTEM_DIRS: tuple[str, ...] = (
    "/usr/local/bin",
    "/usr/local/sbin",
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/Library/LaunchAgents",
    "/Library/LaunchDaemons",
    "/Library/StartupItems",
    "/etc/periodic",
    "/private/etc/periodic",
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
    #: Home-relative paths denied for read as well as write, on top of
    #: ``_SECRET_HOME_SUBDIRS``.  Comes from ``permissions.shell_secret_paths``.
    extra_secret_paths: tuple[str, ...] = ()
    #: Absolute paths the child must be able to read regardless of ``mode``.
    #: A sandbox that launches a *specific* interpreter has to be able to read
    #: it: in ``restricted`` mode a venv outside the workspace is invisible, so
    #: the child dies in ``init_import_site`` before running any tool code.
    #: This is platform runtime, not user data — the same category as
    #: ``/usr/lib``, which is already allowed unconditionally.
    extra_read_paths: tuple[str, ...] = ()
    #: The agent's own home (``~/.agent`` or ``~/.agent-<name>``).  Denied for
    #: read and write because ``config.json`` holds provider API keys;
    #: ``output_root`` and ``scratch_dir`` are reopened inside it.
    agent_home: Path | None = None


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
    # Key the cache on the profile *content*, not on a hand-listed subset of the
    # request fields.  A manual list silently drifts from what the profile is
    # actually a function of: `mode`, `devices` and `home_dir` were all absent,
    # so a `restricted` run reused a previously written `read_all` profile and
    # was granted host-wide reads, `devices=False` kept `iokit-open`, and two
    # different home directories shared one profile whose deny rules named only
    # the first user's home.  Hashing the rendered profile cannot drift.
    profile_text = _macos_seatbelt_profile(request)
    request_key = hashlib.sha256(profile_text.encode("utf-8")).hexdigest()[:32]
    profile_path = profile_dir / f"shell-{request_key}.sb"
    if not profile_path.exists():
        # Durably, not just atomically.  A concurrent shell must never exec a
        # partially written profile — but because the name is content-keyed, an
        # unflushed write that survives a crash as a zero-length file is worse:
        # the `exists()` check above then skips rewriting it forever, and every
        # later run with this request shape execs an empty profile.  Fails closed
        # rather than open, so it breaks the shell instead of opening it up.
        shared._atomic_write_text(profile_path, profile_text)
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


def _both_spellings(paths: Iterable[str]) -> list[str]:
    """Expand each path to its literal and canonical spelling, order-stable.

    Seatbelt enforces on the canonical path, so a relocated directory — a
    ``~/Documents`` symlinked to an external volume, a Dropbox/iCloud folder,
    ``/var`` vs ``/private/var`` — slips through a rule that names only one
    spelling.  Emitting both keeps the rule effective whichever way the link
    exists, including when it is created after this profile was rendered.
    """
    expanded: list[str] = []
    for raw in paths:
        link_path = Path(raw)
        for spelling in (link_path, link_path.resolve(strict=False)):
            candidate = str(spelling)
            if candidate not in expanded:
                expanded.append(candidate)
    return expanded


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
    # Interpreter/runtime roots the child needs merely to start.
    for candidate in _both_spellings(request.extra_read_paths):
        lines.append(
            f'(allow file-read* (subpath "{_seatbelt_literal(candidate)}"))'
        )
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
    # anywhere), then deny the three asset classes that matter.  Seatbelt
    # resolves overlapping rules last-match-wins, so every deny below must
    # follow this default-open allow, and each re-open must follow its deny.
    lines.append('(allow file-write* (subpath "/"))')

    # 1. Secrets — read AND write denied.  Denying only writes leaves the
    #    actual attack (read a credential, POST it out over the open network)
    #    fully available, so the read rule is the load-bearing one here.
    for candidate in _both_spellings(
        f"{home}/{sub}"
        for sub in (*_SECRET_HOME_SUBDIRS, *request.extra_secret_paths)
    ):
        literal = _seatbelt_literal(candidate)
        lines.append(f'(deny file-read* (subpath "{literal}"))')
        lines.append(f'(deny file-write* (subpath "{literal}"))')

    # 2. Later-executed code — write denied.  A writable ~/.zshrc or PATH
    #    directory turns any sandboxed write into unsandboxed execution the
    #    next time the user opens a shell, which defeats every rule above.
    for candidate in _both_spellings(
        [f"{home}/{sub}" for sub in _AUTOSTART_HOME_SUBDIRS]
        + list(_AUTOSTART_SYSTEM_DIRS)
    ):
        lines.append(
            f'(deny file-write* (subpath "{_seatbelt_literal(candidate)}"))'
        )

    # 3a. The agent's own home: config.json holds provider API keys, and the
    #     memory/scheduler databases are the agent's integrity.  output_root
    #     and scratch normally live inside it, so they are reopened next.
    if request.agent_home is not None:
        for candidate in _both_spellings([str(request.agent_home)]):
            literal = _seatbelt_literal(candidate)
            lines.append(f'(deny file-read* (subpath "{literal}"))')
            lines.append(f'(deny file-write* (subpath "{literal}"))')
        for reopened in (output, scratch):
            literal = _seatbelt_literal(reopened)
            lines.append(f'(allow file-read* (subpath "{literal}"))')
            lines.append(f'(allow file-write* (subpath "{literal}"))')

    # 3b. Internal bookkeeping (locks, profiles, scratch internals) stays
    #     hidden.  Must follow the output_root re-open above: it lives inside.
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
    # 4. Protected user data (documents, media, personal library data).
    for candidate in _both_spellings(
        f"{home}/{sub}" for sub in _PROTECTED_HOME_SUBDIRS
    ):
        lines.append(
            f'(deny file-write* (subpath "{_seatbelt_literal(candidate)}"))'
        )
    # Reopen the whole workspace when the policy grants it.  This MUST come
    # after the protected-data denies above: a workspace under ~/Desktop or
    # ~/Documents would otherwise have its write grant silently shadowed by
    # the last-match-wins deny for those home subdirectories, so the same
    # `workspace.write=true` flag worked for some workspaces and not others.
    if request.workspace_write:
        lines.append(
            f'(allow file-write* (subpath "{_seatbelt_literal(workspace)}"))'
        )
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


#: A scratch dir left behind by a hard kill is reclaimed once it is older#: than this.  Generous compared with the shell tool's own timeout, so a
#: long-running command can never have its TMPDIR swept out from under it.
_SCRATCH_MAX_AGE_SECONDS = 6 * 3600


# ── Posture reporting ──────────────────────────────────────────────────────
#
# A security posture nobody can see is one nobody maintains.  `shell_sandbox:
# none` is the single most consequential setting in the system and it was
# surfaced nowhere — not at startup, not in the status row, not in the prompt.
# So it gets set once for a specific task and stays set for months, which is
# exactly what happened to the author's own config.


def effective_sandbox_mode(mode: str, permission_level: str) -> str:
    """The mode that will actually be enforced, given the permission level.

    ``none`` is honoured only at permission level ``full``; anything lower
    falls back to ``read_all``.  This lives here, in one place, because the
    rule was previously applied only inside the shell tool: ``/permissions``
    computed the effective mode without it and would report ``none`` while
    the shell was really running ``read_all``.  A status display that
    disagrees with enforcement is worse than no display — it is the one the
    user trusts.
    """
    if mode == SANDBOX_MODE_NONE and str(permission_level or "") != "full":
        return SANDBOX_MODE_READ_ALL
    return mode


def sandbox_downgrade_note(mode: str, permission_level: str) -> str:
    """Explain a silent downgrade, or "" when none applies."""
    if effective_sandbox_mode(mode, permission_level) == mode:
        return ""
    return (
        f"configured sandbox `{mode}` is not in effect: it requires "
        f"permission level `full`, and the current level is "
        f"`{permission_level}`. Running `read_all` instead."
    )


def sandbox_posture_warning(mode: str, *, devices: bool = True) -> str:
    """A one-line warning when *mode* leaves the machine unprotected, else "".

    Returned rather than printed so every surface (startup banner, the
    `/permissions` output, the status row) says the same sentence.  Three
    copies of this text would drift, and the one that drifts is the one the
    user happens to read.
    """
    if mode != SANDBOX_MODE_NONE:
        return ""
    return (
        "shell sandbox is OFF (danger-full-access): commands can read your "
        "credentials and write your shell startup files. GPU works without "
        "this — that is `shell_devices`, which is on by default. "
        "Re-enable with `/permissions sandbox read_all`."
    )


def narrow_alternatives_hint(mode: str) -> str:
    """What to reach for when a sandboxed command was denied.

    The reason a user disables the sandbox wholesale is almost never that
    they wanted no boundary; it is that something broke and ``none`` was the
    only option they could see at that moment.  Naming the narrow knobs at
    the point of failure is what keeps the big switch from being the obvious
    fix.
    """
    if mode == SANDBOX_MODE_NONE:
        return ""
    return (
        "This looks like a sandbox denial. Before disabling the sandbox, try "
        "the narrow option that matches: GPU/Metal -> `shell_devices: true` "
        "(already the default); writing inside the workspace -> an approved "
        "`write_scope`; a credential path you actually need -> remove it from "
        "`permissions.shell_secret_paths`; reads outside the workspace -> "
        "`/permissions sandbox read_all`. `sandbox none` removes every "
        "boundary and is rarely what the failure needs."
    )


#: Substrings in a failed command's output that indicate the sandbox, rather
#: than the command itself, refused the operation.
_DENIAL_MARKERS: tuple[str, ...] = (
    "Operation not permitted",
    "sandbox-exec",
    "deny file-read",
    "deny file-write",
)


def looks_like_sandbox_denial(output: str) -> bool:
    text = str(output or "")
    return any(marker in text for marker in _DENIAL_MARKERS)


def new_scratch_dir(output_root: Path) -> Path:
    """Create a private scratch directory to use as the child's TMPDIR.

    Also reclaims stale siblings.  Callers pair this with
    :func:`release_scratch_dir` for the normal path, but a SIGKILL skips
    every ``finally`` in the process — so the age sweep here is what keeps
    the directory from growing without bound across crashes.
    """
    scratch_root = output_root / "sandbox"
    scratch_root.mkdir(parents=True, exist_ok=True)
    reclaim_stale_scratch_dirs(scratch_root)
    scratch = scratch_root / f"tmp-{uuid.uuid4().hex[:12]}"
    scratch.mkdir()
    return scratch


def release_scratch_dir(scratch: Path | None) -> None:
    """Remove a scratch directory once its command has finished.

    Every shell call gets a *fresh* scratch dir, so nothing can legitimately
    depend on its contents surviving the call that created it.
    """
    if scratch is None:
        return
    with contextlib.suppress(OSError):
        shutil.rmtree(scratch, ignore_errors=True)


def reclaim_stale_scratch_dirs(
    scratch_root: Path,
    *,
    max_age_seconds: float = _SCRATCH_MAX_AGE_SECONDS,
    now: float | None = None,
) -> int:
    """Delete ``tmp-*`` scratch dirs older than *max_age_seconds*."""
    current = time.time() if now is None else now
    reclaimed = 0
    with contextlib.suppress(OSError):
        for entry in scratch_root.iterdir():
            if not entry.name.startswith("tmp-") or not entry.is_dir():
                continue
            try:
                age = current - entry.stat().st_mtime
            except OSError:
                continue
            if age <= max_age_seconds:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                reclaimed += 1
    return reclaimed


__all__ = [
    "SandboxCommand",
    "SandboxUnavailableError",
    "ShellSandboxRequest",
    "build_sandbox_command",
    "detect_sandbox_support",
    "effective_sandbox_mode",
    "looks_like_sandbox_denial",
    "narrow_alternatives_hint",
    "new_scratch_dir",
    "reclaim_stale_scratch_dirs",
    "sandbox_downgrade_note",
    "sandbox_posture_warning",
    "release_scratch_dir",
]
