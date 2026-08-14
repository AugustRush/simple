"""Tests for the backend-neutral sandbox policy.

These assert the *decision* — which paths a sandboxed child may read and
write — without any reference to seatbelt, Landlock, or profile text.  That
separation is the point of the seam: a second backend has to satisfy exactly
these expectations, and it can only do so if they are stated in terms no
single mechanism owns.

Seatbelt-specific rendering is covered by ``test_filesystem_sandbox.py``.
"""

import pytest

from agent.security.sandbox.policy import (
    SANDBOX_MODE_NONE,
    SANDBOX_MODE_READ_ALL,
    SANDBOX_MODE_RESTRICTED,
    ShellSandboxRequest,
    resolve_policy,
)


def _request(
    tmp_path,
    *,
    workspace_read=True,
    workspace_write=False,
    write_scope=(),
    mode=SANDBOX_MODE_READ_ALL,
    devices=True,
    extra_secret_paths=(),
    extra_read_paths=(),
    agent_home=None,
):
    output = tmp_path / "output"
    return ShellSandboxRequest(
        workspace_root=tmp_path / "workspace",
        output_root=output,
        workspace_read=workspace_read,
        workspace_write=workspace_write,
        write_scope=tuple(write_scope),
        scratch_dir=output / "sandbox" / "tmp",
        mode=mode,
        devices=devices,
        home_dir=tmp_path / "home",
        extra_secret_paths=tuple(extra_secret_paths),
        extra_read_paths=tuple(extra_read_paths),
        agent_home=agent_home,
    )


def _rights(tmp_path, path, **kw):
    return resolve_policy(_request(tmp_path, **kw)).effective_rights(path)


# ── The three protected asset classes ───────────────────────────────────────


def test_secrets_are_denied_for_read_not_only_write(tmp_path):
    """The load-bearing rule: a credential leaks by being *read*."""
    home = tmp_path / "home"
    for secret in (".aws", ".gnupg", ".netrc", ".config/gh", "Library/Keychains"):
        readable, writable = _rights(tmp_path, home / secret)
        assert not readable, f"{secret} must not be readable"
        assert not writable, f"{secret} must not be writable"


def test_secret_denial_covers_files_inside_the_directory(tmp_path):
    """Denials are subtree-wide — the file inside is the thing worth reading."""
    readable, writable = _rights(tmp_path, tmp_path / "home" / ".aws" / "credentials")
    assert (readable, writable) == (False, False)


def test_extra_secret_paths_extend_rather_than_replace_the_defaults(tmp_path):
    home = tmp_path / "home"
    policy = resolve_policy(_request(tmp_path, extra_secret_paths=(".ssh",)))

    assert policy.effective_rights(home / ".ssh" / "id_rsa") == (False, False)
    # A default is still denied alongside the added one.
    assert policy.effective_rights(home / ".aws") == (False, False)


def test_ssh_stays_readable_by_default_so_git_push_works(tmp_path):
    """Deliberate: a default that breaks `git push` gets switched off wholesale."""
    readable, _ = _rights(tmp_path, tmp_path / "home" / ".ssh")
    assert readable


def test_later_executed_code_is_write_denied_but_still_readable(tmp_path):
    """Write is the boundary that matters; reading a shell rc harms nothing."""
    home = tmp_path / "home"
    for target in (".zshrc", ".bashrc", ".local/bin", "Library/LaunchAgents"):
        readable, writable = _rights(tmp_path, home / target)
        assert not writable, f"{target} must not be writable"
        assert readable, f"{target} should stay readable in read_all"


def test_system_path_directories_are_write_denied(tmp_path):
    for target in ("/usr/local/bin", "/opt/homebrew/bin", "/Library/LaunchDaemons"):
        _, writable = _rights(tmp_path, target)
        assert not writable, f"{target} must not be writable"


def test_user_data_is_write_denied(tmp_path):
    home = tmp_path / "home"
    for target in ("Documents", "Desktop", "Downloads", "Pictures"):
        _, writable = _rights(tmp_path, home / target)
        assert not writable, f"{target} must not be writable"


# ── The inverted write policy ───────────────────────────────────────────────


