"""Signals as the web UI sees them, and the feishu chats a trigger can target. Moved out of WebChannel (see the remaining-big-files survey work order). The token check arrives as a capability."""

from __future__ import annotations

from agent.scheduler.models import parse_task_signal

from typing import Any

__all__ = [
    "_signals",
    "_list_feishu_chats",
]


async def _signals(
    request: Any,
    *,
    authorized,
) -> Any:
    """What can be waited for, so a subscription is chosen rather than typed.

        The interface offers these as options because a signal name is matched
        exactly: a near miss does not fire late or fire anyway, it never fires,
        and the only trace is an emission recorded as unmatched.  Listing the
        names that exist turns that failure into one nobody can make by
        accident.
        """
    from starlette.responses import JSONResponse
    from agent.scheduler import open_store

    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    store = open_store()
    try:
        emissions = store.signal_names()
        subscribers: dict[str, int] = {}
        task_labels: dict[str, str] = {}
        for task in store.list_tasks():
            task_labels[task.id] = task.name
            if not task.enabled or task.trigger.trigger_type != "signal":
                continue
            key = str(task.trigger.payload.get("name", "")).strip()
            subscribers[key] = subscribers.get(key, 0) + 1
    finally:
        store.close()
    items = []
    for entry in emissions:
        name = str(entry["name"])
        # A task signal reads as gibberish on its own (task:9f2c…:succeeded),
        # so the task it belongs to travels alongside.  The label itself is
        # left to the interface, which already owns the wording for run
        # statuses; duplicating it here would create a second place to keep
        # in step for no gain.
        parsed = parse_task_signal(name)
        items.append(
            {
                "name": name,
                "source": "task" if parsed else "custom",
                "task_id": parsed[0] if parsed else "",
                "task_name": task_labels.get(parsed[0], "") if parsed else "",
                "status": parsed[1] if parsed else "",
                "last_emitted_at": entry.get("last_at"),
                "emission_count": int(entry.get("count") or 0),
                "subscriber_count": subscribers.get(name, 0),
            }
        )
    return JSONResponse(
        {
            "signals": items,
            "waiting": [
                {"name": key, "subscriber_count": value}
                for key, value in sorted(subscribers.items())
                if key and key not in {item["name"] for item in items}
            ],
        }
    )


def _list_feishu_chats(feishu_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """The chats the bot sits in, page by page.

        The lark client is synchronous; callers run this in a thread so the
        event loop is never blocked, the same rule FeishuOutputSink follows.
        """
    from agent.channels.feishu import FeishuConfig, build_feishu_client
    from lark_oapi.api.im.v1 import ListChatRequestBuilder  # type: ignore[import]

    client = build_feishu_client(FeishuConfig(**feishu_cfg))
    chats: list[dict[str, Any]] = []
    page_token = ""
    for _ in range(10):  # 10 pages x 100 is far past any real bot
        builder = ListChatRequestBuilder().page_size(100)
        if page_token:
            builder = builder.page_token(page_token)
        resp = client.im.v1.chat.list(builder.build())
        if not resp.success():
            raise RuntimeError(
                f"飞书会话列表获取失败：code={resp.code} msg={resp.msg}"
            )
        data = resp.data
        for item in getattr(data, "items", None) or []:
            chat_id = str(getattr(item, "chat_id", "") or "")
            if not chat_id:
                continue
            chats.append(
                {
                    "chat_id": chat_id,
                    "name": str(getattr(item, "name", "") or ""),
                    "description": str(getattr(item, "description", "") or ""),
                    "external": bool(getattr(item, "external", False)),
                }
            )
        if not getattr(data, "has_more", False):
            break
        page_token = str(getattr(data, "page_token", "") or "")
        if not page_token:
            break
    return chats
