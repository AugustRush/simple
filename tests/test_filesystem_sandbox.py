"""Tests for the OS-level shell filesystem sandbox adapter."""

import os
import time
from pathlib import Path

import pytest

from agent.security.filesystem_sandbox import (
    SandboxUnavailableError,
    ShellSandboxRequest,
    build_sandbox_command,
    detect_sandbox_support,
    new_scratch_dir,
    reclaim_stale_scratch_dirs,
    release_scratch_dir,
    _macos_seatbelt_profile,
)


def _request(
    tmp_path,
    *,
    workspace_read=True,
    workspace_write=False,
    write_scope=(),
    mode="read_all",
    devices=True,
    home_dir=None,
    extra_secret_paths=(),
    agent_home=None,
):
    workspace = tmp_path / "workspace"
    output = tmp_path / "output"
    return ShellSandboxRequest(
        workspace_root=workspace,
        output_root=output,
        workspace_read=workspace_read,
        workspace_write=workspace_write,
        write_scope=tuple(write_scope),
        scratch_dir=output / "sandbox" / "tmp",
        mode=mode,
        devices=devices,
        home_dir=home_dir or (tmp_path / "home"),
        extra_secret_paths=tuple(extra_secret_paths),
        agent_home=agent_home,
    )


# ── Profile generation ──────────────────────────────────────────────────────


def test_profile_hides_workspace_when_read_disabled(tmp_path):
    request = _request(tmp_path, workspace_read=False)
    profile = _macos_seatbelt_profile(request)

    assert f'file-read* (subpath "{request.workspace_root}")' not in profile
    assert f'file-read* (subpath "{request.output_root}")' in profile
    assert '(allow file-write* (subpath "/"))' in profile


def test_profile_denies_workspace_writes_by_default(tmp_path):
    request = _request(tmp_path)
    profile = _macos_seatbelt_profile(request)

    assert f'file-read* (subpath "{request.workspace_root}")' in profile
    assert (
        f'(deny file-write* (subpath "{request.workspace_root}"))'
        in profile
    )


def test_profile_reopens_scoped_workspace_writes_after_deny(tmp_path):
    request = _request(
        tmp_path, workspace_write=False, write_scope=["src/app.py"]
    )
    profile = _macos_seatbelt_profile(request)

    assert (
        f'(deny file-write* (subpath "{request.workspace_root}"))'
        in profile
    )
    assert (
        f'(allow file-write* (subpath "{request.workspace_root}/src/app.py"))'
        in profile
    )


def test_profile_workspace_write_survives_protected_dir_shadowing(tmp_path):
    """workspace.write=true must reopen the workspace AFTER the protected-data
    denies, or a workspace under ~/Desktop/~Documents is silently made
    read-only while the same flag opens writes elsewhere."""
    home = tmp_path / "home"
    workspace = home / "Desktop" / "ws"
    request = ShellSandboxRequest(
        workspace_root=workspace,
        output_root=tmp_path / "output",
        workspace_read=True,
        workspace_write=True,
        write_scope=(),
        scratch_dir=tmp_path / "output" / "sandbox" / "tmp",
        mode="read_all",
        home_dir=home,
    )
    profile = _macos_seatbelt_profile(request)

    deny_desktop = (
        f'(deny file-write* (subpath "'
        f'{home.resolve(strict=False) / "Desktop"}"))'
    )
    allow_workspace = (
        f'(allow file-write* (subpath "{workspace.resolve(strict=False)}"))'
    )
    assert deny_desktop in profile
    assert allow_workspace in profile
    assert profile.index(allow_workspace) > profile.index(deny_desktop)


def test_profile_denies_internal_bookkeeping(tmp_path):
    request = _request(tmp_path)
    profile = _macos_seatbelt_profile(request)
    internal = request.output_root / ".simple-internal"

    assert f'(deny file-write* (subpath "{internal}"))' in profile
    assert f'(deny file-read* (subpath "{internal}"))' in profile


