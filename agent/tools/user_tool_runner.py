"""Execute user-authored tools out of process.

Why this is not an import
-------------------------
A user tool is Python the *model wrote*.  Loading it with ``exec_module`` put
it inside the agent process, where it shared everything the agent has: the
provider API keys held in memory and in ``config.json``, the memory database,
the tool registry it could rewrite, and the event loop it could block or
crash.  That made the least-trusted code in the system the least contained —
the shell tool, which merely runs commands, was the only path with an OS
boundary around it.

The tool contract was already a serializable RPC boundary and nobody had
noticed: ``register`` hands back ``fn``, and ``fn(**kwargs)`` returns
JSON-able data.  Nothing in it needs shared memory.  So the tool runs in a
child process and the parent keeps a proxy.

What this buys at every sandbox setting
---------------------------------------
Process isolation is not the same thing as a sandbox, and it is worth
something even when ``shell_sandbox`` is ``none``:

- the child cannot read the parent's in-memory secrets
- it cannot mutate the registry or monkeypatch the running agent
- a crash, a hang, or ``sys.exit`` kills the child, not the session
- a runaway tool can be killed on a timeout

When a sandbox mode *is* configured, the same profile the shell tool uses is
applied on top, so tools inherit the credential-read and autostart-write
denials rather than needing a policy of their own.

What it costs
-------------
Module-level state no longer persists between calls: each invocation is a
fresh interpreter.  For model-authored code that is closer to a fix than a
regression — cross-call globals were never a documented guarantee — but a
tool that memoized in a module dict will now recompute.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Optional

from agent import shared
from agent.exec import ExecRequest, provider_from
from agent.security.filesystem_sandbox import (
    SANDBOX_MODE_NONE,
    SandboxUnavailableError,
    ShellSandboxRequest,
    build_sandbox_command,
    new_scratch_dir,
    release_scratch_dir,
)
from agent.tools import user_tools

DEFAULT_TOOL_TIMEOUT_SECONDS = 120.0

#: Runs one tool call and prints one JSON line.  Kept as a string rather than
#: a file so there is no importable module a tool could shadow on sys.path.
_RUNNER_DRIVER = r"""
import importlib.machinery, importlib.util, inspect, json, sys

deps, module_path, tool_name = sys.argv[1], sys.argv[2], sys.argv[3]
if deps not in sys.path:
    sys.path.insert(0, deps)

try:
    payload = json.loads(sys.stdin.read() or "{}")
except Exception as exc:
    print(json.dumps({"ok": False, "error": "bad input payload: %s" % exc}))
    raise SystemExit(0)

collected = {}


class _Collector:
    def register(self, name, description, parameters, fn, **kwargs):
        collected[str(name)] = fn


def _fail(message):
    print(json.dumps({"ok": False, "error": str(message)[:2000]}))
    raise SystemExit(0)


