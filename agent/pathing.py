from __future__ import annotations

from pathlib import Path


def canonicalize_user_path(path: str | Path, *, base_dir: Path) -> Path:
    base = Path(base_dir).expanduser().resolve(strict=False)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve(strict=False)


def path_contains(container: Path, target: Path) -> bool:
    return target == container or container in target.parents


def paths_overlap(left: Path, right: Path) -> bool:
    return path_contains(left, right) or path_contains(right, left)


def resolve_workspace_path(
    path: str | Path,
    *,
    workspace_root: Path,
    output_dir: Path | None = None,
) -> tuple[Path, str]:
    workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
    resolved = canonicalize_user_path(path, base_dir=workspace_root)
    output_root = (
        Path(output_dir).expanduser().resolve(strict=False)
        if output_dir is not None
        else None
    )
    allowed_roots: list[tuple[str, Path]] = [("workspace", workspace_root)]
    if output_root is not None:
        allowed_roots.append(("output_dir", output_root))
    for kind, root in allowed_roots:
        if path_contains(root, resolved):
            return resolved, kind
    allowed = [str(root) for _, root in allowed_roots]
    raise ValueError(
        f"Path '{path}' is outside the workspace root '{workspace_root}'"
        + (
            f" and output directory '{output_root}'"
            if output_root is not None
            else ""
        )
        + f". Allowed roots: {', '.join(allowed)}"
    )


__all__ = [
    "canonicalize_user_path",
    "path_contains",
    "paths_overlap",
    "resolve_workspace_path",
]
