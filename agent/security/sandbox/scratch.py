"""Private scratch directories used as a sandboxed child's TMPDIR.

Separate from policy and from any backend: every backend points the child's
TMPDIR at one of these, and the lifecycle (create, release, reclaim after a
hard kill) is the same regardless of which one enforces the boundary.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
import shutil
import time
import uuid

#: A scratch dir left behind by a hard kill is reclaimed once it is older
#: than this.  Generous compared with the shell tool's own timeout, so a
#: long-running command can never have its TMPDIR swept out from under it.
_SCRATCH_MAX_AGE_SECONDS = 6 * 3600


def new_scratch_dir(output_root: Path) -> Path:
    """Create a private scratch directory to use as the child's TMPDIR.

    Also reclaims stale siblings.  Callers pair this with
    :func:`release_scratch_dir` for the normal path, but a SIGKILL skips
    every ``finally`` in the process — so the age sweep here is what keeps
    the directory from growing without bound across crashes.
    """
    scratch_root = output_root / "sandbox"
    scratch_root.mkdir(parents=True, exist_ok=True)
    reclaim_stale_scratch_dirs(scratch_root)
    scratch = scratch_root / f"tmp-{uuid.uuid4().hex[:12]}"
    scratch.mkdir()
    return scratch


def release_scratch_dir(scratch: Path | None) -> None:
    """Remove a scratch directory once its command has finished.

    Every shell call gets a *fresh* scratch dir, so nothing can legitimately
    depend on its contents surviving the call that created it.
    """
    if scratch is None:
        return
    with contextlib.suppress(OSError):
        shutil.rmtree(scratch, ignore_errors=True)


def reclaim_stale_scratch_dirs(
    scratch_root: Path,
    *,
    max_age_seconds: float = _SCRATCH_MAX_AGE_SECONDS,
    now: float | None = None,
) -> int:
    """Delete ``tmp-*`` scratch dirs older than *max_age_seconds*."""
    current = time.time() if now is None else now
    reclaimed = 0
    with contextlib.suppress(OSError):
        for entry in scratch_root.iterdir():
            if not entry.name.startswith("tmp-") or not entry.is_dir():
                continue
            try:
                age = current - entry.stat().st_mtime
            except OSError:
                continue
            if age <= max_age_seconds:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                reclaimed += 1
    return reclaimed


__all__ = [
    "new_scratch_dir",
    "reclaim_stale_scratch_dirs",
    "release_scratch_dir",
]
