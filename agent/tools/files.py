"""Shared file access policy for built-in file tools and shell execution.

The policy is constructed once during bootstrap and is immutable for the
lifetime of the Agent instance.  It owns root selection and authorization:
every public file operation targets an explicit root (``workspace`` or
``output_dir``) and a relative path, and is checked against this policy
before any I/O happens.

``workspace`` and ``output_dir`` are distinct security domains.  Only
workspace access is configurable; ``output_dir`` is always readable and
writable.  Bootstrap rejects an overlapping configuration before any file
or shell tool is registered.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
import base64
import errno
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import uuid
from typing import Any, Callable, Mapping, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]


class FilePolicyConfigError(ValueError):
    """Raised when ``file_access`` configuration is invalid."""


# Hard implementation ceilings.  Limits are startup configuration and must
# stay positive and bounded so an unsafe configuration cannot make a single
# tool result or in-memory mutation unbounded.
MAX_READ_LINES_CEILING = 100_000
MAX_READ_BYTES_CEILING = 16 * 1024 * 1024
MAX_SNAPSHOT_BYTES_CEILING = 256 * 1024 * 1024
MAX_WRITE_BYTES_CEILING = 64 * 1024 * 1024
MAX_REPLACEMENTS_CEILING = 10_000
MAX_LIST_RESULTS_CEILING = 100_000

_LIMIT_RANGES: dict[str, tuple[int, int]] = {
    "max_read_lines": (1, MAX_READ_LINES_CEILING),
    "max_read_bytes": (1, MAX_READ_BYTES_CEILING),
    "max_snapshot_bytes": (1, MAX_SNAPSHOT_BYTES_CEILING),
    "max_write_bytes": (1, MAX_WRITE_BYTES_CEILING),
    "max_replacements": (1, MAX_REPLACEMENTS_CEILING),
    "max_list_results": (1, MAX_LIST_RESULTS_CEILING),
}

#: Default ``file_access`` configuration section.  The workspace is
#: readable but not writable unless the operator explicitly opts in.
DEFAULT_FILE_ACCESS: dict[str, Any] = {
    "workspace": {
        "read": True,
        "write": False,
    },
    "max_read_lines": 400,
    "max_read_bytes": 64 * 1024,
    "max_snapshot_bytes": 16 * 1024 * 1024,
    "max_write_bytes": 4 * 1024 * 1024,
    "max_replacements": 100,
    "max_list_results": 1000,
}


@dataclass(frozen=True)
class FileAccessLimits:
    """Positive, bounded resource limits for file service operations."""

    max_read_lines: int = 400
    max_read_bytes: int = 64 * 1024
    max_snapshot_bytes: int = 16 * 1024 * 1024
    max_write_bytes: int = 4 * 1024 * 1024
    max_replacements: int = 100
    max_list_results: int = 1000

    @classmethod
    def from_config(cls, raw: Mapping[str, Any]) -> "FileAccessLimits":
        if not isinstance(raw, Mapping):
            raise FilePolicyConfigError("'file_access' limits must be an object")
        unknown = sorted(set(raw) - set(_LIMIT_RANGES))
        if unknown:
            raise FilePolicyConfigError(
                f"unknown 'file_access' limit(s): {', '.join(unknown)}"
            )
        values: dict[str, int] = {}
        for key, (minimum, maximum) in _LIMIT_RANGES.items():
            value = raw.get(key)
            if value is None:
                continue  # absent -> dataclass default
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise FilePolicyConfigError(
                    f"'file_access.{key}' must be a positive integer, got {value!r}"
                )
            if value > maximum:
                raise FilePolicyConfigError(
                    f"'file_access.{key}' must be between {minimum} and {maximum}, "
                    f"got {value}"
                )
            values[key] = value
        return cls(**values)


@dataclass(frozen=True)
class FileAccessPolicy:
    """Immutable startup policy shared by file tools and shell execution.

    ``workspace_root`` and ``output_root`` are resolved and must be
    disjoint: neither root may equal, contain, or be contained by the
    other.  The model has no tool for reading, replacing, or elevating
    this policy; changing the configuration requires a restart.
    """

    workspace_root: Path
    output_root: Path
    workspace_read: bool = True
    workspace_write: bool = False
    limits: FileAccessLimits = FileAccessLimits()

    def __post_init__(self) -> None:
        workspace = self.workspace_root.expanduser().resolve()
        output = self.output_root.expanduser().resolve()
        object.__setattr__(self, "workspace_root", workspace)
        object.__setattr__(self, "output_root", output)
        if workspace == output or output in workspace.parents or workspace in output.parents:
            raise FilePolicyConfigError(
                "workspace and output_dir must be disjoint (neither may contain "
                f"the other); got workspace={workspace}, output_dir={output}"
            )

    @classmethod
    def from_config(
        cls,
        raw: Mapping[str, Any],
        *,
        workspace_root: str | Path,
        output_root: str | Path,
    ) -> "FileAccessPolicy":
        if not isinstance(raw, Mapping):
            raise FilePolicyConfigError("'file_access' must be an object")
        known = {
            "workspace",
            "max_read_lines",
            "max_read_bytes",
            "max_snapshot_bytes",
            "max_write_bytes",
            "max_replacements",
            "max_list_results",
        }
        unknown = sorted(set(raw) - known)
        if unknown:
            raise FilePolicyConfigError(
                f"unknown 'file_access' key(s): {', '.join(unknown)}"
            )
        workspace_cfg = raw.get("workspace", {})
        if not isinstance(workspace_cfg, Mapping):
            raise FilePolicyConfigError("'file_access.workspace' must be an object")
        unknown_workspace = sorted(set(workspace_cfg) - {"read", "write"})
        if unknown_workspace:
            raise FilePolicyConfigError(
                "unknown 'file_access.workspace' key(s): "
                + ", ".join(unknown_workspace)
            )
        workspace_read = workspace_cfg.get("read", True)
        workspace_write = workspace_cfg.get("write", False)
        if not isinstance(workspace_read, bool):
            raise FilePolicyConfigError(
                "'file_access.workspace.read' must be a boolean, "
                f"got {workspace_read!r}"
            )
        if not isinstance(workspace_write, bool):
            raise FilePolicyConfigError(
                "'file_access.workspace.write' must be a boolean, "
                f"got {workspace_write!r}"
            )
        limits_raw = {key: raw[key] for key in _LIMIT_RANGES if key in raw}
        limits = FileAccessLimits.from_config(limits_raw)
        return cls(
            workspace_root=Path(workspace_root),
            output_root=Path(output_root),
            workspace_read=workspace_read,
            workspace_write=workspace_write,
            limits=limits,
        )


def resolve_file_access_config(
    cfg: Mapping[str, Any],
    *,
    workspace_root: str | Path,
    output_dir: str | Path,
) -> FileAccessPolicy:
    """Build the immutable policy from the top-level application config."""
    raw = cfg.get("file_access", {})
    return FileAccessPolicy.from_config(
        raw,
        workspace_root=workspace_root,
        output_root=output_dir,
    )


# ── File service ───────────────────────────────────────────────────────────

ROOT_WORKSPACE = "workspace"
ROOT_OUTPUT = "output_dir"
ROOT_KINDS = (ROOT_WORKSPACE, ROOT_OUTPUT)

_READ_CHUNK_SIZE = 64 * 1024
_DEFAULT_READ_LINE_COUNT = 200
_UTF8_BOM = b"\xef\xbb\xbf"
_INTERNAL_DIR = ".simple-internal"

# Per-path in-process locks keyed by (root kind, root path, relative path).
_IN_PROCESS_LOCKS: dict[tuple[str, str, str], threading.Lock] = {}
_IN_PROCESS_LOCKS_GUARD = threading.Lock()

_STABLE_ERROR_CODES = frozenset(
    {
        "access_denied",
        "invalid_request",
        "invalid_path",
        "not_found",
        "already_exists",
        "not_directory",
        "not_regular_file",
        "unsupported_encoding",
        "file_too_large",
        "line_too_large",
        "revision_required",
        "revision_conflict",
        "match_count_mismatch",
        "locking_unavailable",
        "atomic_replace_failed",
        "io_error",
    }
)


class FileServiceError(Exception):
    """A stable, structured failure for the file service."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        if code not in _STABLE_ERROR_CODES:
            raise ValueError(f"unknown stable error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.retryable = bool(retryable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "retryable": self.retryable,
            },
        }


