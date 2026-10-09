"""The shell tool: one command, under an OS-level boundary.

Why this module exists
----------------------
``_shell`` is 271 lines and it is the tool everything else is measured against:
it runs an arbitrary command, so every boundary the project has converges here
-- the command blocklist and allowlist, the permission level, the session's
sandbox mode, the file access policy and write scope, the secret-path list, and
a scratch directory the command can write to that is neither the workspace nor
the output directory.

It was the last cluster in ``BuiltinTools``, a 2932-line class, and the most
entangled one: **eight** registry context keys and nine separate reads of the
registry.  What it does *not* need is the host -- the five callables it used to
reach through ``self`` (the workspace root, the output directory, the resource
home, the confirmation prompt, and the two response builders) plus the three
file-layer operations it borrows are injected, so this class depends on ten
functions rather than on a class with forty tools in it.

Five decisions travelled with the code, and each is one somebody would
otherwise "simplify":

- **The sandbox mode is linked to the permission level.**  A ``none`` sandbox
  is honoured only when the *effective* level is ``full``; otherwise it fails
  safe back to the read-all profile.  Turning the sandbox off must not be
  reachable by a config edit that the permission level never agreed to.
- **The pre/post workspace snapshot is not an authorization mechanism.**  It is
  a file-domain invariant on top of the OS boundary: an ``output_dir`` call
  under danger-full-access must still not leave new files in the workspace, so
  what it created is moved out and reported.
- **A sandbox denial is answered with the narrow knob that would have worked.**
  The reason people disable the sandbox wholesale is almost never that they
  wanted no boundary.
- **The scratch dir is released in a ``finally``.**  Every call gets a fresh
  one and nothing may depend on it outliving the call; without the release it
  leaked once per invocation, unbounded for a long-running gateway.
- **The command blocklist is checked before any subprocess exists**, and a
  command that requires confirmation is re-checked after approval so the rest
  of the call runs the ordinary allowed path.

``_SHELL_CONFIRMATION_GUIDANCE`` is the model-facing paragraph attached to
every confirmation-required error; it moved here because this is its only
reader.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path
from typing import Any, Callable, Optional

from agent.exec import ExecRequest, provider_from
from agent.exec.subprocess import OUTPUT_MAX_BYTES
from agent.pathing import path_contains, resolve_workspace_path
from agent.security.shell import shell_authorization_scope_from_active_context
from agent.security.filesystem_sandbox import (
    SANDBOX_MODE_NONE,
    SandboxUnavailableError,
    ShellSandboxRequest,
    build_sandbox_command,
    effective_sandbox_mode,
    looks_like_sandbox_denial,
    narrow_alternatives_hint,
    new_scratch_dir,
    release_scratch_dir,
)
from agent.tools.files import _normalize_write_scope


# Model-facing guidance attached to every confirmation-required shell error.
_SHELL_CONFIRMATION_GUIDANCE = (
    "Only high-risk commands (disk/system destruction, destructive options, "
    "shell operators, pipe-to-shell patterns) require human approval; "
    "medium-risk commands run without asking. An interactive terminal shows "
    "an approval menu automatically; in non-terminal channels "
    "(Feishu/gateway), show the exact command to the user and ask them to "
    "reply “同意” (or run /confirm <token>). After approval, retry the "
    "byte-identical command within 5 minutes — do not modify it and do not "
    "route around the shell tool."
)




class ShellTools:
    """The shell tool, with the ten callables it needs."""

    def __init__(
        self,
        registry: Any,
        active_workspace_root: Callable[[], Path],
        process_output_dir: Callable[[], Path],
        resource_home: Callable[[], Path],
        path_is_inside_workspace: Callable[[Path], bool],
        workspace_file_snapshot: Callable[[], set],
        move_new_workspace_files_to_output_dir: Callable[..., list],
        try_interactive_confirmation: Callable[..., Any],
        ok: Callable[..., dict[str, Any]],
        error: Callable[..., dict[str, Any]],
    ) -> None:
        # Public on purpose: ``ToolRegistry.call`` reads ``fn.__self__.registry``
        # to install the calling registry's merged context on the registry this
        # tool implementation reads.  A shell that hid it would silently run
        # under the composition's sandbox mode, permission level and write scope
        # -- which is the one place that must never happen by accident.
        self.registry = registry
        self._active_workspace_root = active_workspace_root
        self._process_output_dir = process_output_dir
        self._resource_home = resource_home
        self._path_is_inside_workspace = path_is_inside_workspace
        self._workspace_file_snapshot = workspace_file_snapshot
        self._move_new_workspace_files_to_output_dir = move_new_workspace_files_to_output_dir
        self._try_interactive_confirmation = try_interactive_confirmation
        self._ok = ok
        self._error = error

    def sandbox_dir(self) -> Path:
        """Dedicated scratch directory for shell commands.

        Isolated from both the workspace (repo) and the output directory so
        that downloads, clones, and other generated artifacts never pollute
        the project workspace.
        """
        output_dir = self._process_output_dir()
        sandbox = output_dir / "sandbox"
        sandbox.mkdir(parents=True, exist_ok=True)
        return sandbox

    async def run(
        self,
        command: str,
        intent: str = "",
        timeout: int = 300,
        root: str = "workspace",
        cwd: Optional[str] = None,
    ) -> dict[str, Any]:
        # Security: block dangerous commands before spawning any subprocess.
        extra_blocked: list[str] = (
            self.registry.get_context("shell_blocked_commands") or []
        )
        pre_approved: list[str] = (
            self.registry.get_context("shell_allowed_commands") or []
        )
        permission_level: str = str(
            self.registry.get_context("shell_permission_level") or "ask"
        )
        import agent as agent_module

        from agent.security.shell import (
            shell_effective_permission_level,
            shell_session_sandbox_get,
        )

        authorization_scope = shell_authorization_scope_from_active_context()
        # Sandbox mode is linked to the permission level: an unsandboxed
        # (danger-full-access) run is only honored when the effective level
        # is "full"; otherwise it fails safe back to the read-all profile.
        sandbox_mode = str(
            self.registry.get_context("shell_sandbox_mode") or "read_all"
        )
        session_sandbox = shell_session_sandbox_get(authorization_scope)
        if session_sandbox:
            sandbox_mode = session_sandbox
        effective_level = shell_effective_permission_level(
            authorization_scope, permission_level
        )
        if sandbox_mode == SANDBOX_MODE_NONE and effective_level != "full":
            sandbox_mode = effective_sandbox_mode(sandbox_mode, effective_level)
        unsandboxed = sandbox_mode == SANDBOX_MODE_NONE
        shell_devices = bool(
            self.registry.get_context("shell_devices", True)
        )

        output_dir = self._process_output_dir()
        workspace_root = self._active_workspace_root()
        sandbox_dir = self.sandbox_dir()
        _shell_command_check = agent_module._shell_command_check
        safety = _shell_command_check(
            command,
            extra_blocked,
            allowed_roots=frozenset({workspace_root, output_dir}),
            scope=authorization_scope,
            pre_approved=pre_approved,
            permission_level=permission_level,
        )
        if safety.requires_confirmation and await self._try_interactive_confirmation(
            safety=safety,
            command=command,
            extra_blocked=extra_blocked,
            authorization_scope=authorization_scope,
        ):
            # The human approved at the terminal; the command is now on the
            # session allowlist.  Re-check so the rest of this function runs
            # the normal allowed path.
            safety = _shell_command_check(
                command,
                extra_blocked,
                allowed_roots=frozenset({workspace_root, output_dir}),
                scope=authorization_scope,
                pre_approved=pre_approved,
                permission_level=permission_level,
            )
        if not safety.allowed:
            if safety.requires_confirmation:
                error_message = (
                    f"Shell command requires confirmation: {safety.reason} "
                    f"{_SHELL_CONFIRMATION_GUIDANCE}"
                )
                return self._error(
                    error_message,
                    command=command,
                    risk_level=safety.risk_level,
                    requires_confirmation=True,
                    confirmation_token=safety.confirmation_token,
                )
            return self._error(
                f"Shell command rejected: {safety.reason}",
                command=command,
                risk_level=safety.risk_level,
            )

        # The same immutable file policy governs shell descendants.  The
        # sandbox is an OS-level boundary; the pre/post snapshot moving below
        # is not an authorization mechanism.
        file_policy = self.registry.get_context("file_access_policy")
        workspace_read = file_policy.workspace_read if file_policy is not None else True
        workspace_write = (
            file_policy.workspace_write if file_policy is not None else False
        )
        write_scope = _normalize_write_scope(
            self.registry.get_context("write_scope") or ()
        )
        root = str(root or "workspace").strip().casefold()
        if root not in ("workspace", "output_dir"):
            return self._error(
                "Shell root must be 'workspace' or 'output_dir'",
                command=command,
            )
        if cwd:
            candidate = Path(cwd).expanduser()
            if candidate.is_absolute():
                resolved_cwd, call_root = resolve_workspace_path(
                    candidate,
                    workspace_root=workspace_root,
                    output_dir=output_dir,
                )
            else:
                base = workspace_root if root == "workspace" else output_dir
                resolved_cwd, _ = resolve_workspace_path(
                    base / candidate,
                    workspace_root=workspace_root,
                    output_dir=output_dir,
                )
                if not path_contains(base, resolved_cwd):
                    return self._error(
                        f"Shell cwd '{cwd}' escapes root '{root}'",
                        command=command,
                    )
                call_root = root
        else:
            if root == "workspace":
                resolved_cwd, call_root = workspace_root, "workspace"
            else:
                resolved_cwd, call_root = output_dir, "output_dir"

        sandbox = None
        scratch_dir: Optional[Path] = None
        workspace_before: set[Path] | None = None
        if unsandboxed:
            # Danger-full-access: the OS sandbox is off, but the file-domain
            # invariant still holds at the tool layer — output-domain calls
            # must not leave new files in the workspace.
            if call_root == "output_dir":
                workspace_before = self._workspace_file_snapshot()
        else:
            if not workspace_read and self._path_is_inside_workspace(
                resolved_cwd
            ):
                return self._error(
                    "Workspace cwd is not allowed when workspace reads are disabled",
                    command=command,
                    sandbox_unavailable=True,
                )
            try:
                scratch_dir = new_scratch_dir(output_dir)
                sandbox = build_sandbox_command(
                    ShellSandboxRequest(
                        workspace_root=workspace_root,
                        output_root=output_dir,
                        workspace_read=workspace_read,
                        workspace_write=workspace_write,
                        write_scope=write_scope,
                        scratch_dir=scratch_dir,
                        mode=sandbox_mode,
                        devices=shell_devices,
                        extra_secret_paths=tuple(
                            self.registry.get_context("shell_secret_paths") or ()
                        ),
                        agent_home=self._resource_home(),
                    )
                )
            except SandboxUnavailableError as exc:
                return self._error(
                    str(exc),
                    command=command,
                    sandbox_unavailable=True,
                )

        try:
            env = os.environ.copy()
            env["AGENT_OUTPUT_DIR"] = str(output_dir)
            env["AGENT_WORKSPACE_ROOT"] = str(workspace_root)
            env["AGENT_SANDBOX_DIR"] = str(sandbox_dir)
            # The provider prepends the sandbox argv and merges its env, kills
            # the process group on timeout or `/cancel` (so cancellation does
            # not have to wait for the command, and no child outlives us), and
            # heartbeats so a long quiet command is not mistaken for a stalled
            # one.
            exec_result = await provider_from(self.registry).run(
                ExecRequest(
                    argv=("/bin/sh", "-c", command),
                    cwd=str(resolved_cwd) if resolved_cwd is not None else None,
                    env=env,
                    sandbox=sandbox,
                    timeout=timeout,
                    cancel_label=f"shell:{command[:60]}",
                    heartbeat_message="shell command in progress",
                )
            )
            if exec_result.timed_out:
                return self._error(
                    f"Command timed out after {timeout}s",
                    command=command,
                    timed_out=True,
                )
            out = exec_result.stdout_text()
            err = exec_result.stderr_text()
            result = ""
            if out:
                result += f"STDOUT:\n{out}"
            if err:
                result += f"STDERR:\n{err}"
            result += f"\nExit code: {exec_result.returncode}"
            if exec_result.truncated:
                result += (
                    f"\n[output truncated: exceeded {OUTPUT_MAX_BYTES} "
                    "bytes; redirect to a file or use a more specific command "
                    "to inspect the rest]"
                )
            # When a command fails *because the sandbox refused it*, say which
            # narrow knob matches. The reason a user turns the sandbox off
            # wholesale is almost never that they wanted no boundary — it is
            # that something broke and `none` was the only option visible at
            # that moment. (Measured: GPU/Metal works fine under `read_all`
            # with the default `shell_devices: true`; disabling the sandbox
            # for it buys nothing.)
            if exec_result.returncode != 0 and looks_like_sandbox_denial(err):
                hint = narrow_alternatives_hint(sandbox_mode)
                if hint:
                    result += f"\n\n{hint}"
            moved_artifacts: list[dict[str, str]] = []
            if workspace_before is not None:
                moved_artifacts = self._move_new_workspace_files_to_output_dir(
                    before=workspace_before,
                    cwd=resolved_cwd,
                )
                if moved_artifacts:
                    result += "\nMoved generated workspace artifacts to output_dir:"
                    for item in moved_artifacts[:20]:
                        result += f"\n- {item['from']} -> {item['to']}"
                    if len(moved_artifacts) > 20:
                        result += f"\n- ... {len(moved_artifacts) - 20} more"
            return self._ok(
                command=command,
                root=call_root,
                cwd=str(resolved_cwd),
                output=result or "(no output)",
                exit_code=exec_result.returncode,
                moved_artifacts=moved_artifacts,
            )
        except ValueError as e:
            return self._error(f"Invalid shell input: {e}", command=command)
        except Exception as e:
            return self._error(f"Shell command failed: {e}", command=command)
        finally:
            # The scratch dir is this command's TMPDIR.  Every call gets a
            # fresh one, so nothing can depend on it outliving the call —
            # and without this the directory (plus whatever the command
            # wrote into it) leaked once per shell invocation, unbounded for
            # a long-running gateway.
            release_scratch_dir(scratch_dir)

    async def terminate_process(self, proc: Any) -> None:
        if proc is None:
            return
        if getattr(proc, "returncode", None) is None:
            try:
                if hasattr(os, "killpg") and getattr(proc, "pid", None):
                    os.killpg(proc.pid, signal.SIGTERM)
                elif hasattr(proc, "terminate"):
                    proc.terminate()
            except ProcessLookupError:
                pass
            except Exception:
                if hasattr(proc, "terminate"):
                    with contextlib.suppress(Exception):
                        proc.terminate()
        try:
            await asyncio.wait_for(proc.communicate(), timeout=1)
            return
        except asyncio.TimeoutError:
            pass
        except Exception:
            return
        try:
            if hasattr(os, "killpg") and getattr(proc, "pid", None):
                os.killpg(proc.pid, signal.SIGKILL)
            elif hasattr(proc, "kill"):
                proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            if hasattr(proc, "kill"):
                with contextlib.suppress(Exception):
                    proc.kill()
        with contextlib.suppress(Exception):
            await proc.communicate()
