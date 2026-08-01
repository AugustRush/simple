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

from dataclasses import dataclass
from pathlib import Path
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


__all__ = [
    "DEFAULT_FILE_ACCESS",
    "FileAccessLimits",
    "FileAccessPolicy",
    "FilePolicyConfigError",
    "MAX_LIST_RESULTS_CEILING",
    "MAX_READ_BYTES_CEILING",
    "MAX_READ_LINES_CEILING",
    "MAX_REPLACEMENTS_CEILING",
    "MAX_SNAPSHOT_BYTES_CEILING",
    "MAX_WRITE_BYTES_CEILING",
    "resolve_file_access_config",
]
