from __future__ import annotations

import os
import re
import shlex
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ── Risk-level classification ────────────────────────────────────────────────
#
# These tables classify; they do not authorize.  The enforcement boundary for
# shell execution is the OS sandbox in ``filesystem_sandbox.py`` — an
# unbypassable rule about what the child process may touch — not a list of
# command names, which is defeated by any spelling the list did not
# anticipate (``rm`` vs ``find -delete`` vs ``python -c 'os.unlink(...)'``).
#
# What the tables below actually drive:
#   - ``HIGH_RISK_COMMANDS`` / ``HIGH_RISK_PATTERNS`` / ``_HIGH_RISK_OPTIONS``
#     require confirmation at permission level ``ask`` (the default) and are
#     auto-allowed at ``medium`` and above.  Nothing here is blocked
#     unconditionally.
#   - ``MEDIUM_RISK_COMMANDS`` drives the ``risk_level`` label only.  It gates
#     nothing at any permission level: these commands run without
#     confirmation, and the sandbox is what bounds their damage.  The label
#     reaches the user through the consent UI and tool output.
#
# Only two things are refused outright regardless of level: the
# user-configured blacklist (``extra_blocked``), and the structural guards in
# ``_structural_guard`` (inline ``cd``, command substitution) — those
# violate the tool contract rather than being merely risky.

# Commands whose damage is immediate and hard to undo.  Confirmed at level
# "ask"; auto-allowed from "medium" up.
HIGH_RISK_COMMANDS: frozenset[str] = frozenset(
    {
        "mkfs",
        "fdisk",
        "parted",
        "shred",
        "dd",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "init",
    }
)

# Commands that carry real consequences but are ordinary in development work.
# LABEL ONLY — this set does not gate execution at any permission level; it
# feeds ``ShellCheckResult.risk_level`` so the UI can show what a command
# touches.  Their blast radius is bounded by the OS sandbox, not by this list.
MEDIUM_RISK_COMMANDS: frozenset[str] = frozenset(
    {
        "rm",
        "rmdir",
        "mv",
        "cp",
        "chmod",
        "chown",
        "sudo",
        "kill",
        "pkill",
        "killall",
        "ssh",
        "scp",
        "sftp",
        "nc",
        "netcat",
        "curl",
        "wget",
        "ftp",
        "rsync",
        "passwd",
        "usermod",
        "groupadd",
        "useradd",
        "userdel",
        "groupdel",
        "eval",
        "exec",
        "bash",
        "sh",
        "zsh",
        "fish",
        "pip",
        "pip3",
        "npm",
        "yarn",
        "pnpm",
        "brew",
        "apt",
        "apt-get",
        "dnf",
        "yum",
        "pacman",
        "git",
    }
)

# Dangerous shell patterns checked as literal substrings.  Confirmed at level
# "ask"; auto-allowed from "medium" up.
HIGH_RISK_PATTERNS: tuple[str, ...] = (
    # pipe-to-shell
    "curl | sh",
    "wget | sh",
    "curl | bash",
    "wget | bash",
    "wget -O- |",
    "curl -s |",
    # redirect-to-device
    "> /dev/sd",
    "dd if=",
)

# Legacy medium-risk patterns retained for compatibility exports. Inline code
# execution is classified from parsed argv below.
MEDIUM_RISK_PATTERNS: tuple[str, ...] = (
    "python -c",
    "python3 -c",
    "perl -e",
    "ruby -e",
    "bash -c",
    "sh -c",
    "zsh -c",
)

_INLINE_INTERPRETERS = (
    (
        re.compile(r"python(?:\d+(?:\.\d+)*)?\Z"),
        frozenset({"-c"}),
        frozenset({"-W", "-X", "--check-hash-based-pycs"}),
    ),
    (
        re.compile(r"(?:bash|sh|zsh|fish)\Z"),
        frozenset({"-c"}),
        frozenset({"-O", "-o"}),
    ),
    (
        re.compile(r"perl\Z"),
        frozenset({"-e"}),
        frozenset({"-C", "-F", "-I", "-M", "-m"}),
    ),
    (
        re.compile(r"ruby\Z"),
        frozenset({"-e"}),
        frozenset(
            {"-0", "-C", "-E", "-F", "-I", "-K", "-R", "-T", "-W", "-r"}
        ),
    ),
    (
        re.compile(r"(?:node|deno|bun|osascript)\Z"),
        frozenset({"-e"}),
        frozenset(),
    ),
    (
        re.compile(r"php\Z"),
        frozenset({"-r"}),
        frozenset(),
    ),
    (
        re.compile(r"lua\Z"),
        frozenset({"-e"}),
        frozenset({"-l"}),
    ),
)

_HIGH_RISK_OPTIONS: dict[str, frozenset[str]] = {
    "find": frozenset({"-delete"}),
}

HIGH_RISK_SHELL_OPERATORS: frozenset[str] = frozenset(
    {
        "&&",
        "||",
        ";",
        "|",
        "&",
        "`",
        "$(",
        ">",
        ">>",
        "<",
        "<<",
        # A newline separates commands exactly as `;` does -- the tool runs the
        # string through ``/bin/sh -c`` -- so it is a control operator and not
        # whitespace.  Leaving it out made the *same* compound command free
        # when its lines were separated by a newline instead of a semicolon,
        # and it hid an inline ``cd`` from the structural guard below.
        "\n",
    }
)

CWD_ESCAPE_COMMANDS: frozenset[str] = frozenset({"cd", "pushd", "popd"})

# ── Composition: one command or several? ─────────────────────────────────────
#
# An operator is not a risk, it is a *separator*.  `pytest -q 2>&1 | tail -20`
# is two commands nobody needs to be asked about, and treating the `|` as the
# dangerous part meant the default level asked about 19% of ordinary commands
# while `rm -rf /` ran unchallenged (measured; see
# ``scripts/permission_prompt_probe.py``).  So the string is split into
# segments and each segment is classified on its own merits.
#
# Two things survive from the operator-era rules, because they are about
# *authority* rather than syntax:
#
# * a pipe into an interpreter (`curl … | sh`) runs text nothing classified --
#   the right-hand command is the risk, and it is now caught by name instead of
#   by the presence of a pipe;
# * a redirect writes a file, so its *target* is checked against the same roots
#   the sandbox grants (`> ~/.zshrc` asks, `> build/out.txt` does not).

#: Tokens that separate one command from the next inside one ``sh -c`` string.
#: ``;``/``&``/newline run the next segment regardless; ``&&``/``||`` make it
#: conditional, which changes nothing about what the segment itself may do.
_SEQUENCE_OPERATORS: frozenset[str] = frozenset({";", "&", "&&", "||", "\n"})
#: Separators that also make the following segment read the previous one's output.
_PIPE_OPERATORS: frozenset[str] = frozenset({"|", "|&"})
_SEGMENT_OPERATORS: frozenset[str] = _SEQUENCE_OPERATORS | _PIPE_OPERATORS

#: Redirections whose next word names a filesystem target.
_WRITE_REDIRECTS: frozenset[str] = frozenset({">", ">>"})
#: Redirections that only read, or duplicate a descriptor: no path authority.
_READ_REDIRECTS: frozenset[str] = frozenset({"<", "<<", "<<<", "<&", ">&", "<>"})

