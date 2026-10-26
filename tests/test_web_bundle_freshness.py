"""The committed web bundle has to be the one the sources produce.

``agent/_builtin/web/dist`` ships in the package: the gateway serves that directory, so
whatever is committed there *is* the UI, whatever ``frontend/src`` says.  A source change
without a rebuild is silent -- the page loads, every endpoint the frontend calls still
exists (``tests/test_web_route_contract.py`` passes), and the UI is simply the previous
one.  During the work that produced this module it happened three times in one session,
caught each time by somebody remembering.

A hash, not a modification time: ``git clone`` resets mtimes, so an mtime check would be
green precisely in the case that matters (a fresh checkout carrying a stale bundle).  The
digest is written by ``scripts/stamp_web_bundle.py`` as ``build:release``'s last step and
recomputed here by the same function -- one implementation, so the two cannot drift.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DIST = REPO_ROOT / "agent" / "_builtin" / "web" / "dist"


def _stamp_module():
    """``scripts/stamp_web_bundle.py``, by path: it is not a package and should not be."""
    path = REPO_ROOT / "scripts" / "stamp_web_bundle.py"
    spec = importlib.util.spec_from_file_location("stamp_web_bundle", path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("stamp_web_bundle", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def stamp():
    module = _stamp_module()
    if not DIST.is_dir() or not any(DIST.iterdir()):
        pytest.skip(
            "no built bundle in this checkout (source-only tree); nothing to compare"
        )
    if module.read_stamp() is None:
        pytest.skip(
            "this bundle predates the stamp; rebuild with "
            "`npm --prefix frontend run build:release` to record one"
        )
    return module


def test_the_committed_bundle_matches_the_sources(stamp) -> None:
    """Change a component, forget the build, serve the old page -- silently, before this."""
    recorded = stamp.read_stamp()
    current = stamp.digest()

    assert recorded == current, (
        "the committed web bundle is stale.\n"
        f"  recorded: {recorded}\n"
        f"  sources:  {current}\n"
        "Rebuild it: npm --prefix frontend run build:release\n"
        "(`frontend/src/**`, index.html, package.json, vite.config.ts and the tsconfigs "
        "are what the digest covers.)"
    )


def test_the_bundle_is_complete_enough_to_serve(stamp) -> None:
    """A digest can match while the tree is truncated -- a partial copy is its own bug."""
    index = DIST / "index.html"
    assert index.is_file(), "the bundle has no index.html, so the gateway serves nothing"

    html = index.read_text(encoding="utf-8")
    assets = sorted(
        name for name in (DIST / "assets").iterdir() if name.is_file()
    ) if (DIST / "assets").is_dir() else []
    assert assets, "the bundle has no assets/ directory"

    # Every asset the page references must be present, by name: a bundle copied without
    # its hashed chunks renders a blank page and still passes a "files exist" check.
    referenced = [
        token.split('"')[0].split("'")[0].rstrip("/")
        for token in html.split("assets/")[1:]
    ]
    for name in referenced:
        assert (DIST / "assets" / name).is_file(), (
            f"index.html references assets/{name}, which the bundle does not contain"
        )