def _invalid(message: str) -> FileServiceError:
    return FileServiceError("invalid_path", message)


def _validate_root(root: str) -> str:
    if root not in ROOT_KINDS:
        raise _invalid(f"root must be one of {', '.join(ROOT_KINDS)}, got {root!r}")
    return root


def _validate_relative_path(
    rel_path: Any,
    *,
    allow_dot: bool = False,
) -> str:
    if not isinstance(rel_path, str):
        raise _invalid(f"path must be a string, got {type(rel_path).__name__}")
    if not rel_path:
        raise _invalid("path cannot be empty")
    if "\x00" in rel_path:
        raise _invalid("path cannot contain NUL bytes")
    if os.path.isabs(rel_path):
        raise _invalid("path must be relative to the selected root")
    parts = rel_path.split("/")
    if allow_dot and parts == ["."]:
        return rel_path
    if parts[0] == _INTERNAL_DIR:
        raise _invalid(f"'{_INTERNAL_DIR}' is reserved for internal bookkeeping")
    for part in parts:
        if part in (".", ".."):
            raise _invalid("path cannot contain '.' or '..' components")
        if not part:
            raise _invalid("path cannot contain empty components")
    return rel_path


class AuthorizedPath:
    """A validated root-relative path resolved with descriptor-relative,
    no-follow traversal so a symlink or swap cannot redirect it outside the
    selected root.
    """

    def __init__(
        self,
        policy: FileAccessPolicy,
        root: str,
        rel_path: str,
        *,
        allow_dot: bool = False,
    ) -> None:
        root = _validate_root(root)
        rel_path = _validate_relative_path(rel_path, allow_dot=allow_dot)
        self.policy = policy
        self.root = root
        self.rel_path = rel_path
        self.root_path = (
            policy.workspace_root if root == ROOT_WORKSPACE else policy.output_root
        )

    def open_file(self) -> tuple[int, os.stat_result]:
        """Open the final regular file O_RDONLY|O_NOFOLLOW."""
        parent_fd = _open_root_directory(self.root_path)
        try:
            parts = self.rel_path.split("/")
            for index, part in enumerate(parts):
                is_last = index == len(parts) - 1
                flags = os.O_RDONLY | os.O_NOFOLLOW
                if not is_last:
                    flags |= os.O_DIRECTORY
                try:
                    fd = os.open(part, flags, dir_fd=parent_fd)
                except FileNotFoundError as exc:
                    raise FileServiceError("not_found", f"file not found: {self.rel_path}") from exc
                except NotADirectoryError as exc:
                    if _is_symlink_component(parent_fd, part):
                        raise _invalid(
                            "symlink traversal is not allowed: " + self.rel_path
                        ) from exc
                    raise FileServiceError(
                        "not_directory",
                        f"path component is not a directory: {self.rel_path}",
                    ) from exc
                except PermissionError as exc:
                    raise FileServiceError(
                        "access_denied", "permission denied while opening path"
                    ) from exc
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        raise _invalid(
                            "symlink traversal is not allowed: " + self.rel_path
                        ) from exc
                    raise FileServiceError(
                        "io_error", "unable to open path"
                    ) from exc
                os.close(parent_fd)
                parent_fd = fd
            st = os.fstat(parent_fd)
            if not stat.S_ISREG(st.st_mode):
                os.close(parent_fd)
                raise FileServiceError(
                    "not_regular_file",
                    f"target is not a regular file: {self.rel_path}",
                )
            return parent_fd, st
        except BaseException:
            try:
                os.close(parent_fd)
            except OSError:
                pass
            raise

    def open_directory(self) -> tuple[int, os.stat_result]:
        parent_fd = _open_root_directory(self.root_path)
        try:
            parts = [] if self.rel_path == "." else self.rel_path.split("/")
            for index, part in enumerate(parts):
                is_last = index == len(parts) - 1
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
                if not is_last:
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
                try:
                    fd = os.open(part, flags, dir_fd=parent_fd)
                except FileNotFoundError as exc:
                    raise FileServiceError(
                        "not_found", f"directory not found: {self.rel_path}"
                    ) from exc
                except NotADirectoryError as exc:
                    if _is_symlink_component(parent_fd, part):
                        raise _invalid(
                            "symlink traversal is not allowed: " + self.rel_path
                        ) from exc
                    raise FileServiceError(
                        "not_directory",
                        f"target is not a directory: {self.rel_path}",
                    ) from exc
                except PermissionError as exc:
                    raise FileServiceError(
                        "access_denied", "permission denied while opening directory"
                    ) from exc
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        raise _invalid(
                            "symlink traversal is not allowed: " + self.rel_path
                        ) from exc
                    raise FileServiceError(
                        "io_error", "unable to open directory"
                    ) from exc
                os.close(parent_fd)
                parent_fd = fd
            st = os.fstat(parent_fd)
            if not stat.S_ISDIR(st.st_mode):
                os.close(parent_fd)
                raise FileServiceError(
                    "not_directory",
                    f"target is not a directory: {self.rel_path}",
                )
            return parent_fd, st
        except BaseException:
            try:
                os.close(parent_fd)
            except OSError:
                pass
            raise