def test_read_all_mode_opens_reads_and_defaults_writes_open(tmp_path):
    request = _request(tmp_path, mode="read_all")
    profile = _macos_seatbelt_profile(request)

    assert '(allow file-read* (subpath "/"))' in profile
    assert '(allow file-write* (subpath "/"))' in profile
    assert (
        f'(deny file-write* (subpath "{request.workspace_root}"))'
        in profile
    )


def test_restricted_mode_has_no_read_all_rule(tmp_path):
    request = _request(tmp_path, mode="restricted")
    profile = _macos_seatbelt_profile(request)

    assert '(allow file-read* (subpath "/"))' not in profile


def test_device_rules_open_by_default_and_can_be_disabled(tmp_path):
    default = _macos_seatbelt_profile(_request(tmp_path, mode="read_all"))
    disabled = _macos_seatbelt_profile(
        _request(tmp_path, mode="read_all", devices=False)
    )

    assert '(allow iokit-open)' in default
    assert '(global-name "com.apple.Metal")' in default
    assert '(global-name "com.apple.IOAccelerator")' in default
    assert '(allow iokit-open)' not in disabled


def test_device_rules_added_in_restricted_mode_too(tmp_path):
    profile = _macos_seatbelt_profile(
        _request(tmp_path, mode="restricted", devices=True)
    )

    assert '(allow iokit-open)' in profile


def test_none_mode_builds_unsandboxed_command(tmp_path):
    request = _request(tmp_path, mode="none", devices=True)
    sandbox = build_sandbox_command(request)

    assert sandbox.argv_prefix == ()
    assert sandbox.env_updates == {}


def test_profile_keeps_scratch_readable_and_writes_open(tmp_path):
    request = _request(tmp_path)
    profile = _macos_seatbelt_profile(request)

    assert f'file-read* (subpath "{request.scratch_dir}")' in profile
    assert '(allow file-write* (subpath "/"))' in profile
    assert f'(deny file-write* (subpath "{request.scratch_dir}"))' not in profile


def test_profile_escapes_seatbelt_literals(tmp_path):
    workspace = tmp_path / 'weird"name'
    output = tmp_path / "output"
    request = ShellSandboxRequest(
        workspace_root=workspace,
        output_root=output,
        workspace_read=True,
        workspace_write=False,
        write_scope=(),
        scratch_dir=output / "sandbox" / "tmp",
    )
    profile = _macos_seatbelt_profile(request)

    assert f'file-read* (subpath "{str(workspace).replace(chr(34), chr(92) + chr(34))}")' in profile


def test_build_sandbox_command_fails_closed_without_adapter(monkeypatch, tmp_path):
    # Patched in the backend registry rather than via the re-export shim:
    # `build_sandbox_command` calls `detect_sandbox_support` through its own
    # module globals, so that is the name dispatch actually reads.
    monkeypatch.setattr(
        "agent.security.sandbox.backends.detect_sandbox_support",
        lambda: None,
    )
    with pytest.raises(SandboxUnavailableError, match="no enforcing"):
        build_sandbox_command(_request(tmp_path))


def test_profile_opens_tool_state_and_protects_user_data(tmp_path):
    """Writes are open by default; only user data is denied."""
    request = _request(tmp_path)
    profile = _macos_seatbelt_profile(request)
    home = request.home_dir

    # Tool state (caches, app data) is not enumerated: it is open by default.
    assert '(allow file-write* (subpath "/"))' in profile
    for sub in (".cache", ".npm", "Library/Caches", "Library/Application Support"):
        literal = str(home / sub)
        assert f'(deny file-write* (subpath "{literal}"))' not in profile

    # User data surfaces are the deny list.
    for sub in ("Documents", ".ssh", ".aws", ".git-credentials"):
        literal = str(home / sub)
        assert f'(deny file-write* (subpath "{literal}"))' in profile


def test_profile_allows_gui_app_system_services(tmp_path):
    """GUI/rendering apps get the mach/preferences facilities they need."""
    profile = _macos_seatbelt_profile(_request(tmp_path))

    assert "(allow mach-bootstrap)" in profile
    assert "(allow mach-register)" in profile
    assert "(allow mach-lookup)" in profile
    assert "(allow file-issue-extension)" in profile
    assert "(allow user-preference-read)" in profile


