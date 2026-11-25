"""The two calls that make a parked task a thing with a life.

`parked_block` puts waiting tasks in front of the model every turn, and the
coordinator picks one up when a turn ends. Neither of those needs a tool. What
needs one is the *agent* deciding a parked task is no longer worth doing: it has
to be able to close it, and to say why, or the queue grows into a list nobody
reads.

Why two tools and not one
-------------------------
Dropping and finishing are different facts. A task that was done should read
"done" with what came of it; a task that was waved away should read "dropped"
with the reason. Collapsing them into one `close` call would make the queue's
history say "dropped" about work that was actually completed, which is exactly
the kind of small lie that makes a list untrustworthy.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = ["ParkedTaskTools", "register_parked_task_tools"]


class ParkedTaskTools:
    """Finishing and dropping the work the person parked."""

    def __init__(
        self,
        registry: Any,
        store_factory: Callable[[], Any],
        ok: Callable[..., dict[str, Any]],
        error: Callable[..., dict[str, Any]],
    ) -> None:
        # Public on purpose: ``ToolRegistry.call`` reads ``fn.__self__.registry``
        # to install the calling registry's merged context.
        self.registry = registry
        self._store_factory = store_factory
        self._ok = ok
        self._error = error

    def _session_id(self) -> str:
        return str(self.registry.get_context("turn_session_id") or "")

    def _waiting(self, store: Any, session_id: str) -> list[Any]:
        lister = getattr(store, "list_parked_tasks", None)
        if not callable(lister):
            return []
        try:
            return list(
                lister(session_id=session_id, statuses=("waiting", "active"), limit=50)
            )
        except Exception:
            return []

    def _resolve(self, store: Any, session_id: str, task: str) -> Any:
        """Find the task the caller means, by id or by text.

        Text is accepted because the model reads these from the prompt block,
        where they are numbered sentences rather than ids -- requiring an id it
        never saw would make the tool unusable without a `list` call first.
        """
        wanted = str(task or "").strip()
        if not wanted:
            return None
        candidates = self._waiting(store, session_id)
        for item in candidates:
            if str(getattr(item, "id", "") or "") == wanted:
                return item
        lowered = wanted.lower()
        # Exact, then containment either way. Containment is what a model's
        # paraphrase needs -- it reads "顺便查一下天气" in the block and asks to
        # finish "查天气", which is a substring of it. A stricter match would
        # make the tool fail on the wording it was shown.
        for item in candidates:
            text = str(getattr(item, "text", "") or "").strip().lower()
            if text and text == lowered:
                return item
        for item in candidates:
            text = str(getattr(item, "text", "") or "").strip().lower()
            if text and (lowered in text or text in lowered):
                return item
        # Last resort: character overlap. "顺便查一下天气" and "查天气" share four
        # characters out of six; a paraphrase with the words reordered still
        # finds its task, while an unrelated one does not reach the bar.
        wanted_chars = {char for char in lowered if char.strip()}
        if not wanted_chars:
            return None
        best: tuple[float, Any] | None = None
        for item in candidates:
            text = str(getattr(item, "text", "") or "").strip().lower()
            text_chars = {char for char in text if char.strip()}
            if not text_chars:
                continue
            ratio = len(wanted_chars & text_chars) / len(wanted_chars)
            if ratio >= 0.6 and (best is None or ratio > best[0]):
                best = (ratio, item)
        return best[1] if best is not None else None

    def finish_parked_task(self, task: str, outcome: str = "") -> dict[str, Any]:
        """Mark a parked task done, saying what came of it.

        Use this when you have actually done the thing. `outcome` is what the
        person would want to know: the answer, or where it went.
        """
        store = self._store_factory()
        if store is None:
            return self._error("这个部署没有记忆库，读不到排着的事。")
        session_id = self._session_id()
        item = self._resolve(store, session_id, task)
        if item is None:
            return self._error(
                f"没找到这件排着的事：{task}。可以先看当前上下文里的「排着的事」列表。"
            )
        try:
            changed = store.set_parked_status(
                task_id=str(getattr(item, "id", "")),
                status="done",
                outcome=str(outcome or "").strip(),
            )
        except Exception as exc:  # noqa: BLE001 - the reason is the answer
            return self._error(f"标记完成失败：{exc}")
        if not changed:
            return self._error("这件已经不在队列里了（可能刚被处理过）。")
        return self._ok(
            task_id=str(getattr(item, "id", "")),
            text=str(getattr(item, "text", "") or ""),
        )

    def drop_parked_task(self, task: str, reason: str = "") -> dict[str, Any]:
        """Drop a parked task without doing it, saying why.

        Use this when the thing is no longer worth doing -- it became irrelevant,
        the person changed their mind, or it was really part of what you already
        did. Say why: a queue whose entries vanish without explanation is one
        nobody trusts.
        """
        store = self._store_factory()
        if store is None:
            return self._error("这个部署没有记忆库，读不到排着的事。")
        session_id = self._session_id()
        item = self._resolve(store, session_id, task)
        if item is None:
            return self._error(f"没找到这件排着的事：{task}。")
        try:
            changed = store.set_parked_status(
                task_id=str(getattr(item, "id", "")),
                status="dropped",
                outcome=str(reason or "").strip(),
            )
        except Exception as exc:  # noqa: BLE001
            return self._error(f"移除失败：{exc}")
        if not changed:
            return self._error("这件已经不在队列里了。")
        return self._ok(
            task_id=str(getattr(item, "id", "")),
            text=str(getattr(item, "text", "") or ""),
        )


def register_parked_task_tools(r: Any, *, parked_tools: ParkedTaskTools) -> None:
    r.register(
        "finish_parked_task",
        (
            "Mark one of the tasks the person parked (「排着的事」) as done, saying "
            "what came of it. Use it when you have actually finished that thing -- "
            "not when you have merely started it. Accepts the id or the words of "
            "the task as it appears in the list."
        ),
        {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The parked task: its id, or its words as listed.",
                },
                "outcome": {
                    "type": "string",
                    "description": (
                        "What came of it -- the answer, or where the result went. "
                        "This is what the person reads when they look back."
                    ),
                    "default": "",
                },
            },
            "required": ["task"],
        },
        parked_tools.finish_parked_task,
        source="builtin",
        capabilities=("state_write",),
    )

    r.register(
        "drop_parked_task",
        (
            "Remove one of the tasks the person parked without doing it, saying why. "
            "Use it when the thing is no longer worth doing -- it became irrelevant, "
            "they changed their mind, or it was really part of work already done. "
            "Do not use it to silence a task you have not looked at."
        ),
        {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The parked task: its id, or its words as listed.",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Why it is not worth doing. A queue whose entries vanish "
                        "without explanation is one nobody trusts."
                    ),
                    "default": "",
                },
            },
            "required": ["task"],
        },
        parked_tools.drop_parked_task,
        source="builtin",
        capabilities=("state_write",),
    )
