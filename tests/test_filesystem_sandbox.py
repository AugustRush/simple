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
    )


# ── Profile generation ──────────────────────────────────────────────────────


def test_profile_hides_workspace_when_read_disabled(tmp_path):
    request = _request(tmp_path, workspace_read=False)
    profile = _macos_seatbelt_profile(request)

    assert f'file-read* (subpath "{request.workspace_root}")' not in profile
    assert f'file-read* (subpath "{request.output_root}")' in profile
    assert f'file-write* (subpath "{request.output_root}")' in profile


def test_profile_makes_workspace_read_only_by_default(tmp_path):
    request = _request(tmp_path)
    profile = _macos_seatbelt_profile(request)

    assert f'file-read* (subpath "{request.workspace_root}")' in profile
    assert f'file-write* (subpath "{request.workspace_root}")' not in profile


def test_profile_allows_only_scoped_workspace_writes(tmp_path):
    request = _request(
        tmp_path, workspace_write=True, write_scope=["src/app.py"]
    )
    profile = _macos_seatbelt_profile(request)

    assert (
        f'file-write* (subpath "{request.workspace_root}/src/app.py")'
        in profile
    )
    assert f'file-write* (subpath "{request.workspace_root}")' not in profile


def test_profile_denies_internal_bookkeeping(tmp_path):
    request = _request(tmp_path)
    profile = _macos_seatbelt_profile(request)
    internal = request.output_root / ".simple-internal"

    assert f'(deny file-write* (subpath "{internal}"))' in profile
    assert f'(deny file-read* (subpath "{internal}"))' in profile


def test_profile_keeps_scratch_writable(tmp_path):
    request = _request(tmp_path)
    profile = _macos_seatbelt_profile(request)

    assert f'file-write* (subpath "{request.scratch_dir}")' in profile
    assert f'file-read* (subpath "{request.scratch_dir}")' in profile


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
def test_sandbox_allows_workspace_reads(tmp_path):
    request = _request(tmp_path)
    request.workspace_root.mkdir()
    source = request.workspace_root / "a.txt"
    source.write_text("secret", encoding="utf-8")

    result = _run_in_sandbox(request, f"cat {source}")

    assert result.returncode == 0, result.stderr
    assert "secret" in result.stdout


@_NEEDS_SANDBOX
def test_sandbox_denies_workspace_reads_when_read_disabled(tmp_path):
    request = _request(tmp_path, workspace_read=False)
    request.workspace_root.mkdir()
    source = request.workspace_root / "a.txt"
    source.write_text("secret", encoding="utf-8")

    result = _run_in_sandbox(request, f"cat {source}")

    assert result.returncode != 0


@_NEEDS_SANDBOX
def test_sandbox_allows_only_scoped_workspace_writes(tmp_path):
    request = _request(
        tmp_path, workspace_write=True, write_scope=["src/app.py"]
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
def test_sandbox_denies_host_temp_and_internal_output(tmp_path):
    request = _request(tmp_path)
    request.output_root.mkdir()
    internal = request.output_root / ".simple-internal" / "locks"
    internal.mkdir(parents=True)

    host_tmp = _run_in_sandbox(request, "touch /tmp/simple-sandbox-test")
    assert host_tmp.returncode != 0
    assert not Path("/tmp/simple-sandbox-test").exists()

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
