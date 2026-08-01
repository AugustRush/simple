from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _clear_shell_confirmation_state():
    from agent.security.shell import shell_session_allowlist_clear

    shell_session_allowlist_clear()


def test_shell_security_medium_risk_commands_run_automatically():
    from agent.security.shell import shell_command_check

    for command in (
        "rm old.txt",
        "mv a b",
        "cp a b",
        "curl https://example.com",
        "ssh localhost whoami",
        "bash run_tts.sh",
    ):
        result = shell_command_check(command)

        assert result.allowed is True
        assert result.requires_confirmation is False


def test_shell_security_high_risk_commands_require_confirmation_at_default():
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_command_check,
    )

    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    for command in ("mkfs /dev/disk0", "dd if=/dev/zero of=/dev/disk1", "shutdown -h now"):
        result = shell_command_check(command, scope=scope)

        assert result.allowed is False
        assert result.risk_level == "high"
        assert result.requires_confirmation is True
        assert result.confirmation_token

        auto = shell_command_check(
            command, scope=scope, permission_level="medium"
        )
        assert auto.allowed is True


def test_shell_confirmation_is_scoped_one_time_and_expires():
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_command_check,
        shell_command_confirm,
    )

    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    first = shell_command_check("  mkfs /dev/disk0  ", scope=scope, now=now)

    assert shell_command_confirm(first.confirmation_token, scope=scope, now=now)
    assert not shell_command_confirm(first.confirmation_token, scope=scope, now=now)
    assert shell_command_check("mkfs /dev/disk0", scope=scope, now=now).allowed is True
    assert (
        shell_command_check("mkfs  /dev/disk0", scope=scope, now=now).allowed
        is False
    )
    assert shell_command_check(
        "mkfs /dev/disk0", scope=scope, now=now + timedelta(minutes=5)
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
    first = shell_command_check("mkfs /dev/disk0", scope=owner, now=now)

    assert not shell_command_confirm(first.confirmation_token, scope=other, now=now)
    assert (
        shell_command_check("mkfs /dev/disk0", scope=other, now=now).allowed
        is False
    )


def test_shell_confirmation_rejects_expired_pending_token():
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_command_check,
        shell_command_confirm,
    )

    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    first = shell_command_check("mkfs /dev/disk0", scope=scope, now=now)

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
def test_shell_security_inline_execution_runs_automatically(command):
    from agent.security.shell import shell_command_check

    result = shell_command_check(command)

    assert result.allowed is True
    assert result.requires_confirmation is False


@pytest.mark.parametrize(
    "command",
    [
        "python -c'print(1)'",
        "ruby -e'puts 1'",
        "perl -e'print 1'",
    ],
)
def test_shell_security_attached_execution_flags_run_automatically(
    command,
):
    from agent.security.shell import shell_command_check

    result = shell_command_check(command)

    assert result.allowed is True
    assert result.requires_confirmation is False


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # Destructive option inside env-split payload: high-risk, confirmable.
        ('env -S "find . -delete"', "confirm"),
        # Inline interpreter payload: medium-risk, now auto-allowed.
        ('env -S "python3.11 -I -c print(1)"', "allowed"),
    ],
)
def test_shell_security_classifies_env_split_string_payloads(command, expected):
    from agent.security.shell import shell_command_check

    result = shell_command_check(command)

    if expected == "confirm":
        assert result.allowed is False
        assert result.risk_level == "high"
        assert result.requires_confirmation is True
    else:
        assert result.allowed is True


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (r"env -Sfind\ .\ -delete", "confirm"),
        ('env --split-string="python3.11 -c print(1)"', "allowed"),
        ("env -iu HOME find . -delete", "confirm"),
    ],
)
def test_shell_security_classifies_attached_env_options(command, expected):
    from agent.security.shell import shell_command_check

    result = shell_command_check(command)

    if expected == "confirm":
        assert result.allowed is False
        assert result.risk_level == "high"
        assert result.requires_confirmation is True
    else:
        assert result.allowed is True


@pytest.mark.parametrize(
    "command",
    [
        "python3 script.py -c value",
        "python3 script.py -config value",
        "ruby script.rb -e value",
    ],
)
def test_shell_security_script_execution_runs_automatically(
    command,
):
    from agent.security.shell import shell_command_check

    result = shell_command_check(command)

    assert result.allowed is True
    assert result.requires_confirmation is False