def test_unlisted_tool_state_is_writable_without_a_carve_out(tmp_path):
    """The whole point of inverting the policy: no per-tool enumeration."""
    home = tmp_path / "home"
    for target in (".cache", ".npm/_cacache", "Library/Application Support/SomeTool"):
        _, writable = _rights(tmp_path, home / target)
        assert writable, f"{target} should be writable by default"


def test_a_tool_invented_tomorrow_is_writable(tmp_path):
    _, writable = _rights(tmp_path, tmp_path / "home" / ".config" / "brand-new-tool")
    assert writable


# ── Workspace and write_scope ───────────────────────────────────────────────


def test_workspace_is_not_writable_by_default(tmp_path):
    _, writable = _rights(tmp_path, tmp_path / "workspace" / "main.py")
    assert not writable


def test_workspace_write_flag_opens_the_workspace(tmp_path):
    _, writable = _rights(tmp_path, tmp_path / "workspace" / "main.py", workspace_write=True)
    assert writable


def test_write_scope_reopens_only_the_named_subtree(tmp_path):
    policy = resolve_policy(_request(tmp_path, write_scope=("build",)))
    workspace = tmp_path / "workspace"

    assert policy.effective_rights(workspace / "build" / "out.o")[1]
    assert not policy.effective_rights(workspace / "src" / "main.py")[1]


def test_write_scope_star_opens_the_whole_workspace(tmp_path):
    _, writable = _rights(tmp_path, tmp_path / "workspace" / "src" / "main.py", write_scope=("*",))
    assert writable


def test_write_scope_cannot_reopen_a_protected_path_by_escaping(tmp_path):
    """`..` in a scope entry must not resurrect a denied path.

    Writes are open by default, so a path merely *outside* the workspace is
    writable anyway — that is the inverted policy working, not a leak.  What
    the containment check has to stop is a scope entry reaching back into an
    asset class and re-opening it after its denial.
    """
    protected = tmp_path / "home" / "Documents"
    _, writable = _rights(tmp_path, protected, write_scope=("../home/Documents",))
    assert not writable


def test_workspace_read_flag_controls_readability_in_restricted_mode(tmp_path):
    hidden, _ = _rights(
        tmp_path, tmp_path / "workspace" / "main.py",
        mode=SANDBOX_MODE_RESTRICTED, workspace_read=False,
    )
    visible, _ = _rights(
        tmp_path, tmp_path / "workspace" / "main.py",
        mode=SANDBOX_MODE_RESTRICTED, workspace_read=True,
    )
    assert not hidden
    assert visible


# ── Modes ───────────────────────────────────────────────────────────────────


def test_read_all_opens_reads_host_wide_but_not_writes(tmp_path):
    policy = resolve_policy(_request(tmp_path, mode=SANDBOX_MODE_READ_ALL))
    assert policy.read_everywhere
    readable, writable = policy.effective_rights("/opt/some/unrelated/path")
    assert readable
    # Writes are open by default too — the denials are what scope them.
    assert writable
    assert not policy.effective_rights(tmp_path / "home" / "Documents")[1]


def test_restricted_mode_does_not_open_reads_host_wide(tmp_path):
    policy = resolve_policy(_request(tmp_path, mode=SANDBOX_MODE_RESTRICTED))
    assert not policy.read_everywhere
    assert not policy.effective_rights("/opt/unrelated")[0]


def test_restricted_mode_still_reads_output_scratch_and_tool_state(tmp_path):
    policy = resolve_policy(_request(tmp_path, mode=SANDBOX_MODE_RESTRICTED))
    for target in (
        tmp_path / "output",
        tmp_path / "output" / "sandbox" / "tmp",
        tmp_path / "home" / ".cache",
        tmp_path / "home" / ".config",
    ):
        assert policy.effective_rights(target)[0], target


def test_extra_read_paths_are_readable_in_restricted_mode(tmp_path):
    """Without this the child dies in init_import_site before running anything."""
    venv = tmp_path / "venv"
    policy = resolve_policy(
        _request(tmp_path, mode=SANDBOX_MODE_RESTRICTED, extra_read_paths=(str(venv),))
    )
    assert policy.effective_rights(venv / "bin" / "python")[0]


# ── The agent's own home ────────────────────────────────────────────────────


