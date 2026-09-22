"""Measure what one concurrent scheduler run costs, before touching the cap.

Task 5 of ``docs/superpowers/plans/2026-09-22-scheduler-concurrency.md`` is
gated on measurement, not on theory: every run builds its own components
(``agent/cli.py:819-823`` -> ``agent.bootstrap._build_components_async``), so
``max_concurrent_runs`` multiplies memory, file descriptors and provider
request rate rather than only parallelism.  Raising the default without knowing
which of those binds first would be guessing.

Usage::

    python scripts/measure_scheduler_run_cost.py 1 3 5 8

Each concurrency level runs in its own child process, because peak RSS is a
high-water mark: measuring three levels in one process would report the first
level's peak for all of them.  The reported figure is what the *whole process*
reached after building ``n`` sets of components concurrently, so it includes the
interpreter and the agent package -- compare the levels against each other and
against ``n=0``, not against zero.

``mcp_servers`` is cleared before measuring.  A configured MCP server is
connected *per run*, which means one child process per run on top of everything
below -- a real cost, but a different one, and leaving the placeholder entry in
``config.example.json`` in place would make this script measure ``npx``
start-up instead of the component build.
"""

from __future__ import annotations

import asyncio
import json
import resource
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def peak_rss_mb() -> float:
    # ru_maxrss is bytes on macOS and kilobytes on Linux.
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return raw / divisor


async def build_one(cfg: dict, resource_home: Path):
    from agent import _build_components_async

    return await _build_components_async(
        dict(cfg), announce=False, resource_home=resource_home
    )


async def measure(concurrency: int) -> dict:
    import json as _json

    from agent import shared

    cfg = _json.loads((REPO / "config.example.json").read_text())
    cfg["mcp_servers"] = []
    baseline = peak_rss_mb()
    started = time.perf_counter()
    try:
        await asyncio.gather(
            *[build_one(cfg, shared.AGENT_HOME) for _ in range(concurrency)]
        )
    except Exception as exc:  # pragma: no cover - reported, not raised
        return {"concurrency": concurrency, "error": f"{type(exc).__name__}: {exc}"}
    elapsed = time.perf_counter() - started
    return {
        "concurrency": concurrency,
        "seconds": round(elapsed, 3),
        "seconds_each": round(elapsed / concurrency, 3),
        "baseline_rss_mb": round(baseline, 1),
        "peak_rss_mb": round(peak_rss_mb(), 1),
        "rss_per_run_mb": round((peak_rss_mb() - baseline) / concurrency, 1),
    }


def main() -> int:
    levels = [int(item) for item in sys.argv[1:]] or [0, 1, 3, 5, 8]
    for level in levels:
        if level == 0:
            # The floor: the interpreter with the agent package imported and no
            # components built at all.
            result = subprocess.run(
                [sys.executable, "-c", "import agent; print('ok')"],
                cwd=REPO,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(result.stderr.strip()[-400:])
                return 1
            print(json.dumps({"concurrency": 0, "note": "import floor only"}))
            continue
        result = subprocess.run(
            [sys.executable, __file__, "--child", str(level)],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        if line.startswith("{"):
            print(line)
        else:
            print(
                json.dumps(
                    {
                        "concurrency": level,
                        "error": (result.stderr.strip() or "no output")[-400:],
                    }
                )
            )
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--child":
        print(json.dumps(asyncio.run(measure(int(sys.argv[2])))))
    else:
        raise SystemExit(main())
