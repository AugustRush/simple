"""The built-in delivery target: a Feishu chat.

Split out of ``delivery.py`` when that module's ``if target_type != "feishu_chat"``
became a registry. This is the one sender every install has; anything else is a
plugin's contribution, and there is no longer a difference in *kind* between the
two -- only in who registered it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

__all__ = ["send_feishu_chat", "TARGET_TYPE"]

TARGET_TYPE = "feishu_chat"


async def send_feishu_chat(
    *,
    target: Any,
    text: str,
    cfg: dict,
    output_dir: Optional[Path] = None,
) -> str:
    """Post *text* into the chat *target* names.

    Raises when the deployment has no Feishu credentials or the send fails:
    ``SchedulerDelivery.deliver`` owns the retry loop and the failure record, so
    a sender that swallowed its own error would make both of them lie.
    """
    from agent.channels.feishu import (
        FeishuConfig,
        FeishuOutputSink,
        build_feishu_client,
    )

    feishu_cfg = (cfg.get("channels") or {}).get("feishu") or {}
    if not feishu_cfg.get("app_id") or not feishu_cfg.get("app_secret"):
        raise RuntimeError("Feishu delivery requires app_id and app_secret")
    payload = dict(getattr(target, "payload", None) or {})
    chat_id = str(payload.get("chat_id") or "").strip()
    if not chat_id:
        raise RuntimeError("Feishu delivery target has no chat_id")
    # An explicit receive_id_type wins over the chat_type heuristic: the
    # schedule editor writes one because every chat it offers came from the
    # bot's chat list, which only hands out chat_ids -- and the heuristic would
    # read "p2p" and try open_id on a value that is not one.  Old targets
    # without the field keep guessing as before.
    receive_id_type = str(payload.get("receive_id_type", "")).strip()
    if not receive_id_type:
        receive_id_type = (
            "chat_id" if payload.get("chat_type", "p2p") == "group" else "open_id"
        )
    sink = FeishuOutputSink(
        client=build_feishu_client(FeishuConfig(**feishu_cfg)),
        receive_id_type=receive_id_type,
        receive_id=chat_id,
        reply_message_id=None,
        output_dir=output_dir,
        streaming=False,
    )
    await sink._send_response_async(text)
    await sink.drain()
    return "delivered"
