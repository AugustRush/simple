"""Moved to :mod:`agent.kernel.service`.

Kept as a re-export only while the callers are migrated; the service primitive
belongs to the kernel, and this path will be deleted once nothing imports it.  The
re-export exists so the move can be verified as behaviour-preserving -- a test
suite that stays green across the move distinguishes "I broke the service
registry" from "I moved it", which is the distinction every later step depends on.
"""

from __future__ import annotations

from agent.kernel.service import (
    MissingServiceError,
    ServiceRegistry,
    ServiceScope,
    ServiceView,
    require_service,
    resolve,
)

__all__ = [
    "MissingServiceError",
    "ServiceRegistry",
    "ServiceScope",
    "ServiceView",
    "require_service",
    "resolve",
]
