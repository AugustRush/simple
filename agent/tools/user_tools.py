"""Authoring support for user Python tools under ``~/.agent/tools``.

A user tool is a Python module exposing ``register(registry)``.  The agent
imports it in-process, so three invariants keep that from being either
dangerous or destructive to the machine the agent runs on:

1. **Dependencies are isolated.**  Third-party packages a tool needs install
   into ``~/.agent/tools/_deps`` and are put on ``sys.path`` at load time.
   They never reach the ambient interpreter, and — the failure that motivated
   this module — never reach the project directory the agent happens to be
   launched from.
2. **Import is probed out-of-process first.**  A module that raises on import,
   spins, or calls ``sys.exit`` would otherwise take the live session with it.
   The probe runs it in a subprocess and reports what it registered.
3. **In-process loading is authorized.**  Either ``user_tools.enabled=true``
   trusts the whole directory, or an individual file is approved against a
   hash of its contents.  Editing an approved file revokes its approval,
   so approval always refers to code a human actually saw.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from agent import shared

DEPS_DIRNAME = "_deps"
APPROVALS_FILENAME = ".approved.json"

#: Filenames the catalog must never treat as a tool module.
RESERVED_PREFIXES = ("_", ".")

_TOOL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")

# PEP 508-ish: name, optional extras, optional single version specifier.  Kept
# deliberately narrow — this string is passed to pip, and anything with
# whitespace, shell metacharacters, URLs, or path traversal is rejected rather
# than escaped.
_REQUIREMENT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(\[[A-Za-z0-9,._-]+\])?"
    r"((==|>=|<=|~=|!=|<|>)[A-Za-z0-9._*+!-]+)?$"
)

PROBE_TIMEOUT_SECONDS = 30
INSTALL_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    tools: tuple[dict, ...] = ()
    error: str = ""


# ── Paths ────────────────────────────────────────────────────────────────────


def deps_dir(root: Optional[Path] = None) -> Path:
    return (root or shared.TOOLS_DIR) / DEPS_DIRNAME


def approvals_file(root: Optional[Path] = None) -> Path:
    return (root or shared.TOOLS_DIR) / APPROVALS_FILENAME


def tool_path(tool_id: str, root: Optional[Path] = None) -> Path:
    return (root or shared.TOOLS_DIR) / f"{tool_id}.py"


def ensure_deps_on_path(root: Optional[Path] = None) -> Path:
    """Put the isolated dependency directory at the front of ``sys.path``."""
    target = deps_dir(root)
    target.mkdir(parents=True, exist_ok=True)
    entry = str(target)
    if entry in sys.path:
        sys.path.remove(entry)
    sys.path.insert(0, entry)
    return target


def is_tool_module(path: Path, root: Path) -> bool:
    """True when *path* is a loadable tool module rather than support state."""
    if path.suffix != ".py":
        return False
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return not any(
        part.startswith(RESERVED_PREFIXES) for part in relative.parts
    )


# ── Identifier + source validation ───────────────────────────────────────────


def normalize_tool_id(raw: str) -> str:
    """Coerce free text into a safe module stem (empty when unusable)."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")[:64]
    if slug and slug[0].isdigit():
        slug = f"t_{slug}"[:64]
    return slug


def validate_tool_id(tool_id: str) -> Optional[str]:
    if not tool_id:
        return "Tool id must not be empty"
    if not _TOOL_ID_RE.match(tool_id):
        return (
            "Tool id must be lowercase alphanumerics and underscores, "
            "start with a letter or digit, and be at most 64 characters"
        )
    if tool_id.startswith(RESERVED_PREFIXES):
        return "Tool id must not start with '_' or '.'"
    return None


def validate_source(code: str) -> Optional[str]:
    """Return an error message when *code* cannot work as a tool module.

    Structural only — it proves the module *could* register a tool, not that
    the tool is correct.  Behavioural verification is the probe's job.
    """
    if not str(code or "").strip():
        return "Tool source is empty"
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        line = exc.lineno if exc.lineno is not None else "?"
        return f"syntax error at line {line}: {exc.msg}"

    register: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "register"
        ):
            register = node
    if register is None:
        return (
            "module must define a top-level `register(registry)` function "
            "that calls registry.register(...) for each tool it provides"
        )
    if isinstance(register, ast.AsyncFunctionDef):
        return "`register` must be a regular function, not async"
    positional = len(register.args.posonlyargs) + len(register.args.args)
    if positional < 1 and register.args.vararg is None:
        return "`register` must accept the registry as its first argument"

    for node in ast.walk(register):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "register"
            and isinstance(node.ctx, ast.Load)
        ):
            return None
    return (
        "`register` never calls registry.register(...); the module would "
        "load but provide no tools"
    )