#: Redirect targets that name a device or descriptor rather than a file.
_IGNORED_REDIRECT_TARGETS: frozenset[str] = frozenset(
    {
        "-",
        "/dev/null",
        "/dev/zero",
        "/dev/stdin",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/tty",
        "/dev/fd/0",
        "/dev/fd/1",
        "/dev/fd/2",
    }
)

#: Interpreters that will execute a program fed to them on stdin.  A pipe into
#: one of these hands it text that was never classified, which is the one thing
#: a pipe can do that the per-segment check cannot see.
_STDIN_INTERPRETER_NAMES: tuple[str, ...] = (
    "sh",
    "bash",
    "zsh",
    "fish",
    "dash",
    "ksh",
    "csh",
    "tcsh",
    "python",
    "perl",
    "ruby",
    "node",
    "deno",
    "bun",
    "php",
    "lua",
    "tclsh",
    "wish",
    "osascript",
    "rscript",
)

_STDIN_INTERPRETER_RE = re.compile(
    r"(?:" + "|".join(_STDIN_INTERPRETER_NAMES) + r")(?:\d+(?:\.\d+)*)?\Z"
)


def _is_stdin_interpreter(command_name: str) -> bool:
    """True when *command_name* executes a program read from standard input."""
    return bool(_STDIN_INTERPRETER_RE.match(str(command_name or "").lower()))


def _segment_command(
    tokens: list[str],
) -> list[tuple[Optional[str], list[str]]]:
    """Split tokenized *tokens* into ``(introducing_operator, segment)`` pairs.

    The operator recorded with a segment is the one that introduced it, so a
    segment preceded by ``|`` knows it is reading the previous segment's output
    while a segment preceded by ``;`` merely follows it.  A token is only ever
    read as an operator when it is standalone -- ``shlex`` keeps ``;`` inside
    quotes as part of its word -- so segmentation cannot be fooled by quoting.
    """
    segments: list[tuple[Optional[str], list[str]]] = []
    current: list[str] = []
    incoming: Optional[str] = None
    for token in tokens:
        if token in _SEGMENT_OPERATORS:
            if current:
                segments.append((incoming, current))
            current = []
            incoming = token
            continue
        current.append(token)
    if current:
        segments.append((incoming, current))
    return segments


def _redirect_targets(segment: list[str]) -> list[str]:
    """The filesystem targets a segment writes to, in order."""
    targets: list[str] = []
    for index, token in enumerate(segment):
        if token not in _WRITE_REDIRECTS:
            continue
        if index + 1 < len(segment):
            targets.append(segment[index + 1])
    return targets


def _target_outside_roots(
    target: str,
    *,
    allowed_roots: frozenset[Path] | None,
) -> bool:
    """True when writing *target* reaches outside what the sandbox grants.

    A relative target lands in the command's own working directory, which is the
    workspace or the output directory, so it is inside.  An absolute target is
    the authority question: it is inside exactly where the sandbox already
    grants writes -- the roots the tool passes in, plus the process's temporary
    directory, because a scratch file is not a boundary crossing.
    """
    raw = str(target or "").strip()
    if not raw or raw in _IGNORED_REDIRECT_TARGETS:
        return False
    if raw.startswith("&") or raw.isdigit():
        # `2>&1` and friends duplicate a descriptor; no file is named.
        return False
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        return False
    try:
        resolved = expanded.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        # A path that cannot be resolved is treated as outside: fail closed.
        return True
    return not any(
        resolved == root or root in resolved.parents
        for root in _write_roots(allowed_roots)
    )


def _write_boundary_note(allowed_roots: frozenset[Path] | None) -> str:
    roots = _write_roots(allowed_roots)
    if not roots:
        return "the workspace"
    return ", ".join(str(root) for root in roots)


def _redirect_target_reason(
    target: str,
    *,
    allowed_roots: frozenset[Path] | None,
) -> Optional[str]:
    """Why a redirect to *target* needs a human, or ``None`` when it does not."""
    if not _target_outside_roots(target, allowed_roots=allowed_roots):
        return None
    verb = "the path could not be resolved" if _unresolvable(target) else "writes"
    return (
        f"a redirect to '{target}' {verb} outside the workspace "
        f"(writes are granted inside {_write_boundary_note(allowed_roots)})"
    )


def _argument_write_reason(
    segment: list[str],
    index: int,
    *,
    allowed_roots: frozenset[Path] | None,
) -> Optional[str]:
    """Why a command's own arguments need a human, or ``None``.

    This is what the old operator confirmation was accidentally catching: the
    default level asked about `echo start && rm -rf ~/Documents` because of the
    ``&&``, while `rm -rf ~/Documents` on its own line was never looked at.  The
    question the human is actually answering is "may this reach outside the
    workspace to change something", so it is asked about the *target*, whatever
    the spelling and whether or not an operator is present.
    """
    command_name = os.path.basename(segment[index].strip().lstrip("./"))
    entry = _ARGUMENT_WRITE_COMMANDS.get(command_name.lower())
    if entry is None:
        return None
    _mode, verb = entry
    for target in _argument_write_targets(segment, index):
        if _target_outside_roots(target, allowed_roots=allowed_roots):
            return (
                f"'{command_name}' would {verb} '{target}', which is outside "
                f"the workspace (writes are granted inside "
                f"{_write_boundary_note(allowed_roots)})"
            )
    return None


def _unresolvable(target: str) -> bool:
    try:
        Path(str(target or "").strip()).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return True
    return False


def _write_roots(
    allowed_roots: frozenset[Path] | None,
) -> tuple[Path, ...]:
    """Paths a write may name without asking: granted roots plus temp."""
    import tempfile

    roots: list[Path] = []
    for candidate in allowed_roots or ():
        roots.append(Path(candidate).resolve(strict=False))
    for candidate in (tempfile.gettempdir(), "/tmp", "/var/tmp"):
        try:
            resolved = Path(candidate).resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)

# ── Session allowlist ───────────────────────────────────────────────────────

CONFIRMATION_TTL = timedelta(minutes=5)

# Sentinel expiry for "always allow this command" approvals.  The allowlist
# itself is process state keyed by authorization scope, so a far-future
# timestamp is exactly "until the session goes away" without a second
# container to keep in sync.
NEVER_EXPIRES = datetime.max.replace(tzinfo=timezone.utc)

# Permission levels, from most restrictive to least.  ``ask`` is the default:
# low- and medium-risk commands run without confirmation; only high-risk
# constructs (destructive commands/options, shell operators, pipe-to-shell
# patterns) ask the human, and they become runnable after approval.  User
# blacklist and structural guards (cwd escape, command substitution) stay
# unconditional.  ``medium`` additionally auto-allows high-risk commands,
# options, and pipe patterns (plain operators still ask); ``high``
# auto-allows everything except the blacklist and structural guards;
# ``full`` is an alias of ``high`` for readability.
PERMISSION_LEVELS: tuple[str, ...] = ("ask", "medium", "high", "full")


@dataclass(frozen=True)
class ShellAuthorizationScope:
    session_id: str
    channel_name: str
    user_id: str = ""


@dataclass(frozen=True)
class PendingShellConfirmation:
    token: str
    command: str
    scope: ShellAuthorizationScope
    expires_at: datetime


