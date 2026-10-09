"""The output-directory housekeeping tool.

Why this module exists
----------------------
``clean_output`` is one registration and 33 lines: the smallest cluster in
``BuiltinTools``, which is a 5187-line class.  It is also the only tool whose
registration is *conditional* -- it exists only when the deployment has an
output directory, and ``tests/test_output_dir.py`` asserts both halves of that
(registered with one, absent without).

The condition stays in ``BuiltinTools._register``, beside the other
registrations, because that is where "which tools this deployment has" is
decided.  What moved here is the behaviour: what cleaning *means*.  A target
must stay inside the output directory; ``max_age_hours`` filters by mtime;
files go first and then the directories they emptied; and the result reports
the count plus at most ten per-file errors rather than failing the whole call
because one file was locked.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

from agent.pathing import path_contains


class OutputTools:
    """The output-directory housekeeping tool."""

    def __init__(
        self,
        registry: Any,
        output_dir: Optional[Path],
        ok: Callable[..., dict[str, Any]],
        error: Callable[..., dict[str, Any]],
    ) -> None:
        # Public on purpose: ``ToolRegistry.call`` reads ``fn.__self__.registry``
        # to install the calling registry's merged context on the registry this
        # tool implementation reads.  A cluster that hid it would silently stop
        # receiving a session's own write scope, workspace and output dir.
        self.registry = registry
        self._output_dir = output_dir
        self._ok = ok
        self._error = error

    def clean(
        self, max_age_hours: float = 0, subdir: str = ""
    ) -> dict[str, Any]:
        if self._output_dir is None:
            return self._error("Output directory not configured")
        output_dir = self._output_dir.expanduser().resolve(strict=False)
        target = (output_dir / subdir).resolve(strict=False) if subdir else output_dir
        if not path_contains(output_dir, target):
            return self._error("subdir must stay inside the output directory")
        if not target.is_dir():
            return self._ok(deleted=0, message=f"Directory does not exist: {target}")
        now = time.time()
        deleted = 0
        errors: list[str] = []
        for f in target.rglob("*"):
            if not f.is_file():
                continue
            if max_age_hours > 0 and (now - f.stat().st_mtime) < max_age_hours * 3600:
                continue
            try:
                f.unlink()
                deleted += 1
            except Exception as e:
                errors.append(f"{f.name}: {e}")
        for d in sorted((d for d in target.rglob("*") if d.is_dir()), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
        result = self._ok(deleted=deleted, target=str(target))
        if errors:
            result["errors"] = errors[:10]
        return result
