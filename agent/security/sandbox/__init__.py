"""OS-level filesystem sandboxing for shell descendants.

File-tool authorization alone cannot protect the Agent repository because a
shell process can write files independently.  Shell execution therefore
consumes the same immutable :class:`FileAccessPolicy`: the sandbox exposes
the workspace according to its read/write rules, keeps ``output_dir`` (minus
internal bookkeeping) writable, and denies writes everywhere else on the
host.

The package is split by *what is decided* versus *how it is enforced*:

- :mod:`.policy` — what a request permits, in terms of paths and
  capabilities.  No OS mechanism appears here.
- :mod:`.backends` — one module per enforcement mechanism, plus the registry
  that picks an available one and fails closed when there is none.
- :mod:`.scratch` — the private TMPDIR lifecycle, shared by all backends.

Three sandbox modes link to the permission levels:

- ``restricted``: reads limited to platform runtimes plus the workspace and
  output dirs plus the user's cache/state directories.
- ``read_all`` (default): the same write boundary, but reads are allowed
  everywhere on the host (``~/.config``, home caches, miniconda, …) so
  local tooling works without granting write access.
- ``none`` (danger-full-access): no OS sandbox at all; the child sees the
  whole machine including GPU/IOKit.  Honoured only at permission level
  ``full`` — see :func:`~.policy.effective_sandbox_mode`, which is the
  single definition of that linkage so the status display cannot disagree
  with enforcement.

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
sandboxed command is denied, :func:`~.policy.narrow_alternatives_hint` names
this and the other narrow knobs, because ``none`` becomes the obvious fix
only when nothing else is visible at the moment something breaks.

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

from agent.security.sandbox.backends import (
    build_sandbox_command,
    detect_sandbox_support,
)
from agent.security.sandbox.policy import (
    SANDBOX_MODE_NONE,
    SANDBOX_MODE_READ_ALL,
    SANDBOX_MODE_RESTRICTED,
    SANDBOX_MODES,
    PathRule,
    ResolvedSandboxPolicy,
    SandboxCommand,
    SandboxUnavailableError,
    ShellSandboxRequest,
    effective_sandbox_mode,
    looks_like_sandbox_denial,
    narrow_alternatives_hint,
    resolve_policy,
    sandbox_downgrade_note,
    sandbox_posture_warning,
)
from agent.security.sandbox.scratch import (
    new_scratch_dir,
    reclaim_stale_scratch_dirs,
    release_scratch_dir,
)

__all__ = [
    "PathRule",
    "ResolvedSandboxPolicy",
    "SANDBOX_MODES",
    "SANDBOX_MODE_NONE",
    "SANDBOX_MODE_READ_ALL",
    "SANDBOX_MODE_RESTRICTED",
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
    "release_scratch_dir",
    "resolve_policy",
    "sandbox_downgrade_note",
    "sandbox_posture_warning",
]
