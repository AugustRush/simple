"""Background thread that drains queued memory jobs while the user is idle."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import Any, Callable, Optional

from agent import shared

from .context import ContextManager

class BackgroundMemoryWorker:
    """Background thread that processes queued memory jobs during prompt idle time."""

    def __init__(
        self,
        ctx_mgr: ContextManager,
        client: Any,
        model: str,
        api_format: str,
        poll_seconds: float = 1.0,
        client_factory: Optional[Callable[[], Any]] = None,
    ):
        self.ctx_mgr = ctx_mgr
        self.client = client
        self.model = model
        self.api_format = api_format
        self.poll_seconds = poll_seconds
        self.client_factory = client_factory
        self._stop_event = threading.Event()
        # _wake_event lets callers interrupt the poll sleep and trigger an
        # immediate (idle-gate-bypassing) consolidation run without blocking
        # the main asyncio event loop.
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._active_loop: Optional[asyncio.AbstractEventLoop] = None
        self._active_task: Optional[asyncio.Task[Any]] = None
        self._state_lock = threading.Lock()

    def start(self) -> None:
        """Start the worker thread once."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="background-memory-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the worker thread to stop."""
        self._stop_event.set()
        self._wake_event.set()  # unblock any ongoing wait() immediately
        with self._state_lock:
            loop = self._active_loop
            task = self._active_task
        if loop is not None and task is not None and not task.done():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)

    def wake(self) -> None:
        """Interrupt the poll sleep so the worker runs its next job immediately.

        Safe to call from any thread or coroutine.  Does not block.
        When woken the worker bypasses the idle-seconds gate so consolidation
        happens right away rather than waiting up to idle_seconds for the next
        natural trigger.
        """
        self._wake_event.set()

    async def wait(self) -> None:
        """Wait for the worker thread to exit in async call sites."""
        if self._thread:
            await asyncio.to_thread(self._thread.join)

    async def _process_job(self, client: Any) -> bool:
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(
            self.ctx_mgr.process_one_job(
                client,
                self.model,
                api_format=self.api_format,
            )
        )
        with self._state_lock:
            self._active_loop = loop
            self._active_task = task
        try:
            return await task
        finally:
            with self._state_lock:
                self._active_loop = None
                self._active_task = None

    def _run(self) -> None:
        client = self.client_factory() if self.client_factory else self.client
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while not self._stop_event.is_set():
                # Consume the wake signal before deciding whether to run, so a
                # signal arriving while a job is already running is not lost.
                on_demand = self._wake_event.is_set()
                self._wake_event.clear()
                try:
                    while not self._stop_event.is_set():
                        # on_demand bypasses the idle gate: once explicitly
                        # woken, drain the currently available queue rather than
                        # processing only a single job and falling back to the
                        # idle gate for the rest.
                        should_run = (
                            on_demand or self.ctx_mgr.should_process_jobs()
                        )
                        if not should_run:
                            break
                        processed = loop.run_until_complete(self._process_job(client))
                        if not processed:
                            break
                        if not on_demand:
                            continue
                except asyncio.CancelledError:
                    if not self._stop_event.is_set():
                        shared.CONSOLE.print(
                            "[dim]Background consolidation cancelled unexpectedly[/dim]"
                        )
                except Exception as e:
                    shared.CONSOLE.print(f"[dim]Background consolidation error: {e}[/dim]")
                # Sleep for poll_seconds OR until wake()/stop() interrupts,
                # whichever comes first.  This replaces the old _stop_event.wait()
                # so that wake() can also cut the sleep short.
                self._wake_event.wait(timeout=self.poll_seconds)
        finally:
            aclose = getattr(client, "aclose", None)
            if self.client_factory and callable(aclose):
                try:
                    loop.run_until_complete(aclose())
                except Exception:
                    pass
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
