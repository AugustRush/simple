"""The one path a durable runtime fact is written.

Why this exists
---------------
Durable session history was appended from **three** layers, and the sequence "append the
fact, then derive the model-visible projection from it" was written out at each of them:

- ``AgentCore`` recorded ``turn_finished``;
- a channel recorded the runtime events it drained from its ``EventCollector``;
- ``ContextManager`` held both halves and was reached for by the other two.

The cost was not line count, it was that **the caller had to know which context manager
belonged to which session**.  A channel with several live sessions did this:

    state = sessions.get(event.session_id)
    ctx_mgr = getattr(state, "context_manager", None)
    if ctx_mgr is None:
        runtime = session_components.get(event.session_id, components)
        ctx_mgr = resolve(runtime, "context_manager") if isinstance(runtime, dict) else None

-- with a comment explaining that falling back to the factory's base manager "sends tool
events to an unrelated session and makes traces disappear after a browser refresh".  A
lookup chain that has a known wrong answer in it is a lookup chain nobody can check.

What it owns, and what it does not
----------------------------------
It owns the **sequence**: append, then project.  It does not own the projection itself --
that stays with the context manager that holds the clipping helpers and the working-state
shape, and is handed in as a callback.  Saying so plainly rather than implying more: this
unifies *where a fact is written*, which is what was scattered, while leaving the
projection where its inputs live.

The session id is read through a callable rather than captured, because a context manager's
staging id is assigned when its session is bound -- copying it at construction is how the
original bug produced events attributed to an unrelated session.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from agent.memory._helpers import _now

__all__ = ["SessionLog"]


class SessionLog:
    """Append durable runtime facts for one session, then project them."""

    def __init__(
        self,
        store: Any,
        session_id: Callable[[], str],
        *,
        project: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._store = store
        #: A callable, not a string: see the module docstring.  The id is whatever the
        #: staging buffer says *now*, which is what makes the session the event is filed
        #: under the session it happened in.
        self._session_id = session_id
        self._project = project

    @property
    def session_id(self) -> str:
        return str(self._session_id())

    def append(
        self,
        event_type: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        turn_id: str = "",
    ) -> Any:
        """Record one runtime fact and update the model-readable projection.

        The factual event is appended **first** and the projection derives from what was
        stored rather than from the argument, so a projection can never disagree with the
        log it is supposed to summarise.
        """
        payload = payload if isinstance(payload, dict) else {}
        event = self._store.append_agent_event(
            session_id=self.session_id,
            event_type=event_type,
            payload=payload,
            turn_id=turn_id,
            created_at=_now(),
        )
        if self._project is not None:
            self._project(
                event_type=event.event_type,
                payload=event.payload,
                created_at=event.created_at,
            )
        return event
