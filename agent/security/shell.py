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

# Commands listed here are blocked unconditionally (high risk).
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

# Commands that require user confirmation (medium risk).
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

# Dangerous shell patterns checked as literal substrings (high risk).
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
    }
)

CWD_ESCAPE_COMMANDS: frozenset[str] = frozenset({"cd", "pushd", "popd"})

# ── Backward-compatible aliases ──────────────────────────────────────────────

SHELL_BLOCKED_COMMANDS: frozenset[str] = HIGH_RISK_COMMANDS | MEDIUM_RISK_COMMANDS
SHELL_BLOCKED_PATTERNS: tuple[str, ...] = HIGH_RISK_PATTERNS + MEDIUM_RISK_PATTERNS

# ── Session allowlist ───────────────────────────────────────────────────────

CONFIRMATION_TTL = timedelta(minutes=5)


@dataclass(frozen=True)
class ShellAuthorizationScope:
    session_id: str
    channel_name: str
    user_id: str = ""


@dataclass(frozen=True)
class PendingShellConfirmation:
    command: str
    scope: ShellAuthorizationScope
    expires_at: datetime


_DEFAULT_SCOPE = ShellAuthorizationScope("default", "cli", "")
_session_allowlist: dict[tuple[ShellAuthorizationScope, str], datetime] = {}
_pending_tokens: dict[str, PendingShellConfirmation] = {}


def _authorization_scope(
    scope: ShellAuthorizationScope | None,
) -> ShellAuthorizationScope:
    return scope or _DEFAULT_SCOPE


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


def shell_session_allowlist_clear() -> None:
    """Clear all entries from the session allowlist."""
    _session_allowlist.clear()
    _pending_tokens.clear()


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


def shell_command_uses_shell_features(command: str) -> bool:
    """Return True when *command* depends on shell parsing/control features."""
    try:
        return _find_shell_operator(command) is not None
    except _ShellParseError:
        return True


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


def shell_command_confirm(
    token: str,
    *,
    scope: ShellAuthorizationScope,
    now: datetime | None = None,
) -> bool:
    """Redeem one pending token using trusted request identity."""
    current = _authorization_time(now)
    stored = _pending_tokens.get(str(token))
    if stored is None or stored.scope != scope or stored.expires_at <= current:
        if stored is not None and stored.expires_at <= current:
            _pending_tokens.pop(str(token), None)
        return False
    _pending_tokens.pop(str(token), None)
    shell_session_allowlist_add(
        stored.command,
        scope=stored.scope,
        now=current,
        expires_at=stored.expires_at,
    )
    return True