def test_agent_home_is_denied_but_output_and_scratch_are_reopened(tmp_path):
    """config.json holds provider API keys; artifacts still have to work."""
    agent_home = tmp_path / "agenthome"
    output = agent_home / "output"
    request = ShellSandboxRequest(
        workspace_root=tmp_path / "workspace",
        output_root=output,
        workspace_read=True,
        workspace_write=False,
        write_scope=(),
        scratch_dir=output / "sandbox" / "tmp",
        mode=SANDBOX_MODE_READ_ALL,
        home_dir=tmp_path / "home",
        agent_home=agent_home,
    )
    policy = resolve_policy(request)

    assert policy.effective_rights(agent_home / "config.json") == (False, False)
    assert policy.effective_rights(output / "artifact.txt") == (True, True)
    assert policy.effective_rights(output / "sandbox" / "tmp" / "t") == (True, True)


def test_internal_bookkeeping_stays_hidden_inside_a_reopened_output(tmp_path):
    """Ordering matters: the internal denial must survive the output re-open."""
    agent_home = tmp_path / "agenthome"
    output = agent_home / "output"
    request = ShellSandboxRequest(
        workspace_root=tmp_path / "workspace",
        output_root=output,
        workspace_read=True,
        workspace_write=False,
        write_scope=(),
        scratch_dir=output / "sandbox" / "tmp",
        home_dir=tmp_path / "home",
        agent_home=agent_home,
    )
    policy = resolve_policy(request)

    assert policy.effective_rights(output / ".simple-internal") == (False, False)
    assert policy.effective_rights(output / ".simple-internal" / "sandbox") == (
        False,
        False,
    )


# ── Capabilities ────────────────────────────────────────────────────────────


def test_devices_flag_is_carried_through(tmp_path):
    assert resolve_policy(_request(tmp_path, devices=True)).devices
    assert not resolve_policy(_request(tmp_path, devices=False)).devices


def test_mode_none_carries_no_device_grant(tmp_path):
    policy = resolve_policy(_request(tmp_path, mode=SANDBOX_MODE_NONE, devices=True))
    assert not policy.devices


# ── Enumeration for additive backends ───────────────────────────────────────


def test_writable_subtrees_excludes_denied_paths(tmp_path):
    """What an allowlist-only backend needs: grants, with denials removed."""
    policy = resolve_policy(_request(tmp_path, write_scope=("build",)))
    writable = policy.writable_subtrees()
    home = str(tmp_path / "home")

    assert str((tmp_path / "workspace" / "build").resolve()) in writable
    assert not any(path.startswith(f"{home}/Documents") for path in writable)
    assert not any(path.startswith(f"{home}/.aws") for path in writable)


def test_denied_subtrees_reports_what_an_additive_backend_cannot_express(tmp_path):
    policy = resolve_policy(_request(tmp_path))
    denied_reads = policy.denied_subtrees(read=True)
    home = str(tmp_path / "home")

    assert f"{home}/.aws" in denied_reads
    # Write-only denials are not read denials.
    assert f"{home}/Documents" not in denied_reads
    assert f"{home}/Documents" in policy.denied_subtrees(read=False)


def test_a_path_reopened_after_a_denial_counts_as_granted_not_denied(tmp_path):
    """Enumeration must reflect the final decision, not every rule touched."""
    agent_home = tmp_path / "agenthome"
    output = agent_home / "output"
    policy = resolve_policy(
        ShellSandboxRequest(
            workspace_root=tmp_path / "workspace",
            output_root=output,
            workspace_read=True,
            workspace_write=False,
            write_scope=(),
            scratch_dir=output / "sandbox" / "tmp",
            home_dir=tmp_path / "home",
            agent_home=agent_home,
        )
    )
    resolved_output = str(output.resolve())

    assert resolved_output in policy.writable_subtrees()
    assert resolved_output not in policy.denied_subtrees(read=False)


@pytest.mark.parametrize("mode", [SANDBOX_MODE_RESTRICTED, SANDBOX_MODE_READ_ALL])
def test_asset_classes_are_protected_in_every_sandboxed_mode(tmp_path, mode):
    """Reads widen with the mode; the three asset classes do not move."""
    home = tmp_path / "home"
    policy = resolve_policy(_request(tmp_path, mode=mode))

    assert policy.effective_rights(home / ".aws")[0] is False
    assert policy.effective_rights(home / ".zshrc")[1] is False
    assert policy.effective_rights(home / "Documents")[1] is False
