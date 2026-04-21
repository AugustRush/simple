from __future__ import annotations

import asyncio
import contextvars
import copy
from dataclasses import dataclass, field
import inspect
import json
from pathlib import Path
import time
from typing import Any, Callable, Optional

import anthropic

import agent as agent_module
from agent import shared
from agent.config import _compose_system_prompt
from agent.core.output import CliOutputSink, _active_sink, _fmt_tool_inputs
from agent.memory.system import ContextManager
from agent.plugins.catalog import PluginCatalog, PostToolEvent, PreToolEvent
from agent.skills.catalog import SkillCatalog
from agent.tools.runtime import ToolRegistry

DEFAULT_SYSTEM_PROMPT = agent_module.DEFAULT_SYSTEM_PROMPT

@dataclass
class AgentContext:
    """State for a single agent instance."""

    agent_id: str = field(default_factory=shared._new_id)
    role: str = "assistant"
    messages: list[dict] = field(default_factory=list)
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    tools_enabled: bool = True
    metadata: dict = field(default_factory=dict)


@dataclass
class AgentResult:
    agent_id: str
    content: str
    tool_calls_made: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class SubAgentProgressEvent:
    kind: str
    role: Optional[str] = None
    task: Optional[str] = None
    message: str = ""
    completed: int = 0
    total: int = 0


class _TaskLocalContextStack:
    """List-like context stack backed by ContextVar for per-task isolation."""

    def __init__(self) -> None:
        self._stack: contextvars.ContextVar[tuple[AgentContext, ...]] = (
            contextvars.ContextVar("agent_context_stack", default=())
        )

    def append(self, ctx: AgentContext) -> None:
        self._stack.set((*self._stack.get(), ctx))

    def pop(self) -> AgentContext:
        stack = self._stack.get()
        if not stack:
            raise IndexError("pop from empty context stack")
        self._stack.set(stack[:-1])
        return stack[-1]

    def __bool__(self) -> bool:
        return bool(self._stack.get())

    def __getitem__(self, index: int) -> AgentContext:
        return self._stack.get()[index]


