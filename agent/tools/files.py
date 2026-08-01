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
import errno
import hashlib
import os
from pathlib import Path
import stat
from typing import Any, Mapping


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

    def __init__(self, policy: FileAccessPolicy) -> None:
        self.policy = policy

    def _require_workspace_read(self) -> None:
        if not self.policy.workspace_read:
            raise FileServiceError(
                "access_denied",
                "workspace reads are disabled by the file access policy",
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
