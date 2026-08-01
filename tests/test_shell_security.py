from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _clear_shell_confirmation_state():
    from agent.security.shell import shell_session_allowlist_clear

    shell_session_allowlist_clear()


def test_shell_security_module_blocks_destructive_commands():
    from agent.security.shell import shell_command_is_blocked

    assert (
        shell_command_is_blocked("mkfs /dev/disk0")
        == "command 'mkfs' is high risk: disk/system destruction"
    )


def test_shell_security_requires_confirmation_for_restricted_commands():
    from agent.security.shell import shell_command_check

    for command in ("rm old.txt", "mv a b", "cp a b", "curl https://example.com"):
        result = shell_command_check(command)

        assert result.allowed is False
        assert result.risk_level == "medium"
        assert result.requires_confirmation is True
        assert result.confirmation_token


def test_shell_confirmation_is_scoped_one_time_and_expires():
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_command_check,
        shell_command_confirm,
    )

    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    first = shell_command_check("  mv a b  ", scope=scope, now=now)

    assert shell_command_confirm(first.confirmation_token, scope=scope, now=now)
    assert not shell_command_confirm(first.confirmation_token, scope=scope, now=now)
    assert shell_command_check("mv a b", scope=scope, now=now).allowed is True
    assert shell_command_check("mv  a b", scope=scope, now=now).allowed is False
    assert shell_command_check(
        "mv a b", scope=scope, now=now + timedelta(minutes=5)
    ).allowed is False


@pytest.mark.parametrize(
    "other_scope",
    [
        pytest.param(("session-2", "feishu", "user-1"), id="session"),
        pytest.param(("session-1", "cli", "user-1"), id="channel"),
        pytest.param(("session-1", "feishu", "user-2"), id="user"),
    ],
)
def test_shell_confirmation_rejects_other_authorization_scopes(other_scope):
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_command_check,
        shell_command_confirm,
    )

    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    owner = ShellAuthorizationScope("session-1", "feishu", "user-1")
    other = ShellAuthorizationScope(*other_scope)
    first = shell_command_check("mv a b", scope=owner, now=now)

    assert not shell_command_confirm(first.confirmation_token, scope=other, now=now)
    assert shell_command_check("mv a b", scope=other, now=now).allowed is False


def test_shell_confirmation_rejects_expired_pending_token():
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_command_check,
        shell_command_confirm,
    )

    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    first = shell_command_check("mv a b", scope=scope, now=now)

    assert not shell_command_confirm(
        first.confirmation_token,
        scope=scope,
        now=now + timedelta(minutes=5),
    )


@pytest.mark.parametrize(
    "command",
    [
        "python3.11 -c 'print(1)'",
        "python3   -I   -c 'print(1)'",
        "/usr/bin/python3.12 -B -c 'print(1)'",
        "ruby --disable-gems -e 'puts 1'",
        "bash --noprofile -c 'echo ok'",
    ],
)
def test_shell_security_requires_confirmation_for_inline_execution(command):
    from agent.security.shell import shell_command_check

    result = shell_command_check(command)

    assert result.allowed is False
    assert result.risk_level == "medium"
    assert "inline code execution" in result.reason
    assert result.requires_confirmation is True
    assert result.confirmation_token


@pytest.mark.parametrize(
    "command",
    [
        "python -c'print(1)'",
        "ruby -e'puts 1'",
        "perl -e'print 1'",
    ],
)
def test_shell_security_requires_confirmation_for_attached_execution_flags(
    command,
):
    from agent.security.shell import shell_command_check

    result = shell_command_check(command)

    assert result.allowed is False
    assert result.risk_level == "medium"
    assert "inline code execution" in result.reason
    assert result.requires_confirmation is True
    assert result.confirmation_token


@pytest.mark.parametrize(
    ("command", "risk_level", "requires_confirmation"),
    [
        ('env -S "find . -delete"', "high", False),
        ('env -S "python3.11 -I -c print(1)"', "medium", True),
    ],
)
def test_shell_security_classifies_env_split_string_payloads(
    command, risk_level, requires_confirmation
):
    from agent.security.shell import shell_command_check

    result = shell_command_check(command)

    assert result.allowed is False
    assert result.risk_level == risk_level
    assert result.requires_confirmation is requires_confirmation