def _open_root_directory(root_path: Path) -> int:
    try:
        fd = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError as exc:
        raise FileServiceError(
            "not_found", f"root directory missing: {root_path}"
        ) from exc
    except PermissionError as exc:
        raise FileServiceError(
            "access_denied", f"permission denied on root directory: {root_path}"
        ) from exc
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode):
        os.close(fd)
        raise FileServiceError(
            "not_directory", f"root is not a directory: {root_path}"
        )
    return fd


def _is_symlink_component(parent_fd: int, part: str) -> bool:
    try:
        st = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
        return stat.S_ISLNK(st.st_mode)
    except OSError:
        return False


class _LineScanner:
    """Incremental line assembler that reports complete lines (terminators
    retained) and newline metadata without buffering the whole file.
    """

    def __init__(
        self,
        *,
        start_line: int,
        line_count: int,
        max_read_bytes: int,
    ) -> None:
        self.start_line = start_line
        self.line_count = line_count
        self.max_read_bytes = max_read_bytes
        self.pending = ""
        self.total_lines = 0
        self.window: list[str] = []
        self.window_bytes = 0
        self.window_complete = False
        self.newline_kinds: set[str] = set()
        self.line_too_large = False

    def feed(self, text: str) -> None:
        self.pending += text
        self._scan()

    def _scan(self) -> None:
        data = self.pending
        self.pending = ""
        start = 0
        index = 0
        length = len(data)
        while index < length:
            char = data[index]
            if char == "\n":
                self._line(data[start : index + 1], "lf")
                index += 1
                start = index
            elif char == "\r":
                if index + 1 < length:
                    if data[index + 1] == "\n":
                        self._line(data[start : index + 2], "crlf")
                        index += 2
                    else:
                        self._line(data[start : index + 1], "cr")
                        index += 1
                    start = index
                else:
                    # Trailing CR may be the start of a CRLF pair split across
                    # chunk boundaries; defer it until the next feed/finish.
                    break
            else:
                index += 1
        self.pending = data[start:]

    def finish(self) -> None:
        if self.pending:
            if self.pending.endswith("\r"):
                self._line(self.pending, "cr")
            else:
                self._line(self.pending, "none")
            self.pending = ""

    def _line(self, line: str, kind: str) -> None:
        self.total_lines += 1
        if kind != "none":
            self.newline_kinds.add(kind)
        if self.window_complete or self.total_lines < self.start_line:
            return
        line_bytes = len(line.encode("utf-8"))
        if not self.window and line_bytes > self.max_read_bytes:
            self.line_too_large = True
            self.window_complete = True
            return
        if self.window_bytes + line_bytes <= self.max_read_bytes:
            self.window.append(line)
            self.window_bytes += line_bytes
        self.window_complete = len(self.window) >= self.line_count

    @property
    def newline(self) -> str:
        if len(self.newline_kinds) > 1:
            return "mixed"
        if self.newline_kinds:
            return next(iter(self.newline_kinds))
        return "none"


