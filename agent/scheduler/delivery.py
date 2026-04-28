from __future__ import annotations

from pathlib import Path
from typing import Optional

from agent import shared
from .models import DeliveryResult, DeliveryTarget


class SchedulerDelivery:
    def __init__(self, cfg: dict, output_root: Optional[Path] = None):
        self.cfg = cfg
        self.output_root = output_root or (shared.DEFAULT_OUTPUT_DIR / "scheduler")

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
        if target.target_type != "feishu_chat":
            raise ValueError(f"Unsupported channel target: {target.target_type}")
        from channels.feishu import FeishuConfig, FeishuOutputSink, build_feishu_client

        feishu_cfg = self.cfg.get("channels", {}).get("feishu", {})
        if not feishu_cfg.get("app_id") or not feishu_cfg.get("app_secret"):
            raise RuntimeError("Feishu delivery requires app_id and app_secret")
        sink = FeishuOutputSink(
            client=build_feishu_client(FeishuConfig(**feishu_cfg)),
            receive_id_type=(
                "chat_id"
                if target.payload.get("chat_type", "p2p") == "group"
                else "open_id"
            ),
            receive_id=target.payload["chat_id"],
            reply_message_id=None,
            output_dir=output_dir,
            streaming=False,
        )
        await sink._send_response_async(text)
        await sink.drain()
        return "delivered"

    async def deliver(
        self,
        *,
        task_id: str,
        run_id: str,
        delivery_mode: str,
        target: DeliveryTarget,
        text: str,
        output_dir: Optional[Path] = None,
    ) -> DeliveryResult:
        if delivery_mode == "standalone":
            return await self.deliver_standalone(task_id, run_id, text)
        if delivery_mode == "channel":
            status = await self.deliver_channel(
                target=target,
                text=text,
                output_dir=output_dir,
            )
            return DeliveryResult(status=status)
        raise ValueError(f"Unsupported delivery mode: {delivery_mode}")
