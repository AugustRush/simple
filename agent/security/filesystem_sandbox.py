"""Backwards-compatible entry point for :mod:`agent.security.sandbox`.

The implementation moved into a package so that policy (what is permitted)
and enforcement (how) could be separated — see
:mod:`agent.security.sandbox` for the security model and
:mod:`agent.security.sandbox.policy` for why the seam is shaped the way it
is.  This module stays because five production call sites and four test
modules import from it, and churning them buys nothing.

New code should import from :mod:`agent.security.sandbox` directly.
"""

from __future__ import annotations

from agent.security.sandbox import (
    SANDBOX_MODE_NONE,
    SANDBOX_MODE_READ_ALL,
    SANDBOX_MODE_RESTRICTED,
    SANDBOX_MODES,
    PathRule,
    ResolvedSandboxPolicy,
    SandboxCommand,
    SandboxUnavailableError,
    ShellSandboxRequest,
    build_sandbox_command,
    detect_sandbox_support,
    effective_sandbox_mode,
    looks_like_sandbox_denial,
    narrow_alternatives_hint,
    new_scratch_dir,
    reclaim_stale_scratch_dirs,
    release_scratch_dir,
    resolve_policy,
    sandbox_downgrade_note,
    sandbox_posture_warning,
)
from agent.security.sandbox.backends.seatbelt import (
    _DEVICE_SANDBOX_RULES,
    profile_for_request as _macos_seatbelt_profile,
)
from agent.security.sandbox.policy import (
    _AUTOSTART_HOME_SUBDIRS,
    _AUTOSTART_SYSTEM_DIRS,
    _PROTECTED_HOME_SUBDIRS,
    _SECRET_HOME_SUBDIRS,
)
from agent.security.sandbox.scratch import _SCRATCH_MAX_AGE_SECONDS

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