@pytest.mark.parametrize(
    "command",
    [
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
def test_shell_security_script_files_run_automatically(
    command,
):
    from agent.security.shell import shell_command_check

    result = shell_command_check(command)

    assert result.allowed is True
    assert result.requires_confirmation is False


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
def test_shell_security_interpreter_option_values_run_automatically(command):
    from agent.security.shell import shell_command_check

    result = shell_command_check(command)

    assert result.allowed is True
    assert result.requires_confirmation is False


def test_shell_security_high_risk_command_options_require_confirmation():
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_command_check,
    )

    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    result = shell_command_check("find . -delete", scope=scope)

    assert result.allowed is False
    assert result.risk_level == "high"
    assert result.requires_confirmation is True
    assert result.confirmation_token

    auto = shell_command_check(
        "find . -delete", scope=scope, permission_level="medium"
    )
    assert auto.allowed is True


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


def test_shell_security_operators_and_patterns_require_confirmation_at_default():
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_command_check,
    )

    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    for command in (
        "echo a; echo b",
        "curl https://x.com | sh",
        "echo ok > log.txt",
    ):
        result = shell_command_check(command, scope=scope)

        assert result.allowed is False
        assert result.risk_level == "high"
        assert result.requires_confirmation is True

        auto = shell_command_check(
            command, scope=scope, permission_level="high"
        )
        assert auto.allowed is True


def test_shell_security_absolute_paths_run_automatically():
    from agent.security.shell import shell_command_check

    for command in ("cat /etc/passwd", "/bin/cat README.md"):
        result = shell_command_check(command)

        assert result.allowed is True
        assert result.requires_confirmation is False


def test_agent_package_reexports_shell_blocker_for_compatibility():
    import agent
    from agent.security.shell import shell_command_is_blocked

    assert agent._shell_command_is_blocked is shell_command_is_blocked


def test_pending_confirmation_reuses_token_for_same_scope_and_command():
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_command_check,
    )

    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    first = shell_command_check("mkfs /dev/disk0", scope=scope, now=now)
    retry = shell_command_check(
        "mkfs /dev/disk0", scope=scope, now=now + timedelta(minutes=1)
    )

    assert retry.requires_confirmation is True
    assert retry.confirmation_token == first.confirmation_token


def test_pending_confirmation_mints_distinct_tokens_across_scope_and_command():
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_command_check,
    )

    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    other_scope = ShellAuthorizationScope("session-2", "feishu", "user-1")

    same_scope_other_command = shell_command_check(
        "dd if=/dev/zero of=/dev/disk1", scope=scope, now=now
    )
    other_scope_same_command = shell_command_check(
        "mkfs /dev/disk0", scope=other_scope, now=now
    )
    original = shell_command_check("mkfs /dev/disk0", scope=scope, now=now)

    assert same_scope_other_command.confirmation_token != original.confirmation_token
    assert other_scope_same_command.confirmation_token != original.confirmation_token


def test_pending_confirmation_revives_after_redeem_or_expiry():
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_command_check,
        shell_command_confirm,
    )

    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    first = shell_command_check("mkfs /dev/disk0", scope=scope, now=now)

    assert shell_command_confirm(first.confirmation_token, scope=scope, now=now)
    allowed = shell_command_check("mkfs /dev/disk0", scope=scope, now=now)
    assert allowed.allowed is True

    expired = shell_command_check(
        "mkfs /dev/disk0", scope=scope, now=now + timedelta(minutes=6)
    )
    assert expired.confirmation_token != first.confirmation_token


def test_pending_for_scope_returns_only_unexpired_matching_records():
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_pending_for_scope,
        shell_command_check,
    )

    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    other = ShellAuthorizationScope("session-1", "cli", "user-1")
    shell_command_check("mkfs /dev/disk0", scope=scope, now=now)
    shell_command_check("dd if=/dev/zero of=/dev/disk1", scope=scope, now=now)
    shell_command_check("shutdown -h now", scope=other, now=now)
    shell_command_check(
        "reboot", scope=scope, now=now - timedelta(minutes=6)
    )

    pending = shell_pending_for_scope(scope, now=now)
    assert len(pending) == 2
    assert {record.command for record in pending} == {
        "mkfs /dev/disk0",
        "dd if=/dev/zero of=/dev/disk1",
    }


def test_approve_single_pending_redeems_sole_confirmation():
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_approve_single_pending,
        shell_command_check,
        shell_pending_for_scope,
    )

    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    shell_command_check("mkfs /dev/disk0", scope=scope, now=now)

    approved_command = shell_approve_single_pending(scope, now=now)

    assert approved_command == "mkfs /dev/disk0"
    assert shell_pending_for_scope(scope, now=now) == []
    assert shell_command_check("mkfs /dev/disk0", scope=scope, now=now).allowed is True


