from __future__ import annotations

import asyncio
import atexit
import contextvars
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from functools import partial
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import traceback
from typing import Any, Callable, Optional
from dataclasses import dataclass, field

import mcp

from agent import shared
from agent.core.output import _active_sink
from agent.tools import user_tools
from agent.tools.scoped import as_registry

_active_schedule_target: contextvars.ContextVar[Optional[dict[str, Any]]] = (
    contextvars.ContextVar("_active_schedule_target", default=None)
)

#: The signal this turn is answering, when the turn is a scheduled run that
#: was woken by one.  A tool that emits a signal from inside such a run has to
#: know how far from the cascade root it already is, or every hop would look
#: like a fresh start and the depth ceiling would never be reached.  Invisible
#: to ordinary conversation turns, where it stays ``None``.
_active_signal_context: contextvars.ContextVar[Optional[dict[str, Any]]] = (
    contextvars.ContextVar("_active_signal_context", default=None)
)


@dataclass
class RunSelfReport:
    """What a scheduled run says about whether it did the job.

    A mutable object rather than the verdict itself, and that is the whole
    reason this is a class instead of a string.  Sync tools are dispatched into
    a worker thread under ``copy_context()``, so a contextvar *set* inside a
    tool would be written to the copy and never seen by the caller that needs
    it.  Mutating a shared object survives the copy.

    Only a failure is worth acting on, so the tool that writes this records
    "could not do it" and stays silent otherwise; a run that believes it
    succeeded is making a claim, not producing evidence.
    """

    verdict: str = ""
    reason: str = ""


#: Present only inside a scheduled run, so ``report_outcome`` can reach the
#: executor that is waiting to hear it.  ``None`` in an ordinary conversation,
#: where there is nothing to report to.
_active_run_self_report: contextvars.ContextVar[Optional[RunSelfReport]] = (
    contextvars.ContextVar("_active_run_self_report", default=None)
)

# ── Synchronous tool dispatch ────────────────────────────────────────────────
#
# Most tools (file I/O, memory/SQLite, scheduler CRUD) are plain sync
# functions.  Calling them inline stalls the whole event loop: channel
# heartbeats stop, other sessions' messages queue up, and — worst — the
# ToolExecutor's own timeout/progress machinery cannot run, so
# ``tool_timeout_seconds`` silently does not apply to them.  Dispatching
# through a dedicated pool keeps the loop responsive and makes those
# timeouts real.
#
# The pool is deliberately *not* the loop's default executor: channels
# (notably Feishu) push their blocking SDK calls through
# ``run_in_executor(None, ...)``, and a burst of slow tools must not be able
# to starve message delivery.
_SYNC_TOOL_EXECUTOR: Optional[ThreadPoolExecutor] = None
_SYNC_TOOL_EXECUTOR_LOCK = threading.Lock()


def _sync_tool_max_workers() -> int:
    configured = os.environ.get("SIMPLE_SYNC_TOOL_WORKERS", "").strip()
    if configured.isdigit() and int(configured) > 0:
        return int(configured)
    return max(4, min(16, (os.cpu_count() or 4) * 2))


def sync_tool_executor() -> ThreadPoolExecutor:
    """Return the process-wide pool used to run synchronous tools."""
    global _SYNC_TOOL_EXECUTOR
    if _SYNC_TOOL_EXECUTOR is None:
        with _SYNC_TOOL_EXECUTOR_LOCK:
            if _SYNC_TOOL_EXECUTOR is None:
                _SYNC_TOOL_EXECUTOR = ThreadPoolExecutor(
                    max_workers=_sync_tool_max_workers(),
                    thread_name_prefix="agent-sync-tool",
                )
                # Runs before ThreadPoolExecutor's own atexit join (LIFO), so
                # tools still sitting in the queue are dropped rather than
                # started during interpreter shutdown.
                atexit.register(shutdown_sync_tool_executor)
    return _SYNC_TOOL_EXECUTOR


def shutdown_sync_tool_executor(wait: bool = False) -> None:
    """Tear the pool down at process shutdown.  Safe to call more than once."""
    global _SYNC_TOOL_EXECUTOR
    with _SYNC_TOOL_EXECUTOR_LOCK:
        executor, _SYNC_TOOL_EXECUTOR = _SYNC_TOOL_EXECUTOR, None
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=True)


async def _call_sync_tool(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking tool off-loop, preserving the caller's context.

    ``copy_context()`` carries ``_active_sink``, ``_active_agent_context`` and
    the registry's ``_context_override`` into the worker thread, so tools that
    resolve per-turn state keep seeing the turn that invoked them.
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(
        sync_tool_executor(), partial(ctx.run, partial(fn, *args, **kwargs))
    )


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict
    fn: Callable
    source: str = "runtime"
    capabilities: frozenset[str] = field(default_factory=frozenset)
    authorizer: Optional[Callable[[dict, "ToolRegistry"], Optional[dict]]] = None


@dataclass(frozen=True)
class ToolView:
    """A lens over a :class:`ToolRegistry`: which of its tools a copy exposes.

    The three scopes this codebase already needed -- a whole-process registry, a
    per-session registry with one source family removed, and a per-sub-agent
    registry bounded by capability -- differ only in *which tools they accept*.
    Everything else they do is the same copy, so the difference is data.

    Every field is a separate narrowing and all of them compose: a tool is
    exposed only when each specified rule admits it.  ``exclude_source_prefixes``
    and ``capabilities_exclude`` are applied after ``capabilities_include``, so
    a broader inclusion can never re-admit something a narrower rule removed.
    """

    #: Final say.  ``None`` accepts whatever the other rules accept.
    accepts: Optional[Callable[[ToolDef], bool]] = None
    #: Drop tools whose ``source`` starts with any of these.
    exclude_source_prefixes: tuple[str, ...] = ()
    #: The capability bound for a bounded view.  ``None`` is unconstrained; an
    #: empty frozenset admits nothing at all, because "no capability is within
    #: this bound" and "this view is unbounded" have to be different answers.
    capabilities_include: Optional[frozenset[str]] = None
    #: Drop any tool carrying one of these capabilities.
    capabilities_exclude: frozenset[str] = frozenset()

    def accepts_tool(self, tool: "ToolDef") -> bool:
        if self.accepts is not None and not self.accepts(tool):
            return False
        for prefix in self.exclude_source_prefixes:
            if tool.source.startswith(prefix):
                return False
        if self.capabilities_include is not None:
            # A tool that declares nothing is not admitted to a bounded view.
            # Unknown is not the same as safe, and the sub-agent profiles
            # depended on that reading.
            if not tool.capabilities:
                return False
            if not tool.capabilities.issubset(self.capabilities_include):
                return False
        if self.capabilities_exclude & tool.capabilities:
            return False
        return True


# JSON Schema keywords this registry can validate deterministically.  Schemas
# using any other keyword fall back to legacy behavior instead of pretending
# they were fully validated.
_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "pattern",
        "format",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "const",
        "multipleOf",
        "$ref",
    }
)

_JSON_TYPE_MAP = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _invalid_request(message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": "invalid_request",
            "message": message,
            "details": {},
            "retryable": False,
        },
    }


