from __future__ import annotations

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
