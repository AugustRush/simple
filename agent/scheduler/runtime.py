from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from agent import shared
from agent.verification import (
    VERDICT_FAILED,
    VERDICT_NONE,
    VERDICT_UNKNOWN,
    CommandVerifier,
    VerificationResult,
    combine_verdicts,
)

from .models import (
    DEFAULT_SIGNAL_MAX_DEPTH,
    RETRYABLE_RUN_STATUSES,
    RUN_SUCCESS_STATUS,
    RUN_UNVERIFIED_STATUS,
    Acceptance,
    DeliveryResult,
    DeliveryTarget,
    ExecutionResult,
    describe_missed_occurrences,
)
from .store import SchedulerStore


UTC = timezone.utc
logger = logging.getLogger(__name__)


class SchedulerService:
    def __init__(
        self,
        *,
        store: SchedulerStore,
        agent_executor: Callable[..., Awaitable[ExecutionResult]],
        system_executor: Callable[..., Awaitable[ExecutionResult]],
        delivery: Any,
        poll_seconds: float = 30.0,
        lease_seconds: int = 300,
        max_concurrent_runs: int = 3,
        signal_max_depth: int = DEFAULT_SIGNAL_MAX_DEPTH,
    ):
        self.store = store
        self.agent_executor = agent_executor
        self.system_executor = system_executor
        self.delivery = delivery
        self.poll_seconds = poll_seconds
        self.lease_seconds = int(lease_seconds)
        if self.lease_seconds < 3:
            raise ValueError("lease_seconds must be at least 3")
        self.max_concurrent_runs = max(1, int(max_concurrent_runs))
        self.signal_max_depth = max(0, int(signal_max_depth))
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._cancel_requested: set[str] = set()
        self._started_at = datetime.now(UTC)
        self._last_heartbeat = self._started_at
        self._running = False

    async def _store_call(self, method_name: str, *args, **kwargs):
        """Run SQLite work off-loop using a connection owned by that thread."""
        if type(self.store) is not SchedulerStore:
            operation = asyncio.create_task(
                asyncio.to_thread(getattr(self.store, method_name), *args, **kwargs)
            )
        else:
            def invoke():
                thread_store = SchedulerStore(db_path=self.store.db_path)
                try:
                    return getattr(thread_store, method_name)(*args, **kwargs)
                finally:
                    thread_store.close()

            operation = asyncio.create_task(asyncio.to_thread(invoke))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # A thread cannot be cancelled. Wait for it so a claim/complete
            # cannot commit after the scheduler has reported itself stopped.
            with contextlib.suppress(Exception):
                await operation
            raise

    async def run_once(self, now: Optional[datetime] = None) -> int:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        await self._store_call("disable_duplicate_enabled_tasks", current)
        # Before claiming, so a signal queued a moment ago becomes a run in
        # this same iteration instead of waiting out another poll interval.
        # It also means a signal emitted by a run in the previous iteration --
        # or by an agent tool call between iterations -- is acted on here,
        # which is what keeps a cascade costing one poll per hop rather than
        # one poll per hop plus a delivery cycle.
        try:
            await self._store_call(
                "deliver_signals", now=current, max_depth=self.signal_max_depth
            )
        except Exception:
            # Delivery is idempotent and every emission is on disk, so a
            # failure here defers the signal to the next poll rather than
            # losing it.  Claiming clock-scheduled work must not be held
            # hostage to that.
            logger.exception("Signal delivery failed; emissions remain pending")
        claim_operation = asyncio.create_task(
            self._store_call(
                "claim_due_tasks",
                now=current,
                limit=10,
                lease_seconds=self.lease_seconds,
            )
        )
        try:
            claimed = await asyncio.shield(claim_operation)
        except asyncio.CancelledError as cancelled:
            claimed = await self._await_store_completion(claim_operation)
            cleanup = asyncio.gather(
                *(
                    self._store_call(
                        "release_claim",
                        item.task.id,
                        item.run.id,
                        now=datetime.now(UTC),
                        reason="scheduler stopped after claiming task",
                    )
                    for item in claimed
                )
            )
            await self._await_store_completion(cleanup)
            raise cancelled
        if claimed:
            sem = asyncio.Semaphore(self.max_concurrent_runs)

            async def _run_item(item) -> None:
                async with sem:
                    await self._execute_claimed(item.task, item.run)

            await asyncio.gather(*[_run_item(item) for item in claimed])
        return len(claimed)

    @staticmethod
    async def _await_store_completion(operation: asyncio.Future):
        while True:
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                continue

    async def run_forever(self) -> None:
        self._running = True
        try:
            while True:
                self._last_heartbeat = datetime.now(UTC)
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Scheduler iteration failed; retrying after poll interval")
                self._last_heartbeat = datetime.now(UTC)
                await asyncio.sleep(self.poll_seconds)
        finally:
            self._running = False

    def health(self) -> dict[str, Any]:
        return {
            "status": "online" if self._running else "offline",
            "started_at": self._started_at.isoformat(),
            "last_heartbeat": self._last_heartbeat.isoformat(),
            "active_runs": len(self._active_tasks),
            "poll_seconds": self.poll_seconds,
            "max_concurrent_runs": self.max_concurrent_runs,
            "signal_max_depth": self.signal_max_depth,
        }

    async def shutdown(self) -> None:
        pending = [task for task in self._background_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _start_background_claim(self, claimed) -> None:
        operation = asyncio.create_task(self._execute_claimed(claimed.task, claimed.run))
        self._active_tasks[claimed.run.id] = operation
        self._background_tasks.add(operation)
        operation.add_done_callback(self._background_tasks.discard)

    async def run_task_now(self, task_id: str):
        claimed = await self._store_call(
            "claim_task_now",
            task_id,
            now=datetime.now(UTC),
            lease_seconds=self.lease_seconds,
        )
        if claimed is not None:
            self._start_background_claim(claimed)
        return claimed

    async def retry_run(self, task_id: str, run_id: str, *, use_latest: bool = False):
        claimed = await self._store_call(
            "claim_retry",
            task_id,
            run_id,
            use_latest=use_latest,
            now=datetime.now(UTC),
            lease_seconds=self.lease_seconds,
        )
        if claimed is not None:
            self._start_background_claim(claimed)
        return claimed

    async def cancel_run(self, task_id: str, run_id: str) -> bool:
        operation = self._active_tasks.get(run_id)
        if operation is None or operation.done():
            return False
        requested = await self._store_call(
            "request_cancel", task_id, run_id, now=datetime.now(UTC)
        )
        if not requested:
            return False
        self._cancel_requested.add(run_id)
        operation.cancel()
        return True

    async def _renew_lease(self, task, run, lost_ownership: asyncio.Event, now) -> None:
        interval = self.lease_seconds / 3
        # Three consecutive failures span ~one full lease period (interval is
        # lease_seconds/3), by which point the lease has genuinely expired even
        # if the store was only transiently unavailable.
        consecutive_failures = 0
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._store_call(
                    "renew_lease",
                    task.id,
                    run.id,
                    now=now(),
                    lease_seconds=self.lease_seconds,
                )
            except Exception as exc:
                # A transient store error is not "lost ownership" — retrying on
                # the next tick must not interrupt a job whose lease is intact.
                consecutive_failures += 1
                logger.warning(
                    "lease renewal failed (attempt %d): %s",
                    consecutive_failures,
                    exc,
                )
                if consecutive_failures >= 3:
                    lost_ownership.set()
                    return
                continue
            consecutive_failures = 0
            if not renewed:
                lost_ownership.set()
                return

    async def _await_while_owned(self, awaitable, lost_ownership: asyncio.Event):
        operation = asyncio.create_task(awaitable)
        ownership_waiter = asyncio.create_task(lost_ownership.wait())
        try:
            done, _ = await asyncio.wait(
                {operation, ownership_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ownership_waiter in done and lost_ownership.is_set() and not operation.done():
                operation.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await operation
                raise RuntimeError("scheduler lease ownership lost")
            return await operation
        finally:
            ownership_waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ownership_waiter

    async def _complete_interrupted(self, task, run, now, reason: str) -> None:
        await self._store_call(
            "complete_run",
            task.id,
            run.id,
            finished_at=now(),
            status="interrupted",
            error=reason,
        )

    async def _enqueue_automatic_retry(self, task, run, finished_at: datetime) -> None:
        snapshot = dict(getattr(run, "config_snapshot", {}) or {})
        retry_policy = snapshot.get("retry_policy")
        if not isinstance(retry_policy, dict):
            retry_policy = getattr(task, "retry_policy", {}) or {}
        max_attempts = max(1, int(retry_policy.get("max_attempts", 1) or 1))
        attempt = max(1, int(getattr(run, "attempt", 1) or 1))
        if attempt >= max_attempts:
            return
        base_delay = max(0, int(retry_policy.get("backoff_seconds", 30) or 0))
        retry_at = finished_at + timedelta(
            seconds=base_delay * (2 ** max(0, attempt - 1))
        )
        await self._store_call(
            "enqueue_retry",
            task.id,
            run.id,
            retry_at=retry_at,
        )

    async def _owns_unexpired_lease(self, task, run, now) -> bool:
        try:
            return await self._store_call(
                "owns_unexpired_lease", task.id, run.id, now=now()
            )
        except Exception:
            return False

    def _acceptance_for(self, task, run) -> Acceptance:
        """The criterion this execution is judged by.

        Read from the run's own snapshot rather than from the task, because
        that is what makes the judgement stable: editing a task while one of
        its runs is in flight must not change what that run is measured
        against halfway through.
        """
        snapshot = dict(getattr(run, "config_snapshot", {}) or {})
        raw = snapshot.get("acceptance")
        if raw is None:
            return getattr(task, "acceptance", None) or Acceptance()
        return Acceptance.from_dict(raw)

    async def _evaluate_acceptance(
        self, task, run, result: ExecutionResult
    ) -> tuple[str, Optional[VerificationResult]]:
        """Decide what the work was worth, from every source that has a say.

        Two sources, and the rule between them is asymmetric: either can fail
        the run, and both must pass for it to pass.  A self-report is weaker
        evidence than a command, so it can only ever *lower* the verdict -- an
        agent saying "done" must never stand in for a check that could not run.
        """
        acceptance = self._acceptance_for(task, run)
        self_verdict = str(getattr(result, "self_report_verdict", "") or "")
        command = str(acceptance.verify_command or "").strip()
        if not command:
            return combine_verdicts(self_verdict), None
        snapshot = dict(getattr(run, "config_snapshot", {}) or {})
        workspace = str(
            snapshot.get("workspace_root") or getattr(task, "workspace_root", "") or ""
        ).strip()
        verifier = CommandVerifier(
            workspace_root=workspace or Path.cwd(),
            # The same root the criterion was validated against when the task
            # was written.  A narrower one here would reject at 3am a command
            # that was accepted while somebody was still looking at the screen.
            output_dir=shared.DEFAULT_OUTPUT_DIR,
        )
        verification = await verifier.verify(command)
        return combine_verdicts(self_verdict, verification.verdict), verification

    @staticmethod
    def _status_for(verdict: str, delivered: bool) -> str:
        """The run's outcome, from the verdict and whether the result arrived.

        A known problem in either axis is a failure: work that did not meet its
        bar, and a result that did not arrive, both mean the run did not
        achieve what it was for.  ``unverified`` is reserved for the genuinely
        unknown case -- a criterion that could not be evaluated with nothing
        else wrong -- because calling that ``failed`` would assert something
        nobody observed.
        """
        if verdict == VERDICT_FAILED or not delivered:
            return "failed"
        if verdict == VERDICT_UNKNOWN:
            return RUN_UNVERIFIED_STATUS
        return RUN_SUCCESS_STATUS

    async def _execute_claimed(self, task, run) -> None:
        loop = asyncio.get_running_loop()
        monotonic_start = loop.time()

        def run_now() -> datetime:
            elapsed = max(0.0, loop.time() - monotonic_start)
            return run.started_at.astimezone(UTC) + timedelta(seconds=elapsed)

        lost_ownership = asyncio.Event()
        renewal = asyncio.create_task(
            self._renew_lease(task, run, lost_ownership, run_now)
        )
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_tasks[run.id] = current_task
        result: Optional[ExecutionResult] = None
        try:
            snapshot = dict(getattr(run, "config_snapshot", {}) or {})
            kind = str(snapshot.get("kind") or task.kind)
            payload = snapshot.get("payload")
            if not isinstance(payload, dict):
                payload = getattr(task, "payload", {})
            if kind == "agent_prompt":
                execution = self.agent_executor(task, run)
            elif kind == "message":
                text = str(payload.get("message_text", "")).strip()
                if not text:
                    raise ValueError("Message task has no message_text")
                async def message_result():
                    return ExecutionResult(summary=text, text_output=text)

                execution = message_result()
            elif kind == "system_job":
                execution = self.system_executor(task, run)
            else:
                raise ValueError(f"Unsupported task kind: {kind}")

            timeout_seconds = int(
                snapshot.get("timeout_seconds")
                or getattr(task, "timeout_seconds", 1800)
                or 1800
            )
            try:
                result = await asyncio.wait_for(
                    self._await_while_owned(execution, lost_ownership),
                    timeout=max(1, timeout_seconds),
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"scheduled task timed out after {timeout_seconds} seconds"
                ) from exc
            if lost_ownership.is_set() or not await self._owns_unexpired_lease(
                task, run, run_now
            ):
                await self._complete_interrupted(
                    task, run, run_now, "scheduler lease ownership lost before delivery"
                )
                return

            delivery_result = await self._await_while_owned(
                self._deliver(task, run, result), lost_ownership
            )
            if lost_ownership.is_set() or not await self._owns_unexpired_lease(
                task, run, run_now
            ):
                await self._complete_interrupted(
                    task, run, run_now, "scheduler lease ownership lost during delivery"
                )
                return
            output_path = result.output_path
            delivery_status = ""
            delivery_error = ""
            if isinstance(delivery_result, DeliveryResult):
                delivery_status = delivery_result.status
                output_path = delivery_result.output_path or output_path
                delivery_error = delivery_result.error
            elif isinstance(delivery_result, str):
                delivery_status = delivery_result
            else:
                delivery_status = str(delivery_result or "")

            successful_delivery = delivery_status in {"stored", "delivered"}
            if delivery_status == "skipped" and not result.text_output.strip():
                successful_delivery = True
            if not successful_delivery and not delivery_error:
                delivery_error = f"unexpected delivery status: {delivery_status or 'empty'}"
            verdict, verification = await self._evaluate_acceptance(task, run, result)
            status = self._status_for(verdict, successful_delivery)
            # Every reason this run is not a clean success, kept together
            # rather than reduced to one.  A run can be wrong in more than one
            # way at once, and "which of these was it" is a question the record
            # should not have to answer by discarding the others.
            error = "\n".join(
                part
                for part in (
                    str(getattr(result, "self_report_reason", "") or "").strip(),
                    verification.diagnostic() if verification is not None else "",
                    delivery_error,
                )
                if part
            )
            finished_at = run_now()
            # Prefix the summary when this run is the first one after a period
            # in which nothing was running.  The run succeeded, so nothing else
            # about it looks unusual; without this the history reads as a
            # schedule that has been firing on time.
            missed_note = describe_missed_occurrences(
                getattr(run, "missed_count", 0)
            )
            run_summary = (
                f"{missed_note}\n{result.summary}" if missed_note else result.summary
            )
            await self._store_call(
                "complete_run",
                task.id,
                run.id,
                finished_at=finished_at,
                status=status,
                summary=run_summary,
                error=error,
                output_path=output_path,
                delivery_status=delivery_status,
                verdict=verdict,
                verification=verification,
            )
            if status in RETRYABLE_RUN_STATUSES:
                await self._enqueue_automatic_retry(task, run, finished_at)
        except asyncio.CancelledError:
            if run.id in self._cancel_requested:
                await self._store_call(
                    "complete_run",
                    task.id,
                    run.id,
                    finished_at=run_now(),
                    status="cancelled",
                    error="cancelled by user",
                    output_path=result.output_path if result is not None else "",
                )
            else:
                await self._store_call(
                    "release_claim",
                    task.id,
                    run.id,
                    now=run_now(),
                    reason="scheduler stopped",
                )
            raise
        except Exception as exc:
            status = (
                "interrupted"
                if lost_ownership.is_set()
                or "scheduler lease ownership lost" in str(exc)
                else "failed"
            )
            finished_at = run_now()
            await self._store_call(
                "complete_run",
                task.id,
                run.id,
                finished_at=finished_at,
                status=status,
                error=str(exc),
                output_path=result.output_path if result is not None else "",
            )
            if status in RETRYABLE_RUN_STATUSES:
                await self._enqueue_automatic_retry(task, run, finished_at)
        finally:
            self._active_tasks.pop(run.id, None)
            self._cancel_requested.discard(run.id)
            renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewal

    async def _deliver(self, task, run, result: ExecutionResult):
        if callable(self.delivery):
            return await self.delivery(task, run, result)
        snapshot = dict(getattr(run, "config_snapshot", {}) or {})
        delivery_mode = str(snapshot.get("delivery_mode") or task.delivery_mode)
        target = task.delivery_target
        raw_target = snapshot.get("delivery_target")
        if isinstance(raw_target, dict) and raw_target.get("target_type"):
            target = DeliveryTarget(
                target_type=str(raw_target["target_type"]),
                payload=dict(raw_target.get("payload") or {}),
            )
        return await self.delivery.deliver(
            task_id=task.id,
            run_id=run.id,
            delivery_mode=delivery_mode,
            target=target,
            text=result.text_output,
        )
