"""The signal tools: emitting one, listing them, and reporting a failed run.

Why this module exists
----------------------
A scheduled workflow is a graph of steps that wake each other by *signal*.  Three
tools serve that graph and nothing else: ``emit_signal`` records an emission so
whoever subscribes runs on the scheduler's next pass, ``list_signals`` shows
what has been emitted and who is waiting for what, and ``report_outcome`` is
how a run says it could not do its job.

They were methods on ``BuiltinTools``, a 5155-line class.  The survey measured
this as the cheapest remaining cluster: three methods, no leaf instance state,
and -- the reason it could move at all -- they reach the scheduler only through
one accessor, which is injected here rather than inherited.

Two properties travelled with the code, and both are load-bearing:

- **``report_outcome`` has no success case.**  An unattended run is judged by
  its acceptance check; an agent's own assurance that it did well is a claim,
  not evidence, so a parameter usable only in the harmful direction is not
  offered.  Outside a scheduled run it refuses rather than quietly accepting,
  because in a conversation there is a person to tell.
- **``emit_signal`` does not run the subscriber.**  Emitting and running are
  separate steps on purpose: the call may come from inside a run the scheduler
  is currently executing, and starting another run from there would nest one
  scheduler lifetime inside another.  The scheduler picks the emission up on
  its next pass, which is also what lets an emission survive a process that
  stops right after it.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from agent.verification import VERDICT_FAILED

from .runtime import _active_run_self_report, _active_signal_context


class SignalTools:
    """The signal tools, with the scheduler accessor and responders they need."""

    def __init__(
        self,
        schedule_store: Callable[[], Any],
        ok: Callable[..., dict[str, Any]],
        error: Callable[..., dict[str, Any]],
    ) -> None:
        self._schedule_store = schedule_store
        self._ok = ok
        self._error = error

    def report_outcome(self, reason: str) -> dict[str, Any]:
        """Record that this run could not do its job.

        There is deliberately no way to report success.  An unattended run is
        judged by its acceptance check, and an agent's own assurance that it
        did well is a claim rather than evidence -- offering a parameter that
        could only ever be used in the harmful direction would be an invitation
        to use it.

        Refused outside a scheduled run rather than quietly accepted: in a
        conversation there is a person to tell, and a tool that wrote a verdict
        to nowhere would look as though it had done something.
        """
        text = str(reason or "").strip()
        if not text:
            raise ValueError("`reason` is required")
        report = _active_run_self_report.get()
        if report is None:
            raise ValueError(
                "report_outcome 只能在定时任务的运行中使用；"
                "在会话里直接说明遇到的问题即可。"
            )
        report.verdict = VERDICT_FAILED
        report.reason = text
        return self._ok(
            reported=True,
            reason=text,
            note="本次运行将被记为失败，下游步骤不会运行。",
        )

    def emit_signal(
        self,
        name: str,
        payload: Optional[dict[str, Any]] = None,
        intent: str = "",
    ) -> dict[str, Any]:
        """Record a signal so whoever is waiting on it runs.

        ``intent`` is read by the executor's quote check, which refuses this
        call unless it reproduces part of the request that commissioned the
        turn.  It is deliberately not stored: an emission is an action that is
        already over, unlike a task, which is a row that has to be able to
        explain itself months later.

        Deliberately does *not* run the subscriber here.  Emitting and running
        are separate steps on purpose: this call may be made from inside a run
        that the scheduler is currently executing, and starting another run
        from there would nest one scheduler lifetime inside another.  The
        scheduler picks the emission up on its next pass, which is also what
        lets an emission survive a process that stops right after it.
        """
        store = self._schedule_store()
        parent = _active_signal_context.get() or {}
        try:
            emission = store.emit_signal(
                name,
                payload if isinstance(payload, dict) else {},
                source="agent",
                depth=int(parent.get("depth", 0) or 0) + 1 if parent else 0,
                origin_id=str(parent.get("origin_id", "") or "") if parent else "",
            )
        except ValueError as exc:
            return self._error(str(exc))
        subscribers = [
            task.name
            for task in store.list_tasks()
            if task.enabled
            and task.trigger.trigger_type == "signal"
            and str(task.trigger.payload.get("name", "")).strip() == emission.name
        ]
        return self._ok(
            emission_id=emission.id,
            name=emission.name,
            depth=emission.depth,
            cascade_id=emission.origin_id,
            subscribers=subscribers,
            summary_text=(
                f"信号「{emission.name}」已发出，{len(subscribers)} 个任务会在下一轮运行。"
                if subscribers
                else f"信号「{emission.name}」已记录，但目前没有任务订阅它。"
            ),
        )

    def list_signals(self) -> dict[str, Any]:
        store = self._schedule_store()
        emissions = store.signal_names()
        subscribers: dict[str, int] = {}
        for task in store.list_tasks():
            if not task.enabled or task.trigger.trigger_type != "signal":
                continue
            key = str(task.trigger.payload.get("name", "")).strip()
            subscribers[key] = subscribers.get(key, 0) + 1
        return self._ok(
            count=len(emissions),
            items=[
                {
                    "name": entry["name"],
                    "last_emitted_at": entry.get("last_at"),
                    "emission_count": entry.get("count"),
                    "subscriber_count": subscribers.get(entry["name"], 0),
                }
                for entry in emissions
            ],
            # Names nobody has emitted yet cannot appear above, but a
            # subscription written up front still exists and is worth seeing.
            waiting_on_unemitted=[
                {"name": key, "subscriber_count": value}
                for key, value in sorted(subscribers.items())
                if key and key not in {entry["name"] for entry in emissions}
            ],
        )
