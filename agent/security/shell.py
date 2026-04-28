from __future__ import annotations

import os
import re
import shlex
from typing import Optional

# Commands listed here are blocked unconditionally, regardless of arguments.
SHELL_BLOCKED_COMMANDS: frozenset[str] = frozenset(
    {
        "rm",
        "rmdir",
        "mkfs",
        "dd",
        "shred",
        "fdisk",
        "parted",
    }
)

# Dangerous pipe-idiom substrings checked as literal substrings.
SHELL_BLOCKED_PATTERNS: tuple[str, ...] = (
    "curl | sh",
    "wget | sh",
    "wget -O- |",
    "curl -s |",
)


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


def shell_command_is_blocked(
    command: str, extra_blocked: Optional[list[str]] = None
) -> Optional[str]:
    """Return a human-readable block reason if *command* is unsafe."""
    blocked = SHELL_BLOCKED_COMMANDS | frozenset(extra_blocked or [])
    for pattern in SHELL_BLOCKED_PATTERNS:
        if pattern in command:
            return f"command pattern '{pattern}' is blocked for safety"
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return None
    argv0 = _resolve_effective_command(tokens)
    if not argv0:
        return None
    if argv0 in blocked:
        return f"command '{argv0}' is blocked for safety"
    return None