class FileService:
    """Rooted, snapshot-based file operations shared by built-in tools."""

    def __init__(
        self,
        policy: FileAccessPolicy,
        write_scope: Sequence[str | Path] | Callable[[], Sequence[str | Path]] = (),
    ) -> None:
        self.policy = policy
        if callable(write_scope):
            self._write_scope_provider = write_scope
            self.write_scope: tuple[str, ...] = ()
        else:
            self._write_scope_provider = None
            self.write_scope = _normalize_write_scope(write_scope)

    def _effective_write_scope(self) -> tuple[str, ...]:
        if self._write_scope_provider is not None:
            return _normalize_write_scope(self._write_scope_provider())
        return self.write_scope

    def _require_workspace_read(self) -> None:
        if not self.policy.workspace_read:
            raise FileServiceError(
                "access_denied",
                "workspace reads are disabled by the file access policy",
            )

    def _require_workspace_write(self, rel_path: str) -> None:
        if not self.policy.workspace_write:
            raise FileServiceError(
                "access_denied",
                "workspace writes are disabled by the file access policy",
            )
        effective = self._effective_write_scope()
        if not any(_scope_contains(scope, rel_path) for scope in effective):
            raise FileServiceError(
                "access_denied",
                f"path is outside the effective write scope: {rel_path}",
            )

    def read_file(
        self,
        root: str,
        path: str,
        *,
        start_line: int = 1,
        line_count: int | None = None,
    ) -> dict[str, Any]:
        try:
            return self._read_file(root, path, start_line=start_line, line_count=line_count)
        except FileServiceError as exc:
            return exc.to_dict()

    def _read_file(
        self,
        root: str,
        path: str,
        *,
        start_line: int,
        line_count: int | None,
    ) -> dict[str, Any]:
        root = _validate_root(root)
        _validate_relative_path(path)
        if root == ROOT_WORKSPACE:
            self._require_workspace_read()
        if not isinstance(start_line, int) or isinstance(start_line, bool) or start_line < 1:
            raise FileServiceError(
                "invalid_request", "start_line must be a positive integer"
            )
        if line_count is None:
            line_count = min(self.policy.limits.max_read_lines, _DEFAULT_READ_LINE_COUNT)
        if (
            not isinstance(line_count, int)
            or isinstance(line_count, bool)
            or line_count < 1
        ):
            raise FileServiceError(
                "invalid_request", "line_count must be a positive integer"
            )
        if line_count > self.policy.limits.max_read_lines:
            raise FileServiceError(
                "invalid_request",
                f"line_count exceeds the configured limit of "
                f"{self.policy.limits.max_read_lines}",
            )

        authorized = AuthorizedPath(self.policy, root, path)
        fd, before = authorized.open_file()
        try:
            return self._snapshot_read(
                fd,
                root=root,
                path=path,
                start_line=start_line,
                line_count=line_count,
                before=before,
            )
        finally:
            os.close(fd)

    def _snapshot_read(
        self,
        fd: int,
        *,
        root: str,
        path: str,
        start_line: int,
        line_count: int,
        before: os.stat_result,
    ) -> dict[str, Any]:
        limits = self.policy.limits
        if before.st_size > limits.max_snapshot_bytes:
            raise FileServiceError(
                "file_too_large",
                f"file exceeds max_snapshot_bytes ({limits.max_snapshot_bytes})",
            )
        digest = hashlib.sha256()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        scanner = _LineScanner(
            start_line=start_line,
            line_count=line_count,
            max_read_bytes=limits.max_read_bytes,
        )
        bom = False
        bom_decided = False
        bom_prefix = bytearray()
        bytes_read = 0

        def consume_decoded(raw: bytes) -> None:
            try:
                text = decoder.decode(raw)
            except UnicodeDecodeError as exc:
                raise FileServiceError(
                    "unsupported_encoding", "file is not valid UTF-8"
                ) from exc
            if "\x00" in text:
                raise FileServiceError(
                    "unsupported_encoding", "file contains embedded NUL bytes"
                )
            if text:
                scanner.feed(text)

        while True:
            chunk = self._read_chunk(fd, _READ_CHUNK_SIZE)
            if not chunk:
                break
            bytes_read += len(chunk)
            if bytes_read > limits.max_snapshot_bytes:
                raise FileServiceError(
                    "file_too_large",
                    f"file exceeds max_snapshot_bytes ({limits.max_snapshot_bytes})",
                )
            digest.update(chunk)
            data = chunk
            if not bom_decided:
                take = min(3 - len(bom_prefix), len(data))
                bom_prefix += data[:take]
                data = data[take:]
                if len(bom_prefix) == 3:
                    bom_decided = True
                    if bytes(bom_prefix) == _UTF8_BOM:
                        bom = True
                    else:
                        consume_decoded(bytes(bom_prefix))
            if data:
                consume_decoded(data)
        if not bom_decided:
            bom_decided = True
            consume_decoded(bytes(bom_prefix))
        try:
            tail = decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise FileServiceError(
                "unsupported_encoding",
                "file is not valid UTF-8 (truncated code point at end of file)",
            ) from exc
        if "\x00" in tail:
            raise FileServiceError(
                "unsupported_encoding", "file contains embedded NUL bytes"
            )
        if tail:
            scanner.feed(tail)
        scanner.finish()

        after = os.fstat(fd)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise FileServiceError(
                "revision_conflict",
                "file changed while it was being read",
                retryable=True,
            )

        if scanner.line_too_large:
            raise FileServiceError(
                "line_too_large",
                f"the first requested line exceeds max_read_bytes "
                f"({limits.max_read_bytes})",
            )
        if start_line > scanner.total_lines:
            if scanner.total_lines == 0 and start_line == 1:
                pass  # the empty file at start_line=1 is the sole empty-range success
            else:
                raise FileServiceError(
                    "invalid_request",
                    f"start_line {start_line} exceeds total_lines {scanner.total_lines}",
                )

        content = "".join(scanner.window)
        returned_bytes = len(content.encode("utf-8"))
        end_line = (
            start_line + len(scanner.window) - 1 if scanner.window else None
        )
        next_start_line = (
            end_line + 1
            if end_line is not None and end_line < scanner.total_lines
            else None
        )
        return {
            "ok": True,
            "root": root,
            "path": path,
            "content": content,
            "revision": "sha256:" + digest.hexdigest(),
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": scanner.total_lines,
            "next_start_line": next_start_line,
            "size_bytes": after.st_size,
            "returned_bytes": returned_bytes,
            "encoding": "utf-8",
            "bom": bom,
            "newline": scanner.newline,
        }

    def _read_chunk(self, fd: int, size: int) -> bytes:
        return os.read(fd, size)

    # ── Mutations ──────────────────────────────────────────────────────────

    def write_file(
        self,
        root: str,
        path: str,
        *,
        mode: str,
        content: str,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        try:
            return self._write_file(
                root,
                path,
                mode=mode,
                content=content,
                expected_revision=expected_revision,
            )
        except FileServiceError as exc:
            return exc.to_dict()

    def edit_file(
        self,
        root: str,
        path: str,
        *,
        expected_revision: str,
        replacements: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        try:
            return self._edit_file(
                root,
                path,
                expected_revision=expected_revision,
                replacements=replacements,
            )
        except FileServiceError as exc:
            return exc.to_dict()

    def _write_file(
        self,
        root: str,
        path: str,
        *,
        mode: str,
        content: str,
        expected_revision: str | None,
    ) -> dict[str, Any]:
        root = _validate_root(root)
        _validate_relative_path(path)
        if mode not in ("create", "overwrite"):
            raise FileServiceError(
                "invalid_request",
                "mode must be 'create' or 'overwrite'",
            )
        if root == ROOT_WORKSPACE:
            self._require_workspace_write(path)
        payload = _encode_request_text(content, limits=self.policy.limits)
        if mode == "create":
            return self._write_create(root, path, payload)
        return self._write_overwrite(
            root,
            path,
            payload,
            expected_revision=expected_revision,
        )

    def _write_create(
        self,
        root: str,
        path: str,
        payload: bytes,
    ) -> dict[str, Any]:
        authorized = AuthorizedPath(self.policy, root, path)
        lock = _acquire_mutation_locks(self.policy, root, path)
        try:
            parent_fd, _ = _ensure_parent_directories(
                self.policy, root, authorized.rel_path
            )
            try:
                temp_fd, temp_name = _create_temp_file(
                    parent_fd, authorized.rel_path.rsplit("/", 1)[-1], payload
                )
                try:
                    os.link(
                        temp_name,
                        authorized.rel_path.rsplit("/", 1)[-1],
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise FileServiceError(
                        "already_exists", f"create target already exists: {path}"
                    ) from exc
                except (NotImplementedError, OSError) as exc:
                    raise FileServiceError(
                        "atomic_replace_failed",
                        "unable to publish the created file atomically",
                        retryable=True,
                    ) from exc
                finally:
                    _close_fd(temp_fd)
                    try:
                        os.unlink(temp_name, dir_fd=parent_fd)
                    except OSError:
                        pass
                _sync_directory(parent_fd)
            finally:
                _close_fd(parent_fd)
            return {
                "ok": True,
                "root": root,
                "path": path,
                "mode": "create",
                "old_revision": None,
                "new_revision": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "old_size_bytes": 0,
                "new_size_bytes": len(payload),
                "byte_delta": len(payload),
            }
        finally:
            _release_mutation_locks(lock)

    def _write_overwrite(
        self,
        root: str,
        path: str,
        payload: bytes,
        *,
        expected_revision: str | None,
    ) -> dict[str, Any]:
        if not isinstance(expected_revision, str) or not expected_revision:
            raise FileServiceError(
                "revision_required",
                "overwrite requires a valid expected_revision",
            )
        authorized = AuthorizedPath(self.policy, root, path)
        lock = _acquire_mutation_locks(self.policy, root, path)
        try:
            parent_fd, _ = _ensure_parent_directories(
                self.policy, root, authorized.rel_path
            )
            try:
                target_fd, before = authorized.open_file()
                try:
                    old_bytes, revision = _read_target_bytes(
                        target_fd, self.policy.limits, path
                    )
                    _expect_revision(revision, expected_revision, path)
                    bom = old_bytes.startswith(_UTF8_BOM)
                    new_bytes = _UTF8_BOM + payload if bom else payload
                    temp_fd, temp_name = _create_temp_file(
                        parent_fd,
                        authorized.rel_path.rsplit("/", 1)[-1],
                        new_bytes,
                        mode=before.st_mode & 0o7777,
                    )
                    _close_fd(temp_fd)
                    try:
                        _recheck_target_before_replace(
                            self.policy,
                            root,
                            authorized.rel_path,
                            expected_revision,
                            before,
                            path,
                        )
                        try:
                            os.replace(
                                temp_name,
                                authorized.rel_path.rsplit("/", 1)[-1],
                                src_dir_fd=parent_fd,
                                dst_dir_fd=parent_fd,
                            )
                        except (NotImplementedError, OSError) as exc:
                            raise FileServiceError(
                                "atomic_replace_failed",
                                "unable to commit the overwrite atomically",
                                retryable=True,
                            ) from exc
                    finally:
                        try:
                            os.unlink(temp_name, dir_fd=parent_fd)
                        except OSError:
                            pass
                    _sync_directory(parent_fd)
                    old_size = len(old_bytes)
                    return {
                        "ok": True,
                        "root": root,
                        "path": path,
                        "mode": "overwrite",
                        "old_revision": revision,
                        "new_revision": "sha256:"
                        + hashlib.sha256(new_bytes).hexdigest(),
                        "old_size_bytes": old_size,
                        "new_size_bytes": len(new_bytes),
                        "byte_delta": len(new_bytes) - old_size,
                    }
                finally:
                    _close_fd(target_fd)
            finally:
                _close_fd(parent_fd)
        finally:
            _release_mutation_locks(lock)

    def _edit_file(
        self,
        root: str,
        path: str,
        *,
        expected_revision: str,
        replacements: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        root = _validate_root(root)
        _validate_relative_path(path)
        if root == ROOT_WORKSPACE:
            self._require_workspace_write(path)
        if not isinstance(expected_revision, str) or not expected_revision:
            raise FileServiceError(
                "revision_required",
                "edit_file requires a valid expected_revision",
            )
        normalized = _validate_replacements(replacements, self.policy.limits)
        authorized = AuthorizedPath(self.policy, root, path)
        lock = _acquire_mutation_locks(self.policy, root, path)
        try:
            parent_fd, _ = _ensure_parent_directories(
                self.policy, root, authorized.rel_path
            )
            try:
                target_fd, before = authorized.open_file()
                try:
                    old_bytes, revision = _read_target_bytes(
                        target_fd, self.policy.limits, path
                    )
                    _expect_revision(revision, expected_revision, path)
                    bom = old_bytes.startswith(_UTF8_BOM)
                    try:
                        text = (
                            old_bytes[len(_UTF8_BOM) :]
                            .decode("utf-8", errors="strict")
                            if bom
                            else old_bytes.decode("utf-8", errors="strict")
                        )
                    except UnicodeDecodeError as exc:
                        raise FileServiceError(
                            "unsupported_encoding", "file is not valid UTF-8"
                        ) from exc
                    if "\x00" in text:
                        raise FileServiceError(
                            "unsupported_encoding",
                            "file contains embedded NUL bytes",
                        )
                    new_text, total_replaced = _apply_replacements(
                        text, normalized, self.policy.limits
                    )
                    payload = new_text.encode("utf-8", errors="strict")
                    if payload[:3] == _UTF8_BOM or new_text.startswith("\ufeff"):
                        raise FileServiceError(
                            "invalid_request",
                            "edited content must not begin with U+FEFF",
                        )
                    new_bytes = _UTF8_BOM + payload if bom else payload
                    temp_fd, temp_name = _create_temp_file(
                        parent_fd,
                        authorized.rel_path.rsplit("/", 1)[-1],
                        new_bytes,
                        mode=before.st_mode & 0o7777,
                    )
                    _close_fd(temp_fd)
                    try:
                        _recheck_target_before_replace(
                            self.policy,
                            root,
                            authorized.rel_path,
                            expected_revision,
                            before,
                            path,
                        )
                        try:
                            os.replace(
                                temp_name,
                                authorized.rel_path.rsplit("/", 1)[-1],
                                src_dir_fd=parent_fd,
                                dst_dir_fd=parent_fd,
                            )
                        except (NotImplementedError, OSError) as exc:
                            raise FileServiceError(
                                "atomic_replace_failed",
                                "unable to commit the edit atomically",
                                retryable=True,
                            ) from exc
                    finally:
                        try:
                            os.unlink(temp_name, dir_fd=parent_fd)
                        except OSError:
                            pass
                    _sync_directory(parent_fd)
                    old_size = len(old_bytes)
                    return {
                        "ok": True,
                        "root": root,
                        "path": path,
                        "mode": "edit",
                        "old_revision": revision,
                        "new_revision": "sha256:"
                        + hashlib.sha256(new_bytes).hexdigest(),
                        "old_size_bytes": old_size,
                        "new_size_bytes": len(new_bytes),
                        "byte_delta": len(new_bytes) - old_size,
                        "replacement_count": len(normalized),
                        "replaced_occurrences": total_replaced,
                    }
                finally:
                    _close_fd(target_fd)
            finally:
                _close_fd(parent_fd)
        finally:
            _release_mutation_locks(lock)

    def list_files(
        self,
        root: str,
        path: str = ".",
        *,
        recursive: bool = False,
        pattern: str = "*",
        cursor: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        try:
            return self._list_files(
                root,
                path,
                recursive=recursive,
                pattern=pattern,
                cursor=cursor,
                max_results=max_results,
            )
        except FileServiceError as exc:
            return exc.to_dict()

    def _list_files(
        self,
        root: str,
        path: str,
        *,
        recursive: bool,
        pattern: str,
        cursor: str | None,
        max_results: int | None,
    ) -> dict[str, Any]:
        root = _validate_root(root)
        if root == ROOT_WORKSPACE:
            self._require_workspace_read()
        if path is None:
            path = "."
        _validate_relative_path(path, allow_dot=True)
        if not isinstance(pattern, str) or not pattern:
            raise FileServiceError(
                "invalid_request", "pattern must be a non-empty basename glob"
            )
        if "/" in pattern:
            raise FileServiceError(
                "invalid_request", "pattern must not contain a path separator"
            )
        if not isinstance(recursive, bool):
            raise FileServiceError(
                "invalid_request", "recursive must be a boolean"
            )
        if max_results is None:
            max_results = self.policy.limits.max_list_results
        if (
            not isinstance(max_results, int)
            or isinstance(max_results, bool)
            or max_results < 1
        ):
            raise FileServiceError(
                "invalid_request", "max_results must be a positive integer"
            )
        if max_results > self.policy.limits.max_list_results:
            raise FileServiceError(
                "invalid_request",
                f"max_results exceeds the configured limit of "
                f"{self.policy.limits.max_list_results}",
            )
        if not isinstance(cursor, (str, type(None))):
            raise FileServiceError(
                "invalid_request", "cursor must be a string or null"
            )

        resume_after: str | None = None
        if cursor is not None:
            resume_after = _decode_list_cursor(
                cursor,
                root=root,
                path=path,
                recursive=recursive,
                pattern=pattern,
            )

        authorized = AuthorizedPath(self.policy, root, path, allow_dot=True)
        root_fd, root_stat = authorized.open_directory()
        items: list[dict[str, Any]] = []
        last_emitted: str | None = None
        truncated = False
        root_prefix = "" if path == "." else path
        root_identity = (root_stat.st_dev, root_stat.st_ino)
        stack: list[_PendingDirectory] = [
            _PendingDirectory(
                prefix=root_prefix,
                fd=root_fd,
                ancestry=frozenset({root_identity}),
            )
        ]
        try:
            while stack and len(items) < max_results:
                pending = stack[-1]
                if pending.entries is None:
                    try:
                        pending.entries = _scan_directory_entries(
                            pending.fd, pending.prefix
                        )
                    except FileServiceError:
                        stack.pop()
                        _close_fd(pending.fd)
                        continue
                if not pending.entries:
                    stack.pop()
                    _close_fd(pending.fd)
                    continue
                entry = pending.entries.pop(0)
                if (
                    recursive
                    and entry["kind"] == "directory"
                    and entry["identity"] not in pending.ancestry
                ):
                    child = _open_child_directory(
                        self.policy, root, entry["path"]
                    )
                    if child is not None:
                        stack.append(
                            _PendingDirectory(
                                prefix=entry["path"],
                                fd=child,
                                ancestry=pending.ancestry | {entry["identity"]},
                            )
                        )
                if resume_after is not None:
                    if entry["path"] <= resume_after:
                        continue
                    resume_after = None
                if not fnmatch.fnmatchcase(entry["name"], pattern):
                    continue
                items.append(
                    {
                        "path": entry["path"],
                        "kind": entry["kind"],
                        "size_bytes": entry["size_bytes"],
                    }
                )
                last_emitted = entry["path"]
                if len(items) >= max_results:
                    truncated = any(
                        pending.entries is None or bool(pending.entries)
                        for pending in stack
                    )
                    break
        finally:
            while stack:
                _close_fd(stack.pop().fd)
        if truncated and last_emitted is not None:
            next_cursor = _encode_list_cursor(
                root=root,
                path=path,
                recursive=recursive,
                pattern=pattern,
                last=last_emitted,
            )
        else:
            next_cursor = None
        return {
            "ok": True,
            "root": root,
            "path": path,
            "items": items,
            "count": len(items),
            "truncated": truncated,
            "next_cursor": next_cursor,
        }


@dataclass
class _PendingDirectory:
    prefix: str
    fd: int
    ancestry: frozenset[tuple[int, int]]
    entries: list[dict[str, Any]] | None = None


def _entry_kind(st: os.stat_result) -> str:
    if stat.S_ISREG(st.st_mode):
        return "file"
    if stat.S_ISDIR(st.st_mode):
        return "directory"
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    return "other"


def _scan_directory_entries(
    fd: int,
    prefix: str,
) -> list[dict[str, Any]]:
    try:
        with os.scandir(fd) as iterator:
            raw = list(iterator)
    except OSError as exc:
        raise FileServiceError("io_error", "unable to enumerate directory") from exc
    scanned: list[dict[str, Any]] = []
    for entry in raw:
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue  # entry disappeared mid-listing: best-effort semantics
        kind = _entry_kind(st)
        rel_path = entry.name if not prefix else f"{prefix}/{entry.name}"
        scanned.append(
            {
                "name": entry.name,
                "path": rel_path,
                "kind": kind,
                "size_bytes": st.st_size if kind == "file" else None,
                "identity": (st.st_dev, st.st_ino),
            }
        )
    scanned.sort(key=lambda item: item["path"])
    return scanned


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _open_child_directory(
    policy: FileAccessPolicy,
    root: str,
    rel_path: str,
) -> int | None:
    """Open a descendant directory for traversal, or None if it cannot be
    opened (concurrent removal, permission, or symlink substitution).
    """
    try:
        authorized = AuthorizedPath(policy, root, rel_path)
        fd, _ = authorized.open_directory()
        return fd
    except FileServiceError:
        return None


def _list_fingerprint(
    *,
    root: str,
    path: str,
    recursive: bool,
    pattern: str,
) -> str:
    return json.dumps(
        [root, path, recursive, pattern], sort_keys=True, separators=(",", ":")
    )


def _encode_list_cursor(
    *,
    root: str,
    path: str,
    recursive: bool,
    pattern: str,
    last: str,
) -> str:
    payload = {
        "version": 1,
        "request": _list_fingerprint(
            root=root, path=path, recursive=recursive, pattern=pattern
        ),
        "last": last,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8"))
    return encoded.decode("ascii")


def _decode_list_cursor(
    cursor: str,
    *,
    root: str,
    path: str,
    recursive: bool,
    pattern: str,
) -> str:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise FileServiceError(
            "invalid_request", "cursor is malformed"
        ) from exc
    if payload.get("version") != 1:
        raise FileServiceError("invalid_request", "cursor version is unsupported")
    expected = _list_fingerprint(
        root=root, path=path, recursive=recursive, pattern=pattern
    )
    if payload.get("request") != expected:
        raise FileServiceError(
            "invalid_request",
            "cursor was created for a different request",
        )
    last = payload.get("last")
    if not isinstance(last, str) or not last:
        raise FileServiceError("invalid_request", "cursor is missing a resume point")
    return last


# ── Mutation helpers ────────────────────────────────────────────────────────


def _normalize_write_scope(scope: Sequence[str | Path]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw in scope:
        if isinstance(raw, Path):
            text = raw.as_posix()
        elif isinstance(raw, str):
            text = raw
        else:
            raise ValueError(f"invalid write_scope entry: {raw!r}")
        text = text.strip()
        if text in ("", "."):
            normalized.append("*")
            continue
        if text.startswith("./"):
            text = text[2:]
        text = text.rstrip("/")
        if (
            not text
            or os.path.isabs(text)
            or ".." in text.split("/")
            or text == _INTERNAL_DIR
            or text.startswith(_INTERNAL_DIR + "/")
        ):
            raise ValueError(f"invalid write_scope entry: {raw!r}")
        normalized.append(text)
    return tuple(normalized)


def _scope_contains(scope: str, rel_path: str) -> bool:
    if scope == "*":
        return True
    return rel_path == scope or rel_path.startswith(scope + "/")


def write_scope_allows(
    scope: Sequence[str | Path],
    rel_path: str,
) -> bool:
    """Return True when ``rel_path`` is contained in the effective scope."""
    return any(
        _scope_contains(normalized, rel_path)
        for normalized in _normalize_write_scope(scope)
    )


def _encode_request_text(content: Any, *, limits: FileAccessLimits) -> bytes:
    if not isinstance(content, str):
        raise FileServiceError("invalid_request", "content must be a string")
    if "\x00" in content:
        raise FileServiceError("invalid_request", "content cannot contain NUL bytes")
    if content.startswith("\ufeff"):
        raise FileServiceError(
            "invalid_request", "content must not begin with U+FEFF"
        )
    try:
        payload = content.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise FileServiceError(
            "invalid_request", "content must be valid UTF-8"
        ) from exc
    if len(payload) > limits.max_write_bytes:
        raise FileServiceError(
            "file_too_large",
            f"content exceeds max_write_bytes ({limits.max_write_bytes})",
        )
    return payload


def _validate_replacements(
    replacements: Sequence[Mapping[str, Any]],
    limits: FileAccessLimits,
) -> list[tuple[str, str, int]]:
    if not isinstance(replacements, (list, tuple)) or not replacements:
        raise FileServiceError(
            "invalid_request", "replacements must be a non-empty list"
        )
    if len(replacements) > limits.max_replacements:
        raise FileServiceError(
            "invalid_request",
            f"replacements exceed max_replacements ({limits.max_replacements})",
        )
    normalized: list[tuple[str, str, int]] = []
    aggregate = 0
    for item in replacements:
        if not isinstance(item, Mapping):
            raise FileServiceError(
                "invalid_request", "each replacement must be an object"
            )
        unknown = sorted(set(item) - {"old_text", "new_text", "expected_count"})
        if unknown:
            raise FileServiceError(
                "invalid_request",
                "unknown replacement key(s): " + ", ".join(unknown),
            )
        old_text = item.get("old_text")
        new_text = item.get("new_text")
        expected_count = item.get("expected_count")
        if not isinstance(old_text, str) or not old_text:
            raise FileServiceError(
                "invalid_request", "old_text must be a non-empty string"
            )
        if not isinstance(new_text, str):
            raise FileServiceError(
                "invalid_request", "new_text must be a string"
            )
        if (
            isinstance(expected_count, bool)
            or not isinstance(expected_count, int)
            or expected_count < 1
        ):
            raise FileServiceError(
                "invalid_request", "expected_count must be a positive integer"
            )
        if "\x00" in old_text or "\x00" in new_text:
            raise FileServiceError(
                "invalid_request", "replacement text cannot contain NUL bytes"
            )
        try:
            old_bytes = old_text.encode("utf-8", errors="strict")
            new_bytes = new_text.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise FileServiceError(
                "invalid_request", "replacement text must be valid UTF-8"
            ) from exc
        aggregate += len(old_bytes) + len(new_bytes)
        if aggregate > limits.max_write_bytes:
            raise FileServiceError(
                "file_too_large",
                "aggregate replacement payload exceeds max_write_bytes "
                f"({limits.max_write_bytes})",
            )
        normalized.append((old_text, new_text, expected_count))
    return normalized


def _apply_replacements(
    text: str,
    replacements: Sequence[tuple[str, str, int]],
    limits: FileAccessLimits,
) -> tuple[str, int]:
    total_replaced = 0
    for old_text, new_text, expected_count in replacements:
        count = text.count(old_text)
        if count != expected_count:
            raise FileServiceError(
                "match_count_mismatch",
                f"expected {expected_count} occurrence(s) of {old_text!r}, "
                f"observed {count}",
                details={
                    "old_text": old_text,
                    "expected_count": expected_count,
                    "actual_count": count,
                },
            )
        text = text.replace(old_text, new_text)
        total_replaced += count
        if len(text.encode("utf-8", errors="strict")) > limits.max_write_bytes:
            raise FileServiceError(
                "file_too_large",
                f"edited content exceeds max_write_bytes "
                f"({limits.max_write_bytes})",
            )
    return text, total_replaced


def _read_target_bytes(
    fd: int,
    limits: FileAccessLimits,
    path: str,
) -> tuple[bytes, str]:
    before = os.fstat(fd)
    if before.st_size > limits.max_snapshot_bytes:
        raise FileServiceError(
            "file_too_large",
            f"file exceeds max_snapshot_bytes ({limits.max_snapshot_bytes})",
        )
    chunks = bytearray()
    while True:
        chunk = os.read(fd, _READ_CHUNK_SIZE)
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > limits.max_snapshot_bytes:
            raise FileServiceError(
                "file_too_large",
                f"file exceeds max_snapshot_bytes "
                f"({limits.max_snapshot_bytes})",
            )
    after = os.fstat(fd)
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise FileServiceError(
            "revision_conflict",
            f"file changed while it was being read: {path}",
            retryable=True,
        )
    data = bytes(chunks)
    return data, "sha256:" + hashlib.sha256(data).hexdigest()


def _expect_revision(
    revision: str,
    expected_revision: str,
    path: str,
) -> None:
    if revision != expected_revision:
        raise FileServiceError(
            "revision_conflict",
            f"file changed since it was read: {path}",
            details={
                "expected_revision": expected_revision,
                "actual_revision": revision,
            },
            retryable=True,
        )


def _ensure_parent_directories(
    policy: FileAccessPolicy,
    root: str,
    rel_path: str,
) -> tuple[int, str]:
    root_path = policy.workspace_root if root == ROOT_WORKSPACE else policy.output_root
    parts = rel_path.split("/")
    target_name = parts[-1]
    dir_fd = _open_root_directory(root_path)
    try:
        for part in parts[:-1]:
            try:
                fd = os.open(
                    part,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
                    dir_fd=dir_fd,
                )
            except NotADirectoryError as exc:
                raise FileServiceError(
                    "not_directory",
                    f"path component is not a directory: {rel_path}",
                ) from exc
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o777, dir_fd=dir_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    if exc.errno == errno.ENOTDIR:
                        raise FileServiceError(
                            "not_directory",
                            f"path component is not a directory: {rel_path}",
                        ) from exc
                    raise FileServiceError(
                        "io_error", "unable to create parent directory"
                    ) from exc
                try:
                    fd = os.open(
                        part,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
                        dir_fd=dir_fd,
                    )
                except NotADirectoryError as exc:
                    raise FileServiceError(
                        "not_directory",
                        f"path component is not a directory: {rel_path}",
                    ) from exc
                except OSError as exc:
                    raise FileServiceError(
                        "io_error", "unable to open created parent directory"
                    ) from exc
            except PermissionError as exc:
                raise FileServiceError(
                    "access_denied", "permission denied while creating parents"
                ) from exc
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise _invalid(
                        "symlink traversal is not allowed: " + rel_path
                    ) from exc
                raise FileServiceError(
                    "io_error", "unable to traverse parent directory"
                ) from exc
            _close_fd(dir_fd)
            dir_fd = fd
        return dir_fd, target_name
    except BaseException:
        _close_fd(dir_fd)
        raise


def _create_temp_file(
    dir_fd: int,
    name: str,
    payload: bytes,
    *,
    mode: int | None = None,
) -> tuple[int, str]:
    temp_name = f".{name}.simple-tmp-{uuid.uuid4().hex}"
    fd = os.open(
        temp_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o666,
        dir_fd=dir_fd,
    )
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        _close_fd(fd)
        try:
            os.unlink(temp_name, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    return fd, temp_name


def _recheck_target_before_replace(
    policy: FileAccessPolicy,
    root: str,
    rel_path: str,
    expected_revision: str,
    before: os.stat_result,
    path: str,
) -> None:
    fd, current = AuthorizedPath(policy, root, rel_path).open_file()
    try:
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise FileServiceError(
                "revision_conflict",
                f"file was replaced since it was read: {path}",
                retryable=True,
            )
        data, revision = _read_target_bytes(fd, policy.limits, path)
        _expect_revision(revision, expected_revision, path)
    finally:
        _close_fd(fd)


def _sync_directory(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError:
        pass


def _acquire_mutation_locks(
    policy: FileAccessPolicy,
    root: str,
    rel_path: str,
) -> tuple[threading.Lock, int | None]:
    root_path = policy.workspace_root if root == ROOT_WORKSPACE else policy.output_root
    key = (root, str(root_path), rel_path)
    with _IN_PROCESS_LOCKS_GUARD:
        in_process = _IN_PROCESS_LOCKS.setdefault(key, threading.Lock())
    in_process.acquire()
    if fcntl is None:
        in_process.release()
        raise FileServiceError(
            "locking_unavailable",
            "advisory file locking is unavailable on this platform",
            retryable=True,
        )
    lock_dir = policy.output_root / _INTERNAL_DIR / "locks"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_key = hashlib.sha256(
            f"{key[0]}:{key[1]}:{key[2]}".encode("utf-8")
        ).hexdigest()
        advisory_fd = os.open(
            lock_dir / f"{lock_key}.lock",
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        fcntl.flock(advisory_fd, fcntl.LOCK_EX)
    except OSError as exc:
        in_process.release()
        raise FileServiceError(
            "locking_unavailable",
            "unable to acquire advisory file lock",
            retryable=True,
        ) from exc
    return in_process, advisory_fd


def _release_mutation_locks(
    lock: tuple[threading.Lock, int | None],
) -> None:
    in_process, advisory_fd = lock
    if advisory_fd is not None:
        try:
            fcntl.flock(advisory_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        _close_fd(advisory_fd)
    in_process.release()


__all__ = [
    "AuthorizedPath",
    "DEFAULT_FILE_ACCESS",
    "FileAccessLimits",
    "FileAccessPolicy",
    "FileService",
    "FileServiceError",
    "FilePolicyConfigError",
    "MAX_LIST_RESULTS_CEILING",
    "MAX_READ_BYTES_CEILING",
    "MAX_READ_LINES_CEILING",
    "MAX_REPLACEMENTS_CEILING",
    "MAX_SNAPSHOT_BYTES_CEILING",
    "MAX_WRITE_BYTES_CEILING",
    "ROOT_KINDS",
    "ROOT_OUTPUT",
    "ROOT_WORKSPACE",
    "resolve_file_access_config",
]
