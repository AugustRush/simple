from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from .models import DeliveryResult, ExecutionResult
from .store import SchedulerStore


UTC = timezone.utc


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

    async def run_once(self, now: Optional[datetime] = None) -> int:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self.store.disable_duplicate_enabled_tasks(current)
        claimed = self.store.claim_due_tasks(
            now=current,
            limit=10,
            lease_seconds=self.lease_seconds,
        )
        if claimed:
            sem = asyncio.Semaphore(self.max_concurrent_runs)

            async def _run_item(item) -> None:
                async with sem:
                    await self._execute_claimed(item.task, item.run)

            await asyncio.gather(*[_run_item(item) for item in claimed])
        return len(claimed)

    async def run_forever(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self.poll_seconds)

    async def _renew_lease(self, task, run, lost_ownership: asyncio.Event, now) -> None:
        interval = self.lease_seconds / 3
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = self.store.renew_lease(
                    task.id,
                    run.id,
                    now=now(),
                    lease_seconds=self.lease_seconds,
                )
            except Exception:
                renewed = False
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

    def _complete_interrupted(self, task, run, now, reason: str) -> None:
        self.store.complete_run(
            task.id,
            run.id,
            finished_at=now(),
            status="interrupted",
            error=reason,
        )

    def _owns_unexpired_lease(self, task, run, now) -> bool:
        try:
            return self.store.owns_unexpired_lease(task.id, run.id, now=now())
        except Exception:
            return False

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
        result: Optional[ExecutionResult] = None
        try:
            if task.kind == "agent_prompt":
                execution = self.agent_executor(task, run)
            elif task.kind == "message":
                text = str(task.payload.get("message_text", "")).strip()
                if not text:
                    raise ValueError("Message task has no message_text")
                async def message_result():
                    return ExecutionResult(summary=text, text_output=text)

                execution = message_result()
            elif task.kind == "system_job":
                execution = self.system_executor(task, run)
            else:
                raise ValueError(f"Unsupported task kind: {task.kind}")

            result = await self._await_while_owned(execution, lost_ownership)
            if lost_ownership.is_set() or not self._owns_unexpired_lease(
                task, run, run_now
            ):
                self._complete_interrupted(
                    task, run, run_now, "scheduler lease ownership lost before delivery"
                )
                return

            delivery_result = await self._await_while_owned(
                self._deliver(task, run, result), lost_ownership
            )
            if lost_ownership.is_set() or not self._owns_unexpired_lease(
                task, run, run_now
            ):
                self._complete_interrupted(
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
            status = "succeeded" if successful_delivery else "failed"
            if not successful_delivery and not delivery_error:
                delivery_error = f"unexpected delivery status: {delivery_status or 'empty'}"
            self.store.complete_run(
                task.id,
                run.id,
                finished_at=run_now(),
                status=status,
                summary=result.summary,
                error=delivery_error,
                output_path=output_path,
                delivery_status=delivery_status,
            )
        except Exception as exc:
            status = (
                "interrupted"
                if lost_ownership.is_set()
                or "scheduler lease ownership lost" in str(exc)
                else "failed"
            )
            self.store.complete_run(
                task.id,
                run.id,
                finished_at=run_now(),
                status=status,
                error=str(exc),
                output_path=result.output_path if result is not None else "",
            )
        finally:
            renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewal

    async def _deliver(self, task, run, result: ExecutionResult):
        if callable(self.delivery):
            return await self.delivery(task, run, result)
        return await self.delivery.deliver(
            task_id=task.id,
            run_id=run.id,
            delivery_mode=task.delivery_mode,
            target=task.delivery_target,
            text=result.text_output,
        )
