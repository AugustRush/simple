from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Literal,
    Mapping,
    Sequence,
    TypeAlias,
    TypeVar,
    overload,
)

from agent.kernel.service import (
    MissingServiceError,
    ServiceRegistry,
    require_service,
    resolve,
)
from agent.runtime.system_prompt import publish_system_prompt
from agent.core.attachments import MessageAttachment
from agent.core.output import (
    EventCollector,
    RuntimeEvent,
    _active_event_collector,
    _active_sink,
    recorded_text,
)
from agent.runtime.heartbeat import heartbeat_path_for_session
from agent.skills.catalog import prepare_user_message_for_skills
from agent.tools.runtime import _active_schedule_target, _active_signal_context

T = TypeVar("T")
OperationState: TypeAlias = Literal["idle", "active", "cancelling"]


@dataclass(frozen=True)
class TurnInput:
    """Normalized input for one agent turn.

    This deliberately stays transport-neutral so CLI, channels, and future
    gateways can enter the same runtime boundary.
    """

    text: str
    session_id: str = "default"
    channel_name: str = "cli"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    attachments: tuple[MessageAttachment, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(self, "attachments", tuple(self.attachments))

    @classmethod
    def from_text(
        cls,
        text: str,
        *,
        session_id: str = "default",
        channel_name: str = "cli",
        metadata: Mapping[str, Any] | None = None,
        attachments: list[MessageAttachment] | tuple[MessageAttachment, ...] = (),
    ) -> "TurnInput":
        return cls(
            text=text,
            session_id=session_id,
            channel_name=channel_name,
            metadata=metadata or {},
            attachments=tuple(attachments),
        )


@dataclass(frozen=True)
class TurnResult:
    """Transport-neutral result for one completed agent turn."""

    text: str
    tool_calls: tuple[str, ...] = ()
    agent_id: str = ""
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def record_tool_use(self, tool_name: str) -> "TurnResult":
        return replace(self, tool_calls=(*self.tool_calls, tool_name))

    @classmethod
    def from_agent_result(cls, result: Any) -> "TurnResult":
        return cls(
            text=getattr(result, "content", None) or "",
            tool_calls=tuple(getattr(result, "tool_calls_made", ()) or ()),
            agent_id=getattr(result, "agent_id", "") or "",
            error=getattr(result, "error", None),
        )


@dataclass(frozen=True)
class RuntimeComponents:
    """Typed access wrapper for bootstrapped runtime dependencies.

    ``require`` resolves through a :class:`~agent.kernel.service.ServiceRegistry`
    rather than reading a plain mapping, so this is the runtime's *container* and
    not only a typed view of a dict.  The registry adopts ``values`` in place, so
    the many call sites that read or assign the mapping directly and the ones that
    call ``require`` are looking at the same storage while the migration runs.

    The error contract is deliberately unchanged: a missing component still
    raises ``KeyError`` with the same message, because callers and tests match on
    it.  :class:`MissingServiceError` carries better semantics for new code; this
    keeps the old surface honest rather than quietly re-typing an exception
    callers already handle.
    """

    values: Mapping[str, Any]
    #: The container behind :attr:`values`.  Not an init argument: it is derived,
    #: and a caller that could pass one could pass one that disagrees with
    #: ``values``.  A declared field rather than a property because a frozen
    #: dataclass's ``object.__setattr__`` does not honour a property without a
    #: setter -- it raises, which is how this was found.
    services: ServiceRegistry = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", self.values)
        if isinstance(self.values, dict):
            # Adopt in place, so the mapping and the container are one storage.
            object.__setattr__(self, "services", ServiceRegistry.adopt(self.values))
        else:
            # A frozen mapping has nothing to adopt; a private registry is the
            # honest answer, and direct writes were never possible on it anyway.
            object.__setattr__(self, "services", ServiceRegistry(dict(self.values)))

    @overload
    def require(self, name: str) -> Any: ...

    @overload
    def require(self, name: str, expected_type: type[T]) -> T: ...

    def require(self, name: str, expected_type: type[T] | None = None) -> Any:
        try:
            value = self.services.get(name)
        except MissingServiceError as exc:
            # Same exception type and message as before: this moved *where* the
            # lookup happens, not what a caller sees when it fails.
            raise KeyError(f"missing runtime component: {name}") from exc
        if expected_type is not None and not isinstance(value, expected_type):
            raise TypeError(
                f"runtime component {name!r} must be "
                f"{expected_type.__name__}, got {type(value).__name__}"
            )
        return value

    def lookup(self, name: str, default: Any = None) -> Any:
        """A service that may legitimately be absent.

        The sibling of :meth:`require`, for the reads that branch on a missing
        value -- ``plugin_catalog`` is absent in a composition with no plugins,
        ``turn_runner`` in a bare one.  Both go through the container now; the
        difference is only whether absence is an error, which is the caller's
        question and should be visible at the call site rather than implied by
        which of two dict accessors happened to be used.
        """
        return self.services.get(name, default)


def describe_queued_messages(
    interjections: Sequence[Mapping[str, Any]],
    restarts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Describe waiting messages in the order they will actually run.

    Interjections come first: they are folded into the turn running now,
    while restarts only run once it ends.

    Entries are described even when they carry no id.  The queue a reader is
    shown has to match the queue that exists; a caller that can only act on
    identified messages should say so about that entry rather than leave it
    out and report a shorter queue than there is.
    """

    described: list[dict[str, Any]] = []
    for kind, queue in (("interjection", interjections), ("restart", restarts)):
        for entry in queue:
            described.append(
                {
                    "id": str(entry.get("message_id") or ""),
                    "text": str(entry.get("text") or ""),
                    "kind": kind,
                    "urgency": str(entry.get("urgency") or "normal"),
                    "arrived_at": float(entry.get("arrived_at") or 0.0),
                }
            )
    return described


@dataclass(init=False)
class RuntimeSessionState:
    """Mutable per-session state shared by turn-oriented runners."""

    ctx: Any
    tools_used: list[str] = field(default_factory=list)
    turn_count: int = 0
    task_context: str = ""
    context_manager: Any = None
    memory_worker: Any = None
    cancel_token: Any = None  # CancelToken | None
    operation_state: OperationState = "idle"
    accepts_interjections: bool = False
    pending_interjections: list[dict[str, Any]] = field(default_factory=list)
    restart_queue: list[dict[str, Any]] = field(default_factory=list)
    model_override: str | None = None
    base_system_prompt_override: str | None = None
    system_prompt_override: str | None = None
    turn_lock: Any = None
    last_activity: float = 0.0
    runtime_revision: int = 0

    def __init__(
        self,
        ctx: Any,
        tools_used: list[str] | None = None,
        turn_count: int = 0,
        task_context: str = "",
        context_manager: Any = None,
        memory_worker: Any = None,
        cancel_token: Any = None,
        pending_messages: list[dict[str, Any]] | None = None,
        turn_in_progress: bool = False,
        *,
        operation_state: OperationState | None = None,
        accepts_interjections: bool = False,
        pending_interjections: list[dict[str, Any]] | None = None,
        restart_queue: list[dict[str, Any]] | None = None,
        model_override: str | None = None,
        base_system_prompt_override: str | None = None,
        system_prompt_override: str | None = None,
        turn_lock: Any = None,
        last_activity: float = 0.0,
        runtime_revision: int = 0,
    ) -> None:
        if (
            pending_messages is not None
            and pending_interjections is not None
            and pending_messages is not pending_interjections
        ):
            raise ValueError(
                "pending_messages and pending_interjections must reference "
                "the same mailbox"
            )
        mailbox = (
            pending_interjections
            if pending_interjections is not None
            else pending_messages
        )
        if mailbox is None:
            mailbox = []
        restarts = restart_queue if restart_queue is not None else []
        if restarts is mailbox:
            restarts = list(restarts)

        self.ctx = ctx
        self.tools_used = tools_used if tools_used is not None else []
        self.turn_count = turn_count
        self.task_context = task_context
        self.context_manager = context_manager
        self.memory_worker = memory_worker
        self.cancel_token = cancel_token
        self.operation_state = (
            operation_state
            if operation_state is not None
            else ("active" if turn_in_progress else "idle")
        )
        self.accepts_interjections = accepts_interjections
        self.pending_interjections = mailbox
        self.restart_queue = restarts
        self.model_override = model_override
        self.base_system_prompt_override = base_system_prompt_override
        self.system_prompt_override = system_prompt_override
        self.turn_lock = turn_lock if turn_lock is not None else asyncio.Lock()
        self.last_activity = float(last_activity or 0.0)
        self.runtime_revision = int(runtime_revision or 0)

    @property
    def pending_messages(self) -> list[dict[str, Any]]:
        """Compatibility alias for BaseAgent's existing mailbox metadata."""

        return self.pending_interjections

    @pending_messages.setter
    def pending_messages(self, value: list[dict[str, Any]]) -> None:
        self.pending_interjections = (
            list(value) if value is self.restart_queue else value
        )

    @property
    def turn_in_progress(self) -> bool:
        """Compatibility view over the explicit operation state."""

        return self.operation_state != "idle"

    @turn_in_progress.setter
    def turn_in_progress(self, value: bool) -> None:
        self.operation_state = "active" if value else "idle"

    def ensure_task_context(self, text: str) -> None:
        if not self.task_context:
            self.task_context = text[:300]

    def set_session_prompt(self, prompt: str) -> None:
        """Single authority for writing ctx.system_prompt during a session.

        Any runtime path that refreshes the prompt mid-session (dirty skills,
        config reload, workspace switch) must go through here so the current
        task context stays attached.  The task context is the one thing a
        rebuild would otherwise drop, and it is what a long operation's first
        instruction survives as once compaction has taken the message that
        carried it.

        ``send_message`` no longer restores the prompt at turn end — a turn's
        own context rides in its message now, so the prompt is never mutated
        during a turn and has nothing to restore.  What that means here is that
        a refresh made during a turn is *not* undone afterwards; the value set
        here is the session's head from then on.
        """
        import agent as agent_module

        self.ctx.system_prompt = agent_module._with_task_context(
            prompt, self.task_context
        )

    def record_turn(self, tool_calls: list[str]) -> None:
        self.tools_used.extend(tool_calls)
        self.turn_count += 1

    def withdraw_queued(self, message_id: str) -> str | None:
        """Remove one queued message by id and release the sink holding it.

        Returns the withdrawn text, or None when neither queue still holds
        that id.  A message already folded into the running turn is no longer
        in a queue and cannot be withdrawn: the model has read it.
        """

        wanted = str(message_id or "").strip()
        if not wanted:
            return None
        for queue in (self.pending_interjections, self.restart_queue):
            for index, entry in enumerate(queue):
                if str(entry.get("message_id") or "") != wanted:
                    continue
                del queue[index]
                retire = getattr(entry.get("sink"), "retire_queued_message", None)
                if callable(retire):
                    retire()
                return str(entry.get("text") or "")
        return None


class TurnRunner:
    """Executes one normalized turn through the current agent implementation.

    A **plugin**, not a helper the composition news up.  The difference is what
    happens when its collaborators are missing: as a plugin its ``inject`` is
    checked before it activates, so a composition with no ``agent`` has no agent
    loop at all -- a visible gap at composition time.  Constructed ad hoc, it would
    exist and fail at the first turn instead, which moves the same misconfiguration
    from startup into a user's first message.

    It provides itself as ``agent_loop``, so a composition replaces the loop by
    providing that name -- which is what "the agent loop is a plugin" means in
    practice, rather than only that it lives in a plugin-shaped class.
    """

    #: The name this plugin is loaded and replaced under.
    name = "agent_loop"

    #: What must exist before a turn can run.  ``agent`` because a turn is run *on*
    #: an agent, and ``system_prompt`` because the request is assembled through it.
    #: The optional collaborators (``plugin_catalog``, ``post_turn_maintenance``) are
    #: deliberately *not* injected: they may legitimately be absent in a minimal
    #: composition, and a loop that refused to load without them would make the
    #: minimal composition impossible to express.
    inject = ("agent", "system_prompt")

    def __init__(self) -> None:
        self._ctx: Any = None
        self._services: Any = None

    @classmethod
    def bind(cls, components: Any) -> "TurnRunner":
        """The stock loop over *components*, for a composition that mounted none.

        Not the plugin path -- ``setup`` is -- but the one the runtime falls back to,
        so a runtime assembled by hand (tests, the channel runner) keeps working
        without having to mount the loop first.
        """
        loop = cls()
        loop._services = (
            components if isinstance(components, RuntimeComponents)
            else RuntimeComponents(components)
        )
        return loop

    def setup(self, ctx: Any) -> None:
        """Bind the loop to its composition and offer it as ``agent_loop``."""
        self._ctx = ctx
        ctx.provide("agent_loop", self, replace=True)

    def teardown(self) -> None:
        # Dropped so a replaced or unloaded loop cannot be reached through a stale
        # reference held by a caller that resolved it before the replacement.
        self._ctx = None
        self._services = None

    @property
    def _components(self) -> RuntimeComponents:
        """The composition, as a container.

        Kept as a property because every read below is a *service* read: the loop
        resolves collaborators by name, and a plugin that held the raw mapping would
        be a second way to reach services -- the coupling ``inject`` exists to make
        visible.
        """
        if self._ctx is not None:
            return self._ctx.services
        if self._services is not None:
            # Bound through :meth:`bind` rather than mounted as a plugin.
            return (
                self._services.services
                if isinstance(self._services, RuntimeComponents)
                else self._services
            )
        raise RuntimeError(
            "TurnRunner has no composition: it was neither mounted as a plugin "
            "(agent_loop) nor bound to one.  Use TurnRunner.bind(components) or "
            "load it as a plugin."
        )


    async def run(
        self,
        turn_input: TurnInput,
        ctx: Any,
        *,
        stream_callback: Callable[[str], None] | None = None,
    ) -> TurnResult:
        agent = require_service(self._components, "agent")
        kwargs: dict[str, Any] = {"stream_callback": stream_callback}
        if turn_input.attachments:
            kwargs["attachments"] = turn_input.attachments
        result = await agent.send_message(
            ctx,
            turn_input.text,
            **kwargs,
        )
        return TurnResult.from_agent_result(result)

    async def complete_turn(
        self,
        turn_input: TurnInput,
        state: RuntimeSessionState,
        result: TurnResult,
    ) -> list[Any]:
        """Finish a turn and return hook results for continue detection."""
        import agent as agent_module

        tool_calls = list(result.tool_calls)
        state.record_turn(tool_calls)

        hook_results: list[Any] = []
        plugin_catalog = resolve(self._components, "plugin_catalog")
        if plugin_catalog:
            hook_results = await plugin_catalog.fire_turn_end(
                agent_module.TurnEvent(
                    user_input=turn_input.text,
                    agent_response=result.text or "",
                    tool_calls=tool_calls,
                    session_id=turn_input.session_id,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    turn_index=state.turn_count,
                )
            )

        maintenance = resolve(self._components, "post_turn_maintenance")
        if maintenance is None:
            maintenance = agent_module.BaseAgent._post_turn_maintenance

        record_kwargs = {
            "message_id": str(
                turn_input.metadata.get("turn_id")
                or turn_input.metadata.get("message_id")
                or ""
            ),
            "metadata": dict(turn_input.metadata),
        }
        completion_id = str(turn_input.metadata.get("completion_id") or "")
        if completion_id:
            record_kwargs["assistant_message_id"] = completion_id

        # What the record holds for the assistant is what the client was shown.
        # ``result.text`` is the turn's *final* message, but a turn that narrated
        # before calling a tool streamed more than that -- and the web frontend
        # re-reads this record the moment the turn ends, asserting that
        # everything the socket showed is in it.  Record only the answer and the
        # narration disappears at completion, which is exactly what was reported.
        active_sink = _active_sink.get()
        displayed_text = (
            str(getattr(active_sink, "displayed_text", "") or "")
            if active_sink is not None
            else ""
        )

        # Thinking is shown on the same channel and belongs to the same record.
        # It rides the turn's metadata rather than a new column: the store
        # already round-trips ``metadata_json`` for these rows, so a reader gets
        # it back with no schema change -- and the API exposes it for assistant
        # rows only, since the payload is shared with the user row.
        displayed_reasoning = (
            str(getattr(active_sink, "displayed_reasoning", "") or "")
            if active_sink is not None
            else ""
        )
        if displayed_reasoning:
            record_kwargs["metadata"] = {
                **dict(record_kwargs.get("metadata") or {}),
                "_assistant_reasoning": displayed_reasoning,
            }

        maintenance(
            ctx_mgr=state.context_manager,
            agent=require_service(self._components, "agent"),
            ctx=state.ctx,
            user_content=turn_input.text,
            assistant_content=recorded_text(displayed_text, result.text or ""),
            channel=turn_input.channel_name,
            record_kwargs=record_kwargs,
            memory_worker=state.memory_worker,
            system_prompt=(
                state.system_prompt_override
                or require_service(self._components, "system_prompt")
            ),
            task_context=state.task_context,
            error=result.error or "",
        )
        if not result.error:
            state.task_context = ""
        return hook_results


@dataclass(frozen=True)
class TurnExecution:
    """Result of an application-level turn loop."""

    result: TurnResult
    #: Rounds of the continuation loop that ran.  Round 1 is the initial
    #: completion, so ``n`` rounds means ``n - 1`` continuations; ``0`` means
    #: the turn was blocked before any completion.  Distinct from a *step*
    #: (one model request plus the tools it calls), which BaseAgent counts
    #: internally and reports via ``step_started`` / ``step_ended`` events.
    continuation_rounds: int = 1
    blocked: bool = False
    block_reason: str = ""
    events: tuple[RuntimeEvent, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))

    @property
    def failed(self) -> bool:
        return bool(self.result.error)


class AgentCore:
    """Transport-neutral application service for one or more agent turns."""

    def __init__(self, components: RuntimeComponents | Mapping[str, Any]) -> None:
        if isinstance(components, RuntimeComponents):
            self._components = components
        else:
            self._components = RuntimeComponents(components)


    def _turn_runner(self) -> Any:
        """The composition's own loop, or a stock one bound to **this** container.

        A composition's own loop wins when the composition supplied one: providing
        ``agent_loop`` is what makes the loop replaceable, and a loop a composition chose
        is not second-guessed.

        A **stock** ``TurnRunner`` does not win, and that distinction is not cosmetic.  A
        loop carries the container it was bound to, and ``agent_loop`` is provided once,
        over the composition's container.  A *derived* container -- a web session, which
        has its own agent, registry, workspace and output paths -- copies the composition's
        mapping, so it contains that service and would run its turns through the
        composition's ``agent``.  Measured: switching provider in the settings page rebuilt
        the session runtime correctly (new client, new endpoint) and the next message still
        went to the old provider, because the turn ran through the composition's loop.

        So: rebind when it is stock, use it verbatim when it is not.  This is the rule the
        pre-plugin code had (it tested ``isinstance(turn_runner, TurnRunner)`` too), and it
        survives the loop becoming a plugin -- "the loop is replaceable" and "one loop
        instance serves every container" are different statements, and only the first is
        wanted.
        """
        loop = resolve(self._components, "agent_loop")
        if loop is not None and not isinstance(loop, TurnRunner):
            return loop
        return TurnRunner.bind(self._components)

    def _skill_catalog(self) -> Any:
        return resolve(self._components, "skill_catalog")

    def _plugin_catalog(self) -> Any:
        return resolve(self._components, "plugin_catalog")

    @staticmethod
    def _context_metadata(state: RuntimeSessionState) -> Any:
        return getattr(state.ctx, "metadata", None)

    def _metadata_for_prompt_submit(self, turn_input: TurnInput) -> dict[str, Any]:
        metadata = dict(turn_input.metadata)
        return {
            **metadata,
            "channel": turn_input.channel_name,
            "session_id": turn_input.session_id,
        }

    def _prepare_skill_request(self, text: str, state: RuntimeSessionState) -> str:
        skill_catalog = self._skill_catalog()
        ctx_metadata = self._context_metadata(state)
        preset = (
            list(ctx_metadata.get("required_skills_preset") or [])
            if ctx_metadata is not None
            else []
        )
        if skill_catalog is None:
            if ctx_metadata is not None:
                ctx_metadata.pop("required_skills", None)
            return text
        if preset:
            resolved = []
            for skill_id in preset:
                bundle = skill_catalog.get(str(skill_id))
                if bundle is None or not bundle.user_invocable:
                    raise ValueError(f"required skill is unavailable: {skill_id}")
                resolved.append(bundle.id)
            if ctx_metadata is not None:
                ctx_metadata["required_skills"] = resolved
            return text
        normalized, required_skills = prepare_user_message_for_skills(
            text,
            skill_catalog,
        )
        if required_skills:
            if ctx_metadata is not None:
                ctx_metadata["required_skills"] = required_skills
            return normalized
        if ctx_metadata is not None:
            ctx_metadata.pop("required_skills", None)
        return text

    async def _apply_prompt_submit_hooks(
        self,
        text: str,
        turn_input: TurnInput,
        sink: Any,
    ) -> tuple[bool, str]:
        plugin_catalog = self._plugin_catalog()
        if plugin_catalog is None:
            return False, text

        submit_result = await plugin_catalog.fire_prompt_submit(
            text,
            self._metadata_for_prompt_submit(turn_input),
        )
        if submit_result.action == "block":
            if sink is not None:
                sink.on_status(
                    f"Message blocked: {submit_result.message}",
                    level="warning",
                )
                await self._drain_if_supported(sink)
            return True, str(submit_result.message or "")
        if submit_result.context:
            return False, f"[{submit_result.context}]\n\n{text}"
        return False, text

    def _refresh_skill_prompt_if_needed(
        self,
        state: RuntimeSessionState,
    ) -> None:
        skill_catalog = self._skill_catalog()
        if skill_catalog is None or not skill_catalog.consume_dirty():
            return
        import agent as agent_module

        shared_refreshed = agent_module._compose_system_prompt(
            require_service(self._components, "base_system_prompt"),
            require_service(self._components, "registry"),
            require_service(self._components, "workspace_root"),
            require_service(self._components, "output_dir"),
            skill_catalog=skill_catalog,
            plugin_catalog=self._plugin_catalog(),
        )
        publish_system_prompt(self._components.values, shared_refreshed)

        if state.base_system_prompt_override is not None:
            refreshed = agent_module._compose_system_prompt(
                state.base_system_prompt_override,
                require_service(self._components, "registry"),
                require_service(self._components, "workspace_root"),
                require_service(self._components, "output_dir"),
                skill_catalog=skill_catalog,
                plugin_catalog=self._plugin_catalog(),
            )
            state.system_prompt_override = refreshed
        else:
            refreshed = shared_refreshed
        state.set_session_prompt(refreshed)

    def _refresh_component_prompt_if_needed(
        self,
        state: RuntimeSessionState,
    ) -> None:
        prompt = (
            state.system_prompt_override
            if state.system_prompt_override is not None
            else require_service(self._components, "system_prompt")
        )
        if not isinstance(prompt, str) or not prompt:
            return
        # Runs before every continuation step while ctx.messages may already
        # have been compacted past the original request.
        state.set_session_prompt(prompt)

    @staticmethod
    def _message_already_answered(
        state: Any, session_id: str, message_id: str
    ) -> bool:
        """Whether this message already has an assistant answer on record."""
        check = getattr(getattr(state, "context_manager", None), "has_answer", None)
        if not message_id or not callable(check):
            return False
        from agent import shared as _shared

        answered = False
        with _shared._suppress_with_log(
            "could not tell whether a message was already answered"
        ):
            answered = bool(
                check(session_id=str(session_id or ""), message_id=str(message_id))
            )
        return answered

    @staticmethod
    async def _drain_if_supported(sink: Any) -> None:
        drain = getattr(sink, "drain", None)
        if callable(drain):
            result = drain()
            if hasattr(result, "__await__"):
                await result

    def _schedule_target_for_turn(self, turn_input: TurnInput) -> dict[str, Any] | None:
        """Derive a schedule delivery target from turn metadata.

        Channel-agnostic: any transport that provides ``chat_id`` in metadata
        can use channel delivery for scheduled tasks.
        """
        chat_id = turn_input.metadata.get("chat_id")
        if not chat_id:
            return None
        return {
            "delivery_mode": "channel",
            "target_type": turn_input.metadata.get("target_type", "feishu_chat"),
            "chat_id": chat_id,
            "chat_type": turn_input.metadata.get("chat_type", "p2p"),
        }

    def _publish_turn_runtime_metadata(
        self,
        turn_input: TurnInput,
        state: RuntimeSessionState,
    ) -> None:
        ctx_metadata = self._context_metadata(state)
        if ctx_metadata is None:
            return

        metadata = dict(turn_input.metadata)
        ctx_metadata["session_id"] = turn_input.session_id
        ctx_metadata["channel_name"] = turn_input.channel_name
        context_manager = getattr(state, "context_manager", None)
        if context_manager is None:
            ctx_metadata.pop("context_manager", None)
        else:
            ctx_metadata["context_manager"] = context_manager
        user_id = str(metadata.get("user_id") or "")
        if user_id:
            ctx_metadata["user_id"] = user_id
        else:
            ctx_metadata.pop("user_id", None)
        if state.model_override is None:
            ctx_metadata.pop("model_override", None)
        else:
            ctx_metadata["model_override"] = state.model_override
        ctx_metadata["turn_id"] = str(
            metadata.get("turn_id")
            or metadata.get("message_id")
            or f"{turn_input.session_id}:{state.turn_count + 1}"
        )
        for key in ("heartbeat_enabled", "heartbeat_interval", "heartbeat_path"):
            if key in metadata:
                ctx_metadata[key] = metadata[key]

        if "heartbeat_path" not in ctx_metadata:
            output_dir = resolve(self._components, "output_dir")
            if output_dir:
                ctx_metadata["heartbeat_path"] = (
                    Path(output_dir)
                    / "runtime"
                    / "health"
                    / heartbeat_path_for_session(turn_input.session_id).name
                )

    def _drain_collector(self, turn_input: TurnInput) -> tuple[RuntimeEvent, ...]:
        collector = _active_event_collector.get()
        if collector is None:
            return ()
        return tuple(
            RuntimeEvent(
                name=e.name,
                session_id=turn_input.session_id,
                channel_name=turn_input.channel_name,
                fields=dict(e.fields),
                metadata=dict(turn_input.metadata),
            )
            for e in collector.drain()
        )

    async def handle_turn(
        self,
        turn_input: TurnInput,
        state: RuntimeSessionState,
        *,
        sink: Any = None,
        stream_callback: Callable[[str], None] | None = None,
        max_continuations: int = 1,
    ) -> TurnExecution:
        """Run a normalized turn through hooks, tools, memory, and continuations.

        An ``EventCollector`` is activated for the duration of the turn so
        that tool executors, plugin hooks, and maintenance operations can
        emit ``RuntimeEvent`` facts into a single replayable stream.
        """
        collector = EventCollector()
        collector_token = _active_event_collector.set(collector)
        try:
            return await self._handle_turn_impl(
                turn_input, state, sink, stream_callback, max_continuations
            )
        finally:
            _active_event_collector.reset(collector_token)

    async def _handle_turn_impl(
        self,
        turn_input: TurnInput,
        state: RuntimeSessionState,
        sink: Any,
        stream_callback: Callable[[str], None] | None,
        max_continuations: int,
    ) -> TurnExecution:
        collector = _active_event_collector.get()
        if collector is None:  # defensive: caller must go through handle_turn
            raise RuntimeError(
                "AgentCore._handle_turn_impl called without an active EventCollector. "
                "Use handle_turn() instead."
            )

        skill_catalog = self._skill_catalog()
        ctx_metadata = self._context_metadata(state)
        if skill_catalog is not None and ctx_metadata is not None:
            ctx_metadata["skill_catalog"] = skill_catalog
        prompt = self._prepare_skill_request(turn_input.text, state)
        turn_metadata = dict(turn_input.metadata)
        turn_metadata.setdefault(
            "turn_id",
            str(turn_metadata.get("message_id") or uuid.uuid4().hex),
        )
        # What this run *writes* is identified by this run, not by the message.
        # A message taken up twice produces two answers, and both were shown to
        # the reader -- keying the assistant row by message plus iteration alone
        # let the second answer collide with the first and be dropped, so the
        # record kept *older* text than the client had already received.  Minted
        # once here it is stable for every write this run makes, which is what
        # keeps a retried write idempotent.
        run_id = str(turn_metadata.get("run_id") or uuid.uuid4().hex[:12])
        turn_metadata["run_id"] = run_id
        # One message, one turn -- while it is running.  A re-delivery (a client
        # resend, a replay after a reconnect) must not start a second tool loop
        # for the same message: measurably, one message ran two *interleaved*
        # loops under a single turn id (steps 1..42 and 1..56 alternating),
        # doubling its model calls and tool calls, and the second answer could
        # not be recorded under the first one's key.  Refusing the duplicate is
        # what makes "at-least-once delivery" stop costing twice.
        turn_key = str(turn_metadata["turn_id"])
        in_flight = getattr(state, "turns_in_flight", None)
        if in_flight is None:
            in_flight = set()
            state.turns_in_flight = in_flight
        if turn_key in in_flight:
            return TurnExecution(
                result=TurnResult(text=""),
                continuation_rounds=0,
                blocked=True,
                block_reason="this message is already being answered",
            )
        in_flight.add(turn_key)
        prompted_input = TurnInput(
            text=prompt,
            session_id=turn_input.session_id,
            channel_name=turn_input.channel_name,
            metadata=turn_input.metadata,
            attachments=turn_input.attachments,
        )
        if state.context_manager is not None:
            begin_turn = getattr(state.context_manager, "begin_turn", None)
            if callable(begin_turn):
                # ``begin_turn`` writes the user row and says whether *it* wrote
                # it: False means this message was journalled before.  That is
                # the third line of defence, behind the in-flight and queue
                # guards -- the one that catches a delivery arriving down a path
                # neither of them saw.  Only an already *answered* message is
                # refused: one whose earlier attempt died before answering is a
                # legitimate retry and must run.
                journalled_now = begin_turn(
                    user_content=turn_input.text,
                    channel=turn_input.channel_name,
                    message_id=str(turn_metadata["turn_id"]),
                    metadata=turn_metadata,
                )
                if journalled_now is False and self._message_already_answered(
                    state, turn_input.session_id, turn_key
                ):
                    in_flight.discard(turn_key)
                    return TurnExecution(
                        result=TurnResult(text=""),
                        continuation_rounds=0,
                        blocked=True,
                        block_reason="this message has already been answered",
                    )
        blocked, prompt = await self._apply_prompt_submit_hooks(
            prompt,
            prompted_input,
            sink,
        )
        if blocked:
            collector.emit("prompt_blocked", reason=prompt)
            return TurnExecution(
                result=TurnResult(text=""),
                continuation_rounds=0,
                blocked=True,
                block_reason=prompt,
                events=self._drain_collector(prompted_input),
            )

        runtime_input = TurnInput(
            text=prompt,
            session_id=turn_input.session_id,
            channel_name=turn_input.channel_name,
            metadata=turn_metadata,
            attachments=turn_input.attachments,
        )

        state.ensure_task_context(prompt)
        if state.context_manager:
            state.context_manager.mark_activity()
        collector.emit(
            "turn_started",
            text_len=len(prompt),
            text_preview=prompt[:80].replace("\n", " "),
        )
        self._publish_turn_runtime_metadata(runtime_input, state)

        active_sink_token = _active_sink.set(sink) if sink is not None else None
        schedule_target = self._schedule_target_for_turn(prompted_input)
        schedule_target_token = (
            _active_schedule_target.set(schedule_target)
            if schedule_target is not None
            else None
        )
        # Set from turn metadata rather than derived, because the depth a
        # cascade has already reached is not visible from anything in the
        # conversation -- it is a property of the run that is being served.
        signal_context = prompted_input.metadata.get("signal_context")
        signal_context_token = (
            _active_signal_context.set(dict(signal_context))
            if isinstance(signal_context, dict)
            else None
        )
        try:
            final_result = TurnResult(text="")
            iteration_prompt = prompt
            continuation_rounds = 0
            cancelled_by_user = False
            completion_attempted = False
            # Publish the session cancel token so send_message() can check it
            # at every tool-loop boundary.
            if state.cancel_token is not None:
                state.ctx.metadata["cancel_token"] = state.cancel_token
            # Publish a shared reference to the mailbox so send_message's
            # tool-loop can drain it at every iteration without taking
            # a dependency on RuntimeSessionState.  Bare-object test
            # contexts (no .metadata attribute) skip this.
            if hasattr(state.ctx, "metadata"):
                state.ctx.metadata["pending_messages"] = state.pending_messages
            try:
                for iteration_index in range(max(1, int(max_continuations) + 1)):
                    continuation_rounds = iteration_index + 1
                    completion_attempted = False
                    state.ensure_task_context(iteration_prompt)
                    self._refresh_component_prompt_if_needed(state)
                    self._refresh_skill_prompt_if_needed(state)
                    iteration_metadata = dict(turn_metadata)
                    iteration_metadata["completion_id"] = (
                        f"{turn_metadata['turn_id']}:run:{run_id}"
                        f":completion:{iteration_index + 1}"
                    )
                    current_input = TurnInput(
                        text=iteration_prompt,
                        session_id=turn_input.session_id,
                        channel_name=turn_input.channel_name,
                        metadata=iteration_metadata,
                        attachments=(
                            turn_input.attachments if iteration_index == 0 else ()
                        ),
                    )
                    callback = (
                        stream_callback
                        if stream_callback is not None
                        else getattr(sink, "sync_stream_cb", None)
                    )
                    agent_started_at = time.perf_counter()
                    final_result = await self._turn_runner().run(
                        current_input,
                        state.ctx,
                        stream_callback=callback,
                    )
                    agent_duration_ms = round(
                        (time.perf_counter() - agent_started_at) * 1000, 1
                    )
                    collector.emit(
                        "agent_result_ready",
                        tool_calls=len(final_result.tool_calls),
                        error=bool(final_result.error),
                        content_len=len(final_result.text or ""),
                        content_preview=(final_result.text or "")[:80],
                        duration_ms=agent_duration_ms,
                    )
                    completion_attempted = True
                    completion_runner = self._turn_runner()
                    complete_turn = getattr(completion_runner, "complete_turn", None)
                    if not callable(complete_turn):
                        complete_turn = TurnRunner.bind(self._components).complete_turn
                    completion_input = TurnInput(
                        text=(turn_input.text if iteration_index == 0 else iteration_prompt),
                        session_id=turn_input.session_id,
                        channel_name=turn_input.channel_name,
                        metadata=iteration_metadata,
                        attachments=(turn_input.attachments if iteration_index == 0 else ()),
                    )
                    hook_results = await complete_turn(
                        completion_input,
                        state,
                        final_result,
                    )
                    if sink is not None:
                        sink.on_turn_complete(
                            final_result.text or "",
                            list(final_result.tool_calls),
                        )
                        if final_result.error:
                            sink.on_error(final_result.error)
                        await self._drain_if_supported(sink)
                    collector.emit("turn_response_delivered")
                    if final_result.error:
                        collector.emit("turn_error_reported", error=final_result.error)
                        if final_result.error == "cancelled_by_user":
                            collector.emit(
                                "turn_interrupted",
                                reason="cancelled_by_user",
                            )
                    continued = False
                    for hook_result in hook_results or []:
                        if (
                            getattr(hook_result, "action", "") == "continue"
                            and getattr(hook_result, "message", "")
                        ):
                            iteration_prompt = str(hook_result.message)
                            collector.emit(
                                "turn_continued",
                                next_prompt=iteration_prompt,
                            )
                            continued = True
                            break
                    if not continued:
                        break
            except KeyboardInterrupt:
                if state.cancel_token is not None:
                    state.cancel_token.cancel()
                cancelled_by_user = True
                from agent import shared as _shared

                final_result = TurnResult(
                    text=_shared._cancelled_by_user_text(turn_input.text),
                    error="cancelled_by_user",
                )
                collector.emit(
                    "agent_result_ready",
                    tool_calls=len(final_result.tool_calls),
                    error=True,
                    content_len=len(final_result.text),
                    content_preview=final_result.text,
                )
                collector.emit(
                    "turn_interrupted",
                    reason="cancelled_by_user",
                )
                # Run post-turn maintenance so the interrupted turn is
                # recorded in staging and not lost.
                await self._turn_runner().complete_turn(
                    TurnInput(
                        text=iteration_prompt,
                        session_id=turn_input.session_id,
                        channel_name=turn_input.channel_name,
                        metadata=locals().get("iteration_metadata", turn_metadata),
                    ),
                    state,
                    final_result,
                )
                if sink is not None:
                    sink.on_turn_complete(
                        final_result.text,
                        list(final_result.tool_calls),
                    )
                    await self._drain_if_supported(sink)
            return TurnExecution(
                result=final_result,
                continuation_rounds=continuation_rounds,
                events=self._drain_collector(
                    prompted_input  # canonical turn identity
                ),
            )
        except Exception as exc:
            failed_result = TurnResult(text="", error=str(exc))
            collector.emit("turn_failed", error=str(exc))
            failed_input = locals().get("current_input")
            if isinstance(failed_input, TurnInput) and not completion_attempted:
                try:
                    await self._turn_runner().complete_turn(
                        failed_input,
                        state,
                        failed_result,
                    )
                except Exception as maintenance_exc:
                    collector.emit(
                        "turn_completion_failed",
                        error=str(maintenance_exc),
                    )
            return TurnExecution(
                result=failed_result,
                continuation_rounds=continuation_rounds,
                events=self._drain_collector(turn_input),
            )
        finally:
            in_flight.discard(turn_key)
            if state.cancel_token is not None:
                state.ctx.metadata.pop("cancel_token", None)
            if schedule_target_token is not None:
                _active_schedule_target.reset(schedule_target_token)
            if signal_context_token is not None:
                _active_signal_context.reset(signal_context_token)
            if active_sink_token is not None:
                _active_sink.reset(active_sink_token)


def agent_core_for_components(components: dict) -> "AgentCore":
    """The ``AgentCore`` a set of components is run by.

    An ``AgentCore`` already present is returned as-is, so two callers holding the same
    mapping drive the same core rather than one apiece -- and one built here wraps the
    mapping directly.  Relocated from ``cli.py``: the scheduler's run executor needs it,
    and that executor is moving into ``agent/scheduler``, which must not import the CLI.
    """
    agent_core = components.get("agent_core")
    if agent_core is None or isinstance(agent_core, AgentCore):
        return AgentCore(RuntimeComponents(components))
    return agent_core