try:
    spec = importlib.util.spec_from_file_location(
        "_agent_tool_child",
        module_path,
        loader=importlib.machinery.SourceFileLoader(
            "_agent_tool_child", module_path
        ),
    )
    if spec is None or spec.loader is None:
        _fail("unable to create an import spec for the module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
except BaseException as exc:
    _fail("import failed: %s: %s" % (type(exc).__name__, exc))

register = getattr(module, "register", None)
if not callable(register):
    _fail("module does not define a callable register(registry)")

try:
    register(_Collector())
except BaseException as exc:
    _fail("register(registry) raised %s: %s" % (type(exc).__name__, exc))

fn = collected.get(tool_name)
if fn is None:
    _fail("module no longer registers a tool named %r" % tool_name)

try:
    result = fn(**payload)
    if inspect.iscoroutine(result):
        import asyncio as _asyncio

        result = _asyncio.new_event_loop().run_until_complete(result)
except BaseException as exc:
    _fail("%s: %s" % (type(exc).__name__, exc))

if result is None:
    encoded = ""
elif isinstance(result, (dict, list)):
    try:
        encoded = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        _fail("tool returned a value that is not JSON-serializable: %s" % exc)
else:
    encoded = str(result)

print(json.dumps({"ok": True, "result": encoded}))
"""


def _sandbox_for_tools(
    registry: Any,
    scratch_dir: Path,
    output_dir: Path,
    workspace_root: Path,
    tools_root: Path,
):
    """Build the sandbox wrapper, honouring the session's configured mode.

    Deliberately the *same* knobs as the shell tool.  A separate policy for
    tools would be a second thing to keep correct, and the asymmetry it
    creates is what this module exists to remove.
    """
    mode = str(registry.get_context("shell_sandbox_mode") or "read_all")
    if mode == SANDBOX_MODE_NONE:
        return None
    file_policy = registry.get_context("file_access_policy")
    request = ShellSandboxRequest(
        workspace_root=workspace_root,
        output_root=output_dir,
        workspace_read=(
            file_policy.workspace_read if file_policy is not None else True
        ),
        workspace_write=(
            file_policy.workspace_write if file_policy is not None else False
        ),
        write_scope=tuple(registry.get_context("write_scope") or ()),
        scratch_dir=scratch_dir,
        mode=mode,
        devices=bool(registry.get_context("shell_devices", True)),
        extra_secret_paths=tuple(
            registry.get_context("shell_secret_paths") or ()
        ),
        # Reads the child cannot start without.  ``agent_home`` is denied
        # wholesale (config.json holds API keys, and the memory database is
        # the agent's integrity), but the tools directory lives inside it and
        # is precisely what this child is here to execute.
        extra_read_paths=(
            *_interpreter_read_roots(),
            str(tools_root),
            str(user_tools.deps_dir(tools_root)),
        ),
        agent_home=shared.AGENT_HOME,
    )
    return build_sandbox_command(request)


def _interpreter_read_roots() -> tuple[str, ...]:
    """Roots the child needs merely to boot Python.

    A venv is two things: its own prefix (``pyvenv.cfg``, ``site-packages``)
    and the base installation it points at.  Both must be readable or
    ``init_import_site`` fails.
    """
    roots = {sys.prefix, sys.base_prefix, str(Path(sys.executable).resolve().parent)}
    return tuple(sorted(root for root in roots if root))


async def run_user_tool(
    module_path: Path,
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    registry: Any,
    root: Optional[Path] = None,
    timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
) -> Any:
    """Invoke *tool_name* from *module_path* in a child process.

    Returns whatever the tool returned (already JSON-decoded when it was a
    dict/list), or an ``{"ok": False, "error": ...}`` payload — the same shape
    the registry produces for any other failing tool, so callers need no
    special case.
    """
    dependencies = user_tools.deps_dir(root)
    dependencies.mkdir(parents=True, exist_ok=True)

    output_dir = Path(
        registry.get_context("output_dir") or shared.DEFAULT_OUTPUT_DIR
    )
    workspace_root = Path(
        registry.get_context("workspace_root") or Path.cwd()
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    scratch_dir: Path | None = None
    try:
        scratch_dir = new_scratch_dir(output_dir)
        tools_root = Path(root) if root is not None else shared.TOOLS_DIR
        try:
            sandbox = _sandbox_for_tools(
                registry, scratch_dir, output_dir, workspace_root, tools_root
            )
        except SandboxUnavailableError as exc:
            return {
                "ok": False,
                "error": f"user tool '{tool_name}' cannot run: {exc}",
            }

        result = await provider_from(registry).run(
            ExecRequest(
                argv=(
                    sys.executable,
                    "-E",
                    "-c",
                    _RUNNER_DRIVER,
                    str(dependencies),
                    str(module_path),
                    str(tool_name),
                ),
                # Arguments go over stdin, not argv: argv is visible to every
                # other process on the machine via `ps`, and tool inputs
                # routinely carry content the user would not publish.
                stdin=json.dumps(tool_input, ensure_ascii=False).encode("utf-8"),
                # cwd is the per-call scratch dir, never the workspace (a tool
                # must not default to reading or writing the project) and never
                # agent_home (which the sandbox denies, so Python could not
                # even resolve its own sys.path from there).
                cwd=str(scratch_dir),
                sandbox=sandbox,
                timeout=timeout,
                # Registering with the cancel token is what the inline spawn
                # this replaced was missing: a runaway user tool used to
                # survive `/cancel` until its own timeout expired.
                cancel_label=f"user-tool:{tool_name}",
                heartbeat_message=f"user tool '{tool_name}' in progress",
            )
        )
        if result.timed_out:
            return {
                "ok": False,
                "error": (
                    f"user tool '{tool_name}' exceeded {timeout:g}s and was "
                    "terminated"
                ),
            }

        return _decode_result(tool_name, result.stdout, result.stderr, result.returncode)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"user tool '{tool_name}' failed to start: {exc}",
        }
    finally:
        release_scratch_dir(scratch_dir)


def _decode_result(
    tool_name: str,
    stdout: bytes | None,
    stderr: bytes | None,
    returncode: int | None,
) -> Any:
    text = (stdout or b"").decode("utf-8", "replace").strip()
    errors = (stderr or b"").decode("utf-8", "replace").strip()
    last_line = text.splitlines()[-1] if text else ""
    try:
        payload = json.loads(last_line)
    except (json.JSONDecodeError, ValueError):
        detail = errors or text or f"exited with code {returncode}"
        return {
            "ok": False,
            "error": f"user tool '{tool_name}' produced no result: {detail[:2000]}",
        }
    if not payload.get("ok"):
        return {
            "ok": False,
            "error": str(payload.get("error", "user tool failed")),
        }
    encoded = payload.get("result", "")
    if not encoded:
        return ""
    try:
        return json.loads(encoded)
    except (json.JSONDecodeError, ValueError):
        # The tool returned a plain string; hand it back unchanged.
        return encoded


__all__ = ["DEFAULT_TOOL_TIMEOUT_SECONDS", "run_user_tool"]