_DEFAULT_SCOPE = ShellAuthorizationScope("default", "cli", "")
_session_allowlist: dict[tuple[ShellAuthorizationScope, str], datetime] = {}
_pending_tokens: dict[str, PendingShellConfirmation] = {}
# Tokens the human approved with "always allow this command".  Redeeming such
# a token widens the allowlist entry from the pending token's TTL to the whole
# scope, so the mark has to travel with the token rather than with the reply.
_pending_session_scope: set[str] = set()
_session_auto_approve: set[ShellAuthorizationScope] = set()
_session_permission_levels: dict[ShellAuthorizationScope, str] = {}
_session_sandbox_modes: dict[ShellAuthorizationScope, str] = {}


def _authorization_scope(
    scope: ShellAuthorizationScope | None,
) -> ShellAuthorizationScope:
    return scope or _DEFAULT_SCOPE


def shell_authorization_scope_from_active_context() -> ShellAuthorizationScope:
    """The scope of the agent turn that is running, or a safe default.

    A scope is what the approval machinery, the session allowlist and the
    per-session permission level are keyed on, so two spellings of it are two
    answers to "whose permission is this".  The shell tool and the plugin
    installer each used to build one from the same three metadata keys with the
    same three fallbacks; this is that construction, once.

    The import is local because ``agent.core.agent`` reaches back into this
    package's callers, and the fallbacks are deliberately the *narrowest*
    reading of an absent context: a session nobody can name is "default" on the
    "cli" channel with no user, never a broader scope than what was recorded.
    """
    from agent.core.agent import _active_agent_context

    active_context = _active_agent_context.get()
    metadata = active_context.metadata if active_context is not None else {}
    return ShellAuthorizationScope(
        str(metadata.get("session_id") or "default"),
        str(metadata.get("channel_name") or "cli"),
        str(metadata.get("user_id") or ""),
    )


