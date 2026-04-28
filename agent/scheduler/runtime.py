from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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
        self.lease_seconds = lease_seconds
        self.max_concurrent_runs = max(1, int(max_concurrent_runs))

    async def run_once(self, now: Optional[datetime] = None) -> int:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        self.store.disable_duplicate_enabled_tasks(current)
        self.store.recover_stale_runs(current)
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

    async def _execute_claimed(self, task, run) -> None:
        try:
            if task.kind == "agent_prompt":
                result = await self.agent_executor(task, run)
            elif task.kind == "message":
                text = str(task.payload.get("message_text", "")).strip()
                if not text:
                    raise ValueError("Message task has no message_text")
                result = ExecutionResult(summary=text, text_output=text)
            elif task.kind == "system_job":
                result = await self.system_executor(task, run)
            else:
                raise ValueError(f"Unsupported task kind: {task.kind}")
            delivery_result = await self._deliver(task, run, result)
            output_path = result.output_path
            delivery_status = ""
            if isinstance(delivery_result, DeliveryResult):
                delivery_status = delivery_result.status
                output_path = delivery_result.output_path or output_path
            elif isinstance(delivery_result, str):
                delivery_status = delivery_result
            self.store.complete_run(
                task.id,
                run.id,
                finished_at=datetime.now(UTC),
                status="succeeded",
                summary=result.summary,
                output_path=output_path,
                delivery_status=delivery_status,
            )
        except Exception as exc:
            self.store.complete_run(
                task.id,
                run.id,
                finished_at=datetime.now(UTC),
                status="failed",
                error=str(exc),
            )

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