def _schema_has_unsupported_keywords(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return True
    return any(keyword in schema for keyword in _UNSUPPORTED_SCHEMA_KEYWORDS)


def _validate_value(value: Any, schema: Any, field: str) -> Optional[dict]:
    if _schema_has_unsupported_keywords(schema):
        return None
    declared = schema.get("type", "")
    if isinstance(declared, list):
        # JSON Schema lets ``type`` be a list ("string or null"), and this used
        # to be looked up in a dict as if it were a string: a schema written
        # that way failed every call with "unhashable type: 'list'", raised
        # from inside the validator and naming neither the field nor the tool.
        # A schema nobody could write correctly read as a tool that was broken.
        options = [str(item) for item in declared]
        if value is None:
            if "null" not in options:
                return _invalid_request(
                    f"invalid value for field '{field}': null is not one of "
                    + ", ".join(options)
                )
        else:
            accepted = tuple(
                _JSON_TYPE_MAP[item] for item in options if item in _JSON_TYPE_MAP
            )
            if accepted and not isinstance(value, accepted):
                return _invalid_request(
                    f"invalid value for field '{field}': expected "
                    + " or ".join(options)
                    + f", got {type(value).__name__}"
                )
    else:
        expected = _JSON_TYPE_MAP.get(declared or "")
        if expected is not None:
            if declared == "integer" and isinstance(value, bool):
                return _invalid_request(
                    f"invalid value for field '{field}': expected integer"
                )
            if not isinstance(value, expected) or (
                declared == "boolean" and not isinstance(value, bool)
            ):
                return _invalid_request(
                    f"invalid value for field '{field}': expected "
                    f"{declared}, got {type(value).__name__}"
                )
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values and value not in enum_values:
        return _invalid_request(
            f"invalid value for field '{field}': {value!r} is not one of "
            + ", ".join(repr(item) for item in enum_values)
        )
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if minimum is not None and value < minimum:
            return _invalid_request(
                f"invalid value for field '{field}': must be >= {minimum}"
            )
        if maximum is not None and value > maximum:
            return _invalid_request(
                f"invalid value for field '{field}': must be <= {maximum}"
            )
    if declared == "array" or (isinstance(declared, list) and "array" in declared):
        items_schema = schema.get("items")
        if isinstance(items_schema, dict) and isinstance(value, list):
            for index, item in enumerate(value):
                error = _validate_value(item, items_schema, f"{field}[{index}]")
                if error is not None:
                    return error
    return None


def _validate_tool_input(tool_input: Any, schema: Any) -> Optional[dict]:
    if not isinstance(tool_input, dict):
        return _invalid_request("tool input must be an object")
    if _schema_has_unsupported_keywords(schema) or schema.get("type") != "object":
        return None  # unsupported schema shape -> legacy behavior
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    for key in schema.get("required", []):
        # A property that declares a default is supplied by the handler when
        # absent, so it is not a hard requirement regardless of the schema's
        # legacy "required" list.
        prop_schema = properties.get(key)
        if isinstance(prop_schema, dict) and "default" in prop_schema:
            continue
        if key not in tool_input:
            return _invalid_request(f"missing required field: {key}")
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(tool_input) - set(properties))
        if unknown:
            return _invalid_request(
                "unknown field(s): " + ", ".join(unknown)
            )
    for key, value in tool_input.items():
        prop_schema = properties.get(key)
        if prop_schema is None:
            continue
        error = _validate_value(value, prop_schema, key)
        if error is not None:
            return error
    return None