def test_profile_protected_paths_use_request_home_dir(tmp_path):
    home = tmp_path / "other-home"
    request = _request(tmp_path, home_dir=home)
    profile = _macos_seatbelt_profile(request)

    ssh_dir = home / ".ssh"
    real_ssh_dir = Path.home() / ".ssh"
    assert f'(deny file-write* (subpath "{ssh_dir}"))' in profile
    assert f'(deny file-write* (subpath "{real_ssh_dir}"))' not in profile


# ── Seatbelt enforcement details ───────────────────────────────────────────
#
# What survives here is macOS-specific: the `.sb` cache key, and the profile
# text itself.  The backend-neutral question — what the OS actually permits a
# sandboxed child to do — moved to `test_sandbox_conformance.py`, which runs
# the same assertions against whatever backend the host has.


_NEEDS_SANDBOX = pytest.mark.skipif(
    detect_sandbox_support() != "darwin-sandbox-exec",
    reason="requires macOS sandbox-exec",
)



def test_scratch_env_points_inside_output(tmp_path):
    request = _request(tmp_path)
    request.output_root.mkdir()
    sandbox = build_sandbox_command(request)

    assert sandbox.env_updates["TMPDIR"].startswith(
        str(request.output_root / "sandbox")
    )
    assert sandbox.env_updates["TMP"] == sandbox.env_updates["TMPDIR"]
    assert sandbox.env_updates["TEMP"] == sandbox.env_updates["TMPDIR"]


# ── The profile cache key must cover everything the profile depends on ───────


def _profile_path(request):
    return Path(build_sandbox_command(request).argv_prefix[-1])


@_NEEDS_SANDBOX
def test_cached_profile_is_keyed_by_sandbox_mode(tmp_path):
    """A hand-listed cache key drifts from what the profile depends on.

    `mode` was absent from the key, so a `restricted` request reused a
    previously written `read_all` profile and silently received host-wide reads
    — the sandbox mode the caller selected was not the one enforced.
    """
    permissive = _request(tmp_path, mode="read_all")
    strict = _request(tmp_path, mode="restricted")

    permissive_path = _profile_path(permissive)
    strict_path = _profile_path(strict)

    assert permissive_path != strict_path
    open_all = '(allow file-read* (subpath "/"))'
    assert open_all in permissive_path.read_text()
    assert open_all not in strict_path.read_text()


@_NEEDS_SANDBOX
def test_cached_profile_is_keyed_by_device_access(tmp_path):
    with_devices = _profile_path(_request(tmp_path, devices=True))
    without_devices = _profile_path(_request(tmp_path, devices=False))

    assert with_devices != without_devices
    assert "(allow iokit-open)" in with_devices.read_text()
    assert "(allow iokit-open)" not in without_devices.read_text()


@_NEEDS_SANDBOX
def test_cached_profile_is_keyed_by_home_dir(tmp_path):
    """Two homes must not share a profile whose denies name only one of them."""
    alice = _profile_path(_request(tmp_path, home_dir=tmp_path / "alice"))
    bob = _profile_path(_request(tmp_path, home_dir=tmp_path / "bob"))

    assert alice != bob
    assert str(tmp_path / "bob" / "Documents") in bob.read_text()
    assert str(tmp_path / "alice" / "Documents") not in bob.read_text()


@_NEEDS_SANDBOX
def test_identical_requests_reuse_one_profile(tmp_path):
    """Keying on content must not defeat caching for equivalent requests."""
    first = _profile_path(_request(tmp_path, mode="restricted"))
    second = _profile_path(_request(tmp_path, mode="restricted"))
    assert first == second


# ── Protected user data must stay protected when relocated ──────────────────


def test_protected_paths_are_denied_under_both_spellings(tmp_path):
    """Seatbelt enforces on canonical paths.

    A relocated home directory — ~/Documents symlinked to an external volume, a
    Dropbox or iCloud folder — slips through a deny rule that names only the
    symlink, because the kernel checks the resolved path.  Both spellings are
    therefore denied.
    """
    home = tmp_path / "home"
    home.mkdir()
    external = tmp_path / "external" / "Docs"
    external.mkdir(parents=True)
    (home / "Documents").symlink_to(external)

    profile = _macos_seatbelt_profile(_request(tmp_path, home_dir=home))

    assert f'(deny file-write* (subpath "{home / "Documents"}"))' in profile
    assert f'(deny file-write* (subpath "{external}"))' in profile



