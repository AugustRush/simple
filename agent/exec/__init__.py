"""Execution capabilities: where and how the agent runs external work.

Today this is child processes on the local machine
(:mod:`agent.exec.subprocess`).  The package exists so that "run this" can
later mean a container or a remote worker without the callers changing.
"""

from __future__ import annotations

from agent.exec.subprocess import (
    ExecRequest,
    ExecResult,
    LocalSubprocessProvider,
    SubprocessProvider,
    provider_from,
)

__all__ = [
    "ExecRequest",
    "ExecResult",
    "LocalSubprocessProvider",
    "SubprocessProvider",
    "provider_from",
]
