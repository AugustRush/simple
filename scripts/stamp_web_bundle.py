#!/usr/bin/env python3
"""Stamp the committed web bundle with a digest of the sources it was built from.

Why this exists
---------------
``agent/_builtin/web/dist`` is a **committed build product**: the gateway serves that
directory, and a person who installs the package gets the UI that was in the repo.  That
makes "did anybody rebuild after changing ``frontend/src``?" a question with a wrong
answer that nobody can see: the page still loads, still answers every endpoint, and is
simply the previous UI.  During the work that produced this file it happened three times
in one session, and every time the only thing that caught it was somebody remembering.

A digest cannot be fooled by a stale ``checkout`` -- unlike a modification time, which
``git clone`` resets -- and it needs no node to verify, which matters because the check
that consumes it (``tests/test_web_bundle_freshness.py``) runs in the Python suite.

The build writes it, the test recomputes it.  Same module, same function: two
implementations of a hash is one implementation that drifts.

Usage
-----
    python3 scripts/stamp_web_bundle.py            # stamp the existing dist
    python3 scripts/stamp_web_bundle.py --check    # exit 1 if the stamp is stale

``npm run build:release`` runs the first form as its last step, so the stamp is rewritten
exactly when the bundle is.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "frontend"
DIST = REPO_ROOT / "agent" / "_builtin" / "web" / "dist"
STAMP_NAME = ".source-hash"

#: Files the bundle's *content* depends on.  ``src`` is walked whole rather than filtered
#: by suffix: an asset somebody adds under ``src`` changes what the page looks like, and
#: a whitelist would be the next stale-bundle bug.
ROOT_INPUTS = (
    "index.html",
    "package.json",
    "vite.config.ts",
)


def _inputs() -> list[Path]:
    files = [FRONTEND / name for name in ROOT_INPUTS]
    files.extend(sorted(FRONTEND.glob("tsconfig*.json")))
    src = FRONTEND / "src"
    if src.is_dir():
        files.extend(sorted(p for p in src.rglob("*") if p.is_file()))
    return [p for p in files if p.is_file()]


def digest() -> str:
    """A stable digest of everything the bundle is built from."""
    hasher = hashlib.sha256()
    for path in _inputs():
        # The path, then the bytes, each terminated: concatenating raw content would let
        # two different trees hash the same.
        hasher.update(path.relative_to(FRONTEND).as_posix().encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return "sha256:" + hasher.hexdigest()


def stamp_path() -> Path:
    return DIST / STAMP_NAME


def read_stamp() -> str | None:
    path = stamp_path()
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def write_stamp() -> str:
    if not DIST.is_dir() or not any(DIST.iterdir()):
        raise SystemExit(
            f"no bundle to stamp at {DIST} -- build it first "
            "(npm --prefix frontend run build:release)"
        )
    current = digest()
    stamp_path().write_text(current + "\n", encoding="utf-8")
    return current


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the committed bundle against the sources instead of stamping it",
    )
    args = parser.parse_args(argv)

    if not args.check:
        current = write_stamp()
        print(f"stamped {stamp_path().relative_to(REPO_ROOT)} = {current}")
        return 0

    recorded = read_stamp()
    current = digest()
    if recorded is None:
        print(f"no stamp at {stamp_path()} -- the bundle was never recorded", file=sys.stderr)
        return 1
    if recorded != current:
        print(
            "the committed web bundle is stale: frontend/ changed and dist/ was not "
            "rebuilt\n"
            f"  recorded: {recorded}\n"
            f"  sources:  {current}\n"
            "  fix: npm --prefix frontend run build:release",
            file=sys.stderr,
        )
        return 1
    print(f"bundle is current ({current})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