# ── Out-of-process import probe ──────────────────────────────────────────────

_PROBE_DRIVER = r"""
import importlib.machinery, importlib.util, json, sys

deps, module_path = sys.argv[1], sys.argv[2]
if deps not in sys.path:
    sys.path.insert(0, deps)

collected = []


class _Collector:
    def register(self, name, description, parameters, fn, **kwargs):
        collected.append(
            {
                "name": str(name),
                "description": str(description)[:400],
                "parameters": parameters if isinstance(parameters, dict) else {},
                "is_async": bool(
                    getattr(fn, "__code__", None)
                    and fn.__code__.co_flags & 0x80
                ),
            }
        )


def _fail(message):
    print(json.dumps({"ok": False, "error": message[:2000]}))
    raise SystemExit(0)


try:
    spec = importlib.util.spec_from_file_location(
        "_agent_tool_probe",
        module_path,
        loader=importlib.machinery.SourceFileLoader(
            "_agent_tool_probe", module_path
        ),
    )
    if spec is None or spec.loader is None:
        _fail("unable to create an import spec for the module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
except SystemExit:
    raise
except BaseException as exc:
    _fail("import failed: %s: %s" % (type(exc).__name__, exc))

register = getattr(module, "register", None)
if not callable(register):
    _fail("module does not define a callable register(registry)")

try:
    register(_Collector())
except SystemExit:
    raise
except BaseException as exc:
    _fail("register(registry) raised %s: %s" % (type(exc).__name__, exc))

if not collected:
    _fail("register(registry) completed without registering any tool")

print(json.dumps({"ok": True, "tools": collected}))
"""


def _probe_argv(path: Path, root: Optional[Path]) -> tuple[list[str], Path]:
    """Argv for the import probe, plus the cwd it must run in."""
    dependencies = deps_dir(root)
    dependencies.mkdir(parents=True, exist_ok=True)
    return (
        [
            sys.executable,
            # -E only: the probe must see the same importable set the live
            # session would, so a tool relying on an already-installed package
            # is not rejected for a difference the real load would not have.
            "-E",
            "-c",
            _PROBE_DRIVER,
            str(dependencies),
            str(path),
        ],
        # Never the workspace: a probe must not read or write the project.
        shared.AGENT_HOME,
    )


def _parse_probe_output(
    stdout: bytes | None, stderr: bytes | None, returncode: int | None
) -> ProbeResult:
    text = (stdout or b"").decode("utf-8", "replace").strip()
    errors = (stderr or b"").decode("utf-8", "replace").strip()
    last_line = text.splitlines()[-1] if text else ""
    try:
        payload = json.loads(last_line)
    except (json.JSONDecodeError, ValueError):
        detail = errors or text or f"probe exited with code {returncode}"
        return ProbeResult(ok=False, error=f"probe failed: {detail[:2000]}")
    if not payload.get("ok"):
        return ProbeResult(ok=False, error=str(payload.get("error", "probe failed")))
    return ProbeResult(
        ok=True,
        tools=tuple(
            item for item in payload.get("tools", []) if isinstance(item, dict)
        ),
    )


def probe_module_sync(
    path: Path,
    *,
    root: Optional[Path] = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Blocking counterpart of :func:`probe_module`.

    Loading tools happens on startup and after create/remove, which are
    synchronous paths.  Sharing the driver and the output parsing with the
    async probe keeps one definition of what "probing a module" means — two
    copies would drift on the next change to the driver contract.
    """
    import subprocess

    argv, cwd = _probe_argv(path, root)
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(
            ok=False,
            error=(
                f"import did not finish within {timeout:g}s; module-level code "
                "must not block (move slow work inside the tool function)"
            ),
        )
    except Exception as exc:
        return ProbeResult(ok=False, error=f"unable to start probe: {exc}")
    return _parse_probe_output(
        completed.stdout, completed.stderr, completed.returncode
    )


async def probe_module(
    path: Path,
    *,
    root: Optional[Path] = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Import *path* in a subprocess and report the tools it registers.

    The live session is never the thing that executes untrusted module-level
    code first, so an import that raises, hangs, or exits cannot kill it.
    """
    dependencies = deps_dir(root)
    dependencies.mkdir(parents=True, exist_ok=True)
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            # -E only: the probe must see the same importable set the live
            # session would, so a tool relying on an already-installed package
            # is not rejected for a difference the real load would not have.
            "-E",
            "-c",
            _PROBE_DRIVER,
            str(dependencies),
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Never the workspace: a probe must not read or write the project.
            cwd=str(shared.AGENT_HOME),
        )
    except Exception as exc:
        return ProbeResult(ok=False, error=f"unable to start probe: {exc}")

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return ProbeResult(
            ok=False,
            error=(
                f"import did not finish within {timeout:g}s; module-level code "
                "must not block (move slow work inside the tool function)"
            ),
        )

    text = (stdout or b"").decode("utf-8", "replace").strip()
    errors = (stderr or b"").decode("utf-8", "replace").strip()
    last_line = text.splitlines()[-1] if text else ""
    try:
        payload = json.loads(last_line)
    except (json.JSONDecodeError, ValueError):
        detail = errors or text or f"probe exited with code {process.returncode}"
        return ProbeResult(ok=False, error=f"probe failed: {detail[:2000]}")

    if not payload.get("ok"):
        return ProbeResult(ok=False, error=str(payload.get("error", "probe failed")))
    tools = tuple(
        item for item in payload.get("tools", []) if isinstance(item, dict)
    )
    return ProbeResult(ok=True, tools=tools)