class ToolRegistry:
    """Central registry for all tools."""

    def scoped(self, ctx: Any) -> Any:
        """A view of this registry bound to *ctx*'s lifetime.

        The kernel calls this when a plugin reaches ``ctx.<name>`` for the registry,
        so a registration made through the view is undone when the plugin unloads --
        see :mod:`agent.tools.scoped`.  Declared here rather than inferred: the kernel
        asks a service whether it supports scoping, and a service that does not is
        handed over unchanged and keeps its identity for an ``is`` check.
        """
        from agent.tools.scoped import ScopedTools

        return ScopedTools(self, ctx)

    _DEFAULT_TOOL_CAPABILITIES: dict[tuple[str, str], frozenset[str]] = {
        ("builtin", "current_time"): frozenset({"read"}),
        ("builtin", "read_file"): frozenset({"read"}),
        ("builtin", "list_files"): frozenset({"read"}),
        ("builtin", "memory_read"): frozenset({"read"}),
        ("builtin", "memory_search"): frozenset({"read"}),
        ("builtin", "memory_index"): frozenset({"read"}),
        ("builtin", "context_retrieve"): frozenset({"read"}),
        ("builtin", "schedule_list"): frozenset({"read"}),
        ("builtin", "schedule_runs"): frozenset({"read"}),
        ("builtin", "commitments"): frozenset({"read"}),
        # A promise is a row written to this machine's scheduler, so it is a
        # state write -- but not `requires_intent`: a promise may be the
        # agent's own initiative, and there is no user sentence to quote.
        ("builtin", "commit"): frozenset({"state_write"}),
        ("builtin", "web_search"): frozenset({"read"}),
        ("builtin", "web_fetch"): frozenset({"read"}),
        ("builtin", "tavily_search"): frozenset({"read"}),
        # Five tools were missing from this table entirely, which read as
        # "no capabilities" -- the same answer as a tool nobody had classified
        # yet, and the same one the workflow trio above was rescued from.  A
        # profile that allows reads dropped the read tools; `report_outcome`
        # records a verdict into the run, so it is a state write (and it takes
        # no `intent`, so it is not `requires_intent` -- its whole purpose is
        # the declaration).
        ("builtin", "list_tools"): frozenset({"read"}),
        ("builtin", "list_signals"): frozenset({"read"}),
        ("builtin", "list_installed_plugins"): frozenset({"read"}),
        ("builtin", "read_step_output"): frozenset({"read"}),
        ("builtin", "report_outcome"): frozenset({"state_write"}),
        ("builtin", "write_file"): frozenset({"output_write"}),
        ("builtin", "edit_file"): frozenset({"output_write"}),
        ("builtin", "clean_output"): frozenset({"output_write"}),
        ("builtin", "clear_context"): frozenset({"state_write"}),
        ("builtin", "shell"): frozenset({"shell", "requires_intent"}),
        ("builtin", "transcribe_audio"): frozenset({"read"}),
        ("builtin", "send_file"): frozenset({"side_effect"}),
        ("builtin", "memory_write"): frozenset({"state_write"}),
        ("builtin", "set_identity"): frozenset({"state_write"}),
        ("builtin", "memory_clear"): frozenset({"state_write"}),
        # The one creation verb: a task, or a chain of them (see
        # `ScheduleTools.automation_create`). It replaced `schedule_create` and
        # `workflow_create`, whose overlap made "which tool?" a question the model
        # had to answer by guessing.
        ("builtin", "automation_create"): frozenset({"state_write", "requires_request"}),
        # Declared at the registration site rather than here, like the rest of
        # the schedule maintenance half: see the note below on why a destructive
        # schedule tool declares its intent.  The default here has to agree with
        # that declaration or a reader takes the table for the answer.
        ("builtin", "schedule_delete"): frozenset({"state_write", "requires_intent"}),
        # The maintenance half of the schedule tools, and the split is
        # deliberate.  `requires_request` is "nobody asked, so propose it
        # instead"; `requires_intent` is "say what you are doing", which is what
        # these need.  Correcting a task that is already failing is the case
        # where the asker's own sentence is hardest to point at -- they asked
        # why it broke, not for a particular field to change -- and refusing the
        # fix would leave the only move as delete-and-recreate, which is the
        # thing an edit exists to replace.  So the gate is a self-declaration,
        # like ``shell``'s, and it lands in the audit trail where a task that
        # changed under somebody can be read back.
        #
        # `schedule_cancel` is here rather than ungated because cancelling is
        # destructive to the work in flight, and one sentence naming the run is
        # cheap next to that.
        ("builtin", "schedule_update"): frozenset({"state_write", "requires_intent"}),
        ("builtin", "schedule_set_enabled"): frozenset({"state_write", "requires_intent"}),
        ("builtin", "schedule_run"): frozenset({"state_write", "requires_intent"}),
        ("builtin", "schedule_cancel"): frozenset({"state_write", "requires_intent"}),
        # The workflow trio was missing from this table entirely, which read as
        # "no capabilities" -- the same answer as a tool nobody had classified
        # yet.  Classified here because `workflow_create` now has to carry the
        # request requirement, and a group where half the entries exist is how
        # the next reader concludes the other half was considered.
        ("builtin", "workflow_list"): frozenset({"read"}),
        ("builtin", "workflow_delete"): frozenset({"state_write", "requires_intent"}),
        # Same reasoning as `schedule_update`: rebuilding a chain instead of
        # editing it is what leaves steps behind whose run history is stranded,
        # so the edit is the move that has to be available.
        ("builtin", "workflow_update"): frozenset({"state_write", "requires_intent"}),
        # Producing an action nobody asked for is what `requires_request`
        # refuses, and emitting a signal is an action: it starts whatever
        # subscribed to that name.  The stray emission that accompanied the
        # unasked task this was written for is the same failure with a shorter
        # blast radius, so it gets the same check.
        #
        # The two deletes carry `requires_intent` rather than
        # `requires_request`, and the difference is the one the distinction
        # exists for.  A creation is an answer to a question, so refusing an
        # unasked one is right; tidying up after an edit is not, and demanding
        # the user's words for it would fail on the sentence that never named a
        # deletion.  That is the argument against `requires_request`, and it is
        # not an argument against a self-declaration: `schedule_cancel` already
        # declares, deleting a task takes its run history with it -- strictly
        # more than cancelling does -- and once the schemas are sent for the
        # life of the session (see `ContextAssembler.select_tools`), a delete
        # nobody chose cannot be left as the one destructive call with nothing
        # in front of it.
        ("builtin", "emit_signal"): frozenset({"state_write", "requires_request"}),
        ("runtime:skill", "activate_skill"): frozenset({"read"}),
        ("runtime:skill", "list_skill_files"): frozenset({"read"}),
        ("runtime:skill", "read_skill_file"): frozenset({"read"}),
        # The three tools that rewrite or remove an existing bundle are state
        # writes, but deliberately **not** `requires_intent`: that capability is
        # checked per tool and unconditionally, so putting it on `update_skill`
        # would also demand a reason for editing a bundle the agent created
        # itself -- the freedom this boundary exists to keep.  What actually
        # costs an intent is the *ownership of the bundle named in the call*,
        # which only a call-aware check can see; `SkillCatalog.register_tools`
        # installs that check as each tool's `authorizer`, and it refuses an
        # intent-less write to a hand-written or externally installed bundle.
        # `create_skill` has no such gate: a new bundle takes nothing from
        # anyone, and refuses an id that already exists.
        ("runtime:skill", "create_skill"): frozenset({"state_write"}),
        ("runtime:skill", "update_skill"): frozenset({"state_write"}),
        ("runtime:skill", "delete_skill"): frozenset({"state_write"}),
        ("runtime:skill", "write_skill_file"): frozenset({"state_write"}),
        ("runtime:spawn", "spawn_agent"): frozenset({"orchestration"}),
    }

    def __init__(self, console: Optional[Any] = None):
        self._tools: dict[str, ToolDef] = {}
        self._prompt_generation: int = 0
        self._context: dict[str, Any] = {}
        self._context_override: contextvars.ContextVar[Optional[dict[str, Any]]] = (
            contextvars.ContextVar("tool_registry_context_override", default=None)
        )
        self.console = console
        # Cache for to_anthropic_format(), invalidated by _prompt_generation.
        # The tool list is rebuilt once per registration change rather than on
        # every tool-loop iteration; register()/unregister_*() bump the counter.
        self._anthropic_tools_cache: Optional[list[dict]] = None
        self._anthropic_tools_generation: int = -1

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        fn: Callable,
        *,
        replace: bool = False,
        source: str = "runtime",
        capabilities: tuple[str, ...] | list[str] | set[str] | frozenset[str] | None = None,
        authorizer: Optional[
            Callable[[dict, "ToolRegistry"], Optional[dict]]
        ] = None,
    ) -> "ToolDef":
        """Register *name*, returning the definition that was installed.

        The return value is the contribution's **identity**: a caller that wants its
        registration undone only while it is still the one installed compares this
        object.  A name-keyed undo cannot do that -- it lets the first contributor's
        unload remove the second contributor's tool.
        """
        if name in self._tools:
            existing = self._tools[name]
            if not replace:
                raise ValueError(
                    f"Tool '{name}' is already registered by source '{existing.source}'. "
                    "Pass replace=True to overwrite it."
                )
            if existing.source != source:
                raise ValueError(
                    f"Tool '{name}' is already registered by source '{existing.source}'. "
                    f"Only the same source may replace it; got '{source}'."
                )
        definition = ToolDef(
            name=name,
            description=description,
            parameters=parameters,
            fn=fn,
            source=source,
            capabilities=self._coerce_capabilities(name, source, capabilities),
            authorizer=authorizer,
        )
        self._tools[name] = definition
        self._prompt_generation += 1
        return definition

    @classmethod
    def _coerce_capabilities(
        cls,
        name: str,
        source: str,
        capabilities: tuple[str, ...] | list[str] | set[str] | frozenset[str] | None,
    ) -> frozenset[str]:
        if capabilities is None:
            return cls._DEFAULT_TOOL_CAPABILITIES.get((source, name), frozenset())
        return frozenset(str(item) for item in capabilities if str(item).strip())

    def tool(self, name: str, description: str, parameters: dict):
        def decorator(fn: Callable):
            self.register(name, description, parameters, fn)
            return fn

        return decorator

    def to_anthropic_format(self, names: Optional[set[str]] = None) -> list[dict]:
        if (
            self._anthropic_tools_cache is None
            or self._anthropic_tools_generation != self._prompt_generation
        ):
            self._anthropic_tools_cache = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in self._tools.values()
            ]
            self._anthropic_tools_generation = self._prompt_generation
        if names is None:
            return self._anthropic_tools_cache
        return [tool for tool in self._anthropic_tools_cache if tool["name"] in names]

    @staticmethod
    def _error_payload(tool_name: str, message: str) -> str:
        return json.dumps(
            {"ok": False, "tool": tool_name, "error": message},
            ensure_ascii=False,
        )

    @staticmethod
    def _tool_has_safe_kwargs(tool_input: dict) -> bool:
        """Return True if all keys in *tool_input* are valid Python identifiers."""
        return all(
            str(k).isidentifier()
            for k in tool_input
        )

    async def call(self, tool_name: str, tool_input: dict) -> str:
        if tool_name not in self._tools:
            return self._error_payload(tool_name, f"tool '{tool_name}' not found")
        override_registry: Optional["ToolRegistry"] = None
        override_token = None
        try:
            fn = self._tools[tool_name].fn
            owner = getattr(fn, "__self__", None)
            # The *owner's* registry, not this call's: the merge below has to be installed
            # on the object the tool implementation reads its context from.  A view that
            # is not recognised as the owner installs nothing, and the tool then silently
            # reads the composition's context -- a sub-agent's narrowed write scope and a
            # session's own workspace arrive exactly this way.  `as_registry` is the one
            # place that unwraps (see its docstring).
            owner_registry = as_registry(owner) if owner is not None else None
            if isinstance(owner_registry, ToolRegistry) and owner_registry is not self:
                merged_context = dict(owner_registry._context)
                merged_context.update(self._context)
                override_registry = owner_registry
                override_token = owner_registry._context_override.set(merged_context)
            validation_error = _validate_tool_input(tool_input, self._tools[tool_name].parameters)
            if validation_error is not None:
                return json.dumps(validation_error, ensure_ascii=False)
            authorizer = self._tools[tool_name].authorizer
            if authorizer is not None:
                denial = authorizer(tool_input, self)
                if denial is not None:
                    return json.dumps(denial, ensure_ascii=False)
            if self._tool_has_safe_kwargs(tool_input):
                if asyncio.iscoroutinefunction(fn):
                    result = await fn(**tool_input)
                else:
                    result = await _call_sync_tool(fn, **tool_input)
            else:
                # MCP tools and others with non-identifier parameter names
                # can't use ** unpacking — pass the dict directly.
                if asyncio.iscoroutinefunction(fn):
                    result = await fn(tool_input)
                else:
                    result = await _call_sync_tool(fn, tool_input)
            if isinstance(result, (dict, list)):
                return json.dumps(result, ensure_ascii=False)
            return "" if result is None else str(result)
        except (asyncio.TimeoutError, TimeoutError):
            return self._error_payload(tool_name, f"Timeout calling tool '{tool_name}'")
        except ValueError as e:
            return self._error_payload(
                tool_name, f"Invalid input for tool '{tool_name}': {e}"
            )
        except Exception as e:
            if self.console is not None:
                self.console.print(
                    f"[yellow]Tool '{tool_name}' failed: {e}\n{traceback.format_exc()}[/yellow]"
                )
            return self._error_payload(
                tool_name, f"Error calling tool '{tool_name}': {e}"
            )
        finally:
            if override_registry is not None and override_token is not None:
                override_registry._context_override.reset(override_token)

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def tool_capabilities(self, name: str) -> frozenset[str]:
        """Capabilities declared by a registered tool (empty if unknown)."""
        tool = self._tools.get(name)
        return tool.capabilities if tool is not None else frozenset()

    def tool_source(self, name: str) -> str:
        """Registration source of a tool (empty when it is not registered)."""
        tool = self._tools.get(name)
        return tool.source if tool is not None else ""

    def tools_with_capability(self, capability: str) -> list[str]:
        return [name for name, t in self._tools.items() if capability in t.capabilities]

    def set_context(self, key: str, value: Any) -> None:
        self._context[key] = value

    def get_context(self, key: str, default: Any = None) -> Any:
        override = self._context_override.get()
        if override is not None and key in override:
            return override[key]
        return self._context.get(key, default)

    def derive(
        self,
        view: ToolView,
        context: Optional[dict[str, Any]] = None,
    ) -> "ToolRegistry":
        """The registry a scope sees: *view*'s tools, over this context.

        The single implementation behind every tool-set restriction this
        codebase needs -- ``fork`` for a session, the sub-agent construction in
        ``BaseAgent``, and anything a plugin scope adds later.  Keeping one
        implementation matters because the alternative was three, and a rule
        that lived in only one of them read as a rule everywhere.

        Tool definitions are shared rather than copied: they are immutable after
        registration for the lifetime of a config revision, and a bound tool
        method has to keep pointing at its owner registry (``call`` installs
        this view's context as a ContextVar override while invoking it).
        """
        derived = type(self)(console=self.console)
        derived._tools = {
            name: tool
            for name, tool in self._tools.items()
            if view.accepts_tool(tool)
        }
        # Clone the mutable containers on the way in -- inherited and
        # overridden alike -- so neither a child's edit nor a parent's later
        # edit reaches the other.  Scalar context values (a policy object, a
        # path string) are shared on purpose: they are read-only by convention
        # and copying them would break identity checks.
        derived._context = {
            key: (
                type(value)(value)
                if isinstance(value, (list, dict, set))
                else value
            )
            for key, value in self._context.items()
        }
        if context:
            for key, value in context.items():
                if isinstance(value, (list, dict, set)):
                    value = type(value)(value)
                derived._context[key] = value
        derived._prompt_generation = self._prompt_generation
        return derived

    def fork(
        self,
        context: Optional[dict[str, Any]] = None,
        *,
        exclude_source_prefixes: tuple[str, ...] = (),
    ) -> "ToolRegistry":
        """Create a cheap session view over the registered tool functions.

        Kept as its own name because callers and tests use it; the behaviour is
        :meth:`derive` with a source-prefix view.
        """
        return self.derive(
            ToolView(exclude_source_prefixes=tuple(exclude_source_prefixes)),
            context,
        )

    def unregister_by_source_prefix(self, prefix: str) -> None:
        """Remove every tool whose ``source`` *starts with* *prefix*.

        A prefix, not an owner: ``plugin:foo`` also matches ``plugin:foobar``.  Kept
        for the composition's own groupings (``user_tool:``, ``mcp:<server>``), where
        the caller owns the whole namespace.  A **plugin's** contribution must not use
        it -- see :meth:`unregister_contribution`, which is what the scoped view
        records, because two plugin names may share a prefix.
        """
        for name in [
            n for n, tool in self._tools.items() if tool.source.startswith(prefix)
        ]:
            self._tools.pop(name, None)
        self._prompt_generation += 1

    def unregister_contribution(self, name: str, contributed: "ToolDef") -> None:
        """Remove *name* only while it is still *contributed*.

        The undo a scoped view records.  Identity rather than name or source, so a
        plugin that unloads cannot take down a tool another plugin has since
        installed under the same name -- and so ``plugin:foo`` cannot take
        ``plugin:foobar``'s tools with it.
        """
        if self._tools.get(name) is contributed:
            self._tools.pop(name, None)
            self._prompt_generation += 1



