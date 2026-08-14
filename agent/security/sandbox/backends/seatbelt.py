"""macOS seatbelt backend: render a resolved policy as a ``.sb`` profile.

Everything seatbelt-specific lives here — the sexpr syntax, the platform
runtime allowlist, the mach/GUI capability block, and the last-match-wins
ordering the profile depends on.  None of it is visible to callers, who hand
in a :class:`~agent.security.sandbox.policy.ResolvedSandboxPolicy` and get
back a :class:`~agent.security.sandbox.policy.SandboxCommand`.

The ordering constraint is the whole reason this file is separate.  Seatbelt
resolves overlapping rules last-match-wins, so "open writes, then carve out
the asset classes, then reopen an approved scope" is expressible only as a
*sequence*.  A backend with additive-only semantics (Landlock) cannot consume
that sequence, so the sequence stops here: it is how this renderer chooses to
transcribe the policy, not part of the policy.
"""

from __future__ import annotations

import hashlib
import os
import sys

from agent import shared
from agent.security.sandbox.policy import (
    PathRule,
    ResolvedSandboxPolicy,
    SandboxCommand,
    ShellSandboxRequest,
    resolve_policy,
)

BACKEND_NAME = "darwin-sandbox-exec"

_MACOS_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

_DEVICE_SANDBOX_RULES: tuple[str, ...] = (
    '(allow mach-lookup (global-name "com.apple.IOAccelerator"))',
    '(allow mach-lookup (global-name "com.apple.Metal"))',
    '(allow mach-lookup (global-name "com.apple.MTLCompilerService"))',
    "(allow iokit-open)",
)

#: Read-only platform runtime.  The child sees only the minimum
#: executables/libraries needed to launch commands.  Not part of the policy:
#: these are macOS's own layout, meaningless to any other backend.
_PLATFORM_READ_SUBPATHS: tuple[str, ...] = (
    "/System",
    "/usr/lib",
    "/usr/bin",
    "/usr/sbin",
    "/bin",
    "/sbin",
    "/usr/share",
    "/Library/Apple",
    "/private/etc",
    "/private/var/db",
    "/private/var/run",
    "/private/var/select",
    "/dev",
)


def is_available() -> bool:
    """True when this host can enforce a seatbelt sandbox."""
    return sys.platform == "darwin" and os.path.exists(_MACOS_SANDBOX_EXEC)


def _seatbelt_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _render_rule(rule: PathRule) -> list[str]:
    """Transcribe one rule, read line before write line.

    A rule that decides both accesses (a secret denial, an ``agent_home``
    re-open) produces two lines; ``None`` produces none, leaving whatever a
    broader rule established — which is exactly seatbelt's own semantics.
    """
    literal = _seatbelt_literal(rule.path)
    lines: list[str] = []
    if rule.read is not None:
        verb = "allow" if rule.read else "deny"
        lines.append(f'({verb} file-read* (subpath "{literal}"))')
    if rule.write is not None:
        verb = "allow" if rule.write else "deny"
        lines.append(f'({verb} file-write* (subpath "{literal}"))')
    return lines


def render_profile(policy: ResolvedSandboxPolicy) -> str:
    """Render *policy* as seatbelt profile text."""
    lines = [
        "(version 1)",
        '(import "system.sb")',
        "(deny default)",
        # Process/runtime primitives required to launch and manage commands.
        "(allow process*)",
        "(allow sysctl-read)",
    ]
    if policy.network:
        lines.append("(allow network*)")
    lines.extend(
        (
            "(allow ipc-posix-shm*)",
            "(allow ipc-posix-sem*)",
            "(allow file-read-metadata)",
        )
    )
    for subpath in _PLATFORM_READ_SUBPATHS:
        lines.append(f'(allow file-read* (subpath "{subpath}"))')

    for rule in policy.read_grants:
        lines.extend(_render_rule(rule))

    # Generic system facilities every GUI/rendering app needs (crashpad/XPC
    # handshakes use unique per-run mach names, so they cannot be
    # enumerated; app-sandbox extensions and preference reads are generic
    # app capabilities — the same set application.sb grants App Store apps).
    #
    # Sits between the two policy sections because it is a *capability*
    # grant, not a path grant: nothing below can override it, and nothing
    # above depends on it.
    lines.extend(
        (
            "(allow mach-bootstrap)",
            "(allow mach-register)",
            "(allow mach-lookup)",
            "(allow file-issue-extension)",
            "(allow user-preference-read)",
        )
    )

    # The restriction section, in resolution order: the default-open write
    # first, then each carve-out, then each re-open.  Last-match-wins makes
    # this order load-bearing — reordering it silently reopens a denied path.
    for rule in policy.restrictions:
        lines.extend(_render_rule(rule))

    if policy.devices:
        lines.extend(_DEVICE_SANDBOX_RULES)
    lines.append('(allow mach-lookup (global-name "com.apple.system.logger"))')
    lines.append(
        "(allow mach-lookup "
        '(global-name "com.apple.system.opendirectoryd.libinfo"))'
    )
    return "\n".join(lines) + "\n"


def build_command(request: ShellSandboxRequest) -> SandboxCommand:
    """Write the profile for *request* and return the argv prefix that applies it."""
    profile_dir = request.output_root / ".simple-internal" / "sandbox"
    profile_dir.mkdir(parents=True, exist_ok=True)
    # Key the cache on the profile *content*, not on a hand-listed subset of the
    # request fields.  A manual list silently drifts from what the profile is
    # actually a function of: `mode`, `devices` and `home_dir` were all absent,
    # so a `restricted` run reused a previously written `read_all` profile and
    # was granted host-wide reads, `devices=False` kept `iokit-open`, and two
    # different home directories shared one profile whose deny rules named only
    # the first user's home.  Hashing the rendered profile cannot drift.
    profile_text = render_profile(resolve_policy(request))
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
        platform=BACKEND_NAME,
    )


def profile_for_request(request: ShellSandboxRequest) -> str:
    """Convenience for tests and diagnostics: request in, profile text out."""
    return render_profile(resolve_policy(request))


__all__ = [
    "BACKEND_NAME",
    "build_command",
    "is_available",
    "profile_for_request",
    "render_profile",
]
