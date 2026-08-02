"""Tests for the OS-level shell filesystem sandbox adapter."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent.security.filesystem_sandbox import (
    SandboxUnavailableError,
    ShellSandboxRequest,
    build_sandbox_command,
    detect_sandbox_support,
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
    monkeypatch.setattr(
        "agent.security.filesystem_sandbox.detect_sandbox_support",
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


# ── Real sandbox enforcement (macOS sandbox-exec) ──────────────────────────


_NEEDS_SANDBOX = pytest.mark.skipif(
    detect_sandbox_support() != "darwin-sandbox-exec",
    reason="requires macOS sandbox-exec",
)


def _run_in_sandbox(request: ShellSandboxRequest, command: str):
    sandbox = build_sandbox_command(request)
    env = dict(os.environ)
    env.update(sandbox.env_updates)
    request.output_root.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [*sandbox.argv_prefix, "/bin/sh", "-c", command],
        env=env,
        cwd=request.output_root,
        capture_output=True,
        text=True,
        timeout=20,
    )


@_NEEDS_SANDBOX
def test_sandbox_blocks_workspace_write_by_default(tmp_path):
    request = _request(tmp_path)
    request.workspace_root.mkdir()
    target = request.workspace_root / "new.txt"

    result = _run_in_sandbox(request, f"touch {target}")

    assert result.returncode != 0
    assert not target.exists()


@_NEEDS_SANDBOX
def test_sandbox_allows_output_writes(tmp_path):
    request = _request(tmp_path)
    request.output_root.mkdir()
    target = request.output_root / "made.txt"

    result = _run_in_sandbox(request, f"touch {target}")

    assert result.returncode == 0, result.stderr
    assert target.exists()


@_NEEDS_SANDBOX
def test_sandbox_allows_writes_to_user_cache_dirs(tmp_path):
    request = _request(tmp_path, home_dir=tmp_path / "home")
    cache = request.home_dir / ".npm"
    cache.mkdir(parents=True)
    target = cache / "cache-write-test"

    result = _run_in_sandbox(request, f"touch {target}")

    assert result.returncode == 0, result.stderr
    assert target.exists()


@_NEEDS_SANDBOX
def test_sandbox_allows_writes_to_application_support(tmp_path):
    import shlex

    request = _request(tmp_path, home_dir=tmp_path / "home")
    app_support = request.home_dir / "Library" / "Application Support"
    app_support.mkdir(parents=True)
    target = app_support / "app-state-write-test"

    result = _run_in_sandbox(request, f"touch {shlex.quote(str(target))}")

    assert result.returncode == 0, result.stderr
    assert target.exists()


@_NEEDS_SANDBOX
def test_sandbox_keeps_home_documents_read_only(tmp_path):
    request = _request(tmp_path, home_dir=tmp_path / "home")
    documents = request.home_dir / "Documents"
    documents.mkdir(parents=True)
    target = documents / "secret.txt"

    result = _run_in_sandbox(request, f"touch {target}")

    assert result.returncode != 0
    assert not target.exists()


@_NEEDS_SANDBOX
def test_sandbox_denies_credentials_and_reopens_scoped_workspace(tmp_path):
    """write_scope reopens paths even inside a protected user-data root."""
    home = tmp_path / "home"
    workspace = home / "Desktop" / "ws"
    workspace.mkdir(parents=True)
    (workspace / "src").mkdir()
    output = tmp_path / "output"
    request = ShellSandboxRequest(
        workspace_root=workspace,
        output_root=output,
        workspace_read=True,
        workspace_write=False,
        write_scope=("src",),
        scratch_dir=output / "sandbox" / "tmp",
        home_dir=home,
    )
    output.mkdir(parents=True, exist_ok=True)

    credentials = home / ".ssh"
    credentials.mkdir(parents=True)
    credential_target = credentials / "id_rsa"
    scoped_target = workspace / "src" / "ok.txt"
    desktop_target = home / "Desktop" / "other" / "no.txt"

    result = _run_in_sandbox(
        request,
        (
            f"touch {credential_target}; echo CRED=$?; "
            f"touch {scoped_target}; echo SCOPED=$?; "
            f"mkdir -p {desktop_target.parent}; touch {desktop_target}; "
            "echo DESKTOP=$?"
        ),
    )

    assert "CRED=1" in result.stdout
    assert "SCOPED=0" in result.stdout
    assert "DESKTOP=1" in result.stdout
    assert credential_target.exists() is False
    assert scoped_target.exists() is True
    assert desktop_target.exists() is False


@_NEEDS_SANDBOX
def test_sandbox_allows_workspace_reads(tmp_path):
    request = _request(tmp_path)
    request.workspace_root.mkdir()
    source = request.workspace_root / "a.txt"
    source.write_text("secret", encoding="utf-8")

    result = _run_in_sandbox(request, f"cat {source}")

    assert result.returncode == 0, result.stderr
    assert "secret" in result.stdout


@_NEEDS_SANDBOX
def test_sandbox_read_all_mode_reads_outside_workspace(tmp_path):
    request = _request(tmp_path, mode="read_all")
    request.output_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("home-secret", encoding="utf-8")

    result = _run_in_sandbox(request, f"cat {outside}")

    assert result.returncode == 0, result.stderr
    assert "home-secret" in result.stdout


@_NEEDS_SANDBOX
def test_sandbox_restricted_mode_blocks_outside_reads(tmp_path):
    request = _request(tmp_path, mode="restricted")
    request.output_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("home-secret", encoding="utf-8")

    result = _run_in_sandbox(request, f"cat {outside}")

    assert result.returncode != 0
    assert "home-secret" not in result.stdout


@_NEEDS_SANDBOX
def test_sandbox_denies_workspace_reads_when_read_disabled(tmp_path):
    # "workspace reads disabled" is a restricted-mode policy; read_all mode
    # intentionally opens reads everywhere, so this must use restricted.
    request = _request(tmp_path, workspace_read=False, mode="restricted")
    request.workspace_root.mkdir()
    source = request.workspace_root / "a.txt"
    source.write_text("secret", encoding="utf-8")

    result = _run_in_sandbox(request, f"cat {source}")

    assert result.returncode != 0


@_NEEDS_SANDBOX
def test_sandbox_allows_only_scoped_workspace_writes(tmp_path):
    request = _request(
        tmp_path, workspace_write=False, write_scope=["src/app.py"]
    )
    (request.workspace_root / "src").mkdir(parents=True)
    allowed = request.workspace_root / "src" / "app.py"
    denied = request.workspace_root / "other.txt"

    ok = _run_in_sandbox(request, f"touch {allowed}")
    assert ok.returncode == 0, ok.stderr
    assert allowed.exists()

    bad = _run_in_sandbox(request, f"touch {denied}")
    assert bad.returncode != 0
    assert not denied.exists()


@_NEEDS_SANDBOX
def test_sandbox_opens_host_temp_and_denies_internal_output(tmp_path):
    request = _request(tmp_path)
    request.output_root.mkdir()
    internal = request.output_root / ".simple-internal" / "locks"
    internal.mkdir(parents=True)

    host_tmp = _run_in_sandbox(request, "touch /tmp/simple-sandbox-test")
    assert host_tmp.returncode == 0, host_tmp.stderr
    assert Path("/tmp/simple-sandbox-test").exists()
    Path("/tmp/simple-sandbox-test").unlink(missing_ok=True)

    internal_tmp = request.output_root / ".simple-internal" / "probe"
    internal_write = _run_in_sandbox(request, f"touch {internal_tmp}")
    assert internal_write.returncode != 0
    assert not internal_tmp.exists()


@_NEEDS_SANDBOX
def test_sandbox_enforces_rule_in_child_processes(tmp_path):
    request = _request(tmp_path)
    request.workspace_root.mkdir()
    target = request.workspace_root / "child.txt"

    result = _run_in_sandbox(
        request,
        f"(touch {target}) ; wait ; echo done",
    )

    assert not target.exists()
    assert "Operation not permitted" in result.stderr


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


@_NEEDS_SANDBOX
def test_sandbox_blocks_writes_through_a_relocated_documents_dir(tmp_path):
    """The OS-enforced version of the invariant above."""
    real_tmp = Path(tmp_path).resolve()
    home = real_tmp / "home"
    home.mkdir(exist_ok=True)
    external = real_tmp / "external" / "Docs"
    external.mkdir(parents=True, exist_ok=True)
    (home / "Documents").symlink_to(external)
    secret = external / "tax.txt"
    secret.write_text("secret", encoding="utf-8")

    request = _request(tmp_path, home_dir=home)

    # Through the symlink...
    via_link = _run_in_sandbox(request, f"echo pwned > {home / 'Documents' / 'tax.txt'}")
    assert via_link.returncode != 0
    assert secret.read_text(encoding="utf-8") == "secret"

    # ...and through the canonical path.
    via_real = _run_in_sandbox(request, f"echo pwned > {secret}")
    assert via_real.returncode != 0
    assert secret.read_text(encoding="utf-8") == "secret"


@_NEEDS_SANDBOX
def test_sandbox_denies_writes_to_protected_credential_files(tmp_path):
    """subpath does match a regular file, so credential files are covered."""
    real_tmp = Path(tmp_path).resolve()
    home = real_tmp / "home"
    home.mkdir(exist_ok=True)
    netrc = home / ".netrc"
    netrc.write_text("machine example login me", encoding="utf-8")

    request = _request(tmp_path, home_dir=home)
    result = _run_in_sandbox(request, f"echo pwned > {netrc}")

    assert result.returncode != 0
    assert netrc.read_text(encoding="utf-8") == "machine example login me"