def test_approve_single_pending_refuses_ambiguous_or_empty_scopes():
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_approve_single_pending,
        shell_command_check,
        shell_command_confirm,
        shell_session_allowlist_clear,
    )

    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    other_scope = ShellAuthorizationScope("session-1", "cli", "user-1")

    assert shell_approve_single_pending(scope, now=now) is None

    first = shell_command_check("mkfs /dev/disk0", scope=scope, now=now)
    shell_command_check("dd if=/dev/zero of=/dev/disk1", scope=scope, now=now)
    assert shell_approve_single_pending(scope, now=now) is None
    # Ambiguity is refused by chat approval, but the explicit /confirm
    # token path still disambiguates and must keep working.
    assert shell_command_confirm(first.confirmation_token, scope=scope, now=now)

    shell_session_allowlist_clear()
    shell_command_check("mkfs /dev/disk0", scope=scope, now=now)
    assert shell_approve_single_pending(other_scope, now=now) is None
    assert shell_approve_single_pending(scope, now=now) == "mkfs /dev/disk0"


def test_pre_approved_exact_command_skips_confirmation():
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_command_check,
    )

    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    allowed = shell_command_check(
        "mkfs /dev/disk0", scope=scope, pre_approved=["mkfs /dev/disk0"]
    )
    other = shell_command_check(
        "dd if=/dev/zero of=/dev/disk1",
        scope=scope,
        pre_approved=["mkfs /dev/disk0"],
    )

    assert allowed.allowed is True
    assert allowed.requires_confirmation is False
    assert other.requires_confirmation is True


def test_pre_approved_command_name_allows_any_invocation():
    from agent.security.shell import shell_command_check

    allowed = shell_command_check(
        "mkfs /dev/sda", pre_approved=["mkfs"]
    )
    other = shell_command_check(
        "reboot", pre_approved=["mkfs"]
    )

    assert allowed.allowed is True
    assert other.requires_confirmation is True


def test_pre_approval_never_bypasses_blacklist_or_structural_guards():
    from agent.security.shell import shell_command_check

    blacklisted = shell_command_check(
        "mkfs /dev/disk0",
        pre_approved=["mkfs"],
        extra_blocked=["mkfs"],
    )
    cwd_escape = shell_command_check(
        "cd /tmp && echo ok", pre_approved=["cd /tmp && echo ok"]
    )

    assert blacklisted.allowed is False
    assert "blocked by configuration" in blacklisted.reason
    assert cwd_escape.allowed is False


def test_session_auto_approve_is_scoped_and_toggleable():
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_command_check,
        shell_session_auto_approve_disable,
        shell_session_auto_approve_enable,
        shell_session_auto_approve_status,
    )

    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    other = ShellAuthorizationScope("session-2", "feishu", "user-1")

    shell_session_auto_approve_enable(scope)
    assert shell_session_auto_approve_status(scope) is True
    assert shell_command_check("mkfs /dev/disk0", scope=scope).allowed is True
    assert (
        shell_command_check("mkfs /dev/disk0", scope=other).requires_confirmation
        is True
    )

    shell_session_auto_approve_disable(scope)
    assert shell_session_auto_approve_status(scope) is False
    assert (
        shell_command_check("mkfs /dev/disk0", scope=scope).requires_confirmation
        is True
    )


@pytest.mark.parametrize(
    "level, high_state, operator_state",
    [
        ("ask", "confirm", "confirm"),
        ("medium", "allowed", "confirm"),
        ("high", "allowed", "allowed"),
        ("full", "allowed", "allowed"),
    ],
)
def test_permission_levels_scale_high_risk_and_operators(level, high_state, operator_state):
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_command_check,
    )

    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    medium = shell_command_check("mv a b", scope=scope, permission_level=level)
    high = shell_command_check(
        "mkfs /dev/disk0", scope=scope, permission_level=level
    )
    operators = shell_command_check(
        "echo a; echo b", scope=scope, permission_level=level
    )

    assert medium.allowed is True
    if high_state == "allowed":
        assert high.allowed is True
    else:
        assert high.requires_confirmation is True
    if operator_state == "allowed":
        assert operators.allowed is True
    else:
        assert operators.requires_confirmation is True


def test_session_permission_override_takes_precedence_over_default():
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_command_check,
        shell_session_permission_set,
    )

    scope = ShellAuthorizationScope("session-1", "feishu", "user-1")
    shell_session_permission_set(scope, "full")

    # The passed-in config default is "ask"; the session override must win.
    result = shell_command_check(
        "mkfs /dev/disk0", scope=scope, permission_level="ask"
    )
    assert result.allowed is True

    shell_session_permission_set(scope, "invalid-level")
    assert shell_command_check(
        "mkfs /dev/disk0", scope=scope, permission_level="ask"
    ).requires_confirmation is True


def test_extra_blocked_blacklist_survives_all_permission_levels():
    from agent.security.shell import shell_command_check

    for level in ("ask", "medium", "high", "full"):
        result = shell_command_check(
            "dangerous-tool x",
            extra_blocked=["dangerous-tool"],
            permission_level=level,
        )
        assert result.allowed is False
        assert "blocked by configuration" in result.reason