# ── Approval ledger ──────────────────────────────────────────────────────────


def source_digest(code: str) -> str:
    return hashlib.sha256(str(code or "").encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _read_approvals(root: Optional[Path] = None) -> dict[str, str]:
    path = approvals_file(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _write_approvals(entries: dict[str, str], root: Optional[Path] = None) -> None:
    path = approvals_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    shared._atomic_write_text(
        path, json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True)
    )


def record_approval(tool_id: str, digest: str, root: Optional[Path] = None) -> None:
    """Approve one tool file by content, so later edits need re-approval."""
    entries = _read_approvals(root)
    entries[tool_id] = digest
    _write_approvals(entries, root)


def revoke_approval(tool_id: str, root: Optional[Path] = None) -> None:
    entries = _read_approvals(root)
    if entries.pop(tool_id, None) is not None:
        _write_approvals(entries, root)


def is_approved(tool_id: str, digest: str, root: Optional[Path] = None) -> bool:
    if not digest:
        return False
    return _read_approvals(root).get(tool_id) == digest


def approved_ids(root: Optional[Path] = None) -> set[str]:
    return set(_read_approvals(root))


# ── Dependency installation ──────────────────────────────────────────────────


def validate_requirement(requirement: str) -> Optional[str]:
    spec = str(requirement or "").strip()
    if not spec:
        return "Package name must not be empty"
    if len(spec) > 128:
        return "Package specifier is too long"
    if not _REQUIREMENT_RE.match(spec):
        return (
            "Package specifier must be a plain name with an optional extras "
            "group and a single version constraint, e.g. 'markdown', "
            "'httpx[http2]', or 'markdown>=3.5'. URLs, local paths, and "
            "shell metacharacters are not accepted."
        )
    return None


def _install_commands(spec: str, target: Path) -> list[list[str]]:
    """Installer invocations to try, in order of preference.

    A ``uv``-created virtualenv commonly has no ``pip`` at all, so the agent
    cannot assume ``python -m pip`` exists.  Both forms below write only into
    *target*.
    """
    commands: list[list[str]] = []
    if importlib.util.find_spec("pip") is not None:
        commands.append(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-input",
                "--disable-pip-version-check",
                "--upgrade",
                "--target",
                str(target),
                spec,
            ]
        )
    uv_binary = shutil.which("uv")
    if uv_binary:
        commands.append(
            [
                uv_binary,
                "pip",
                "install",
                "--python",
                sys.executable,
                "--target",
                str(target),
                spec,
            ]
        )
    return commands


