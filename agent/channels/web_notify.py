"""Proving, from the settings page, that a failure would actually reach you.

Why this exists
---------------
The notification settings are a promise the page cannot otherwise verify: a
desktop banner depends on the platform having a notification centre and on the
terminal hosting the gateway having permission to use it; a Feishu mirror
depends on the app's send scope and on the chat id naming a chat the bot can
post in. Every one of those is invisible in the config file, and the moment you
find out is a failed run at 07:00 that nobody hears.

So the page gets one button that sends a *real* notification down every
configured path and says, per path, what happened. The wording of the test
message is deliberately about itself -- a test that looks like a real failure
trains the person to dismiss the real one.

What this does not do
---------------------
It does not save anything, and it does not touch a run. It reads the config as
it stands, which is the honest answer to "will this work?" only after saving --
so the button sits beside the setting it tests and the page's own dirty state
is what tells you whether the config it used is the one on screen.
"""

from __future__ import annotations

from typing import Any

__all__ = ["_notify_test"]


async def _notify_test(request: Any, *, authorized) -> Any:
    """Send one real notification down every configured path.

    Returns per-path results rather than a single boolean: "the banner did not
    appear but the chat message did" is the answer that tells the person what to
    fix, and a summary would throw it away.
    """
    from starlette.responses import JSONResponse

    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    from agent.channels.desktop_notify import (
        desktop_notifications_enabled,
        notify as desktop_notify,
    )
    from agent.config import load_config

    cfg, _ = load_config()
    title = "Simple Agent · 通知测试"
    body = "这是一条测试通知。真的失败时，你会在这里收到任务名和失败原因。"

    channels: list[dict[str, Any]] = []

    # ── desktop ──────────────────────────────────────────────────────────
    desktop_on = desktop_notifications_enabled(cfg)
    if not desktop_on:
        channels.append(
            {
                "channel": "desktop",
                "ok": False,
                "skipped": True,
                "detail": "本机通知已关闭（notifications.desktop = false）",
            }
        )
    else:
        sent = await desktop_notify(title, body)
        channels.append(
            {
                "channel": "desktop",
                "ok": bool(sent),
                "skipped": False,
                "detail": (
                    "已发出，看屏幕右上角"
                    if sent
                    else "系统没有接受这条通知：可能是没有通知中心、"
                    "或运行 gateway 的终端没有通知权限"
                ),
            }
        )

    # ── feishu ───────────────────────────────────────────────────────────
    feishu = _feishu_section(cfg)
    chat_id = str(feishu.get("notify_chat_id") or "").strip()
    if not chat_id:
        channels.append(
            {
                "channel": "feishu",
                "ok": False,
                "skipped": True,
                "detail": "还没选「失败通知发到」的会话，失败只走本机通知",
            }
        )
    else:
        result = await _send_feishu_test(chat_id, f"{title}\n{body}")
        channels.append(
            {
                "channel": "feishu",
                "ok": bool(result.get("ok")),
                "skipped": False,
                "detail": str(result.get("detail") or ""),
            }
        )

    fired = [item for item in channels if item["ok"]]
    return JSONResponse(
        {
            "ok": bool(fired),
            "channels": channels,
            # The one sentence the page needs: did anything actually leave.
            "summary": (
                f"{len(fired)} 条通道已发出"
                if fired
                else "没有任何通道能发出通知"
            ),
        }
    )


def _feishu_section(cfg: Any) -> dict[str, Any]:
    section = cfg.get("channels") if isinstance(cfg, dict) else None
    feishu = section.get("feishu") if isinstance(section, dict) else None
    return dict(feishu) if isinstance(feishu, dict) else {}


async def _send_feishu_test(chat_id: str, text: str) -> dict[str, Any]:
    """Post one message through the same delivery path a failed run uses.

    Not a hand-rolled HTTP call: the point is to prove *this* configuration can
    send, and a check that builds its own request would pass while the agent's
    own deliveries failed. That is the same rule the provider test follows.
    """
    from agent.config import load_config

    cfg, _ = load_config()
    # Ask before sending, so the answer is a sentence about the config rather
    # than the delivery layer's own English error ("Feishu delivery requires
    # app_id and app_secret") shown in a Chinese settings page. The existing
    # /api/feishu/test says the same thing in the same words; two buttons that
    # test the same channel must not explain the same problem differently.
    feishu = _feishu_section(cfg)
    if not feishu.get("app_id") or not feishu.get("app_secret"):
        return {
            "ok": False,
            "detail": "还没有配置飞书应用（app_id / app_secret），在下面「高级 JSON」里填",
        }
    try:
        from agent.scheduler import DeliveryTarget
        from agent.scheduler.delivery import SchedulerDelivery

        service = SchedulerDelivery(cfg)
        target = DeliveryTarget(
            target_type="feishu_chat",
            payload={"chat_id": chat_id, "receive_id_type": "chat_id"},
        )
        result = await service.deliver_channel(target=target, text=text)
    except Exception as exc:  # noqa: BLE001 - the reason is the answer
        return {"ok": False, "detail": f"发送失败：{exc}"}
    status = str(getattr(result, "status", "") or "")
    if status == "sent":
        return {"ok": True, "detail": "已发到选定的会话，去飞书看一眼"}
    error = str(getattr(result, "error", "") or "")
    return {
        "ok": False,
        "detail": f"发送没有成功（{status or '未知状态'}）" + (f"：{error}" if error else ""),
    }