def _pending_confirmation(
    command: str,
    *,
    scope: ShellAuthorizationScope,
    now: datetime,
) -> str:
    for stale_token, record in list(_pending_tokens.items()):
        if record.expires_at <= now:
            _pending_tokens.pop(stale_token, None)
    token = str(uuid.uuid4())
    _pending_tokens[token] = PendingShellConfirmation(
        command=_normalize_command(command),
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


def _resolve_effective_command_index(tokens: list[str]) -> Optional[int]:
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if _is_env_assignment(token):
            idx += 1
            continue

        cmd = os.path.basename(token.strip().lstrip("./"))
        if cmd == "env":
            idx += 1
            while idx < len(tokens):
                token = tokens[idx]
                if token == "--":
                    idx += 1
                    break
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
            continue

        if cmd == "sudo":
            idx += 1
            while idx < len(tokens):
                token = tokens[idx]
                if token == "--":
                    idx += 1
                    break
                if token.startswith("-"):
                    idx += 1
                    if token in {
                        "-g",
                        "--group",
                        "-h",
                        "--host",
                        "-p",
                        "--prompt",
                        "-R",
                        "--chroot",
                        "-r",
                        "--role",
                        "-t",
                        "--type",
                        "-u",
                        "--user",
                    } and idx < len(tokens):
                        idx += 1
                    continue
                break
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
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
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


def _command_requires_shell_operator_block(command: str) -> Optional[str]:
    operator = _find_shell_operator(command)
    if operator is None:
        return None
    words = _iter_command_words(command)
    if any(word in CWD_ESCAPE_COMMANDS for word in words):
        return "inline cwd changes are blocked; use the shell tool cwd parameter"
    if operator in {"`", "$("}:
        return "shell command substitution is blocked for safety"
    if operator in {">", ">>", "<", "<<"}:
        return "shell redirection is blocked; use file tools or command arguments"
    return f"shell control operator '{operator}' is blocked for safety"


# ── Main entry point ─────────────────────────────────────────────────────────


def shell_command_is_blocked(
    command: str,
    extra_blocked: Optional[list[str]] = None,
    *,
    allowed_roots: frozenset[Path] | None = None,
) -> Optional[str]:
    """Return a human-readable block reason if *command* is unsafe.

    Backward-compatible wrapper around ``shell_command_check``.
    Returns None if allowed, a reason string if blocked.
    """
    result = shell_command_check(
        command, extra_blocked=extra_blocked, allowed_roots=allowed_roots
    )
    if result.allowed:
        return None
    return result.reason


def shell_command_check(
    command: str,
    extra_blocked: Optional[list[str]] = None,
    *,
    allowed_roots: frozenset[Path] | None = None,
    scope: ShellAuthorizationScope | None = None,
    now: datetime | None = None,
) -> ShellCheckResult:
    """Classify *command* by risk level and determine whether it may run.

    Returns a ``ShellCheckResult`` with risk level, reason, and
    confirmation requirements.

    *allowed_roots*, when provided, exempts absolute-path arguments
    that resolve inside one of those directories from the usual
    "absolute path" checkpoint.
    """
    extra = frozenset(extra_blocked or [])
    authorization_scope = _authorization_scope(scope)
    authorization_now = _authorization_time(now)
    normalized_command = _normalize_command(command)

    # Parse before allowlist and risk checks so malformed input always fails closed.
    try:
        tokens = _parse_command_tokens(command)
        shell_operator_block = _command_requires_shell_operator_block(command)
        command_index = _resolve_effective_command_index(tokens)
    except _ShellParseError:
        return ShellCheckResult(
            allowed=False,
            risk_level="high",
            reason="command could not be parsed safely",
        )

    if shell_operator_block:
        return ShellCheckResult(
            allowed=False,
            risk_level="high",
            reason=shell_operator_block,
        )

    argv0: Optional[str] = None
    if command_index is not None:
        argv0 = _resolve_effective_command(tokens)

    # ── Check session allowlist first ────────────────────────────────────
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

    # ── High-risk patterns (literal substring match) ─────────────────────
    for pattern in HIGH_RISK_PATTERNS:
        if pattern in command:
            return ShellCheckResult(
                allowed=False,
                risk_level="high",
                reason=f"command pattern '{pattern}' is blocked for safety",
            )

    if not argv0:
        return ShellCheckResult(
            allowed=True, risk_level="low", reason="empty command"
        )

    # ── High-risk commands ───────────────────────────────────────────────
    if argv0 in (HIGH_RISK_COMMANDS | extra) or argv0 in extra:
        # Check if it's in extra_blocked (always high risk)
        if argv0 in extra and argv0 not in HIGH_RISK_COMMANDS:
            return ShellCheckResult(
                allowed=False,
                risk_level="high",
                reason=f"command '{argv0}' is blocked by configuration",
            )
        if argv0 in HIGH_RISK_COMMANDS:
            return ShellCheckResult(
                allowed=False,
                risk_level="high",
                reason=f"command '{argv0}' is high risk: disk/system destruction",
            )

    high_risk_option = _find_high_risk_option(tokens, command_index)
    if high_risk_option:
        return ShellCheckResult(
            allowed=False,
            risk_level="high",
            reason=(
                f"command option '{argv0} {high_risk_option}' is high risk: "
                "destructive operation"
            ),
        )

    if _is_inline_execution(tokens, command_index):
        token = _pending_confirmation(
            normalized_command,
            scope=authorization_scope,
            now=authorization_now,
        )
        return ShellCheckResult(
            allowed=False,
            risk_level="medium",
            reason=f"command '{argv0}' is medium risk: inline code execution",
            requires_confirmation=True,
            confirmation_token=token,
        )

    if _is_script_execution(tokens, command_index):
        token = _pending_confirmation(
            normalized_command,
            scope=authorization_scope,
            now=authorization_now,
        )
        return ShellCheckResult(
            allowed=False,
            risk_level="medium",
            reason=f"command '{argv0}' is medium risk: script execution",
            requires_confirmation=True,
            confirmation_token=token,
        )

    if _has_absolute_path_token(tokens, allowed_roots=allowed_roots):
        token = _pending_confirmation(
            normalized_command,
            scope=authorization_scope,
            now=authorization_now,
        )
        return ShellCheckResult(
            allowed=False,
            risk_level="medium",
            reason="command uses absolute path arguments outside the tool cwd boundary",
            requires_confirmation=True,
            confirmation_token=token,
        )

    # ── Medium-risk commands ─────────────────────────────────────────────
    if argv0 in MEDIUM_RISK_COMMANDS:
        token = _pending_confirmation(
            normalized_command,
            scope=authorization_scope,
            now=authorization_now,
        )
        risk_descriptions = {
            "rm": "file deletion",
            "rmdir": "directory removal",
            "mv": "file move or overwrite",
            "cp": "file copy or overwrite",
            "chmod": "permission change",
            "chown": "ownership change",
            "sudo": "privilege escalation",
            "kill": "process termination",
            "pkill": "process termination",
            "killall": "process termination",
            "ssh": "remote access",
            "scp": "remote file transfer",
            "sftp": "remote file transfer",
            "nc": "raw network access",
            "netcat": "raw network access",
            "curl": "network request or download",
            "wget": "network request or download",
            "ftp": "network file transfer",
            "rsync": "file synchronization",
            "passwd": "password modification",
            "usermod": "user account modification",
            "groupadd": "group management",
            "useradd": "user account modification",
            "userdel": "user account modification",
            "groupdel": "group management",
            "eval": "code execution",
            "exec": "code execution",
            "bash": "shell execution",
            "sh": "shell execution",
            "zsh": "shell execution",
            "fish": "shell execution",
            "pip": "package installation",
            "pip3": "package installation",
            "npm": "package or script execution",
            "yarn": "package or script execution",
            "pnpm": "package or script execution",
            "brew": "package installation",
            "apt": "package installation",
            "apt-get": "package installation",
            "dnf": "package installation",
            "yum": "package installation",
            "pacman": "package installation",
            "git": "repository state or network operation",
        }
        desc = risk_descriptions.get(argv0, "potentially dangerous operation")
        return ShellCheckResult(
            allowed=False,
            risk_level="medium",
            reason=f"command '{argv0}' is medium risk: {desc}",
            requires_confirmation=True,
            confirmation_token=token,
        )

    # ── Low-risk: allow ──────────────────────────────────────────────────
    return ShellCheckResult(
        allowed=True, risk_level="low", reason="command is low risk"
    )
