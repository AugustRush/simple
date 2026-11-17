from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from agent import shared
from .delivery_targets import DeliveryTargetRegistry, shared_registry
from .models import DeliveryResult, DeliveryTarget


class SchedulerDelivery:
    def __init__(
        self,
        cfg: dict,
        output_root: Optional[Path] = None,
        targets: Optional[DeliveryTargetRegistry] = None,
    ):
        self.cfg = cfg
        self.output_root = output_root or (shared.DEFAULT_OUTPUT_DIR / "scheduler")
        # Taken, not built: a composition that registered a target type must be
        # the same registry the runs read, and the process-wide default is what
        # the two callers with no composition get.  A registry built here would
        # be a second answer to "what can this install deliver to".
        self.targets = targets if targets is not None else shared_registry()

    async def deliver_standalone(
        self, task_id: str, run_id: str, text: str
    ) -> DeliveryResult:
        if not str(text or "").strip():
            return DeliveryResult(status="skipped")
        output_dir = self.output_root / task_id
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{run_id}.md"
        shared._atomic_write_text(path, text)
        return DeliveryResult(status="stored", output_path=str(path))

    async def deliver_channel(
        self,
        *,
        target: DeliveryTarget,
        text: str,
        output_dir: Optional[Path] = None,
    ) -> str:
        """Hand *text* to whichever sender claims this target type.

        The lookup is the whole change: this used to be
        ``if target.target_type != "feishu_chat": raise``, so the only way to
        deliver anywhere else was to edit this file. The refusal now names the
        types that *are* available, because "Unsupported channel target: slack"
        leaves the reader guessing whether the name is wrong or the capability
        is missing -- and those want different fixes.
        """
        sender = self.targets.sender_for(target.target_type)
        if sender is None:
            available = ", ".join(self.targets.types()) or "(none registered)"
            raise ValueError(
                f"Unsupported channel target: {target.target_type!r}; "
                f"this install can deliver to: {available}"
            )
        return await sender(
            target=target,
            text=text,
            cfg=self.cfg,
            output_dir=output_dir,
        )

    async def deliver(
        self,
        *,
        task_id: str,
        run_id: str,
        delivery_mode: str,
        target: DeliveryTarget,
        text: str,
        output_dir: Optional[Path] = None,
        max_retries: int = 3,
    ) -> DeliveryResult:
        if delivery_mode not in ("standalone", "channel"):
            # Refused here rather than inside the retry loop below: a mode
            # nothing can deliver is not a mode that waiting five seconds
            # will make deliverable.
            raise ValueError(f"Unsupported delivery mode: {delivery_mode}")
        # Written down first, whichever mode this is.  What a run produced is a
        # fact about the run; sending it to a chat is an extra action on top of
        # that fact.  Only the standalone mode used to persist it, so a step
        # that notified a channel left nothing behind -- and nothing behind is
        # unrecoverable: the row keeps a 120-character summary and the text
        # itself is gone.  Writing before the send also means the artifact
        # survives a delivery that fails.
        stored = await self.deliver_standalone(task_id, run_id, text)
        if delivery_mode == "standalone":
            return stored
        last_error: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                status = await self.deliver_channel(
                    target=target,
                    text=text,
                    output_dir=output_dir,
                )
                return DeliveryResult(status=status, output_path=stored.output_path)
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    await asyncio.sleep(5.0 * (attempt + 1))
        return DeliveryResult(
            status="failed",
            error=str(last_error) if last_error else "unknown error",
            output_path=stored.output_path,
        )