#: How long ``disconnect``/``reconnect`` wait for an owner task to finish its own
#: unwind.  Bounded because a server that hung during start-up must not hang the
#: turn that asked to restart it; ``False`` from :meth:`MCPClient._await_task` is
#: what "asked, not yet confirmed" is reported as.
_MCP_STOP_TIMEOUT = 10.0

#: How long one health probe may take before the server counts as not answering.  Short,
#: because the answer is "the child is wedged or gone" and waiting longer only delays the
#: reconnect; a genuinely busy server answers a ping from its own read loop.
_MCP_PING_TIMEOUT = 5.0


class _MCPServer:
    """One MCP server's live state.

    Private: the client owns these and hands callers plain dicts, so the
    bookkeeping can change without changing an interface with it.
    """

    __slots__ = ("cfg", "name", "stop", "task", "session", "tool_names", "state")

    def __init__(self, cfg: dict, name: str) -> None:
        self.cfg = cfg
        self.name = name
        #: This server's own shutdown request.  The task that entered its
        #: ``AsyncExitStack`` waits on it and leaves the stack itself -- see
        #: :meth:`MCPClient._run_server`, which is the whole reason it exists.
        self.stop = asyncio.Event()
        self.task: Optional[asyncio.Task[None]] = None
        self.session: Any = None
        #: The tool names this server registered.  Owned per server, so
        #: ``registered_tools`` is a count of what is actually installed rather
        #: than of what was installed once.
        self.tool_names: set[str] = set()
        #: ``connecting`` -> ``connected`` -> ``stopped``, or ``failed``.
        self.state = "connecting"


