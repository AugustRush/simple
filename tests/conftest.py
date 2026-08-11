"""Shared test configuration.

Puts the project root on ``sys.path`` and — more importantly — points the
agent's home directory at a throwaway location.

``agent.shared`` derives every path it exports (memory, scheduler DB, config,
plugins, output) from ``SIMPLE_AGENT_HOME`` *at import time*, so the override
has to land before the first ``import agent`` anywhere in the suite.  A
conftest at the rootdir is imported before collection begins, which is early
enough.

Without it the suite reads and writes the developer's real ``~/.agent``: tests
persist scheduler tasks and memory rows there, and the per-home advisory lock
collides with any ``simple gateway`` the developer happens to be running — so
the gateway tests passed or failed depending on whether the daemon was up.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).parent.parent))

_TEST_AGENT_HOME = Path(tempfile.mkdtemp(prefix="simple-test-home-")).resolve()
os.environ["SIMPLE_AGENT_HOME"] = str(_TEST_AGENT_HOME)

# Belt and braces: if something already imported agent.shared before this ran,
# its module-level paths are bound to the real home — rebind them.
import agent.shared as _shared  # noqa: E402

if _shared.AGENT_HOME != _TEST_AGENT_HOME:
    _shared._set_agent_home(_TEST_AGENT_HOME)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TEST_AGENT_HOME, ignore_errors=True)
