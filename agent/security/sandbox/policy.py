"""What the sandbox permits, stated without reference to any OS mechanism.

This module answers "which paths may this child read and write, and which
device/network capabilities does it get" and stops there.  Turning that answer
into an enforcement mechanism belongs to a backend (:mod:`.backends`).

The split exists because the two enforcement mechanisms available do not share
a rule language.  macOS seatbelt resolves overlapping rules *last-match-wins*,
so a policy is naturally written as "open writes everywhere, then carve out the
three asset classes, then reopen an approved scope".  Linux Landlock is a
purely additive allowlist: it has no deny rule at all, and a nested path may
not carry fewer rights than its parent, so that same policy cannot be
transcribed rule for rule.  Handing a backend a *rule sequence* would therefore
hand it something only one backend can consume.

What a backend receives instead is a :class:`ResolvedSandboxPolicy` — a
question-answering object.  ``effective_rights(path)`` returns the rights that
actually hold for a path, and ``readable_subtrees`` / ``writable_subtrees``
enumerate the grants an additive backend needs.  Ordering is an internal detail
of how this module records its answer, never part of the answer itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Iterable, Iterator

SANDBOX_MODE_RESTRICTED = "restricted"
SANDBOX_MODE_READ_ALL = "read_all"
SANDBOX_MODE_NONE = "none"
SANDBOX_MODES: tuple[str, ...] = (
    SANDBOX_MODE_RESTRICTED,
    SANDBOX_MODE_READ_ALL,
    SANDBOX_MODE_NONE,
)


class SandboxUnavailableError(RuntimeError):
    """Raised when no enforcing filesystem sandbox can be constructed."""


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

# Read-only state directories a restricted child still needs so tools can
# consume their own caches.  In read_all mode reads are open anyway and these
# are redundant but harmless.
_TOOL_STATE_HOME_SUBDIRS: tuple[str, ...] = (
    ".cache",
    ".npm",
    ".local",
    ".config",
    "Library/Caches",
    "Library/Application Support",
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


@dataclass(frozen=True)
class PathRule:
    """Rights this policy assigns to one subtree.

    ``read`` and ``write`` are tri-state: ``None`` means the rule says nothing
    about that access and leaves whatever a broader rule established.
    """

    path: str
    read: bool | None = None
    write: bool | None = None

    def covers(self, path: str) -> bool:
        return path == self.path or path.startswith(self.path.rstrip("/") + os.sep)


@dataclass(frozen=True)
class ResolvedSandboxPolicy:
    """The rights a sandboxed child gets, independent of how they are enforced.

    ``read_grants`` opens reads; ``restrictions`` opens writes broadly and then
    carves out the protected asset classes.  Both are recorded in resolution
    order — later entries refine earlier ones — but a backend should not read
    them positionally.  Ask :meth:`effective_rights`, or enumerate with
    :meth:`readable_subtrees` / :meth:`writable_subtrees`, and the ordering
    stops being your problem.
    """

    mode: str
    read_grants: tuple[PathRule, ...]
    restrictions: tuple[PathRule, ...]
    devices: bool
    network: bool
    #: True when reads start open host-wide (``read_all``) rather than closed.
    read_everywhere: bool
    #: True when writes start open host-wide before the carve-outs.
    write_everywhere: bool

    def _rules(self) -> Iterator[PathRule]:
        yield from self.read_grants
        yield from self.restrictions

    def effective_rights(self, path: str | Path) -> tuple[bool, bool]:
        """Return ``(readable, writable)`` for *path* under this policy.

        The last rule covering *path* wins, which is how the policy was
        recorded; callers see only the answer.
        """
        target = str(Path(path).resolve(strict=False))
        readable = self.read_everywhere
        writable = False
        for rule in self._rules():
            if not rule.covers(target):
                continue
            if rule.read is not None:
                readable = rule.read
            if rule.write is not None:
                writable = rule.write
        return readable, writable

    def readable_subtrees(self) -> tuple[str, ...]:
        """Paths granted read access, for backends that must enumerate grants.

        Meaningless when :attr:`read_everywhere` is set — an additive backend
        cannot express "everything except these", which is why Landlock
        downgrades ``read_all`` rather than approximating it.
        """
        return self._granted(read=True)

    def writable_subtrees(self) -> tuple[str, ...]:
        """Paths granted write access, for backends that must enumerate grants."""
        return self._granted(read=False)

    def _granted(self, *, read: bool) -> tuple[str, ...]:
        decided: dict[str, bool] = {}
        for rule in self._rules():
            value = rule.read if read else rule.write
            if value is None:
                continue
            decided[rule.path] = value
        return tuple(path for path, allowed in decided.items() if allowed)

    def denied_subtrees(self, *, read: bool) -> tuple[str, ...]:
        """Paths explicitly refused, so a backend can report what it cannot express."""
        decided: dict[str, bool] = {}
        for rule in self._rules():
            value = rule.read if read else rule.write
            if value is None:
                continue
            decided[rule.path] = value
        return tuple(path for path, allowed in decided.items() if not allowed)


def _both_spellings(paths: Iterable[str]) -> list[str]:
    """Expand each path to its literal and canonical spelling, order-stable.

    Enforcement happens on the canonical path, so a relocated directory — a
    ``~/Documents`` symlinked to an external volume, a Dropbox/iCloud folder,
    ``/var`` vs ``/private/var`` — slips through a rule that names only one
    spelling.  Emitting both keeps the rule effective whichever way the link
    exists, including when it is created after this policy was resolved.
    """
    expanded: list[str] = []
    for raw in paths:
        link_path = Path(raw)
        for spelling in (link_path, link_path.resolve(strict=False)):
            candidate = str(spelling)
            if candidate not in expanded:
                expanded.append(candidate)
    return expanded


def _path_is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def resolve_policy(request: ShellSandboxRequest) -> ResolvedSandboxPolicy:
    """Turn a request into the rights it implies, with no OS mechanism involved.

    This is where the inverted write policy is decided: writes open by default
    so every local tool (npm, pip, uv, git, HuggingFace, Chrome/Electron, MCP
    servers, …) can persist caches and app state without a per-tool carve-out,
    then three asset classes are denied — secrets (read *and* write),
    later-executed code, and user data.
    """
    # Canonical paths throughout: /var is a symlink to /private/var on macOS,
    # and a rule written for one spelling silently fails to match the other.
    workspace = str(request.workspace_root.resolve(strict=False))
    output = str(request.output_root.resolve(strict=False))
    scratch = str(request.scratch_dir.resolve(strict=False))
    internal = str((request.output_root / ".simple-internal").resolve(strict=False))
    home = str(request.home_dir.resolve(strict=False))

    read_grants: list[PathRule] = []
    read_everywhere = request.mode == SANDBOX_MODE_READ_ALL
    if read_everywhere:
        # Convenience default: reads are open everywhere, writes stay scoped.
        read_grants.append(PathRule("/", read=True))
    if request.workspace_read:
        read_grants.append(PathRule(workspace, read=True))
    # output_dir and scratch are always readable for generated artifacts.
    read_grants.append(PathRule(output, read=True))
    read_grants.append(PathRule(scratch, read=True))
    # Interpreter/runtime roots the child needs merely to start.
    for candidate in _both_spellings(request.extra_read_paths):
        read_grants.append(PathRule(candidate, read=True))
    for sub in _TOOL_STATE_HOME_SUBDIRS:
        read_grants.append(PathRule(f"{home}/{sub}", read=True))

    restrictions: list[PathRule] = [PathRule("/", write=True)]

    # 1. Secrets — read AND write denied.  Denying only writes leaves the
    #    actual attack (read a credential, POST it out over the open network)
    #    fully available, so the read rule is the load-bearing one here.
    for candidate in _both_spellings(
        f"{home}/{sub}"
        for sub in (*_SECRET_HOME_SUBDIRS, *request.extra_secret_paths)
    ):
        restrictions.append(PathRule(candidate, read=False, write=False))

    # 2. Later-executed code — write denied.  A writable ~/.zshrc or PATH
    #    directory turns any sandboxed write into unsandboxed execution the
    #    next time the user opens a shell, which defeats every rule above.
    for candidate in _both_spellings(
        [f"{home}/{sub}" for sub in _AUTOSTART_HOME_SUBDIRS]
        + list(_AUTOSTART_SYSTEM_DIRS)
    ):
        restrictions.append(PathRule(candidate, write=False))

    # 3a. The agent's own home: config.json holds provider API keys, and the
    #     memory/scheduler databases are the agent's integrity.  output_root
    #     and scratch normally live inside it, so they are reopened next.
    if request.agent_home is not None:
        for candidate in _both_spellings([str(request.agent_home)]):
            restrictions.append(PathRule(candidate, read=False, write=False))
        for reopened in (output, scratch):
            restrictions.append(PathRule(reopened, read=True, write=True))

    # 3b. Internal bookkeeping (locks, profiles, scratch internals) stays
    #     hidden.  Must follow the output_root re-open above: it lives inside.
    restrictions.append(PathRule(internal, read=False, write=False))

    # The workspace is not writable unless write_scope enables it.
    if not request.workspace_write and "*" not in request.write_scope:
        restrictions.append(PathRule(workspace, write=False))

    # 4. Protected user data (documents, media, personal library data).
    for candidate in _both_spellings(
        f"{home}/{sub}" for sub in _PROTECTED_HOME_SUBDIRS
    ):
        restrictions.append(PathRule(candidate, write=False))

    # Reopen the whole workspace when the policy grants it.  This MUST come
    # after the protected-data denies above: a workspace under ~/Desktop or
    # ~/Documents would otherwise have its write grant silently shadowed by
    # the later deny for those home subdirectories, so workspace.write=true
    # worked for some workspaces and not others.
    if request.workspace_write:
        restrictions.append(PathRule(workspace, write=True))

    # Approved write_scope entries reopen paths after their denies.
    for scope in request.write_scope:
        if scope == "*":
            restrictions.append(PathRule(workspace, write=True))
            continue
        candidate_path = request.workspace_root / scope
        if _path_is_within(candidate_path, request.workspace_root):
            restrictions.append(
                PathRule(str(candidate_path.resolve(strict=False)), write=True)
            )

    return ResolvedSandboxPolicy(
        mode=request.mode,
        read_grants=tuple(read_grants),
        restrictions=tuple(restrictions),
        devices=request.devices and request.mode != SANDBOX_MODE_NONE,
        network=True,
        read_everywhere=read_everywhere,
        write_everywhere=True,
    )


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
