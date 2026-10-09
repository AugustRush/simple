"""The file tools: reading, writing and resolving paths under a policy.

Why this module exists
----------------------
Thirteen members of ``BuiltinTools`` -- a 3441-line class -- are the file
layer: the four CRUD tools (``read_file`` / ``write_file`` / ``edit_file`` /
``list_files``), ``send_file``, the path resolvers they share, the write
authorizer, and the one **property** that decides which file service is in use.

That property is why this was the last plain-looking cluster and not a plain
move.  ``file_service`` resolves the provider through four fallbacks, in this
order: a forked session's ``ContextVar`` override, the constructor's injected
provider, the registry's ``file_service`` context, and finally a default
``FileService`` built from the workspace root and the output directory.  It is
deliberately resolved *on use* rather than in ``__init__``, because bootstrap
injects the constructor argument before it sets the context -- reading eagerly
would miss a provider that arrives a few lines later and silently substitute a
second service nobody asked for.  The four-way order and the laziness both
moved here intact, and the fallback instance lives on this class rather than on
the host.

Four members are read from outside the cluster, and they are reached through
the host's ``self._file_tools`` handle rather than re-injected: ``_shell``
takes the workspace snapshot, asks whether a path is inside the workspace, and
moves newly created files into the output directory; ``_transcribe_audio``
resolves a tool path.  That is the whole external surface, measured with an AST
pass before anything moved.

``file_mutation_authorizer`` travels with them because it is the call-aware
half of the same policy: ``write_file``/``edit_file`` carry the base
``output_write`` capability, and a *workspace* target additionally needs the
``workspace_write`` capability, the startup switch and containment in the
effective write scope.  It takes the registry as an argument (the executor
passes the calling one), which is why it does not read the host's.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from agent.core.output import OutputSink, _active_sink
from agent.pathing import path_contains, resolve_workspace_path
from agent.tools.files import (
    FileAccessPolicy,
    FileProvider,
    FileService,
    write_scope_allows,
)
from agent.tools.runtime import ToolRegistry


class FileTools:
    """The file tools, with the registry, roots and responders they need."""

    def __init__(
        self,
        registry: Any,
        workspace_root: Path,
        injected_file_service: Optional[FileProvider],
        active_workspace_root: Callable[[], Path],
        process_output_dir: Callable[[], Path],
        ok: Callable[..., dict[str, Any]],
        error: Callable[..., dict[str, Any]],
    ) -> None:
        # Public on purpose: ``ToolRegistry.call`` reads ``fn.__self__.registry``
        # to install the calling registry's merged context on the registry this
        # tool implementation reads.  A cluster that hid it would silently stop
        # receiving a session's own write scope, workspace and output dir.
        self.registry = registry
        self._workspace_root = workspace_root
        self._injected_file_service = injected_file_service
        self._active_workspace_root = active_workspace_root
        self._process_output_dir = process_output_dir
        self._ok = ok
        self._error = error
        #: Built lazily by ``file_service``; kept per instance so a fork's
        #: override cannot leak into the process-wide default.
        self._fallback_file_service: FileProvider | None = None

    @property
    def file_service(self) -> FileProvider:
        """The file provider, resolved late so injection always wins.

        Resolved on use rather than in ``__init__`` because the registry
        context is not necessarily populated by the time the tools are built —
        bootstrap injects the constructor argument first and sets the context
        afterwards.  Reading the context eagerly would silently miss a
        provider that arrives a few lines later, and the fallback below would
        take its place: a second service, with default policy, that nobody
        asked for.
        """
        # Constructor injection remains the default for ordinary CLI calls;
        # a forked session may override it through ToolRegistry's ContextVar.
        override = getattr(self.registry, "_context_override", None)
        active_override = override.get() if override is not None else None
        if isinstance(active_override, dict) and "file_service" in active_override:
            return active_override["file_service"]
        if self._injected_file_service is not None:
            return self._injected_file_service
        from_context = self.registry.get_context("file_service")
        if from_context is not None:
            return from_context
        if self._fallback_file_service is None:
            # Default policy mirrors the startup defaults: a readable,
            # non-writable workspace plus an always-usable output_dir.
            # The write_scope is resolved per call from the active registry
            # context so sub-agent scopes apply even though the policy is
            # immutable.
            self._fallback_file_service = FileService(
                FileAccessPolicy(
                    workspace_root=self._workspace_root,
                    output_root=self._process_output_dir(),
                ),
                write_scope=lambda: self.registry.get_context("write_scope") or (),
            )
        return self._fallback_file_service

    def file_mutation_authorizer(
        self,
        tool_input: dict[str, Any],
        registry: ToolRegistry,
    ) -> dict[str, Any] | None:
        """Call-aware authorization for root-dependent file mutations.

        ``write_file``/``edit_file`` carry the base ``output_write``
        capability and are available in every Agent profile for
        ``output_dir``.  A workspace target additionally requires the
        ``workspace_write`` capability (implementation profile with an
        explicit write_scope), the startup workspace write switch, and
        containment in the effective write scope.
        """
        if tool_input.get("root") != "workspace":
            return None
        profile = registry.get_context("capability_profile")
        if profile is not None and profile != "full":
            if profile != "implementation":
                return self.structured_access_denied(
                    "workspace writes require the workspace_write capability"
                )
            scope = registry.get_context("write_scope") or ()
            path = tool_input.get("path", "")
            if not write_scope_allows(scope, path):
                return self.structured_access_denied(
                    f"path is outside the effective write scope: {path}"
                )
        policy = registry.get_context("file_access_policy")
        if policy is not None and not policy.workspace_write:
            return self.structured_access_denied(
                "workspace writes are disabled by the file access policy"
            )
        return None

    @staticmethod
    def structured_access_denied(message: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": "access_denied",
                "message": message,
                "details": {},
                "retryable": False,
            },
        }

    def resolve_tool_path(self, path: str) -> tuple[Path, str]:
        return resolve_workspace_path(
            path,
            workspace_root=self._active_workspace_root(),
            output_dir=self._process_output_dir(),
        )

    def resolve_output_path(self, path: str) -> tuple[Path, str]:
        """Resolve relative paths against the output directory.

        Generated/downloaded files belong in output_dir, not the
        workspace (repo).  Absolute paths are accepted when they are already
        inside an allowed root.
        """
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            output_dir = self._process_output_dir()
            path = str(output_dir / candidate)
        return resolve_workspace_path(
            path,
            workspace_root=self._active_workspace_root(),
            output_dir=self._process_output_dir(),
        )

    def path_is_inside_workspace(self, path: Path) -> bool:
        return path_contains(
            self._active_workspace_root(),
            path.expanduser().resolve(strict=False),
        )

    def workspace_file_snapshot(self) -> set[Path]:
        """Return files currently in the workspace, ignoring agent outputs."""
        files: set[Path] = set()
        workspace_root = self._active_workspace_root()
        output_dir = self._process_output_dir()
        if not workspace_root.exists():
            return files
        for path in workspace_root.rglob("*"):
            if path.is_symlink():
                continue
            resolved = path.resolve(strict=False)
            if path.is_dir():
                if path.name in {".git", "__pycache__", ".pytest_cache"}:
                    continue
                continue
            if path_contains(output_dir, resolved):
                continue
            files.add(resolved)
        return files

    def move_new_workspace_files_to_output_dir(
        self,
        *,
        before: set[Path],
        cwd: Path,
    ) -> list[dict[str, str]]:
        output_dir = self._process_output_dir()
        workspace_root = self._active_workspace_root()
        moved: list[dict[str, str]] = []
        for path in sorted(self.workspace_file_snapshot() - before):
            if not path.is_file() or path_contains(output_dir, path):
                continue
            try:
                relative = path.relative_to(workspace_root)
            except ValueError:
                relative = path.name
            dest = output_dir / "workspace-artifacts" / relative
            if dest.exists():
                dest = dest.with_name(f"{dest.stem}-{uuid.uuid4().hex[:8]}{dest.suffix}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
            moved.append({"from": str(path), "to": str(dest)})

            # Clean up empty directories left by nested generated artifacts,
            # stopping at cwd/workspace boundaries.
            parent = path.parent
            stop_dirs = {workspace_root, cwd.resolve(strict=False)}
            while parent not in stop_dirs and parent != parent.parent:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        return moved


    def send_file(self, path: str) -> dict[str, Any]:
        try:
            resolved, _root_kind = self.resolve_output_path(path)
            if not resolved.exists():
                return self._error(f"'{path}' does not exist", path=str(resolved))
            if not resolved.is_file():
                return self._error(f"'{path}' is not a regular file", path=str(resolved))
            sink = _active_sink.get()
            if sink is None:
                return self._error(
                    "Current channel does not support sending files in this context.",
                    path=str(resolved),
                )
            # Channels that support attachments override queue_attachment.
            # Base class no-op means the channel doesn't support file delivery.
            has_override = (
                type(sink).queue_attachment
                is not OutputSink.queue_attachment
            )
            if not has_override:
                return self._error(
                    "Current channel does not support sending files in this context.",
                    path=str(resolved),
                )
            sink.queue_attachment(resolved)
            return self._ok(path=str(resolved), queued=True)
        except ValueError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Error queueing file: {e}")

    def read_file(
        self,
        root: str = "workspace",
        path: str = "",
        start_line: int = 1,
        line_count: Optional[int] = None,
    ) -> dict[str, Any]:
        return self.file_service.read_file(
            root,
            path,
            start_line=start_line,
            line_count=line_count,
        )

    def write_file(
        self,
        root: str,
        path: str,
        mode: str,
        content: str,
        expected_revision: Optional[str] = None,
    ) -> dict[str, Any]:
        return self.file_service.write_file(
            root,
            path,
            mode=mode,
            content=content,
            expected_revision=expected_revision,
        )

    def edit_file(
        self,
        root: str,
        path: str,
        expected_revision: str,
        replacements: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.file_service.edit_file(
            root,
            path,
            expected_revision=expected_revision,
            replacements=replacements,
        )

    def list_files(
        self,
        root: str = "workspace",
        path: str = ".",
        pattern: str = "*",
        recursive: bool = False,
        cursor: Optional[str] = None,
        max_results: Optional[int] = None,
    ) -> dict[str, Any]:
        return self.file_service.list_files(
            root,
            path,
            recursive=recursive,
            pattern=pattern,
            cursor=cursor,
            max_results=max_results,
        )
