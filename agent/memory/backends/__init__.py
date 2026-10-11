"""Alternative memory backends, each satisfying ``agent.memory.contracts``.

A backend here is an implementation of a *service*, not a fork of the agent: it
provides ``ltm_store`` (and, when it wants to own the whole memory system, the
``memory`` palace over it), and it is selected by a profile's
``service_replacements`` rather than by editing the composition.

``sqlite_fts`` is the one that exists, and it exists for a reason that is not
"another store would be nice": a contract nobody has implemented twice is a
description of one implementation.  The built-in store passes
``tests/test_memory_contract.py::run_store_contract``; so does this one, and that
is the evidence behind the word "replaceable".
"""

from __future__ import annotations

from .sqlite_fts import SqliteFtsMemoryPlugin, SqliteFtsStore

__all__ = ["SqliteFtsMemoryPlugin", "SqliteFtsStore"]