# ── Secret reads, autostart writes, agent home ─────────────────────────────



def test_extra_secret_paths_extend_rather_than_replace_defaults(tmp_path):
    request = _request(tmp_path, extra_secret_paths=(".ssh",))
    profile = _macos_seatbelt_profile(request)
    home = str((tmp_path / "home").resolve())

    assert f'(deny file-read* (subpath "{home}/.ssh"))' in profile
    assert f'(deny file-read* (subpath "{home}/.netrc"))' in profile


def test_autostart_system_dirs_are_write_denied(tmp_path):
    profile = _macos_seatbelt_profile(_request(tmp_path))
    for path in ("/usr/local/bin", "/opt/homebrew/bin", "/Library/LaunchDaemons"):
        assert f'(deny file-write* (subpath "{path}"))' in profile


def test_secret_denies_follow_the_default_open_write_rule(tmp_path):
    """Seatbelt is last-match-wins: order is the whole enforcement story."""
    profile = _macos_seatbelt_profile(_request(tmp_path))
    lines = profile.splitlines()
    open_writes = lines.index('(allow file-write* (subpath "/"))')
    home = str((tmp_path / "home").resolve())
    secret_deny = lines.index(f'(deny file-read* (subpath "{home}/.netrc"))')
    autostart_deny = lines.index(f'(deny file-write* (subpath "{home}/.zshrc"))')

    assert open_writes < secret_deny
    assert open_writes < autostart_deny


# ── Scratch directory lifecycle ────────────────────────────────────────────


def test_release_scratch_dir_removes_it(tmp_path):
    output = tmp_path / "output"
    scratch = new_scratch_dir(output)
    (scratch / "tempfile").write_text("junk", encoding="utf-8")

    release_scratch_dir(scratch)

    assert not scratch.exists()


def test_release_scratch_dir_is_idempotent_and_null_safe(tmp_path):
    scratch = new_scratch_dir(tmp_path / "output")
    release_scratch_dir(scratch)
    release_scratch_dir(scratch)  # already gone
    release_scratch_dir(None)


def test_new_scratch_dir_reclaims_stale_siblings(tmp_path):
    """A SIGKILL skips every `finally`, so age-based reclamation is the backstop."""
    output = tmp_path / "output"
    stale = new_scratch_dir(output)
    fresh = new_scratch_dir(output)
    old = time.time() - (7 * 3600)
    os.utime(stale, (old, old))

    new_scratch_dir(output)

    assert not stale.exists()
    assert fresh.exists()  # recent dirs belong to in-flight commands


def test_reclaim_ignores_unrelated_entries(tmp_path):
    scratch_root = tmp_path / "output" / "sandbox"
    scratch_root.mkdir(parents=True)
    keeper = scratch_root / "not-a-scratch-dir"
    keeper.mkdir()
    old = time.time() - (99 * 3600)
    os.utime(keeper, (old, old))

    assert reclaim_stale_scratch_dirs(scratch_root) == 0
    assert keeper.exists()


@_NEEDS_SANDBOX
def test_shell_tool_does_not_leak_a_scratch_dir_per_call(tmp_path):
    """The leak this fixes: one directory per shell invocation, forever."""
    import asyncio

    from agent import BuiltinTools, MemoryPalace, ToolRegistry

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "output"
    output.mkdir()

    registry = ToolRegistry()
    memory = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
    )
    tools = BuiltinTools(
        memory=memory,
        registry=registry,
        workspace_root=workspace,
        output_dir=output,
    )

    for _ in range(3):
        result = asyncio.run(
            tools._shell("echo hi", intent="test scratch cleanup")
        )
        assert result.get("ok") is True, result

    leftovers = list((output / "sandbox").glob("tmp-*"))
    assert leftovers == [], f"leaked scratch dirs: {leftovers}"