async def install_dependency(
    requirement: str,
    *,
    root: Optional[Path] = None,
    timeout: float = INSTALL_TIMEOUT_SECONDS,
) -> dict:
    """Install *requirement* into the isolated dependency directory.

    ``--target`` is what makes this safe to run from anywhere: the installer
    writes only into ``~/.agent/tools/_deps``, never into the active
    interpreter's site-packages and never into the project the agent was
    launched from.
    """
    error = validate_requirement(requirement)
    if error:
        return {"ok": False, "error": error}
    spec = requirement.strip()
    target = deps_dir(root)
    target.mkdir(parents=True, exist_ok=True)

    commands = _install_commands(spec, target)
    if not commands:
        return {
            "ok": False,
            "error": (
                "No installer is available: this interpreter has no 'pip' "
                "module and 'uv' is not on PATH."
            ),
            "package": spec,
        }

    failure: dict = {"ok": False, "error": "installation failed", "package": spec}
    for argv in commands:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Run from the agent home so a stray pyproject.toml in the
                # workspace can never influence resolution.
                cwd=str(shared.AGENT_HOME),
            )
        except Exception as exc:
            failure = {
                "ok": False,
                "error": f"unable to start {argv[0]}: {exc}",
                "package": spec,
            }
            continue

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return {
                "ok": False,
                "error": f"install timed out after {timeout:g}s",
                "package": spec,
            }

        out = (stdout or b"").decode("utf-8", "replace")
        err = (stderr or b"").decode("utf-8", "replace")
        if process.returncode != 0:
            failure = {
                "ok": False,
                "error": f"install failed (exit {process.returncode})",
                "package": spec,
                "stderr": err[-2000:] or out[-2000:],
            }
            continue

        ensure_deps_on_path(root)
        return {
            "ok": True,
            "package": spec,
            "installed_to": str(target),
            "stdout": (out or err)[-2000:],
            "summary_text": (
                f"Installed '{spec}' into the isolated tool dependency directory "
                f"{target}. The project workspace and the ambient Python "
                "environment were not modified."
            ),
        }
    return failure


def installed_dependencies(root: Optional[Path] = None) -> list[str]:
    """Top-level distribution names present in the isolated deps directory."""
    target = deps_dir(root)
    if not target.is_dir():
        return []
    names: set[str] = set()
    for entry in target.iterdir():
        if entry.name.endswith(".dist-info"):
            names.add(entry.name.rsplit("-", 2)[0])
    return sorted(names)


# ── Authoring pipeline ───────────────────────────────────────────────────────


def resolve_catalog(registry) -> tuple[Any, bool]:
    """Return ``(catalog, user_tools_enabled)`` for the running session."""
    from agent.tools.runtime import UserToolCatalog

    components = registry.get_context("components") or {}
    catalog = components.get("user_tool_catalog") if components else None
    enabled = bool(components.get("user_tools_enabled", False)) if components else False
    if catalog is None:
        catalog = UserToolCatalog()
    return catalog, enabled


async def author_tool(
    tool_id: str,
    code: str,
    *,
    registry,
    replace: bool = False,
    root: Optional[Path] = None,
) -> dict:
    """Validate, probe, confirm, install, and activate one user tool.

    Every step before activation is reversible and side-effect free from the
    session's point of view: the candidate is written to a staging path that
    the catalog does not discover, so a module that fails validation, fails to
    import, or is declined by the user leaves nothing callable behind.
    """
    catalog, enabled = resolve_catalog(registry)
    target_root = Path(root) if root is not None else Path(getattr(catalog, "root", shared.TOOLS_DIR))
    target_root.mkdir(parents=True, exist_ok=True)

    id_error = validate_tool_id(tool_id)
    if id_error:
        return {"ok": False, "error": id_error}

    destination = tool_path(tool_id, target_root)
    if destination.exists() and not replace:
        return {
            "ok": False,
            "error": (
                f"Tool '{tool_id}' already exists at {destination}. "
                "Use update_tool to change it."
            ),
        }
    if not destination.exists() and replace:
        return {"ok": False, "error": f"Tool '{tool_id}' does not exist yet"}

    source_error = validate_source(code)
    if source_error:
        return {
            "ok": False,
            "error": f"Tool source rejected: {source_error}",
            "stage": "validate",
        }

    # Dot-prefixed so the catalog never discovers it, but still `.py` so the
    # probe's import machinery recognises it as a source file.
    staging = target_root / f".candidate_{tool_id}.py"
    shared._atomic_write_text(staging, code)
    try:
        probe = await probe_module(staging, root=target_root)
        if not probe.ok:
            return {
                "ok": False,
                "error": f"Tool failed its import check: {probe.error}",
                "stage": "probe",
                "recovery_hint": (
                    "Fix the module and retry. If it needs a third-party "
                    "package, call install_tool_dependency first — the package "
                    "installs into the agent's isolated tool dependency "
                    "directory, never into the current project."
                ),
            }

        registered = [str(item.get("name", "")) for item in probe.tools]
        digest = source_digest(code)
        if not is_approved(tool_id, digest, target_root):
            from agent.security.tool_approval import (
                authorization_scope,
                confirm_tool_activation,
            )
            from agent.security.plugin_approval import (
                plugin_install_record_pending,
            )
            from agent.security.tool_approval import approval_source

            scope = authorization_scope()
            approved, needs_pending = await confirm_tool_activation(
                tool_id=tool_id,
                digest=digest,
                reason=(
                    f"activating this tool runs its Python in the agent process; "
                    f"it registers: {', '.join(registered) or 'nothing'}"
                ),
                scope=scope,
            )
            if not approved:
                payload: dict = {
                    "ok": False,
                    "error": (
                        f"Tool '{tool_id}' was not activated: running generated "
                        "Python in-process requires human confirmation"
                    ),
                    "cancelled": True,
                    "stage": "approval",
                    "would_register": registered,
                }
                if needs_pending:
                    plugin_install_record_pending(
                        scope, approval_source(tool_id, digest)
                    )
                    payload.update(
                        {
                            "requires_confirmation": True,
                            "confirmation_guidance": (
                                f"工具 `{tool_id}` 已通过语法与导入检查，将注册："
                                f"{', '.join(registered) or '（无）'}。"
                                "激活它等于在 agent 进程内执行这段 Python。"
                                "请把代码展示给用户并请其回复『同意』；"
                                "批准后用完全相同的参数重试。"
                            ),
                        }
                    )
                return payload
            record_approval(tool_id, digest, target_root)

        shared._atomic_write_text(destination, code)
    finally:
        staging.unlink(missing_ok=True)

    loaded = catalog.load_into_registry(registry, require_approval=not enabled)
    activated = tool_id in loaded
    return {
        "ok": True,
        "tool_id": tool_id,
        "path": str(destination),
        "registered_tools": registered,
        "activated": activated,
        "loaded_user_tools": loaded,
        "dependencies_dir": str(deps_dir(target_root)),
        "summary_text": (
            f"{'Updated' if replace else 'Created'} user tool '{tool_id}' at "
            f"{destination} and {'activated' if activated else 'staged'} it. "
            f"Callable now: {', '.join(registered) or 'none'}."
        ),
    }


