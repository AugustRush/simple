from __future__ import annotations

import os
import re
import shlex
import uuid
from dataclasses import dataclass
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

# Medium-risk patterns: inline code execution.
MEDIUM_RISK_PATTERNS: tuple[str, ...] = (
    "python -c",
    "python3 -c",
    "perl -e",
    "ruby -e",
    "bash -c",
    "sh -c",
    "zsh -c",
)

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

_session_allowlist: set[str] = set()

# Pending confirmation tokens: token → command
_pending_tokens: dict[str, str] = {}


def shell_session_allowlist_add(command_base: str) -> None:
    """Add an exact command string to the session allowlist."""
    _session_allowlist.add(command_base)


def shell_session_allowlist_clear() -> None:
    """Clear all entries from the session allowlist."""
    _session_allowlist.clear()


def shell_session_allowlist_contains(command_base: str) -> bool:
    """Check whether an exact command string is in the session allowlist."""
    return command_base in _session_allowlist


def shell_command_uses_shell_features(command: str) -> bool:
    """Return True when *command* depends on shell parsing/control features."""
    return _find_shell_operator(command) is not None


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


def shell_command_confirm(token: str, command: str) -> bool:
    """Verify a confirmation token and add the command to the session allowlist.

    Returns True if the token was valid and the command is now allowed.
    """
    stored = _pending_tokens.pop(token, None)
    if stored is None or stored != command:
        return False
    shell_session_allowlist_add(command)
    return True


# ── Internal helpers ─────────────────────────────────────────────────────────


def _is_env_assignment(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", token))


def _resolve_effective_command(tokens: list[str]) -> Optional[str]:
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
                if token.startswith("-"):
                    idx += 1
                    if token in {
                        "-C",
                        "--chdir",
                        "-S",
                        "--split-string",
                        "-u",
                        "--unset",
                    } and idx < len(tokens):
                        idx += 1
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

        return cmd
    return None


def _iter_command_words(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    words: list[str] = []
    for token in tokens:
        if token in HIGH_RISK_SHELL_OPERATORS:
            continue
        if token in {"(", ")", "{", "}"}:
            continue
        words.append(os.path.basename(token.strip().lstrip("./")))
    return words


def _find_shell_operator(command: str) -> Optional[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        if token in HIGH_RISK_SHELL_OPERATORS:
            return token
    if "$(" in command:
        return "$("
    return None


def _has_absolute_path_token(tokens: list[str]) -> bool:
    for token in tokens:
        if token == "--":
            continue
        if token.startswith("-"):
            continue
        if token.startswith("/"):
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
    command: str, extra_blocked: Optional[list[str]] = None
) -> Optional[str]:
    """Return a human-readable block reason if *command* is unsafe.

    Backward-compatible wrapper around ``shell_command_check``.
    Returns None if allowed, a reason string if blocked.
    """
    result = shell_command_check(command, extra_blocked=extra_blocked)
    if result.allowed:
        return None
    return result.reason


def shell_command_check(
    command: str, extra_blocked: Optional[list[str]] = None
) -> ShellCheckResult:
    """Classify *command* by risk level and determine whether it may run.

    Returns a ``ShellCheckResult`` with risk level, reason, and
    confirmation requirements.
    """
    extra = frozenset(extra_blocked or [])

    shell_operator_block = _command_requires_shell_operator_block(command)
    if shell_operator_block:
        return ShellCheckResult(
            allowed=False,
            risk_level="high",
            reason=shell_operator_block,
        )

    # ── Parse command tokens ─────────────────────────────────────────────
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    argv0: Optional[str] = None
    if tokens:
        argv0 = _resolve_effective_command(tokens)

    # ── Check session allowlist first ────────────────────────────────────
    if shell_session_allowlist_contains(command):
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

    # ── Medium-risk patterns ─────────────────────────────────────────────
    for pattern in MEDIUM_RISK_PATTERNS:
        if pattern in command:
            token = str(uuid.uuid4())
            _pending_tokens[token] = command
            return ShellCheckResult(
                allowed=False,
                risk_level="medium",
                reason=f"command pattern '{pattern}' is medium risk: inline code execution",
                requires_confirmation=True,
                confirmation_token=token,
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

    if _has_absolute_path_token(tokens):
        token = str(uuid.uuid4())
        _pending_tokens[token] = command
        return ShellCheckResult(
            allowed=False,
            risk_level="medium",
            reason="command uses absolute path arguments outside the tool cwd boundary",
            requires_confirmation=True,
            confirmation_token=token,
        )

    # ── Medium-risk commands ─────────────────────────────────────────────
    if argv0 in MEDIUM_RISK_COMMANDS:
        token = str(uuid.uuid4())
        _pending_tokens[token] = command
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