class MCPClient:
    """Connect to external MCP servers and inject tools into registry.

    A server's lifetime is its own, not the process's: :meth:`disconnect` stops
    one, :meth:`reconnect` starts one again, the ``mcp_reconnect`` tool is the
    model's handle on both, and :meth:`close` is the composition's -- it stops
    every server still running.

    The rule the whole design follows
    ---------------------------------
    **AnyIO cancel scopes must be exited by the task that entered them.**  The
    session ``mcp.stdio_client`` opens is one, so cancelling its task from
    outside would leave a scope to be exited by a task that never entered it: a
    ``RuntimeError`` from the SDK at best, a task that never unwinds at worst.
    No caller ever cancels these tasks, therefore.  Each one waits on its own
    ``stop`` event (and on the client-wide ``_shutdown``) and leaves ``async with
    AsyncExitStack()`` itself; :meth:`disconnect` only sets the event and awaits
    that same task -- which is also why it can say the stdio child is gone before
    it returns.
    """

    def __init__(self, registry: ToolRegistry):
        # This client *owns* a set of tools (by source prefix), so it needs the registry
        # rather than a contribution to it: a view has no
        # `unregister_by_source_prefix`, and `_forget_tools`' defensive `getattr` would
        # then find nothing while a disconnected server's process really had stopped.
        self.registry = as_registry(registry)
        #: Safe server name -> live state.  ``_server_key`` derives the name;
        #: ``_resolve`` maps a caller's spelling of it onto the same entry.
        self._servers: dict[str, _MCPServer] = {}
        self._server_tasks: list[asyncio.Task[None]] = []
        self._shutdown = asyncio.Event()
        # Two maps, two questions.  ``_configured`` is what this client is
        # expected to be running right now -- what ``configured_servers`` counts
        # -- and ``_known`` is every config it was ever handed: a disconnected
        # server is no longer configured, but it is still startable, which is
        # what ``reconnect`` needs after ``disconnect``.
        self._configured: dict[str, dict] = {}
        self._known: dict[str, dict] = {}
        self._extra_env: dict[str, str] = {}
        #: The dict the composition publishes as ``mcp_status``, kept in step so a
        #: model-triggered reconnect does not leave it describing startup.  See
        #: :meth:`bind_status`.
        self._status: Optional[dict[str, Any]] = None
        self._self_tool_registered = False
        #: Seconds between liveness pings; 0 (the default) disables them.
        self._health_interval = 0.0
        self._health_task: Optional[asyncio.Task[None]] = None
        #: Servers that have already had their one automatic reconnect.  Per name and
        #: per process: a manual ``reconnect`` clears the mark, because somebody asking
        #: again is new information, while a timer retrying the same corpse forever is
        #: the loop this cap exists to prevent.
        self._auto_retried: set[str] = set()

    # -- names ---------------------------------------------------------------

    @staticmethod
    def _safe_name(value: str) -> str:
        name = re.sub(r"[^0-9a-zA-Z_]+", "_", value.strip().lower())
        return name.strip("_") or "mcp"

    @classmethod
    def _server_key(cls, cfg: dict) -> str:
        """The name a config is filed under: its own ``name``, else its command's."""
        name = str(cfg.get("name") or "").strip()
        if not name:
            name = Path(str(cfg.get("command", "") or "")).name
        return cls._safe_name(name or "mcp")

    def _resolve(self, name: str) -> str:
        """Map a caller's spelling onto the key the server is filed under.

        ``disconnect("my server")`` and ``disconnect("my_server")`` are the same
        request: one is what the config says, the other is what the tool names
        are prefixed with, and a caller may have read either.
        """
        raw = str(name or "").strip()
        if raw in self._servers or raw in self._known:
            return raw
        return self._safe_name(raw)

    # -- connecting ----------------------------------------------------------

    async def connect_from_config(
        self, config: dict, extra_env: dict[str, str] | None = None
    ):
        """Connect every server in *config*, counting failures rather than raising.

        Called once by the composition with the merged list, and again by a plugin
        reload with only the servers that appeared -- so a second call adds to
        what this client is configured to run; it does not replace it.
        """
        self._extra_env = extra_env or {}
        self._ensure_health_monitor()
        self._register_self_tool()
        for server_cfg in list(config.get("mcp_servers", []) or []):
            try:
                await self._connect_server(server_cfg)
            except Exception as e:
                shared.CONSOLE.print(
                    f"[yellow]MCP server connect failed "
                    f"({server_cfg.get('name', '?')}): {e}[/yellow]"
                )
        self._publish_status()

    async def _connect_server(self, cfg: dict) -> None:
        """Start one server and wait until its tools are registered, or it failed."""
        name = self._server_key(cfg)
        if name in self._servers:
            # A newer config for a name that is already running: stop the old
            # process before the new one tries to register the same tool names.
            await self.disconnect(name)
        handle = _MCPServer(cfg, name)
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        handle.task = asyncio.create_task(
            self._run_server(handle, ready), name=f"mcp-server-{name}"
        )
        self._servers[name] = handle
        self._server_tasks.append(handle.task)
        self._known[name] = cfg
        self._configured[name] = cfg
        try:
            await ready
        except asyncio.CancelledError:
            # The *caller* was cancelled while waiting for startup.  The task is
            # still the only thing allowed to exit its own contexts, so it is
            # asked to (stop), never cancelled.
            handle.stop.set()
            await asyncio.gather(handle.task, return_exceptions=True)
            raise
        finally:
            self._publish_status()

    async def _run_server(
        self,
        handle: _MCPServer,
        ready: asyncio.Future[None],
    ) -> None:
        """Own one MCP server's complete enter/wait/exit lifecycle.

        Same task from the first line to the last, on purpose: the contexts
        ``stdio_client`` and ``ClientSession`` enter are exited here, by the task
        that entered them, once :meth:`_wait_for_stop` says it is time.  Nothing
        outside this method closes them.
        """
        server_name = handle.name
        command = str(handle.cfg.get("command", "")).strip()
        if not command:
            handle.state = "failed"
            if not ready.done():
                ready.set_exception(ValueError("MCP server config requires 'command'"))
            return

        # Merge: agent-level env < server-specific env (server wins)
        server_env = dict(handle.cfg.get("env", {}) or {})
        merged_env = (
            {**self._extra_env, **server_env} if self._extra_env or server_env else None
        )
        cwd = handle.cfg.get("cwd") or self._extra_env.get("AGENT_OUTPUT_DIR")
        params = mcp.StdioServerParameters(
            command=command,
            args=list(handle.cfg.get("args", []) or []),
            env=merged_env or None,
            cwd=str(cwd) if cwd else None,
        )
        try:
            async with AsyncExitStack() as stack:
                # The MCP SDK otherwise binds the child process' stderr
                # directly to the real terminal.  Keep diagnostics, but route
                # them away from prompt_toolkit's live input line.
                errlog = self._open_server_errlog(stack, server_name)
                read_stream, write_stream = await stack.enter_async_context(
                    mcp.stdio_client(params, errlog=errlog)
                )
                session = await stack.enter_async_context(
                    mcp.ClientSession(read_stream, write_stream)
                )
                await session.initialize()
                handle.session = session
                tools_result = await session.list_tools()
                for tool in getattr(tools_result, "tools", []):
                    self._register_tool(handle, session, tool)
                handle.state = "connected"
                if not ready.done():
                    ready.set_result(None)
                self._publish_status()
                await self._wait_for_stop(handle)
                handle.state = "stopped"
        except asyncio.CancelledError:
            handle.state = "stopped"
            if not ready.done():
                ready.cancel()
            raise
        except Exception as exc:
            handle.state = "failed"
            if not ready.done():
                ready.set_exception(exc)
            else:
                shared.CONSOLE.print(
                    f"[yellow]MCP server stopped ({server_name}): {exc}[/yellow]"
                )
        finally:
            # The stack has been left by now, so the session and the child are
            # gone.  The tools it registered are this task's to take back: a
            # registration pointing at a closed session is worse than none, and a
            # count that kept it would be describing a server that is not there.
            handle.session = None
            self._forget_tools(handle)
            self._publish_status()

    async def _wait_for_stop(self, handle: _MCPServer) -> None:
        """Block until *handle* is asked to stop, or the client is closing.

        Two waiters, because the callers need different scopes: ``disconnect`` and
        ``reconnect`` stop **one** server while ``close`` stops all of them, and
        neither may reach into the task.  Whichever fires first ends this task's
        turn at its ``AsyncExitStack``, which the task then leaves itself.
        """
        stop_waiter = asyncio.ensure_future(handle.stop.wait())
        shutdown_waiter = asyncio.ensure_future(self._shutdown.wait())
        try:
            await asyncio.wait(
                {stop_waiter, shutdown_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for waiter in (stop_waiter, shutdown_waiter):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(stop_waiter, shutdown_waiter, return_exceptions=True)

    def _open_server_errlog(self, stack: AsyncExitStack, server_name: str):
        """Return a process-compatible stderr target owned by this client."""
        output_dir = str(self._extra_env.get("AGENT_OUTPUT_DIR", "") or "").strip()
        if not output_dir:
            return subprocess.DEVNULL
        try:
            log_dir = Path(output_dir) / "mcp-logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            return stack.enter_context(
                (log_dir / f"{server_name}.stderr.log").open(
                    "a", encoding="utf-8"
                )
            )
        except OSError:
            # A logging failure must not prevent an otherwise healthy MCP
            # server from connecting, and falling back to the terminal would
            # reintroduce the input-line corruption this guard prevents.
            return subprocess.DEVNULL

    def _register_tool(self, handle: _MCPServer, session: Any, tool: Any) -> None:
        original_name = str(getattr(tool, "name", "")).strip()
        if not original_name:
            return
        server_name = handle.name
        registered_name = f"mcp_{server_name}_{self._safe_name(original_name)}"
        description = getattr(tool, "description", None) or f"MCP tool {original_name}"
        parameters = getattr(tool, "inputSchema", None) or {
            "type": "object",
            "properties": {},
            "required": [],
        }

        async def _call_mcp_tool(tool_args: dict | None = None, **extra: Any):
            # tool_args is the raw input dict when keys are non-identifiers;
            # extra captures any keyword-style params from legacy callers.
            arguments = tool_args if isinstance(tool_args, dict) else extra
            result = await session.call_tool(original_name, arguments=arguments or None)
            text_blocks = []
            for block in getattr(result, "content", []) or []:
                block_type = getattr(block, "type", "")
                if block_type == "text":
                    text_blocks.append(getattr(block, "text", ""))
                else:
                    text_blocks.append(str(block))
            return {
                "ok": not bool(getattr(result, "isError", False)),
                "server": server_name,
                "tool": original_name,
                "text": "\n".join(b for b in text_blocks if b).strip(),
                "structured": getattr(result, "structuredContent", None),
            }

        self.registry.register(
            registered_name,
            description,
            parameters,
            _call_mcp_tool,
            source=f"mcp:{server_name}",
        )
        handle.tool_names.add(registered_name)

    def _forget_tools(self, handle: _MCPServer) -> None:
        """Take back every tool *handle* registered.

        By source prefix (``mcp:<server>``), which the composition owns outright
        -- unlike a plugin's contribution, which has to undo by identity
        (``unregister_contribution``): two plugin names may share a prefix, but
        one server owns every tool under its own.

        Idempotent by construction, and called twice on purpose: once by the
        server's own task as it leaves its stack, and again by :meth:`disconnect`,
        which covers a task that died before it could clean up and a task that
        never started at all.
        """
        handle.tool_names.clear()
        unregister = getattr(self.registry, "unregister_by_source_prefix", None)
        if unregister is not None:
            unregister(f"mcp:{handle.name}")

    # -- the model's handle on the lifecycle ---------------------------------

    def _register_self_tool(self) -> None:
        """Offer the model one entry point: ``mcp_reconnect``.

        Source ``mcp`` rather than ``mcp:<server>``: it has to outlive every
        individual server, because the moment a server's tools are gone is
        exactly when a caller wants to ask for it to come back.
        """
        if self._self_tool_registered:
            return
        register = getattr(self.registry, "register", None)
        if register is None:
            return
        list_tools = getattr(self.registry, "list_tools", None)
        if list_tools is not None and "mcp_reconnect" in list_tools():
            # Another client already put it on this registry, which means the same
            # composition: one handle is enough, and it reaches the same servers.
            self._self_tool_registered = True
            return
        register(
            "mcp_reconnect",
            (
                "Restart an MCP server and register its tools again. This stops an "
                "external process and starts it again, so it costs what starting the "
                "server costs and it interrupts the calls in flight on it. Use it when "
                "an MCP tool failed or the server's tools disappeared. Without "
                "`server`, every configured server is restarted."
            ),
            {
                "type": "object",
                "properties": {
                    "server": {
                        "type": "string",
                        "description": "Server name to restart; omit to restart them all.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            self._reconnect_tool,
            source="mcp",
            capabilities=("state_write",),
        )
        self._self_tool_registered = True

    async def _reconnect_tool(self, server: str | None = None, **_ignored: Any):
        """The tool body: restart one server, or all, and report each one."""
        targets = [str(server)] if server else sorted(self._configured)
        if not targets:
            return {
                "ok": False,
                "error": "no MCP servers are configured for this session",
                "results": [],
            }
        results = [await self.reconnect(target) for target in targets]
        return {
            "ok": all(item.get("ok") for item in results),
            "results": results,
            "status": self.status_summary(),
        }

    # -- liveness ------------------------------------------------------------

    def configure_health_checks(self, seconds: float) -> None:
        """Ask for a periodic ping per connected server; ``0`` keeps them off.

        The MCP SDK reports no exit for a stdio child, so a server that dies is
        invisible until a tool call fails -- and by then the model has already been
        offered its tools.  A ping is the only way to notice without one.  Off by
        default because it is a background timer: this agent grows polling loops when
        somebody asks for one, not before.
        """
        try:
            self._health_interval = max(0.0, float(seconds or 0.0))
        except (TypeError, ValueError):
            self._health_interval = 0.0

    def _ensure_health_monitor(self) -> None:
        """Start the monitor on first connect, if an interval was configured."""
        if self._health_interval <= 0 or self._health_task is not None:
            return
        if self._health_task is not None and not self._health_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._health_task = loop.create_task(
            self._monitor_health(), name="mcp-health-monitor"
        )

    async def _monitor_health(self) -> None:
        """Ping every connected server on the interval, and recover once per server."""
        while True:
            await asyncio.sleep(self._health_interval)
            for handle in list(self._servers.values()):
                if handle.state != "connected" or handle.session is None:
                    continue
                if await self._server_answers(handle):
                    continue
                await self._recover(handle)

    @staticmethod
    async def _server_answers(handle: _MCPServer) -> bool:
        """One ping.  A session that cannot be pinged is not reported as broken."""
        send_ping = getattr(handle.session, "send_ping", None)
        if send_ping is None:
            return True
        try:
            await asyncio.wait_for(send_ping(), timeout=_MCP_PING_TIMEOUT)
            return True
        except Exception:  # noqa: BLE001 - any failure is "not answering"
            return False

    async def _recover(self, handle: _MCPServer) -> None:
        """One automatic reconnect, then leave it to a caller.

        Bounded on purpose: a server that is permanently broken must not be restarted
        every interval forever -- that is the retry loop this repository has fixed
        twice already (consolidation, lease replay), and a timer makes it worse than a
        turn because nobody is watching.  A manual ``reconnect`` clears the mark.
        """
        name = handle.name
        if name in self._auto_retried:
            return
        self._auto_retried.add(name)
        shared.CONSOLE.print(
            f"[yellow]MCP server {name!r} stopped answering; reconnecting once[/yellow]"
        )
        result = await self._reconnect(name)
        if not result.get("ok"):
            shared.CONSOLE.print(
                f"[red]MCP server {name!r} did not come back "
                f"({result.get('error', 'unknown error')}); call mcp_reconnect to try "
                "again[/red]"
            )

    # -- disconnect / reconnect ----------------------------------------------

    async def disconnect(self, name: str) -> dict[str, Any]:
        """Stop one server, then take its tools back.

        Returns what happened instead of raising: "there is no such server" is an
        answer a caller (a reload, a model) has to report, and a failure to close
        one server should not crash the composition shutting down.

        The order is the promise.  Setting ``stop`` lets the **owner** task unwind
        its own ``AsyncExitStack``, so the stdio child is reaped by the task that
        started it; only then are the registrations removed, so no tool call can
        land on a session that is already closed.
        """
        key = self._resolve(name)
        handle = self._servers.get(key)
        if handle is None:
            if key in self._known:
                # Stopped once already: still "nothing is running", which is the
                # answer the caller asked for, so it is a result and not an error.
                self._configured.pop(key, None)
                self._publish_status()
                return {
                    "ok": True,
                    "server": key,
                    "stopped": True,
                    "was_connected": False,
                    "tools_removed": 0,
                }
            return {
                "ok": False,
                "server": str(name),
                "error": (
                    f"unknown MCP server {str(name)!r}: not one this client has "
                    "been configured with"
                ),
            }
        was_connected = handle.state == "connected"
        tools_removed = len(handle.tool_names)
        # The owner's own exit, requested.  Never a cancel from here: see the
        # class docstring for why that is the one move this class does not make.
        handle.stop.set()
        stopped = True
        if handle.task is not None:
            stopped = await self._await_task(handle.task)
            if handle.task in self._server_tasks:
                self._server_tasks.remove(handle.task)
        # Belt and braces for a task that never reached its own cleanup.
        self._forget_tools(handle)
        self._servers.pop(key, None)
        self._configured.pop(key, None)
        self._publish_status()
        return {
            "ok": True,
            "server": key,
            "stopped": stopped,
            "was_connected": was_connected,
            "tools_removed": tools_removed,
        }

    async def reconnect(self, name: str) -> dict[str, Any]:
        """Stop one server and start it again from the config it was given.

        A failure is returned as a failure: the old session is gone by then, so
        reporting success would describe a server that is not there.  The config
        stays known, so the call can simply be made again.

        A *call* is new information, so it also clears this server's automatic-retry
        mark: the liveness monitor gets one attempt per server, and somebody asking
        again deserves a fresh one.
        """
        self._auto_retried.discard(self._resolve(name))
        return await self._reconnect(name)

    async def _reconnect(self, name: str) -> dict[str, Any]:
        """The reconnect itself, without touching the automatic-retry mark."""
        key = self._resolve(name)
        cfg = self._known.get(key)
        if cfg is None:
            handle = self._servers.get(key)
            cfg = handle.cfg if handle is not None else None
        if cfg is None:
            return {
                "ok": False,
                "server": str(name),
                "connected": False,
                "error": (
                    f"unknown MCP server {str(name)!r}: not one this client has "
                    "been configured with"
                ),
            }
        if key in self._servers:
            await self.disconnect(key)
        try:
            await self._connect_server(cfg)
        except Exception as exc:
            return {
                "ok": False,
                "server": key,
                "connected": False,
                "error": str(exc),
            }
        handle = self._servers.get(key)
        return {
            "ok": True,
            "server": key,
            "connected": True,
            "tools": sorted(handle.tool_names) if handle is not None else [],
        }

    async def _await_task(self, task: asyncio.Task[None]) -> bool:
        """Wait for *task* to finish; ``False`` when it could not be waited for.

        ``False`` is not a failure to report as one: it means the task belongs to
        another loop (see :meth:`close`), so that loop has to run the unwind we
        just requested.  Returning it lets the caller say "asked, not yet
        confirmed" instead of inventing a success.

        The wait is bounded, because a server that hung during start-up must not
        hang the turn that asked to restart it.  ``shield`` is load-bearing:
        without it the timeout would cancel the gather, and the gather would
        cancel the task -- the one move this class may never make.
        """
        if task.done():
            await asyncio.gather(task, return_exceptions=True)
            return True
        if task.get_loop() is not asyncio.get_running_loop():
            return False
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.gather(task, return_exceptions=True)),
                timeout=_MCP_STOP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return False
        return True

    # -- reading the state ---------------------------------------------------

    def status_summary(self) -> dict[str, Any]:
        """The truth about the servers, counted from the servers themselves.

        These used to be counters maintained by hand, which is how a server that
        died in the background could still be counted as connected: nothing
        decremented on the way out.  Derived state makes a crash, a disconnect, a
        failed reconnect and a successful one all visible without anyone
        remembering to update a number.
        """
        servers = list(self._servers.values())
        return {
            "configured_servers": len(self._configured),
            "connected_servers": sum(1 for h in servers if h.state == "connected"),
            "failed_servers": sum(1 for h in servers if h.state == "failed"),
            "registered_tools": sum(len(h.tool_names) for h in servers),
        }

    def server_states(self) -> list[dict[str, Any]]:
        """Per-server state, for a caller that has to report one line each."""
        return [
            {
                "server": handle.name,
                "state": handle.state,
                "connected": handle.state == "connected",
                "tools": sorted(handle.tool_names),
            }
            for handle in self._servers.values()
        ]

    def is_connected(self, name: str) -> bool:
        """Whether *name* has a live session with its tools registered right now."""
        handle = self._servers.get(self._resolve(name))
        return handle is not None and handle.state == "connected"

    def bind_status(self, status: dict[str, Any]) -> None:
        """Publish this client's state into the composition's ``mcp_status`` dict.

        The composition publishes that dict at setup -- holding what it is about to
        attempt -- and the background connect fills it in.  A reconnect triggered
        later, by the model through ``mcp_reconnect``, has nobody re-reading
        ``status_summary``, so from here on every state change is written into the
        same dict: the composition's account of what is connected keeps describing
        the servers instead of the moment it started.

        Binding writes nothing itself: the setup value is the honest one until an
        attempt has reported something.
        """
        self._status = status

    def _publish_status(self) -> None:
        status = self._status
        if status is None:
            return
        try:
            status.clear()
            status.update(self.status_summary())
        except (AttributeError, TypeError):
            # Not a mutable mapping of ours: stop trying rather than fail a
            # reconnect over a status detail.
            self._status = None

    # -- shutting everything down --------------------------------------------

    async def close(self) -> None:
        """Signal shutdown, then wait for the server tasks **this loop** owns.

        A task belongs to the loop that created it, and ``asyncio.gather`` refuses a
        task from another one -- ``ValueError: The future belongs to a different
        loop``.  This used to gather whatever was in the list, so a composition built
        on one loop and torn down on another raised instead of closing, and every such
        call site is a real one: a gateway restart, a session rebuilt after a config
        change, a test that builds with ``asyncio.run`` and closes with a second one.
        Measured on ``tests/test_mcp_plugin.py``, which does exactly that.

        A foreign task cannot be awaited from here, and does not need to be:

        * if its loop is **closed**, asyncio already cancelled it while closing -- the
          cleanup the task owed has been requested, and there is nothing left to wait
          for;
        * if its loop is still **running** (another thread's), ``_shutdown`` has been
          set, which is the signal that task is waiting on, and cancelling it is that
          loop's business, so we ask it to.

        Every server task also waits on this client's ``_shutdown`` beside its own
        stop event, so setting it here is all they need to leave their contexts
        themselves.  A closed client is finished: ``_shutdown`` stays set, so a
        later ``connect_from_config`` stops immediately.  A composition that wants
        servers back builds a new client, which is what a config change does.
        """
        self._shutdown.set()
        # The monitor is this client's own task, so cancelling it is allowed and
        # expected: it belongs to whoever started it, which is this class.  (A server
        # task is the opposite case -- see the class docstring.)
        monitor = self._health_task
        self._health_task = None
        if monitor is not None and not monitor.done():
            try:
                if monitor.get_loop() is asyncio.get_running_loop():
                    monitor.cancel()
            except RuntimeError:
                # No running loop: the task's loop is closing, which cancels it too.
                pass
        if self._server_tasks:
            loop = asyncio.get_running_loop()
            mine: list[asyncio.Task[None]] = []
            foreign: list[asyncio.Task[None]] = []
            for task in self._server_tasks:
                (mine if task.get_loop() is loop else foreign).append(task)
            self._server_tasks.clear()
            if mine:
                await asyncio.gather(*mine, return_exceptions=True)
            for task in foreign:
                if task.done():
                    continue
                owner = task.get_loop()
                if owner.is_running():
                    try:
                        owner.call_soon_threadsafe(task.cancel)
                    except RuntimeError:
                        # The loop stopped between the check and the call; its own
                        # close has already cancelled the task.
                        pass
        self._publish_status()


class UserToolCatalog:
    """Discover and load user-authored Python tool plugins.

    Loading executes local Python inside the live session, so the catalog
    admits a module only when the whole directory is trusted
    (``user_tools.enabled=true``) or the individual file was approved by
    content hash.  Approval survives restarts; editing the file does not
    survive approval.
    """

    def __init__(self, root: Optional[Path] = None):
        self.root = root or shared.TOOLS_DIR

    @property
    def deps_dir(self) -> Path:
        return user_tools.deps_dir(self.root)

    def discover(self) -> list[Path]:
        """Tool modules on disk, excluding ``_deps`` and other private paths."""
        if not self.root.is_dir():
            return []
        return [
            path
            for path in sorted(self.root.rglob("*.py"))
            if user_tools.is_tool_module(path, self.root)
        ]

    def tool_id_for(self, path: Path) -> str:
        return path.relative_to(self.root).with_suffix("").as_posix()

    def load_into_registry(
        self,
        registry: ToolRegistry,
        *,
        require_approval: bool = False,
    ) -> list[str]:
        """Register every admissible tool module without importing it here.

        With *require_approval* set, only files whose current contents match a
        recorded approval are loaded — the mode used when ``user_tools`` is
        not globally enabled, so a tool the user explicitly approved keeps
        working across restarts without trusting the whole directory.

        Loading used to ``exec_module`` each file into the live session, which
        put model-authored code in the same process as the provider API keys,
        the memory database and the registry itself.  Instead the module is
        probed out of process for its schema, and what gets registered is a
        proxy that runs the real call in a sandboxed child (see
        ``user_tool_runner``).  Nothing from the tool is imported here.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        user_tools.ensure_deps_on_path(self.root)
        # Replacing a module's tools needs `unregister_by_source_prefix`, which a view
        # does not have: unwrapped, or `create_tool` writes the file, records its
        # approval, and then raises -- a tool that exists on disk and nowhere else.
        registry = as_registry(registry)
        registry.unregister_by_source_prefix("user_tool:")
        loaded: list[str] = []
        for tool_file in self.discover():
            plugin_id = self.tool_id_for(tool_file)
            if require_approval and not user_tools.is_approved(
                plugin_id, user_tools.file_digest(tool_file), self.root
            ):
                continue
            source = f"user_tool:{plugin_id}"
            probe = user_tools.probe_module_sync(tool_file, root=self.root)
            if not probe.ok:
                shared.CONSOLE.print(
                    f"[yellow]Failed to load user tool plugin {tool_file}: "
                    f"{probe.error}[/yellow]"
                )
                continue
            registered_any = False
            for spec in probe.tools:
                name = str(spec.get("name") or "").strip()
                if not name:
                    continue
                registry.register(
                    name,
                    str(spec.get("description") or ""),
                    spec.get("parameters") or {"type": "object", "properties": {}},
                    self._proxy_for(tool_file, name, registry),
                    replace=True,
                    source=source,
                )
                registered_any = True
            if registered_any:
                loaded.append(plugin_id)
            else:
                shared.CONSOLE.print(
                    f"[yellow]User tool plugin {tool_file} registered no tools[/yellow]"
                )
        return loaded

    def _proxy_for(
        self, tool_file: Path, tool_name: str, registry: ToolRegistry
    ) -> Callable:
        """An async callable that forwards one tool call to a child process."""

        async def _invoke(**tool_input: Any) -> Any:
            from agent.tools.user_tool_runner import run_user_tool

            return await run_user_tool(
                tool_file,
                tool_name,
                tool_input,
                registry=registry,
                root=self.root,
            )

        _invoke.__name__ = f"user_tool_{tool_name}"
        _invoke.__doc__ = (
            f"Proxy for user tool {tool_name!r} in {tool_file}; the tool body "
            "runs in a sandboxed child process."
        )
        return _invoke