def remove_tool(tool_id: str, *, registry, root: Optional[Path] = None) -> dict:
    """Delete a user tool file, revoke its approval, and unload it."""
    catalog, enabled = resolve_catalog(registry)
    target_root = Path(root) if root is not None else Path(getattr(catalog, "root", shared.TOOLS_DIR))

    id_error = validate_tool_id(tool_id)
    if id_error:
        return {"ok": False, "error": id_error}
    destination = tool_path(tool_id, target_root)
    if not destination.exists():
        return {"ok": False, "error": f"Tool '{tool_id}' not found at {destination}"}
    try:
        destination.unlink()
    except OSError as exc:
        return {"ok": False, "error": f"Failed to delete tool: {exc}"}
    revoke_approval(tool_id, target_root)
    loaded = catalog.load_into_registry(registry, require_approval=not enabled)
    return {
        "ok": True,
        "tool_id": tool_id,
        "removed_from": str(destination),
        "loaded_user_tools": loaded,
        "summary_text": f"Deleted user tool '{tool_id}' and unloaded it.",
    }


def describe_tools(registry, *, root: Optional[Path] = None) -> dict:
    """Inventory of user tools on disk with load and approval state."""
    catalog, enabled = resolve_catalog(registry)
    target_root = Path(root) if root is not None else Path(getattr(catalog, "root", shared.TOOLS_DIR))
    target_root.mkdir(parents=True, exist_ok=True)

    live = (
        {
            name
            for name in registry.list_tools()
            if str(registry.tool_source(name)).startswith("user_tool:")
        }
        if hasattr(registry, "tool_source")
        else set()
    )

    entries: list[dict] = []
    for path in catalog.discover() if hasattr(catalog, "discover") else []:
        tool_id = path.relative_to(target_root).with_suffix("").as_posix()
        digest = file_digest(path)
        entries.append(
            {
                "tool_id": tool_id,
                "path": str(path),
                "approved": is_approved(tool_id, digest, target_root),
                "loaded": enabled or is_approved(tool_id, digest, target_root),
            }
        )
    return {
        "ok": True,
        "tools_dir": str(target_root),
        "dependencies_dir": str(deps_dir(target_root)),
        "installed_dependencies": installed_dependencies(target_root),
        "user_tools_enabled": enabled,
        "tools": entries,
        "registered_tool_names": sorted(live),
    }


__all__ = [
    "APPROVALS_FILENAME",
    "DEPS_DIRNAME",
    "ProbeResult",
    "approvals_file",
    "approved_ids",
    "author_tool",
    "deps_dir",
    "describe_tools",
    "ensure_deps_on_path",
    "file_digest",
    "install_dependency",
    "installed_dependencies",
    "is_approved",
    "is_tool_module",
    "normalize_tool_id",
    "probe_module",
    "record_approval",
    "remove_tool",
    "resolve_catalog",
    "revoke_approval",
    "source_digest",
    "tool_path",
    "validate_requirement",
    "validate_source",
    "validate_tool_id",
]