class BaseAgent:
    """Core agent: streams Claude, handles tool_use loop."""

    _MAX_TRUNCATION_CONTINUATIONS = 2
    _CONTINUE_PROMPT = (
        "Continue exactly from where you left off. "
        "Do not repeat previous text. "
        "Do not restart the answer."
    )

    def __init__(
        self,
        client: Any,
        registry: ToolRegistry,
        model: str = shared.DEFAULT_MODEL,
        max_tokens: int = shared.DEFAULT_MAX_TOKENS,
        api_format: str = "anthropic",
    ):
        self.client = client
        self.registry = registry
        self.api_format = api_format
        self.model = model
        self.max_tokens = max_tokens
        self.context_manager: Optional[ContextManager] = None
        self.plugin_catalog: Optional["PluginCatalog"] = None
        self.max_parallel_agents = shared.DEFAULT_MAX_PARALLEL_AGENTS
        self.sub_agent_timeout_seconds = shared.DEFAULT_SUB_AGENT_TIMEOUT_SECONDS
        self._context_stack = _TaskLocalContextStack()

    def _emit_subagent_event(self, event: SubAgentProgressEvent) -> None:
        sink = _active_sink.get()
        if sink is not None:
            sink.on_subagent_event(event)
            return
        CliOutputSink(shared.CONSOLE).on_subagent_event(event)

    def set_model(self, model: str) -> None:
        """Switch the model used for subsequent calls."""
        self.model = model

    def current_context(self) -> Optional["AgentContext"]:
        return self._context_stack[-1] if self._context_stack else None

    # ── Format-aware API helpers ──────────────────────────────────────────

    def _tools_for_api(self, tools: list[dict]) -> Any:
        """Convert tools to the right format; return NOT_GIVEN/None if empty."""
        if not tools:
            return anthropic.NOT_GIVEN if self.api_format == "anthropic" else None
        if self.api_format == "openai":
            # Convert Anthropic tool schema → OpenAI function-calling format
            return [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                }
                for t in tools
            ]
        return tools  # anthropic format as-is

    def _inject_system(self, messages: list[dict], system_prompt: str) -> list[dict]:
        """For OpenAI format, prepend system as first message."""
        if self.api_format == "openai":
            return [{"role": "system", "content": system_prompt}] + messages
        return messages  # Anthropic passes system separately

    async def _create(self, ctx: "AgentContext", tools: list[dict]) -> Any:
        """Non-streaming API call, returns a normalised response object."""
        if self.api_format == "anthropic":
            return await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=ctx.system_prompt,
                messages=ctx.messages,
                tools=self._tools_for_api(tools),
            )
        else:
            # OpenAI-compatible
            kwargs: dict = dict(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=self._inject_system(ctx.messages, ctx.system_prompt),
            )
            api_tools = self._tools_for_api(tools)
            if api_tools:
                kwargs["tools"] = api_tools
            return await self.client.chat.completions.create(**kwargs)

    def _parse_response(self, response: Any) -> tuple[str, str, list[dict]]:
        """
        Parse a response object into (stop_reason, text, tool_calls).
        tool_calls: list of {"name": ..., "id": ..., "input": {...}}
        """
        if self.api_format == "anthropic":
            stop_reason = response.stop_reason  # "end_turn" | "tool_use"
            text_blocks = [b for b in response.content if hasattr(b, "text")]
            text = " ".join(b.text for b in text_blocks)
            tool_calls = [
                {"name": b.name, "id": b.id, "input": b.input}
                for b in response.content
                if b.type == "tool_use"
            ]
            return stop_reason, text, tool_calls
        else:
            # OpenAI
            choice = response.choices[0]
            finish = choice.finish_reason  # "stop" | "tool_calls"
            msg = choice.message
            text = msg.content or ""
            if finish == "tool_calls" and msg.tool_calls:
                tool_calls = []
                for tc in msg.tool_calls:
                    try:
                        inp = json.loads(tc.function.arguments)
                    except Exception:
                        inp = {}
                    tool_calls.append(
                        {"name": tc.function.name, "id": tc.id, "input": inp}
                    )
                return "tool_use", text, tool_calls
            return "end_turn", text, []

    def _response_completion_error(self, response: Any) -> Optional[str]:
        """Classify provider completion states that should not be treated as clean ends."""
        if self.api_format != "openai":
            return None
        try:
            finish = response.choices[0].finish_reason
        except Exception:
            return None
        if finish == "length":
            return "Model response was truncated (finish_reason=length)"
        return None

    @staticmethod
    def _merge_continuation_text(prefix: str, continuation: str) -> str:
        """Append continuation text while trimming simple duplicated overlap."""
        if not prefix:
            return continuation
        if not continuation:
            return prefix
        max_overlap = min(len(prefix), len(continuation), 64)
        for size in range(max_overlap, 0, -1):
            if prefix.endswith(continuation[:size]):
                return prefix + continuation[size:]
        return prefix + continuation

    async def _continue_truncated_response(
        self,
        ctx: "AgentContext",
        partial_text: str,
    ) -> tuple[str, Optional[str]]:
        """Try to complete a truncated final response with bounded follow-up calls."""
        merged = partial_text
        continuation_error = "Model response was truncated (finish_reason=length)"
        for _ in range(self._MAX_TRUNCATION_CONTINUATIONS):
            continuation_ctx = copy.deepcopy(ctx)
            continuation_ctx.messages.append({"role": "assistant", "content": merged})
            continuation_ctx.messages.append(
                {"role": "user", "content": self._CONTINUE_PROMPT}
            )
            response = await self._create(continuation_ctx, [])
            stop_reason, text, tool_uses = self._parse_response(response)
            if stop_reason == "tool_use" and tool_uses:
                break
            merged = self._merge_continuation_text(merged, text)
            continuation_error = self._response_completion_error(response)
            if continuation_error is None:
                return merged, None
        return (
            merged,
            f"Model response remained truncated after "
            f"{self._MAX_TRUNCATION_CONTINUATIONS} auto-continue attempts",
        )

    def _assistant_message(self, response: Any, text: str) -> dict:
        """Build the assistant history entry after a tool_use stop."""
        if self.api_format == "anthropic":
            return {"role": "assistant", "content": response.content}
        else:
            # For OpenAI we store the raw message object (or a dict)
            msg = response.choices[0].message
            entry: dict = {"role": "assistant", "content": msg.content}
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            return entry

    def _tool_result_messages(
        self, tool_calls: list[dict], results: list[str]
    ) -> list[dict]:
        """Build tool-result history entries for both formats."""
        if self.api_format == "anthropic":
            return [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tc["id"], "content": r}
                        for tc, r in zip(tool_calls, results)
                    ],
                }
            ]
        else:
            # OpenAI: one message per tool result
            return [
                {"role": "tool", "tool_call_id": tc["id"], "content": r}
                for tc, r in zip(tool_calls, results)
            ]

    def _format_agent_error(self, exc: Exception) -> str:
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return "Model request timed out"
        if isinstance(exc, ValueError):
            return f"Invalid model request: {exc}"
        return str(exc) or exc.__class__.__name__

    @staticmethod
    def _synthesize_tool_only_response(
        tool_history: list[tuple[str, str]]
    ) -> str:
        for tool_name, raw_result in reversed(tool_history):
            if tool_name != "schedule_create":
                continue
            try:
                payload = json.loads(raw_result)
            except Exception:
                continue
            if not isinstance(payload, dict) or not payload.get("ok"):
                continue
            summary_text = str(payload.get("summary_text", "")).strip()
            if summary_text:
                return summary_text
        return ""

    async def _run_tool_uses(self, tool_uses: list[dict]) -> list[str]:
        # D3: wrap each regular tool call with a wall-clock timeout so a hung
        # user-generated tool cannot block the loop indefinitely.
        async def _exec_regular(tu: dict) -> str:
            name = tu["name"]
            sink = _active_sink.get()
            # pre_tool hook — a blocking result short-circuits execution
            if self.plugin_catalog:
                pre = await self.plugin_catalog.fire_pre_tool(
                    PreToolEvent(tool_name=name, tool_kwargs=tu["input"])
                )
                if pre.action == "block":
                    if sink:
                        sink.on_tool_blocked(name, pre.message)
                    else:
                        shared.CONSOLE.print(
                            f"\n[cyan]→ {name}[/cyan] [yellow](blocked by plugin: {pre.message})[/yellow]"
                        )
                    return json.dumps(
                        {"ok": False, "blocked": True, "reason": pre.message}
                    )
            # Announce the tool call *before* execution so the user sees
            # what is happening while they wait (UX fix #2).
            if sink:
                sink.on_tool_start(name, tu["input"])
            else:
                shared.CONSOLE.print(
                    f"\n[cyan]→ {name}[/cyan]{_fmt_tool_inputs(name, tu['input'])}"
                )
            try:
                res = await asyncio.wait_for(
                    self.registry.call(name, tu["input"]),
                    timeout=shared.REGULAR_TOOL_TIMEOUT,
                )
            except asyncio.TimeoutError:
                res = json.dumps(
                    {
                        "ok": False,
                        "error": f"tool '{name}' timed out after {shared.REGULAR_TOOL_TIMEOUT}s",
                    }
                )
            # Display result after call completes
            if sink:
                sink.on_tool_end(name, res)
            else:
                shared.CONSOLE.print(
                    f"[dim]{res[:200]}{'...' if len(res) > 200 else ''}[/dim]"
                )
            # post_tool hook — observational, does not alter the result
            if self.plugin_catalog:
                await self.plugin_catalog.fire_post_tool(
                    PostToolEvent(tool_name=name, tool_kwargs=tu["input"], result=res)
                )
            return res

        # M2: use a sentinel so we can distinguish "tool not run" from "tool returned empty"
        _MISSING = object()
        results: list[Any] = [_MISSING] * len(tool_uses)

        delegated_tool_names = {"spawn_agent", "team_run"}
        regular_calls = [
            (idx, tu)
            for idx, tu in enumerate(tool_uses)
            if tu["name"] not in delegated_tool_names
        ]
        if regular_calls:
            # D2: return_exceptions=True preserves successes when one tool errors
            raw = await asyncio.gather(
                *[_exec_regular(tu) for _, tu in regular_calls],
                return_exceptions=True,
            )
            for (idx, tu), outcome in zip(regular_calls, raw):
                if isinstance(outcome, BaseException):
                    results[idx] = json.dumps(
                        {"ok": False, "error": f"tool '{tu['name']}' raised: {outcome}"}
                    )
                else:
                    results[idx] = outcome

        delegated_calls = [
            (idx, tu)
            for idx, tu in enumerate(tool_uses)
            if tu["name"] in delegated_tool_names
        ]
        if delegated_calls:
            roles = ", ".join(
                tu["input"].get("role", tu["name"]) for _, tu in delegated_calls
            )
            total_spawns = len(delegated_calls)
            progress_state = {
                "completed": 0,
                "last_notified_completed": 0,
                "last_emit_monotonic": time.monotonic(),
            }
            idle_heartbeat_seconds = 10.0

            def _emit_progress(*, stale: bool = False) -> None:
                if progress_state["completed"] >= total_spawns:
                    return
                progress_state["last_notified_completed"] = progress_state["completed"]
                progress_state["last_emit_monotonic"] = time.monotonic()
                message = (
                    f"Sub-agents still running: {progress_state['completed']}/{total_spawns} completed"
                    if stale
                    else f"Sub-agents running: {progress_state['completed']}/{total_spawns} completed"
                )
                self._emit_subagent_event(
                    SubAgentProgressEvent(
                        kind="batch_progress",
                        completed=progress_state["completed"],
                        total=total_spawns,
                        message=message,
                    )
                )
            self._emit_subagent_event(
                SubAgentProgressEvent(
                    kind="batch_started",
                    total=total_spawns,
                    message=(
                        f"Starting {total_spawns} sub-agents "
                        f"(limit {self.max_parallel_agents}): {roles}"
                    ),
                )
            )
            # D4: semaphore-based dispatch lets faster agents in later batches start
            # as soon as a slot frees, rather than waiting for an entire batch to finish.
            sem = asyncio.Semaphore(self.max_parallel_agents)
            heartbeat_stop = asyncio.Event()

            async def _heartbeat() -> None:
                while not heartbeat_stop.is_set():
                    try:
                        await asyncio.wait_for(heartbeat_stop.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                    if heartbeat_stop.is_set():
                        break
                    if (
                        progress_state["completed"]
                        == progress_state["last_notified_completed"]
                        and time.monotonic() - progress_state["last_emit_monotonic"]
                        >= idle_heartbeat_seconds
                    ):
                        _emit_progress(stale=True)

            async def _exec_spawn_with_sem(tu: dict) -> str:
                async with sem:
                    try:
                        outcome = await self.registry.call(tu["name"], tu["input"])
                    except Exception as exc:
                        progress_state["completed"] += 1
                        _emit_progress()
                        raise

                    progress_state["completed"] += 1
                    _emit_progress()
                    return outcome

            # D5: return_exceptions=True prevents one failing spawn from cancelling others
            heartbeat_task = asyncio.create_task(_heartbeat())
            try:
                raw_spawn = await asyncio.gather(
                    *[_exec_spawn_with_sem(tu) for _, tu in delegated_calls],
                    return_exceptions=True,
                )
            finally:
                heartbeat_stop.set()
                await heartbeat_task
                self._emit_subagent_event(
                    SubAgentProgressEvent(
                        kind="batch_finished",
                        completed=progress_state["completed"],
                        total=total_spawns,
                        message=(
                            "Sub-agent batch finished: "
                            f"{progress_state['completed']}/{total_spawns} completed"
                        ),
                    )
                )
            for (idx, tu), outcome in zip(delegated_calls, raw_spawn):
                if isinstance(outcome, BaseException):
                    results[idx] = json.dumps(
                        {
                            "ok": False,
                            "role": tu["input"].get("role", "?"),
                            "error": f"{tu['name']} failed: {outcome}",
                        }
                    )
                else:
                    results[idx] = outcome

        # M2: replace any slot that was never assigned (programming error guard)
        return [
            r
            if r is not _MISSING
            else json.dumps({"ok": False, "error": "tool result missing"})
            for r in results
        ]

    async def send_message(
        self,
        ctx: "AgentContext",
        user_message: str,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> "AgentResult":
        # Capture original system prompt before any per-turn injections.
        original_system = ctx.system_prompt
        tool_calls_made: list[str] = []
        tool_result_history: list[tuple[str, str]] = []
        result_text = ""

        # B1: wrap ALL mutations (prompt injection, messages append, stack push)
        # inside the try/finally so they are always cleaned up on error.
        try:
            # Inject relevant context into system prompt for this turn.
            # retrieve_context() includes both:
            #   1. Recent staging buffer turns (current session, not yet consolidated)
            #   2. LTM search results (historical sessions)
            # Using retrieve_ltm_context() alone would miss any conversation from
            # the current session that has been compacted out of ctx.messages but
            # not yet consolidated into LTM, causing the agent to "forget" recent
            # turns when asked about them.
            if self.context_manager:
                retrieved = self.context_manager.retrieve_implicit_context(
                    user_message,
                    current_messages=ctx.messages,
                )
                if retrieved:
                    ctx.system_prompt = ctx.system_prompt + "\n\n" + retrieved
            skill_catalog: Optional[SkillCatalog] = ctx.metadata.get("skill_catalog")
            required_skills: list[str] = list(ctx.metadata.get("required_skills", []))
            if skill_catalog and required_skills:
                active_blocks = []
                for skill_ref in required_skills:
                    activation = skill_catalog.activation_text(skill_ref, explicit=True)
                    if activation:
                        active_blocks.append(activation)
                if active_blocks:
                    ctx.system_prompt = (
                        ctx.system_prompt
                        + "\n\n## Active Skills\n"
                        + "\n\n".join(active_blocks)
                    )

            ctx.messages.append({"role": "user", "content": user_message})
            self._context_stack.append(ctx)

            # D1: bounded tool-call loop — prevents infinite model loops
            for _iteration in range(shared.MAX_TOOL_CALL_ITERATIONS + 1):
                if _iteration == shared.MAX_TOOL_CALL_ITERATIONS:
                    return AgentResult(
                        agent_id=ctx.agent_id,
                        content=result_text,
                        tool_calls_made=tool_calls_made,
                        error=(
                            f"Tool-call loop exceeded {shared.MAX_TOOL_CALL_ITERATIONS} "
                            "iterations; possible model loop detected."
                        ),
                    )
                tools = self.registry.to_anthropic_format() if ctx.tools_enabled else []

                try:
                    if stream_callback:
                        # Stream for display AND use the full response for tool detection.
                        response, streamed_text = await self._stream_response(
                            ctx, tools, stream_callback
                        )
                    else:
                        response = await self._create(ctx, tools)
                        streamed_text = ""
                    stop_reason, text, tool_uses = self._parse_response(response)

                    if stop_reason == "tool_use" and tool_uses:
                        # M4: only update result_text from the parsed text field;
                        # do not allow streamed_text from a prior iteration to bleed in.
                        if text:
                            result_text = text
                        ctx.messages.append(self._assistant_message(response, text))

                        tool_calls_made.extend(tu["name"] for tu in tool_uses)
                        results = await self._run_tool_uses(tool_uses)
                        tool_result_history.extend(
                            (tu["name"], res) for tu, res in zip(tool_uses, results)
                        )
                        ctx.messages.extend(
                            self._tool_result_messages(tool_uses, results)
                        )
                        continue
                    else:
                        # Prefer the parsed text; fall back to streamed text for
                        # the final turn (streaming accumulates what the user saw).
                        result_text = text or streamed_text or result_text
                        if not result_text and tool_result_history:
                            result_text = self._synthesize_tool_only_response(
                                tool_result_history
                            )
                        ctx.messages.append(
                            {"role": "assistant", "content": result_text}
                        )
                        completion_error = self._response_completion_error(response)
                        if completion_error:
                            result_text, continuation_error = (
                                await self._continue_truncated_response(ctx, result_text)
                            )
                            ctx.messages[-1] = {
                                "role": "assistant",
                                "content": result_text,
                            }
                            return AgentResult(
                                agent_id=ctx.agent_id,
                                content=result_text,
                                tool_calls_made=tool_calls_made,
                                error=continuation_error,
                            )
                        break

                except Exception as e:
                    return AgentResult(
                        agent_id=ctx.agent_id,
                        content="",
                        tool_calls_made=tool_calls_made,
                        error=self._format_agent_error(e),
                    )
        finally:
            # Always restore the original system prompt and pop the context stack.
            ctx.system_prompt = original_system
            if self._context_stack and self._context_stack[-1] is ctx:
                self._context_stack.pop()

        return AgentResult(
            agent_id=ctx.agent_id,
            content=result_text,
            tool_calls_made=tool_calls_made,
        )

    async def _stream_response(
        self,
        ctx: "AgentContext",
        tools: list[dict],
        callback: Callable[[str], Any],
    ) -> tuple[Any, str]:
        """Stream response text chunk-by-chunk and return (full_response, collected_text).

        ``callback`` may be a plain sync function or an async coroutine function;
        both are handled transparently.

        For Anthropic: uses stream.get_final_message() to obtain the complete response.
        For OpenAI: accumulates tool_call deltas and rebuilds a synthetic response.
        """
        collected: list[str] = []
        if self.api_format == "anthropic":
            async with self.client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=ctx.system_prompt,
                messages=ctx.messages,
                tools=self._tools_for_api(tools),
            ) as stream:
                async for text in stream.text_stream:
                    collected.append(text)
                    _r = callback(text)
                    if inspect.isawaitable(_r):
                        await _r
                response = await stream.get_final_message()
            return response, "".join(collected)

        # OpenAI streaming — accumulate tool_call deltas as well
        kwargs: dict = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=self._inject_system(ctx.messages, ctx.system_prompt),
            stream=True,
        )
        api_tools = self._tools_for_api(tools)
        if api_tools:
            kwargs["tools"] = api_tools
        finish_reason = "stop"
        tool_calls_acc: dict[int, dict] = {}  # index -> {id, name, arguments}
        # AsyncOpenAI.chat.completions.create() is a coroutine; await it to get
        # the AsyncStream object, then iterate the stream chunk by chunk.
        # Do NOT remove the `await` — create() returns a coroutine, not an
        # async iterable, so `async for chunk in create(...)` raises TypeError.
        async for chunk in await self.client.chat.completions.create(**kwargs):
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta.content:
                collected.append(delta.content)
                _r = callback(delta.content)
                if inspect.isawaitable(_r):
                    await _r
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": tc_delta.id or "",
                            "name": (
                                tc_delta.function.name if tc_delta.function else ""
                            )
                            or "",
                            "arguments": "",
                        }
                    acc = tool_calls_acc[idx]
                    if tc_delta.id:
                        acc["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            acc["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            acc["arguments"] += tc_delta.function.arguments
            if choice.finish_reason:
                finish_reason = choice.finish_reason

        # Build a synthetic response object using module-level dataclasses
        oi_tool_calls = (
            [
                shared._OAITC(v["id"], shared._OAIFunc(v["name"], v["arguments"]))
                for _, v in sorted(tool_calls_acc.items())
            ]
            if tool_calls_acc
            else None
        )

        response = shared._OAIResponse(
            [
                shared._OAIChoice(
                    finish_reason,
                    shared._OAIMsg("".join(collected), oi_tool_calls),
                )
            ]
        )
        return response, "".join(collected)

    def register_spawn_capability(
        self, base_system_prompt: str, workspace_root: Optional[Path] = None
    ) -> None:
        """Register lightweight delegation tools.

        The main agent can call spawn_agent one or more times in a single turn.
        Multiple calls are executed in parallel (via asyncio.gather in send_message).
        Sub-agents receive all regular tools but NOT spawn_agent, preventing recursion.
        """
        parent = self  # captured reference to the parent agent

        def _sub_registry() -> ToolRegistry:
            # Snapshot the registry to avoid RuntimeError if tools are added
            # concurrently (e.g. via /generate-tool while a spawn batch runs).
            tools_snapshot = dict(parent.registry._tools)
            sub_registry = ToolRegistry(console=shared.CONSOLE)
            for name, tool_def in tools_snapshot.items():
                if name not in {"spawn_agent", "team_run"}:
                    sub_registry._tools[name] = tool_def
            # Deep-copy the context dict so sub-agents cannot mutate parent's
            # mutable values (e.g. shell_blocked_commands list).
            sub_registry._context = copy.deepcopy(parent.registry._context)
            return sub_registry

        def _sub_system_prompt(
            sub_registry: ToolRegistry,
            system_suffix: str = "",
        ) -> tuple[str, Optional[AgentContext]]:
            # Always build system prompt from base_system_prompt + sub_registry
            # so it reflects only the tools the sub-agent actually has, and does NOT
            # include transient per-turn LTM injections from the parent's active context.
            # Pass output_dir (from registry context) and skill_catalog so the
            # capabilities section in the sub-agent prompt is complete.
            output_dir_str = sub_registry._context.get("output_dir")
            output_dir_path = Path(output_dir_str) if output_dir_str else None
            active_ctx = parent.current_context()
            # Only pass a real SkillCatalog instance — metadata may contain test
            # stubs or other objects that lack the summary_lines() method.
            skill_catalog_for_prompt: Optional[SkillCatalog] = None
            if active_ctx:
                sc = active_ctx.metadata.get("skill_catalog")
                if isinstance(sc, SkillCatalog):
                    skill_catalog_for_prompt = sc
            sys_prompt = _compose_system_prompt(
                base_system_prompt,
                sub_registry,
                workspace_root,
                output_dir=output_dir_path,
                skill_catalog=skill_catalog_for_prompt,
            )
            if system_suffix:
                sys_prompt += f"\n\n{system_suffix}"
            return sys_prompt, active_ctx

        def _propagate_sub_metadata(
            sub_ctx: AgentContext, active_ctx: Optional[AgentContext]
        ) -> None:
            # Propagate skill metadata so sub-agents can also activate skills.
            if active_ctx:
                if "skill_catalog" in active_ctx.metadata:
                    sub_ctx.metadata["skill_catalog"] = active_ctx.metadata[
                        "skill_catalog"
                    ]
                if "required_skills" in active_ctx.metadata:
                    sub_ctx.metadata["required_skills"] = list(
                        active_ctx.metadata["required_skills"]
                    )

        def _new_sub_agent(sub_registry: ToolRegistry) -> "BaseAgent":
            sub_agent = BaseAgent(
                parent.client,
                sub_registry,
                model=parent.model,
                max_tokens=parent.max_tokens,
                api_format=parent.api_format,
            )
            sub_agent.context_manager = parent.context_manager
            sub_agent.max_parallel_agents = parent.max_parallel_agents
            sub_agent.sub_agent_timeout_seconds = parent.sub_agent_timeout_seconds
            return sub_agent

        def _parse_quality_result(content: str) -> tuple[Optional[dict], str]:
            try:
                parsed = json.loads(content)
            except Exception:
                return None, (
                    "member result must be JSON with keys: "
                    "findings, evidence, confidence, risks"
                )
            if not isinstance(parsed, dict):
                return None, "member result JSON must be an object"
            required = {
                "findings": list,
                "evidence": list,
                "risks": list,
            }
            missing = [key for key in [*required, "confidence"] if key not in parsed]
            if missing:
                return None, "member result missing required keys: " + ", ".join(missing)
            invalid_types = [
                key
                for key, expected in required.items()
                if not isinstance(parsed[key], expected)
            ]
            if invalid_types:
                return None, (
                    "member result has invalid list keys: "
                    + ", ".join(invalid_types)
                )
            if not isinstance(parsed["confidence"], (int, float)):
                return None, "member result confidence must be a number"
            parsed["confidence"] = max(0.0, min(1.0, float(parsed["confidence"])))
            return parsed, ""

        async def spawn_agent(role: str, task: str, system_suffix: str = "") -> dict:
            sub_registry = _sub_registry()
            sub_agent = _new_sub_agent(sub_registry)
            sys_prompt, active_ctx = _sub_system_prompt(sub_registry, system_suffix)
            sub_ctx = AgentContext(role=role, system_prompt=sys_prompt)
            _propagate_sub_metadata(sub_ctx, active_ctx)
            parent._emit_subagent_event(
                SubAgentProgressEvent(
                    kind="agent_started",
                    role=role,
                    task=task,
                    message=f"{role} started: {task[:120]}",
                )
            )
            started_at = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    sub_agent.send_message(sub_ctx, task),
                    timeout=parent.sub_agent_timeout_seconds,
                )
            except asyncio.TimeoutError:
                # D6: include the last partial content from sub_ctx messages so the
                # parent has some information about what was completed before the timeout.
                partial = ""
                for msg in reversed(sub_ctx.messages):
                    if msg.get("role") == "assistant" and msg.get("content"):
                        partial = str(msg["content"])[:500]
                        break
                payload: dict = {
                    "ok": False,
                    "role": role,
                    "task": task,
                    "timed_out": True,
                    "error": (
                        f"sub-agent timed out after {parent.sub_agent_timeout_seconds}s"
                    ),
                }
                if partial:
                    payload["partial_content"] = partial
                parent._emit_subagent_event(
                    SubAgentProgressEvent(
                        kind="agent_failed",
                        role=role,
                        task=task,
                        message=payload["error"],
                    )
                )
                return payload
            except Exception as e:
                # B4: catch all exceptions so one failing spawn cannot cancel its
                # sibling agents in the same asyncio.gather batch.
                payload = {
                    "ok": False,
                    "role": role,
                    "task": task,
                    "error": f"sub-agent failed: {parent._format_agent_error(e)}",
                }
                parent._emit_subagent_event(
                    SubAgentProgressEvent(
                        kind="agent_failed",
                        role=role,
                        task=task,
                        message=payload["error"],
                    )
                )
                return payload

            payload = {
                "ok": result.error is None,
                "role": role,
                "task": task,
                "content": result.content or "(no output)",
                "tool_calls_made": result.tool_calls_made,
            }
            if result.error:
                payload["error"] = result.error
            elapsed = time.monotonic() - started_at
            parent._emit_subagent_event(
                SubAgentProgressEvent(
                    kind="agent_finished" if result.error is None else "agent_failed",
                    role=role,
                    task=task,
                    message=(
                        f"{role} finished in {elapsed:.1f}s"
                        if result.error is None
                        else f"{role} failed in {elapsed:.1f}s: {result.error}"
                    ),
                )
            )
            return payload

        async def team_run(
            goal: str,
            members: list[dict],
            max_concurrency: Optional[int] = None,
        ) -> dict:
            goal = str(goal or "").strip()
            if not goal:
                return {"ok": False, "error": "goal is required"}
            if not isinstance(members, list) or not members:
                return {"ok": False, "error": "members must be a non-empty list"}
            limit = max(1, int(max_concurrency or parent.max_parallel_agents))
            sem = asyncio.Semaphore(limit)

            async def _run_member(index: int, member: dict) -> dict:
                if not isinstance(member, dict):
                    return {
                        "ok": False,
                        "name": f"member-{index + 1}",
                        "role": "",
                        "task": "",
                        "error": "member must be an object",
                    }
                name = str(member.get("name") or f"member-{index + 1}").strip()
                role = str(member.get("role") or "teammate").strip()
                task = str(member.get("task") or "").strip()
                if not task:
                    return {
                        "ok": False,
                        "name": name,
                        "role": role,
                        "task": task,
                        "error": "member task is required",
                    }
                async with sem:
                    sub_registry = _sub_registry()
                    sub_agent = _new_sub_agent(sub_registry)
                    sys_prompt, active_ctx = _sub_system_prompt(
                        sub_registry,
                        (
                            "You are one member of a lightweight agent team. "
                            "Work independently and return ONLY valid JSON with keys: "
                            "findings (list), evidence (list), confidence (number 0..1), "
                            "risks (list). Do not include markdown."
                        ),
                    )
                    sub_ctx = AgentContext(role=role, system_prompt=sys_prompt)
                    _propagate_sub_metadata(sub_ctx, active_ctx)
                    prompt = (
                        f"Team goal:\n{goal}\n\n"
                        f"Your member name: {name}\n"
                        f"Your role: {role}\n"
                        f"Your task:\n{task}\n\n"
                        "Return ONLY JSON with keys: findings, evidence, confidence, risks."
                    )
                    try:
                        result = await asyncio.wait_for(
                            sub_agent.send_message(sub_ctx, prompt),
                            timeout=parent.sub_agent_timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        return {
                            "ok": False,
                            "name": name,
                            "role": role,
                            "task": task,
                            "timed_out": True,
                            "error": (
                                f"team member timed out after "
                                f"{parent.sub_agent_timeout_seconds}s"
                            ),
                        }
                    except Exception as exc:
                        return {
                            "ok": False,
                            "name": name,
                            "role": role,
                            "task": task,
                            "error": f"team member failed: {parent._format_agent_error(exc)}",
                        }
                    content = result.content or ""
                    if result.error:
                        return {
                            "ok": False,
                            "name": name,
                            "role": role,
                            "task": task,
                            "content": content,
                            "error": result.error,
                        }
                    parsed, quality_error = _parse_quality_result(content)
                    if quality_error:
                        return {
                            "ok": False,
                            "name": name,
                            "role": role,
                            "task": task,
                            "content": content,
                            "quality_error": quality_error,
                        }
                    return {
                        "ok": True,
                        "name": name,
                        "role": role,
                        "task": task,
                        "result": parsed,
                        "tool_calls_made": result.tool_calls_made,
                    }

            results = await asyncio.gather(
                *[_run_member(index, member) for index, member in enumerate(members)]
            )
            completed = sum(1 for item in results if item.get("ok") is True)
            invalid = sum(1 for item in results if item.get("quality_error"))
            failed = len(results) - completed - invalid
            return {
                "ok": failed == 0 and invalid == 0,
                "goal": goal,
                "summary": {
                    "total": len(results),
                    "completed": completed,
                    "failed": failed,
                    "invalid": invalid,
                },
                "members": results,
                "synthesis_instruction": (
                    "Use the member results as evidence. Merge duplicates, surface "
                    "disagreements, and produce the final answer yourself."
                ),
            }

        self.registry.register(
            "spawn_agent",
            (
                "Spawn a specialized sub-agent to handle a task from a particular perspective. "
                "Call this tool multiple times in a single response to run sub-agents in PARALLEL. "
                "Each sub-agent has a fresh context and all regular tools."
            ),
            {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "description": (
                            "Role / persona of the sub-agent "
                            "(e.g. 'researcher', 'critic', 'implementer', 'devil's advocate')"
                        ),
                    },
                    "task": {
                        "type": "string",
                        "description": "The specific task or question for this sub-agent.",
                    },
                    "system_suffix": {
                        "type": "string",
                        "description": (
                            "Optional extra instructions appended to the system prompt "
                            "to shape this sub-agent's behavior."
                        ),
                    },
                },
                "required": ["role", "task"],
            },
            spawn_agent,
            source="runtime:spawn",
        )
        self.registry.register(
            "team_run",
            (
                "Run a lightweight team of independent sub-agents for parallel "
                "research, review, or implementation planning. Each member must "
                "return structured JSON with findings, evidence, confidence, and risks; "
                "the lead agent then synthesizes the final answer."
            ),
            {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "Overall goal the team is working toward.",
                    },
                    "members": {
                        "type": "array",
                        "description": "Independent team member tasks.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "role": {"type": "string"},
                                "task": {"type": "string"},
                            },
                            "required": ["role", "task"],
                        },
                    },
                    "max_concurrency": {
                        "type": "integer",
                        "description": "Optional concurrency limit for member execution.",
                    },
                },
                "required": ["goal", "members"],
            },
            team_run,
            source="runtime:team",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. SELF-EVOLUTION
# ─────────────────────────────────────────────────────────────────────────────
