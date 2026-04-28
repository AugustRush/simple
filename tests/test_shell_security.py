from __future__ import annotations


def test_shell_security_module_blocks_destructive_commands():
    from agent.security.shell import shell_command_is_blocked

    assert shell_command_is_blocked("rm -rf /") == "command 'rm' is blocked for safety"


def test_agent_package_reexports_shell_blocker_for_compatibility():
    import agent
    from agent.security.shell import shell_command_is_blocked

    assert agent._shell_command_is_blocked is shell_command_is_blocked
