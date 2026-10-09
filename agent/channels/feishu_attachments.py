"""Leases and capacity for Feishu attachment uploads.

Why this module exists
----------------------
An attachment is cleaned up by whoever ends up owning it, and the ownership has
to survive three ways a turn can end: the batch worker runs it, the coordinator
cancels it before it runs, or the coordinator hands the file over to the
channel.  ``_AttachmentLease`` is the receipt that makes those three
answerable, ``_QueuedAttachment`` pairs a path with its lease, and
``_AttachmentBatchCapacityLease`` reserves a slice of one process-wide
semaphore so a burst of sinks cannot all upload at once.

They were module-level code inside ``agent/channels/feishu.py``, next to 2152
lines of channel and sink.  They are not Feishu-specific: they are about
threads, files and ownership, and they never touch the lark client -- which is
why they can be read and tested without one.

Two things are deliberately *not* here:

- ``_ATTACHMENT_CANCEL_GRACE_SECONDS`` stayed in ``feishu.py``, because the
  sink is what waits on it while cancelling a queued upload.
- The capacity semaphore is defined and read **in this module only**.  A test
  that shrinks the capacity must patch it here; re-exporting it into
  ``feishu.py`` would leave two bindings, and a patch against the wrong one
  would silently change nothing.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


_ATTACHMENT_MAX_IN_FLIGHT = 2

_ATTACHMENT_BATCH_CAPACITY = threading.BoundedSemaphore(
    _ATTACHMENT_MAX_IN_FLIGHT
)
_ATTACHMENT_BATCH_CAPACITY_LOCK = threading.Lock()
_ATTACHMENT_BATCH_CAPACITY_ERROR = "Feishu attachment upload capacity exhausted"

def _cleanup_attachment_path(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
        path.parent.rmdir()
    except OSError:
        logger.exception(
            "deferred Feishu attachment cleanup failed: attachment=%s",
            path,
        )

class _AttachmentLease:
    """Opaque cleanup receipt shared by the coordinator and batch worker."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._state = "pending"
        self._cleanup_owned = False
        self._lock = threading.Lock()

    def run(self, operation: Callable[[Path], None]) -> bool:
        with self._lock:
            if self._state == "cancelled":
                return False
            if self._state != "pending":
                raise RuntimeError("attachment lease is not pending")
            self._state = "running"
        try:
            operation(self.path)
        finally:
            self._finish()
        return True

    def cancel_pending(self) -> None:
        cleanup = False
        with self._lock:
            if self._state == "pending":
                self._state = "cancelled"
                cleanup = self._cleanup_owned
        if cleanup:
            _cleanup_attachment_path(self.path)

    def transfer_cleanup(self) -> bool:
        cleanup = False
        with self._lock:
            if self._cleanup_owned:
                return True
            self._cleanup_owned = True
            cleanup = self._state in ("cancelled", "finished")
        if cleanup:
            _cleanup_attachment_path(self.path)
        return True

    def _finish(self) -> None:
        cleanup = False
        with self._lock:
            self._state = "finished"
            cleanup = self._cleanup_owned
        if cleanup:
            _cleanup_attachment_path(self.path)

@dataclass(frozen=True)
class _QueuedAttachment:
    path: Path
    lease: _AttachmentLease

class _AttachmentBatchCapacityLease:
    """A fixed-size, idempotently released reservation of global capacity."""

    def __init__(
        self,
        capacity: threading.BoundedSemaphore,
        size: int,
    ) -> None:
        self._capacity = capacity
        self._size = size
        self._released = False
        self._lock = threading.Lock()

    @classmethod
    def try_acquire(cls, size: int) -> "_AttachmentBatchCapacityLease | None":
        capacity = _ATTACHMENT_BATCH_CAPACITY
        with _ATTACHMENT_BATCH_CAPACITY_LOCK:
            acquired = 0
            for _index in range(size):
                if capacity.acquire(blocking=False):
                    acquired += 1
                    continue
                for _rollback in range(acquired):
                    capacity.release()
                return None
        return cls(capacity, acquired)

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
            with _ATTACHMENT_BATCH_CAPACITY_LOCK:
                for _index in range(self._size):
                    self._capacity.release()