@pytest.mark.parametrize(
    ("command", "risk_level"),
    [
        (r"env -Sfind\ .\ -delete", "high"),
        ('env --split-string="python3.11 -c print(1)"', "medium"),
        ("env -iu HOME find . -delete", "high"),
    ],
)
def test_shell_security_classifies_attached_env_options(command, risk_level):
    from agent.security.shell import shell_command_check

    result = shell_command_check(command)

    assert result.allowed is False
    assert result.risk_level == risk_level


@pytest.mark.parametrize(
    "command",
    [
        "python3 script.py -c value",
        "python3 script.py -config value",
        "ruby script.rb -e value",
    ],
)
def test_shell_security_requires_confirmation_for_script_execution(
    command,
):
    from agent.security.shell import shell_command_check

    result = shell_command_check(command)

    assert result.allowed is False
    assert result.risk_level == "medium"
    assert "script execution" in result.reason
    assert result.requires_confirmation is True
    assert result.confirmation_token


@pytest.mark.parametrize(
    "command",
    [
        # The demonstrated bypass: a network/browser action routed through a
        # script file must not silently skip the confirmation curl would get.
        "ruby --disable-gems wb.rb navigate '{\"url\":\"https://x.com/explore\"}'",
        "python3 env_check.py",
        "node app.js",
        "perl script.pl",
        "php script.php",
        "osascript control.scpt",
        "python3 -m pip install requests",
        "python3 -",
    ],
)
def test_shell_security_script_execution_cannot_bypass_network_confirmation(
    command,
):
    from agent.security.shell import shell_command_check

    result = shell_command_check(command)

    assert result.allowed is False
    assert result.risk_level == "medium"
    assert "script execution" in result.reason
    assert result.requires_confirmation is True


@pytest.mark.parametrize(
    "command",
    [
        "python3 --version",
        "python3 -V",
        "ruby --version",
        "node --help",
        "php -v",
    ],
)
def test_shell_security_interpreter_flags_without_scripts_stay_low(command):
    from agent.security.shell import shell_command_check

    result = shell_command_check(command)

    assert result.allowed is True
    assert result.risk_level == "low"


@pytest.mark.parametrize(
    "command",
    [
        "python3 -W ignore -c 'print(1)'",
        "ruby -I lib -e 'puts 1'",
        "bash -O extglob -c 'echo ok'",
    ],
)
def test_shell_security_scans_past_interpreter_option_values(command):
    from agent.security.shell import shell_command_check

    result = shell_command_check(command)

    assert result.allowed is False
    assert result.risk_level == "medium"
    assert result.requires_confirmation is True


def test_shell_security_blocks_high_risk_command_options():
    from agent.security.shell import shell_command_check

    result = shell_command_check("find . -delete")

    assert result.allowed is False
    assert result.risk_level == "high"
    assert result.requires_confirmation is False
    assert result.confirmation_token == ""


def test_shell_security_fails_closed_on_malformed_quoting_even_when_allowlisted():
    from agent.security.shell import (
        shell_command_check,
        shell_session_allowlist_add,
    )

    command = "python3 -c 'unterminated"
    shell_session_allowlist_add(command)

    result = shell_command_check(command)

    assert result.allowed is False
    assert result.risk_level == "high"
    assert result.requires_confirmation is False
    assert result.confirmation_token == ""


def test_shell_security_blocks_shell_cwd_escape_sequences():
    from agent.security.shell import shell_command_check

    for command in ("cd /tmp && echo ok", "pushd /tmp; echo ok"):
        result = shell_command_check(command)

        assert result.allowed is False
        assert result.risk_level == "high"
        assert result.requires_confirmation is False


def test_shell_security_requires_confirmation_for_absolute_paths():
    from agent.security.shell import shell_command_check

    for command in ("cat /etc/passwd", "/bin/cat README.md"):
        result = shell_command_check(command)

        assert result.allowed is False
        assert result.risk_level == "medium"
        assert result.requires_confirmation is True
        assert result.confirmation_token


def test_agent_package_reexports_shell_blocker_for_compatibility():
    import agent
    from agent.security.shell import shell_command_is_blocked

    assert agent._shell_command_is_blocked is shell_command_is_blocked