def _authorization_time(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_command(command: str) -> str:
    return str(command).strip()


def shell_session_allowlist_add(
    command_base: str,
    *,
    scope: ShellAuthorizationScope | None = None,
    now: datetime | None = None,
    expires_at: datetime | None = None,
) -> None:
    """Allow one normalized command in one authorization scope until expiry."""
    current = _authorization_time(now)
    expiry = _authorization_time(expires_at) if expires_at else current + CONFIRMATION_TTL
    _session_allowlist[
        (_authorization_scope(scope), _normalize_command(command_base))
    ] = expiry


def shell_session_allowlist_add_persistent(
    command_base: str,
    *,
    scope: ShellAuthorizationScope | None = None,
    now: datetime | None = None,
) -> None:
    """Allow one normalized command in one scope for the rest of the session.

    Used when the human answers an approval prompt with "always allow this
    command" instead of the one-off consent that ``shell_command_confirm``
    grants.  Scope, not the command, is what the entry is keyed by, so this
    never leaks across sessions or channels.
    """
    shell_session_allowlist_add(
        command_base,
        scope=scope,
        now=now,
        expires_at=NEVER_EXPIRES,
    )


def shell_session_allowlist_clear() -> None:
    """Clear all entries from the session allowlist."""
    _session_allowlist.clear()
    _pending_tokens.clear()
    _pending_session_scope.clear()
    _session_auto_approve.clear()
    _session_permission_levels.clear()
    _session_sandbox_modes.clear()


def shell_session_auto_approve_enable(scope: ShellAuthorizationScope) -> None:
    """Skip medium-risk confirmation for every command in one scope."""
    shell_session_permission_set(scope, "medium")


def shell_session_auto_approve_disable(scope: ShellAuthorizationScope) -> None:
    """Re-enable per-command confirmation for one scope."""
    shell_session_permission_set(scope, "ask")


def shell_session_auto_approve_status(scope: ShellAuthorizationScope) -> bool:
    """Return True when medium-risk commands bypass confirmation in *scope*."""
    return shell_session_permission_get(scope) in ("medium", "high", "full")


def shell_session_permission_set(
    scope: ShellAuthorizationScope, level: str
) -> None:
    """Override the permission level for one session/channel/user scope."""
    normalized = str(level or "").strip().casefold()
    if normalized in PERMISSION_LEVELS:
        _session_permission_levels[scope] = normalized
    else:
        _session_permission_levels.pop(scope, None)


def shell_session_permission_get(scope: ShellAuthorizationScope) -> str:
    """Return the session override ("" when the config default applies)."""
    return _session_permission_levels.get(scope, "")


def shell_session_permission_clear(scope: ShellAuthorizationScope) -> None:
    """Remove the session permission override so the config default applies."""
    _session_permission_levels.pop(scope, None)


def _effective_permission_level(
    scope: ShellAuthorizationScope, permission_level: Optional[str]
) -> str:
    session_override = shell_session_permission_get(scope)
    if session_override:
        return session_override
    value = str(permission_level or "ask").strip().casefold()
    return value if value in PERMISSION_LEVELS else "ask"


def shell_effective_permission_level(
    scope: ShellAuthorizationScope,
    permission_level: Optional[str] = None,
) -> str:
    """Return the effective level for *scope* (session override wins)."""
    return _effective_permission_level(scope, permission_level)


def shell_session_sandbox_set(
    scope: ShellAuthorizationScope, mode: str
) -> None:
    """Override the shell sandbox mode for one session/channel/user scope."""
    from agent.security.filesystem_sandbox import SANDBOX_MODES

    normalized = str(mode or "").strip().casefold()
    if normalized in SANDBOX_MODES:
        _session_sandbox_modes[scope] = normalized
    else:
        _session_sandbox_modes.pop(scope, None)


def shell_session_sandbox_get(scope: ShellAuthorizationScope) -> str:
    """Return the session sandbox override ("" = config default applies)."""
    return _session_sandbox_modes.get(scope, "")


def shell_session_sandbox_clear(scope: ShellAuthorizationScope) -> None:
    """Remove the session sandbox override so the config default applies."""
    _session_sandbox_modes.pop(scope, None)


def shell_session_allowlist_contains(
    command_base: str,
    *,
    scope: ShellAuthorizationScope | None = None,
    now: datetime | None = None,
) -> bool:
    """Check for an unexpired exact command approval in one scope."""
    key = (_authorization_scope(scope), _normalize_command(command_base))
    expiry = _session_allowlist.get(key)
    if expiry is None:
        return False
    if expiry <= _authorization_time(now):
        _session_allowlist.pop(key, None)
        return False
    return True


def shell_session_allowlist_commands(
    *,
    scope: ShellAuthorizationScope | None = None,
    now: datetime | None = None,
) -> list[str]:
    """List the commands currently approved for one scope, longest-lived last.

    Expired entries are pruned on the way out so the caller never reports an
    approval the next check would reject anyway.
    """
    resolved = _authorization_scope(scope)
    current = _authorization_time(now)
    result: list[tuple[datetime, str]] = []
    for (entry_scope, command), expiry in list(_session_allowlist.items()):
        if entry_scope != resolved:
            continue
        if expiry <= current:
            _session_allowlist.pop((entry_scope, command), None)
            continue
        result.append((expiry, command))
    result.sort(key=lambda pair: (pair[0], pair[1]))
    return [command for _expiry, command in result]


def shell_session_allowlist_clear_scope(
    scope: ShellAuthorizationScope | None = None,
) -> int:
    """Revoke every session approval for one scope; return the count removed.

    This is the escape hatch for an "always allow" answer: consent that can be
    granted but never taken back is not consent.  Outstanding prompts are left
    alone — they are live questions, and revoking past answers must not silently
    answer them.
    """
    resolved = _authorization_scope(scope)
    removed = 0
    for key in [key for key in _session_allowlist if key[0] == resolved]:
        _session_allowlist.pop(key, None)
        removed += 1
    return removed


def shell_command_uses_shell_features(command: str) -> bool:
    """Return True when *command* depends on shell parsing/control features."""
    try:
        return _find_shell_operator(command) is not None
    except _ShellParseError:
        return True


def shell_session_scope_supported(
    name: str, confirmation_token: str, scope: Any
) -> bool:
    """Whether "always allow this" is meaningful for one consent prompt.

    Only a shell approval carries the two things a session-wide entry is keyed
    by: a redeemable pending token and an authorization scope.  An install, an
    activation and a memory wipe are one-shot, so a surface that offered the
    option for them would be promising something the redemption cannot deliver.

    Asked once, here, because two surfaces now have to give the same answer:
    the browser hides the button on it and the terminal hides the menu entry on
    it, and a duplicate of this predicate that drifted would mean one of them
    offering an answer the other refuses.
    """
    if str(name or "") != "shell" or not str(confirmation_token or ""):
        return False
    return isinstance(scope, ShellAuthorizationScope)


# ── ShellCheckResult ────────────────────────────────────────────────────────


@dataclass
class ShellCheckResult:
    """Structured result of a shell command safety check."""

    allowed: bool
    risk_level: str  # 'low' | 'medium' | 'high'
    reason: str
    requires_confirmation: bool = False
    confirmation_token: str = ""

    # Backward-compatible truthiness: non-None means "blocked"
    def __bool__(self) -> bool:
        """Truthy when the command is NOT allowed (backward compat)."""
        return not self.allowed


def shell_pending_mark_session_scope(
    token: str,
    *,
    scope: ShellAuthorizationScope,
) -> bool:
    """Record that *token* was approved for the whole session, not just once.

    The consent UI knows the human's intent but not the pending record, and
    the caller that redeems the token (``shell_command_confirm``) knows the
    record but not the intent.  This is the hand-off between the two: without
    it the "always allow" answer would decay into the same five-minute
    allowlist entry as a plain approval.
    """
    stored = _pending_tokens.get(str(token))
    if stored is None or stored.scope != scope:
        return False
    _pending_session_scope.add(str(token))
    return True


def shell_command_confirm(
    token: str,
    *,
    scope: ShellAuthorizationScope,
    now: datetime | None = None,
) -> bool:
    """Redeem one pending token using trusted request identity."""
    current = _authorization_time(now)
    key = str(token)
    stored = _pending_tokens.get(key)
    if stored is None or stored.scope != scope or stored.expires_at <= current:
        if stored is not None and stored.expires_at <= current:
            _pending_tokens.pop(key, None)
        _pending_session_scope.discard(key)
        return False
    _pending_tokens.pop(key, None)
    if key in _pending_session_scope:
        # "Always allow this command": outlive the token that carried the
        # approval instead of inheriting its five-minute window.
        _pending_session_scope.discard(key)
        shell_session_allowlist_add_persistent(
            stored.command,
            scope=stored.scope,
            now=current,
        )
        return True
    shell_session_allowlist_add(
        stored.command,
        scope=stored.scope,
        now=current,
        expires_at=stored.expires_at,
    )
    return True


def shell_pending_reject(
    token: str,
    *,
    scope: ShellAuthorizationScope,
) -> bool:
    """Retire one pending token because the human refused it.

    A refusal has to be a terminal state.  Without this the record simply stays
    pending until its TTL lapses, so a later unrelated approval reply ("yes",
    "批准") can redeem the very command the human just declined.
    """
    stored = _pending_tokens.get(str(token))
    if stored is None or stored.scope != scope:
        return False
    _pending_tokens.pop(str(token), None)
    _pending_session_scope.discard(str(token))
    return True


def shell_pending_for_scope(
    scope: ShellAuthorizationScope,
    now: datetime | None = None,
) -> list[PendingShellConfirmation]:
    """Return unexpired pending confirmations for one authorization scope."""
    current = _authorization_time(now)
    return [
        record
        for token, record in list(_pending_tokens.items())
        if record.scope == scope and record.expires_at > current
    ]


def shell_approve_single_pending(
    scope: ShellAuthorizationScope,
    now: datetime | None = None,
) -> Optional[str]:
    """Approve the sole pending confirmation for *scope*; return its command.

    Chat approval is unambiguous only when exactly one unexpired pending
    confirmation exists for the scope.  Returns the normalized approved
    command when it was redeemed; returns ``None`` when there is nothing
    pending, several pending records exist (ambiguous), or redemption
    failed.  Callers must never treat ``None`` as approval.
    """
    pending = shell_pending_for_scope(scope, now=now)
    if len(pending) != 1:
        return None
    record = pending[0]
    if shell_command_confirm(record.token, scope=scope, now=now):
        return record.command
    return None


def _pending_confirmation(
    command: str,
    *,
    scope: ShellAuthorizationScope,
    now: datetime,
) -> str:
    normalized_command = _normalize_command(command)
    for stale_token, record in list(_pending_tokens.items()):
        if record.expires_at <= now:
            _pending_tokens.pop(stale_token, None)
        elif record.scope == scope and record.command == normalized_command:
            # Reuse the outstanding approval instead of minting a fresh token
            # on every retry; otherwise the human's approval could never be
            # matched to the next identical attempt.
            return record.token
    token = str(uuid.uuid4())
    _pending_tokens[token] = PendingShellConfirmation(
        token=token,
        command=normalized_command,
        scope=scope,
        expires_at=now + CONFIRMATION_TTL,
    )
    return token


# ── Internal helpers ─────────────────────────────────────────────────────────


class _ShellParseError(ValueError):
    pass


def _parse_command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError as exc:
        raise _ShellParseError(str(exc)) from exc


def _is_env_assignment(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", token))


def _skip_env_wrapper(tokens: list[str], idx: int) -> int:
    """Advance past an ``env`` invocation, returning the next token's index.

    ``env -S`` / ``--split-string`` expands one word into several, so this may
    rewrite ``tokens`` in place; the returned index refers to the rewritten list.
    """
    idx += 1
    while idx < len(tokens):
        token = tokens[idx]
        if token == "--":
            return idx + 1
        if _is_env_assignment(token):
            idx += 1
            continue
        if token == "--split-string":
            if idx + 1 < len(tokens):
                split_tokens = _parse_command_tokens(tokens[idx + 1])
                tokens[idx : idx + 2] = split_tokens
                continue
            idx += 1
            continue
        if token.startswith("--split-string="):
            split_tokens = _parse_command_tokens(token.partition("=")[2])
            tokens[idx : idx + 1] = split_tokens
            continue
        if token.startswith("--"):
            idx += 1
            if token in {"--chdir", "--unset"} and idx < len(tokens):
                idx += 1
            continue
        if token.startswith("-") and token != "-":
            option_chars = token[1:]
            value_option = min(
                (
                    (option_chars.index(option), option)
                    for option in "CSu"
                    if option in option_chars
                ),
                default=None,
            )
            if value_option is None:
                idx += 1
                continue

            value_option_index, option = value_option
            attached_value = option_chars[value_option_index + 1 :]
            if option == "S":
                if attached_value:
                    split_tokens = _parse_command_tokens(attached_value)
                    tokens[idx : idx + 1] = split_tokens
                elif idx + 1 < len(tokens):
                    split_tokens = _parse_command_tokens(tokens[idx + 1])
                    tokens[idx : idx + 2] = split_tokens
                else:
                    idx += 1
                continue

            idx += 1 if attached_value else 2
            continue
        break
    return idx


# Options that consume the word after them, so the wrapper skipper must step
# over two tokens rather than one.
_SUDO_VALUE_OPTIONS = frozenset({
    "-g", "--group", "-h", "--host", "-p", "--prompt",
    "-R", "--chroot", "-r", "--role", "-t", "--type",
    "-u", "--user",
})


def _skip_sudo_wrapper(tokens: list[str], idx: int) -> int:
    """Advance past a ``sudo`` invocation and its options."""
    idx += 1
    while idx < len(tokens):
        token = tokens[idx]
        if token == "--":
            return idx + 1
        if token.startswith("-"):
            idx += 1
            if token in _SUDO_VALUE_OPTIONS and idx < len(tokens):
                idx += 1
            continue
        break
    return idx


def _resolve_effective_command_index(tokens: list[str]) -> Optional[int]:
    """Index of the word that actually runs, past any `env`/`sudo` wrapper."""
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if _is_env_assignment(token):
            idx += 1
            continue

        cmd = os.path.basename(token.strip().lstrip("./"))
        if cmd == "env":
            idx = _skip_env_wrapper(tokens, idx)
            continue
        if cmd == "sudo":
            idx = _skip_sudo_wrapper(tokens, idx)
            continue
        return idx
    return None


def _resolve_effective_command(tokens: list[str]) -> Optional[str]:
    idx = _resolve_effective_command_index(tokens)
    if idx is None:
        return None
    return os.path.basename(tokens[idx].strip().lstrip("./"))


def _parse_shell_tokens(command: str) -> list[str]:
    try:
        # Include the backtick in the punctuation set so command substitution
        # via backticks (``echo `id```) is tokenized as a standalone operator
        # and caught by _find_shell_operator.  shlex's default punctuation
        # ("();<>|&") leaves backticks glued to the surrounding word, so a
        # backtick pair never matched the lone "`" in HIGH_RISK_SHELL_OPERATORS.
        #
        # The newline is punctuation for the same kind of reason: shlex treats
        # it as whitespace, which is exactly what a shell does *not* do with it,
        # so it is moved out of ``whitespace`` and into the punctuation set to
        # come back as a token.  A newline inside quotes stays part of its word,
        # because a quoted character is never punctuation -- which is right: it
        # separates nothing there.
        lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&`\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError as exc:
        raise _ShellParseError(str(exc)) from exc


def _iter_command_words(command: str) -> list[str]:
    words: list[str] = []
    for token in _parse_shell_tokens(command):
        if token in HIGH_RISK_SHELL_OPERATORS:
            continue
        if token in {"(", ")", "{", "}"}:
            continue
        words.append(os.path.basename(token.strip().lstrip("./")))
    return words


def _find_shell_operator(command: str) -> Optional[str]:
    for token in _parse_shell_tokens(command):
        if token in HIGH_RISK_SHELL_OPERATORS:
            return token
    if "$(" in command:
        return "$("
    if "`" in command:
        return "`"
    return None


# ── Python environment mutation ──────────────────────────────────────────────
#
# Installing a package is a durable change to something the agent does not own:
# the user's project manifest, its lockfile, or the interpreter every other
# program on the machine shares.  The agent reaches for it most often for its
# own convenience — a generated tool wants a library — and that motive should
# never spend the user's project.  `install_tool_dependency` covers that case
# without touching anything outside ``~/.agent/tools/_deps``, so these commands
# are worth a human decision even when the session is otherwise unrestricted.

_PYTHON_COMMAND_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")

#: Flags that redirect an install away from the ambient/project environment.
_INSTALL_ISOLATION_FLAGS = ("--target", "-t", "--prefix", "--root")

_MANIFEST_SUBCOMMANDS = {"add", "remove"}


def _install_targets_isolated_dir(args: list[str]) -> bool:
    return any(
        arg == flag or arg.startswith(f"{flag}=")
        for arg in args
        for flag in _INSTALL_ISOLATION_FLAGS
    )


def _first_subcommand(args: list[str]) -> str:
    for arg in args:
        if not arg.startswith("-"):
            return arg
    return ""


def _pip_mutation_reason(args: list[str], label: str) -> Optional[str]:
    subcommand = _first_subcommand(args)
    if subcommand not in {"install", "uninstall"}:
        return None
    if subcommand == "install" and _install_targets_isolated_dir(args):
        return None
    return (
        f"'{label} {subcommand}' changes the Python environment shared with "
        "the user's project. To give an agent tool a library, use the "
        "install_tool_dependency tool, which installs into the agent's "
        "private dependency directory instead."
    )


def _environment_mutation_reason(
    tokens: list[str], command_index: int
) -> Optional[str]:
    """Why this command would mutate the project or ambient Python env."""
    words = tokens[command_index:]
    if not words:
        return None
    name = os.path.basename(words[0].strip().lstrip("./"))
    args = words[1:]

    # `python -m pip install x` is the same act as `pip install x`.
    if _PYTHON_COMMAND_RE.fullmatch(name):
        if args and args[0] == "-m" and len(args) > 1:
            name, args = os.path.basename(args[1]), args[2:]
        elif args and args[0].startswith("-m") and len(args[0]) > 2:
            name, args = os.path.basename(args[0][2:]), args[1:]
        else:
            return None

    if name in {"pip", "pip3"}:
        return _pip_mutation_reason(args, name)

    if name == "uv":
        subcommand = _first_subcommand(args)
        if subcommand == "pip":
            rest = args[args.index("pip") + 1 :]
            return _pip_mutation_reason(rest, "uv pip")
        if subcommand in _MANIFEST_SUBCOMMANDS:
            return (
                f"'uv {subcommand}' rewrites the project's pyproject.toml and "
                "uv.lock. If this is for an agent tool rather than the "
                "project, use the install_tool_dependency tool instead."
            )
        return None

    if name == "poetry":
        subcommand = _first_subcommand(args)
        if subcommand in _MANIFEST_SUBCOMMANDS:
            return (
                f"'poetry {subcommand}' rewrites the project's pyproject.toml "
                "and poetry.lock. If this is for an agent tool rather than "
                "the project, use the install_tool_dependency tool instead."
            )
        return None

    if name == "pipenv":
        subcommand = _first_subcommand(args)
        if subcommand not in {"install", "uninstall"}:
            return None
        # A bare `pipenv install` restores the existing Pipfile; naming a
        # package is what edits it.
        named = [
            arg
            for arg in args[args.index(subcommand) + 1 :]
            if not arg.startswith("-")
        ]
        if subcommand == "install" and not named:
            return None
        return (
            f"'pipenv {subcommand}' rewrites the project's Pipfile. If this "
            "is for an agent tool rather than the project, use the "
            "install_tool_dependency tool instead."
        )

    return None


def _is_inline_execution(tokens: list[str], command_index: int) -> bool:
    command_name = os.path.basename(tokens[command_index].strip().lstrip("./"))
    for pattern, execution_flags, options_with_values in _INLINE_INTERPRETERS:
        if pattern.fullmatch(command_name):
            idx = command_index + 1
            while idx < len(tokens):
                token = tokens[idx]
                if token == "--":
                    return False
                if token == "-":
                    # The program arrives on stdin (``python3 - <<'PY'``), so
                    # there is nothing on the command line to classify.  This is
                    # the same opacity the ``-c`` flags exist to catch, and the
                    # heredoc that supplies it is no longer an operator-level
                    # confirmation of its own.
                    return True
                if token.startswith(tuple(execution_flags)):
                    return True
                if token in options_with_values:
                    idx += 2
                    continue
                if not token.startswith("-"):
                    return False
                idx += 1
            return False
    return False


def _is_script_execution(tokens: list[str], command_index: int) -> bool:
    """Return True when an interpreter command runs a script/module file.

    Inline code (``-c``/``-e``/``-r``) is already medium risk.  A script
    file argument is equally arbitrary code execution — e.g.
    ``ruby wb.rb navigate ...`` or ``python3 env_check.py`` — so it must
    require the same confirmation instead of being classified "low risk"
    and silently bypassing the curl/wget network confirmation.
    """
    command_name = os.path.basename(tokens[command_index].strip().lstrip("./"))
    for pattern, _execution_flags, options_with_values in _INLINE_INTERPRETERS:
        if pattern.fullmatch(command_name):
            idx = command_index + 1
            while idx < len(tokens):
                token = tokens[idx]
                if token == "--":
                    return idx + 1 < len(tokens)
                if token == "-" or not token.startswith("-"):
                    # "-" reads a script from stdin; any other positional is
                    # a script/module file.
                    return True
                if token in options_with_values:
                    idx += 2
                    continue
                idx += 1
            return False
    return False


def _find_high_risk_option(
    tokens: list[str], command_index: int
) -> Optional[str]:
    command_name = os.path.basename(tokens[command_index].strip().lstrip("./"))
    high_risk_options = _HIGH_RISK_OPTIONS.get(command_name, frozenset())
    return next(
        (
            token
            for token in tokens[command_index + 1 :]
            if token in high_risk_options
        ),
        None,
    )


#: Command-name prefixes whose every spelling destroys a disk.  ``mkfs`` alone
#: was in ``HIGH_RISK_COMMANDS`` and real invocations spell it ``mkfs.ext4``,
#: ``mkfs.vfat`` … — an exact-name match missed the only form anybody types
#: (measured: ``mkfs.ext4 /dev/sda1`` ran unasked at the default level).
_HIGH_RISK_COMMAND_PREFIXES: tuple[str, ...] = ("mkfs", "newfs")

#: Subcommands that make an otherwise-inert utility destructive, keyed by the
#: command whose *first* operand is the subcommand.
_HIGH_RISK_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "diskutil": frozenset(
        {
            "erasedisk",
            "erasevolume",
            "reformat",
            "zerodisk",
            "randomdisk",
            "partitiondisk",
            "deletevolume",
            "deletecontainer",
            "apfsdeletecontainer",
        }
    ),
}


def _high_risk_command_reason(segment: list[str], index: int) -> Optional[str]:
    """Why *segment*'s own command is destructive, or ``None``."""
    command_name = os.path.basename(segment[index].strip().lstrip("./"))
    lowered = command_name.lower()
    if command_name in HIGH_RISK_COMMANDS:
        return f"command '{command_name}' is high risk: disk/system destruction"
    if lowered.startswith(_HIGH_RISK_COMMAND_PREFIXES) and lowered != "mkfs":
        return f"command '{command_name}' is high risk: disk/system destruction"
    subcommands = _HIGH_RISK_SUBCOMMANDS.get(lowered)
    if subcommands:
        first_operand = next(
            (
                token
                for token in segment[index + 1 :]
                if not token.startswith("-")
            ),
            "",
        )
        if first_operand.lower() in subcommands:
            return (
                f"command '{command_name} {first_operand}' is high risk: "
                "disk/system destruction"
            )
    return None


#: Flags that make ``rm``'s reach unreadable: with a wildcard the set of paths
#: being deleted is not in the command, it is in the filesystem.
_GLOB_CHARS: tuple[str, ...] = ("*", "?", "[")
#: Operands that name "everything here" without naming anything.
_WHOLE_DIRECTORY_OPERANDS: frozenset[str] = frozenset({".", "./", "..", "../"})


def _workspace_irreversible_reason(
    segment: list[str], index: int
) -> Optional[str]:
    """Why *segment* destroys something inside the workspace irreversibly.

    The second half of the gate's sentence ("or when its effect cannot be undone
    inside it").  These are the commands whose damage the workspace boundary
    does **not** bound, because the loss is uncommitted work: removing untracked
    files, discarding a working tree, or deleting through a wildcard so that what
    disappears is decided by what happens to be on disk.

    Deliberately short and deliberately not "everything that writes".  `rm -rf
    node_modules`, `rm -rf build`, `truncate` on a named file and every ordinary
    build command stay free: they name their target, and the target is inside
    the blast radius the human already granted by pointing the agent at the
    directory.  What cannot be granted that way is an *unbounded* target.
    """
    command_name = os.path.basename(segment[index].strip().lstrip("./")).lower()
    args = [token for token in segment[index + 1 :] if token != "--"]

    if command_name == "rm":
        operands = [token for token in args if not token.startswith("-")]
        if any(
            operand in _WHOLE_DIRECTORY_OPERANDS
            or any(char in operand for char in _GLOB_CHARS)
            for operand in operands
        ):
            return (
                "this deletes a wildcard, or the whole directory, so what "
                "disappears is decided by what is on disk rather than by the "
                "command; uncommitted work would not come back"
            )
        return None

    if command_name != "git":
        return None

    subcommand = next((token for token in args if not token.startswith("-")), "")
    subcommand = subcommand.lower()
    flags = {token for token in args if token.startswith("-")}
    if subcommand == "clean" and any(
        char in flag.lstrip("-") for flag in flags for char in "fxdX"
    ):
        return (
            "'git clean' deletes untracked files, which are exactly the files "
            "with no copy anywhere else"
        )
    if subcommand == "reset" and "--hard" in flags:
        return (
            "'git reset --hard' discards uncommitted changes, which have no "
            "other copy"
        )
    if subcommand in {"restore", "checkout"}:
        operands = [token for token in args if not token.startswith("-")]
        if any(
            operand in _WHOLE_DIRECTORY_OPERANDS or operand == "--"
            for operand in operands[1:]
        ):
            return (
                f"'git {subcommand}' on the whole tree discards uncommitted "
                "changes, which have no other copy"
            )
    return None


def _has_absolute_path_token(
    tokens: list[str],
    *,
    allowed_roots: frozenset[Path] | None = None,
) -> bool:
    """Return True when *tokens* contain an absolute path outside *allowed_roots*.

    Absolute paths that resolve inside any allowed root are safe — they
    reference the same files a relative path would, so no confirmation
    is needed.  When *allowed_roots* is ``None`` (legacy callers), every
    absolute path is treated as potentially out-of-bound.
    """
    for token in tokens:
        if token == "--":
            continue
        if token.startswith("-"):
            continue
        if token.startswith("/"):
            if allowed_roots is not None:
                try:
                    resolved = Path(token).resolve(strict=False)
                except Exception:
                    return True
                if any(
                    resolved == root or root in resolved.parents
                    for root in allowed_roots
                ):
                    continue
            return True
    return False


def _structural_guard(command: str) -> Optional[tuple[str, str]]:
    """Refuse command strings the tool contract cannot honour.

    Two things stay unconditional, at every permission level, because they are
    not "risky" but *unrepresentable*:

    * **inline ``cd``/``pushd``/``popd``** -- the cwd belongs to the tool's own
      ``cwd`` parameter, and a ``cd`` inside one ``sh -c`` moves it for every
      later segment.  A compound command is scanned word by word because that
      is exactly where it hides (`echo ok<newline>cd /tmp`); a simple command is
      caught by its ``argv0`` in ``shell_command_check``.
    * **command substitution** (``$(…)`` and backticks) -- the executed text is
      not in the string being classified, so no per-segment analysis can see it.

    Everything else that used to be classified here (``|``, ``&&``, ``;``,
    redirects) is now handled by segmenting the command and checking each
    segment's own authority, because an operator is a separator rather than a
    hazard.  See ``_segment_command``.
    """
    substitution = _find_shell_operator(command)
    if substitution in {"`", "$("} or "$(" in command or "`" in command:
        return ("blocked", "shell command substitution is blocked for safety")
    if _find_shell_operator(command) is None:
        return None
    if any(word in CWD_ESCAPE_COMMANDS for word in _iter_command_words(command)):
        return (
            "blocked",
            "inline cwd changes are blocked; use the shell tool cwd parameter",
        )
    return None


def _command_is_pre_approved(
    normalized_command: str,
    argv0: Optional[str],
    pre_approved: Optional[list[str]] = None,
) -> bool:
    """Match a command against the configured allowlist.

    An entry containing a space matches the exact normalized command; a bare
    entry (no space) matches any invocation of that command name, e.g.
    ``osascript`` allows every osascript call.  Pre-approval exempts the
    command from confirmation — including high-risk commands — but never
    overrides the user blacklist or the structural guards (command
    substitution, inline ``cd``), which the caller checks first.
    """
    for raw_entry in pre_approved or ():
        entry = _normalize_command(raw_entry)
        if not entry:
            continue
        if " " in entry:
            if normalized_command == entry:
                return True
        elif argv0 is not None and argv0 == entry:
            return True
    return False


# ── Main entry point ─────────────────────────────────────────────────────────


def shell_command_is_blocked(
    command: str,
    extra_blocked: Optional[list[str]] = None,
    *,
    allowed_roots: frozenset[Path] | None = None,
    pre_approved: Optional[list[str]] = None,
    permission_level: Optional[str] = None,
) -> Optional[str]:
    """Return a human-readable block reason if *command* is unsafe.

    Backward-compatible wrapper around ``shell_command_check``.
    Returns None if allowed, a reason string if blocked.
    """
    result = shell_command_check(
        command,
        extra_blocked=extra_blocked,
        allowed_roots=allowed_roots,
        pre_approved=pre_approved,
        permission_level=permission_level,
    )
    if result.allowed:
        return None
    return result.reason


def _reads_program_from_stdin(segment: list[str], index: int) -> bool:
    """True when an interpreter executes this segment's *stdin* as a program.

    This is the one thing a pipe can do that a per-segment check cannot see:
    ``curl … | sh`` hands the shell text that was never classified, while
    ``cat x | python3 app.py`` names a program file that the segment check did
    classify.  The distinction is whether the interpreter was handed anything
    to run: no argument at all, an explicit ``-``, or the ``-s`` that means
    "read the script from stdin".  A flag that only prints something
    (``python3 --version``) is not a program source, and a positional argument
    *is* the program.
    """
    command_name = os.path.basename(segment[index].strip().lstrip("./")).lower()
    if not _is_stdin_interpreter(command_name):
        return False
    for token in segment[index + 1 :]:
        if token == "--":
            continue
        if token == "-":
            return True
        if token == "-s":
            return True
        if token.startswith("-"):
            return False
        # A positional argument is the program.
        return False
    return True


#: Commands whose non-flag arguments name something they modify or delete, the
#: form of the mapping and the verb to say it with:
#:
#: * ``"all"`` -- every non-flag argument is affected (``rm a b`` deletes both,
#:   ``mv a b`` removes the source as well as writing the destination);
#: * ``"last"`` -- only the destination is written (``cp src dst``, ``tee f``);
#: * ``"of"`` -- the ``of=`` operand (``dd of=/dev/disk2``).
#:
#: Reads are not here on purpose: the sandbox leaves reads open, so
#: ``cat /etc/passwd`` is not an authority question.  What the human is asked
#: about is a command *reaching outside the workspace to change something*.
_ARGUMENT_WRITE_COMMANDS: dict[str, tuple[str, str]] = {
    "rm": ("all", "delete"),
    "rmdir": ("all", "delete"),
    "shred": ("all", "destroy"),
    "truncate": ("all", "empty"),
    "chmod": ("all", "change permissions on"),
    "chown": ("all", "change ownership of"),
    "chgrp": ("all", "change the group of"),
    "mkfifo": ("all", "create a special file at"),
    "mknod": ("all", "create a special file at"),
    "mv": ("all", "move"),
    "mkdir": ("all", "create a directory at"),
    "touch": ("all", "create or update"),
    "cp": ("last", "copy onto"),
    "tee": ("last", "overwrite"),
    "install": ("last", "install over"),
    "rsync": ("last", "sync onto"),
    "scp": ("last", "copy onto"),
    "ln": ("last", "link at"),
    "dd": ("of", "write"),
}


def _argument_write_targets(segment: list[str], index: int) -> list[str]:
    """The paths a segment's own arguments would modify."""
    command_name = os.path.basename(segment[index].strip().lstrip("./")).lower()
    entry = _ARGUMENT_WRITE_COMMANDS.get(command_name)
    if entry is None:
        return []
    mode, _verb = entry
    operands: list[str] = []
    for token in segment[index + 1 :]:
        if token == "--":
            continue
        if token.startswith("-"):
            continue
        operands.append(token)
    if mode == "of":
        return [
            token.partition("=")[2]
            for token in segment[index + 1 :]
            if token.startswith("of=") and token.partition("=")[2]
        ]
    if mode == "last":
        return operands[-1:]
    return operands


def shell_command_check(
    command: str,
    extra_blocked: Optional[list[str]] = None,
    *,
    allowed_roots: frozenset[Path] | None = None,
    scope: ShellAuthorizationScope | None = None,
    now: datetime | None = None,
    pre_approved: Optional[list[str]] = None,
    permission_level: Optional[str] = None,
) -> ShellCheckResult:
    """Classify *command* by risk level and determine whether it may run.

    The command is split on its separators and each segment is classified on
    its own merits, because an operator is a *separator* rather than a hazard:
    ``pytest -q 2>&1 | tail -20`` is two harmless commands, while
    ``curl … | sh`` is caught because the thing reading the pipe is a shell.
    A redirect is judged by where it writes.  Command substitution and inline
    cwd changes stay unconfirmable structural refusals (``_structural_guard``).

    Returns a ``ShellCheckResult`` with risk level, reason, and confirmation
    requirements.

    *allowed_roots*, when provided, exempts absolute-path arguments and
    redirect targets that resolve inside one of those directories from the
    usual "absolute path" checkpoint.
    """
    extra = frozenset(extra_blocked or [])
    authorization_scope = _authorization_scope(scope)
    authorization_now = _authorization_time(now)
    normalized_command = _normalize_command(command)
    effective_level = _effective_permission_level(
        authorization_scope, permission_level
    )

    # Parse before allowlist and risk checks so malformed input always fails closed.
    # Two tokenizations on purpose: ``_parse_command_tokens`` (shlex.split) is
    # what argv0 resolution and the absolute-path label have always used, while
    # ``_parse_shell_tokens`` makes standalone tokens of the operators, which is
    # what segmentation needs -- shlex.split glues a bare ``;`` onto the word in
    # front of it, so `echo a; rm -rf /` read as one segment with argv0 ``echo``.
    try:
        tokens = _parse_command_tokens(command)
        shell_tokens = _parse_shell_tokens(command)
        command_index = _resolve_effective_command_index(tokens)
        structural_guard = _structural_guard(command)
        segments = _segment_command(shell_tokens)
    except _ShellParseError:
        return ShellCheckResult(
            allowed=False,
            risk_level="high",
            reason="command could not be parsed safely",
        )

    argv0: Optional[str] = None
    if command_index is not None:
        argv0 = _resolve_effective_command(tokens)

    # ── Structural guards: never confirmable ─────────────────────────────
    # Inline cwd changes violate the tool contract (the cwd belongs to the
    # tool's own cwd parameter, not to the command string).  Check argv0 here
    # so a bare ``cd /tmp`` or a newline-separated ``cd /etc\ncat passwd``
    # (neither of which contains a shell operator) is still blocked; the
    # structural guard below only fires when the command is compound.
    if argv0 in CWD_ESCAPE_COMMANDS:
        return ShellCheckResult(
            allowed=False,
            risk_level="high",
            reason="inline cwd changes are blocked; use the shell tool cwd parameter",
        )
    if structural_guard is not None:
        return ShellCheckResult(
            allowed=False,
            risk_level="high",
            reason=structural_guard[1],
        )

    if not argv0:
        return ShellCheckResult(
            allowed=True, risk_level="low", reason="empty command"
        )

    def _segment_argv0(segment: list[str]) -> Optional[str]:
        index = _resolve_effective_command_index(segment)
        if index is None:
            return None
        return os.path.basename(segment[index].strip().lstrip("./"))

    # ── User-configured blacklist: unconditional at every level ──────────
    # Checked before the session allowlist so a command the human confirmed
    # earlier in the session cannot bypass a blacklist entry added since.
    # Every segment is checked, not just the first: `echo ok && blocked-tool`
    # used to slip past a guard that read only argv0.
    for name in {argv0, *filter(None, (_segment_argv0(s) for _op, s in segments))}:
        if name in extra:
            return ShellCheckResult(
                allowed=False,
                risk_level="high",
                reason=f"command '{name}' is blocked by configuration",
            )

    # ── Check session allowlist ──────────────────────────────────────────
    if shell_session_allowlist_contains(
        normalized_command,
        scope=authorization_scope,
        now=authorization_now,
    ):
        return ShellCheckResult(
            allowed=True,
            risk_level="low",
            reason="command was confirmed for this session",
        )

    # ── Explicit allowlist entries bypass confirmation ───────────────────
    if _command_is_pre_approved(normalized_command, argv0, pre_approved):
        return ShellCheckResult(
            allowed=True,
            risk_level="low",
            reason="command is pre-approved (no confirmation required)",
        )

    def _confirm(reason: str) -> ShellCheckResult:
        token = _pending_confirmation(
            normalized_command,
            scope=authorization_scope,
            now=authorization_now,
        )
        return ShellCheckResult(
            allowed=False,
            risk_level="high",
            reason=reason,
            requires_confirmation=True,
            confirmation_token=token,
        )

    # ── Per-segment classification ───────────────────────────────────────
    #
    # Order is load-bearing: environment mutation asks at *every* level, then
    # the level-gated constructs, then the two composition rules (a pipe into
    # an interpreter, a redirect outside the granted roots) which are also
    # level-gated because they are authority questions rather than syntax.
    segment_indexes = [
        (operator, segment, _resolve_effective_command_index(segment))
        for operator, segment in segments
    ]

    for _operator, segment, index in segment_indexes:
        if index is None:
            continue
        environment_reason = _environment_mutation_reason(segment, index)
        if environment_reason is not None:
            return _confirm(environment_reason)

    if effective_level == "ask":
        for _operator, segment, index in segment_indexes:
            if index is None:
                continue
            segment_name = os.path.basename(
                segment[index].strip().lstrip("./")
            )
            if _is_inline_execution(segment, index):
                return _confirm(
                    "inline interpreter code cannot be safely classified"
                )
            if _reads_program_from_stdin(segment, index):
                return _confirm(
                    f"the pipe feeds '{segment_name}', which executes its input "
                    "as a program; that text is not classified"
                )
            high_risk_option = _find_high_risk_option(segment, index)
            if high_risk_option:
                return _confirm(
                    f"command option '{segment_name} {high_risk_option}' is high "
                    "risk: destructive operation"
                )
            command_reason = _high_risk_command_reason(segment, index)
            if command_reason is not None:
                return _confirm(command_reason)
            write_reason = _argument_write_reason(
                segment, index, allowed_roots=allowed_roots
            )
            if write_reason is not None:
                return _confirm(write_reason)
            irreversible_reason = _workspace_irreversible_reason(segment, index)
            if irreversible_reason is not None:
                return _confirm(irreversible_reason)

        for pattern in HIGH_RISK_PATTERNS:
            if pattern in command:
                return _confirm(f"command pattern '{pattern}' is high risk")

        for _operator, segment, _index in segment_indexes:
            for target in _redirect_targets(segment):
                target_reason = _redirect_target_reason(
                    target, allowed_roots=allowed_roots
                )
                if target_reason is not None:
                    return _confirm(target_reason)

    # ── Allowed: keep the real risk tier for transparency ───────────────
    inline_execution = any(
        index is not None and _is_inline_execution(segment, index)
        for _operator, segment, index in segment_indexes
    )
    high_risk_option = next(
        (
            option
            for _operator, segment, index in segment_indexes
            if index is not None
            for option in [_find_high_risk_option(segment, index)]
            if option
        ),
        None,
    )
    redirected_outside = any(
        _redirect_target_reason(target, allowed_roots=allowed_roots) is not None
        for _operator, segment, _index in segment_indexes
        for target in _redirect_targets(segment)
    )
    pipes_into_interpreter = any(
        index is not None and _reads_program_from_stdin(segment, index)
        for _operator, segment, index in segment_indexes
    )
    writes_outside = any(
        index is not None
        and _argument_write_reason(segment, index, allowed_roots=allowed_roots)
        is not None
        for _operator, segment, index in segment_indexes
    )
    destructive_command = any(
        index is not None and _high_risk_command_reason(segment, index) is not None
        for _operator, segment, index in segment_indexes
    )
    irreversible_inside = any(
        index is not None
        and _workspace_irreversible_reason(segment, index) is not None
        for _operator, segment, index in segment_indexes
    )
    segment_names = {
        name for name in (_segment_argv0(s) for _op, s in segments) if name
    }

    risk_level = "low"
    if (
        destructive_command
        or high_risk_option
        or inline_execution
        or pipes_into_interpreter
        or writes_outside
        or irreversible_inside
        or redirected_outside
        or any(pattern in command for pattern in HIGH_RISK_PATTERNS)
    ):
        risk_level = "high"
    elif (
        segment_names & MEDIUM_RISK_COMMANDS
        or any(
            index is not None and _is_script_execution(segment, index)
            for _operator, segment, index in segment_indexes
        )
        or _has_absolute_path_token(tokens, allowed_roots=allowed_roots)
    ):
        risk_level = "medium"
    return ShellCheckResult(
        allowed=True, risk_level=risk_level, reason="command is allowed"
    )
