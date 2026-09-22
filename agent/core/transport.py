"""Protocol-specific LLM dispatch.

First-principles boundary: every "if api_format == 'anthropic' else openai"
that used to be sprinkled across ``BaseAgent`` lives behind this interface,
so adding a new provider format is a single new ``ModelTransport`` subclass
and zero edits to the agent loop.

Each implementation owns its own message shape, tool schema, streaming
protocol, and any provider-specific helpers (e.g. OpenAI ``model_extra``
field sanitization).  The agent never inspects ``api_format`` directly.
"""

from __future__ import annotations

import abc
import copy
from dataclasses import dataclass
import inspect
import json
from typing import Any, Callable, Optional

import anthropic

from agent import shared
from agent.usage import ProviderUsage, extract_provider_usage


# ── OpenAI-specific constants kept with the OpenAI transport ────────────────

_OPENAI_MESSAGE_RESERVED_FIELDS = frozenset(
    {
        "role",
        "content",
        "tool_calls",
        "function_call",
        "name",
        "refusal",
        "audio",
        "annotations",
        "parsed",
        "model_extra",
    }
)
_SKIP_OPENAI_EXTRA = object()

#: Fields a provider may put reasoning *text* in, on a delta or a message.
#: Gateways disagree on the name and agree on nothing else, so all the names
#: seen in the wild are listed and the first non-empty one wins.  This is a
#: read-only view: the same field also stays in the message history (see
#: `_message_extras`), which the OpenAI tool loop is tested to depend on.
_REASONING_EXTRA_FIELDS = ("reasoning_content", "reasoning", "thinking")

#: Anthropic's own thinking levels, as token budgets.  The API rejects a budget
#: below 1024 and requires it to stay under max_tokens, so the value is clamped
#: per call rather than trusted from config.
_ANTHROPIC_THINKING_BUDGETS = {"low": 1024, "medium": 4096, "high": 16384}


@dataclass(frozen=True)
class ModelEndpoint:
    """The SDK client that owns a model, and the wire format it speaks.

    The two travel together on purpose.  A caller that spells out a model id
    but holds only a client is one careless pairing away from sending a
    foreign model to the active provider's endpoint — which the provider
    answers with a 400 naming its own models, and which reads like a bug in
    the agent rather than a mismatch between a model and its group.
    """

    client: Any
    api_format: str


class ModelTransport(abc.ABC):
    """Format-specific dispatch contract for one LLM provider.

    All methods are deliberately stateless w.r.t. agent loop state — the
    caller passes model/messages/tools per call.  The transport's only
    instance state is the SDK client.
    """

    #: Wire format this transport speaks; set by each implementation.
    api_format: str = ""

    #: How hard this provider's models should think, in our own vocabulary.
    #: None means the config never said, and the provider keeps its own
    #: default — which is why the attribute is read as "is not None" rather
    #: than compared against a default word.
    thinking_effort: Optional[str] = None

    #: Whether a streaming call asks the provider to report what it cost.
    #: On by default because ``stream_options`` is part of the OpenAI wire
    #: format this transport already speaks, and because a stream that reports
    #: nothing cannot be recorded at all — which is how the interactive path
    #: went unmeasured while every non-streaming call was recorded.  A gateway
    #: that rejects the parameter names it in its own error, and
    #: ``providers.<name>.stream_usage: false`` turns it off.
    stream_usage: bool = True

    def __init__(
        self,
        client: Any,
        thinking_effort: Any = None,
        stream_usage: Any = True,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        self.client = client
        self.thinking_effort = shared.normalize_thinking_effort(thinking_effort)
        self.stream_usage = bool(stream_usage)
        #: This provider's configured extra request headers, unresolved: a
        #: value may still contain ``{session}``, substituted per request
        #: rather than here because one client serves every conversation (see
        #: :meth:`_headers_kwarg`).
        self.headers = dict(headers) if isinstance(headers, dict) else {}

    def _headers_kwarg(self) -> dict[str, Any]:
        """``{"extra_headers": ...}`` for this request, or empty when none.

        Empty rather than ``{"extra_headers": None}`` on purpose: a provider
        that declares no headers must send a request byte-identical to the one
        this transport sent before headers existed.

        The session is read here, at request time, not at construction time.
        The transport is built once per provider configuration and shared by
        every conversation, so a value captured at construction could only be
        a process-wide constant -- and a gateway asking for one id per
        conversation would see one id for all of them.
        """
        if not self.headers:
            return {}
        session = shared.current_session_id()
        return {
            "extra_headers": {
                name: value.replace(shared.SESSION_HEADER_PLACEHOLDER, session)
                for name, value in self.headers.items()
            }
        }

    # ── Reasoning ──────────────────────────────────────────────────────

    @staticmethod
    def _reasoning_text(message: Any) -> str:
        """Reasoning text carried by a streamed delta or a finished message.

        Read straight off the object because the field is provider-specific:
        the SDK parks it in ``model_extra`` when the class does not declare it
        and in the instance dict when a gateway over-declares it.
        """
        if message is None:
            return ""
        for container in (
            getattr(message, "model_extra", None),
            getattr(message, "__dict__", None),
        ):
            if not isinstance(container, dict):
                continue
            for key in _REASONING_EXTRA_FIELDS:
                value = container.get(key)
                if isinstance(value, str) and value:
                    return value
        return ""

    @staticmethod
    async def _emit_reasoning(
        callback: Optional[Callable[[str], Any]], piece: str
    ) -> None:
        """Hand one reasoning fragment to *callback*, sync or async."""
        if callback is None or not piece:
            return
        _r = callback(piece)
        if inspect.isawaitable(_r):
            await _r

    def endpoint_for(self, model: Optional[str] = None) -> ModelEndpoint:
        """The endpoint that owns *model*.

        One provider per transport, so every model resolves to this client.
        ``RoutingTransport`` overrides this with the real model -> provider
        lookup; callers that need a client *for a model* ask here rather than
        pairing a model string with whichever client happens to be at hand.
        """
        return ModelEndpoint(self.client, self.api_format)

    # ── Tool/schema shaping ────────────────────────────────────────────

    @abc.abstractmethod
    def convert_tools(
        self, tools: list[dict], model: Optional[str] = None
    ) -> Any:
        """Convert Anthropic-shaped tool list into this provider's format.

        Returns whatever value should be passed to ``create``/``stream`` as
        the ``tools`` argument — including the provider's "no tools" sentinel
        (e.g. ``anthropic.NOT_GIVEN``) when ``tools`` is empty.
        """

    # ── Round-trip calls ───────────────────────────────────────────────

    @abc.abstractmethod
    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> Any:
        """Non-streaming completion; returns a provider-native response."""

    @abc.abstractmethod
    async def stream(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[dict],
        tools: list[dict],
        callback: Callable[[str], Any],
        reasoning_callback: Optional[Callable[[str], Any]] = None,
    ) -> tuple[Any, str]:
        """Streaming completion; returns (final_response, collected_text).

        ``callback`` receives the *answer* text.  ``reasoning_callback`` — when
        given — receives the model's thinking, which is a separate channel: the
        caller shows it in a note above the reply and must never append it to
        the answer.  Both may be sync or async, and a transport that cannot
        stream reasoning progressively may deliver it in one piece.
        """

    @abc.abstractmethod
    async def simple_chat(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        prompt: str,
    ) -> Optional[str]:
        """Single-turn, tool-free chat. Returns text or None on failure."""

    # ── Response parsing ───────────────────────────────────────────────

    @abc.abstractmethod
    def parse_response(
        self, response: Any, model: Optional[str] = None
    ) -> tuple[str, str, list[dict]]:
        """Return ``(stop_reason, text, tool_calls)``.

        ``stop_reason`` is normalised to ``"tool_use"`` or ``"end_turn"``.
        Each tool call is ``{"name", "id", "input"}``.
        """

    @abc.abstractmethod
    def completion_error(
        self, response: Any, model: Optional[str] = None
    ) -> Optional[str]:
        """Describe a non-clean completion (truncation, refusal) or None."""

    def has_incomplete_tool_calls(
        self, response: Any, model: Optional[str] = None
    ) -> bool:
        """Return true when a truncated response contains partial tool protocol."""
        return False

    @staticmethod
    def observed_input_tokens(response: Any) -> Optional[int]:
        """The provider's exact input-token count for the call, if reported.

        This is the only ground truth available for what a payload actually
        cost.  Both supported providers report it on every response, under
        different names, so the base implementation reads either.
        """
        value = extract_provider_usage(response).input_tokens
        return value or None

    @staticmethod
    def observed_usage(response: Any) -> ProviderUsage:
        return extract_provider_usage(response)

    # ── Message-history shaping ────────────────────────────────────────

    @abc.abstractmethod
    def build_assistant_message(
        self, response: Any, text: str, model: Optional[str] = None
    ) -> dict:
        """Construct the assistant turn entry to append to messages."""

    @abc.abstractmethod
    def build_tool_result_messages(
        self, tool_calls: list[dict], results: list[str],
        model: Optional[str] = None,
    ) -> list[dict]:
        """Build the tool-result message(s) to append after a tool batch."""

    @abc.abstractmethod
    def tool_result_rollback_count(
        self, tool_call_count: int, model: Optional[str] = None
    ) -> int:
        """How many trailing messages a tool batch added, for rollback math."""

    @abc.abstractmethod
    def build_final_message(
        self, response: Any, text: str, model: Optional[str] = None
    ) -> dict:
        """Build the assistant entry for an ``end_turn`` (no tool calls) response.

        Distinct from ``build_assistant_message`` because some providers
        carry extra metadata on the final turn (e.g. OpenAI ``model_extra``
        fields) that the tool-batch path also needs; others (Anthropic) can
        collapse to a plain text entry once tool_use blocks are gone.
        """

    @abc.abstractmethod
    def image_content_block(self, mime_type: str, base64_data: str) -> dict[str, Any]:
        """Provider-shaped content block for an inline image attachment."""


# ─────────────────────────────────────────────────────────────────────────────
# Anthropic
# ─────────────────────────────────────────────────────────────────────────────


class AnthropicTransport(ModelTransport):
    api_format = "anthropic"

    def convert_tools(
        self, tools: list[dict], model: Optional[str] = None
    ) -> Any:
        return tools if tools else anthropic.NOT_GIVEN

    def _thinking_kwarg(self, max_tokens: int, effort_override=None) -> dict:
        """``thinking=...`` for this call's effort, if any.

        ``effort_override`` is a per-model effort replacing the provider's own
        for this call (see ``RoutingTransport``); ``None`` means the call made
        no override and the provider's configured effort stands.  Nothing is
        sent when the effective effort is None or "off", so every existing
        config keeps making exactly today's request.  A budget must stay under
        ``max_tokens`` (the API rejects otherwise), and the clamp is what makes
        ``high`` usable on a provider capped at a few thousand tokens.
        """
        effort = (
            shared.normalize_thinking_effort(effort_override)
            if effort_override is not None
            else self.thinking_effort
        )
        if effort is None or effort == "off":
            return {}
        # The API demands a budget of at least 1024 *and strictly below*
        # max_tokens.  A call with no such room (max_tokens <= 1024) would be
        # a request that cannot be satisfied -- sent anyway it is a guaranteed
        # 400 -- so the effort is dropped and the call goes out as a plain
        # one rather than as a refusal.
        if int(max_tokens) <= 1024:
            return {}
        budget = _ANTHROPIC_THINKING_BUDGETS.get(effort, 1024)
        budget = max(1024, min(budget, int(max_tokens) - 1024))
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}

    @staticmethod
    def _thinking_text(response: Any) -> str:
        blocks = getattr(response, "content", None) or ()
        return "".join(
            getattr(block, "thinking", "") or ""
            for block in blocks
            if getattr(block, "type", "") == "thinking"
        )

    async def create(
        self, *, model, max_tokens, system, messages, tools,
        thinking_effort=None,
    ):
        return await self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=self.convert_tools(tools),
            **self._thinking_kwarg(max_tokens, thinking_effort),
            **self._headers_kwarg(),
        )

    async def stream(
        self, *, model, max_tokens, system, messages, tools, callback,
        reasoning_callback=None, thinking_effort=None,
    ):
        collected: list[str] = []
        async with self.client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=self.convert_tools(tools),
            **self._thinking_kwarg(max_tokens, thinking_effort),
            **self._headers_kwarg(),
        ) as stream:
            # ``text_stream`` yields answer text only, so the thinking blocks
            # are read off the finished message instead.  That trades
            # progressive thinking for leaving the verified text path exactly
            # as it was; the SDK's raw-event iterator would stream both, but it
            # would also have to replace this loop wholesale.
            async for text in stream.text_stream:
                collected.append(text)
                _r = callback(text)
                if inspect.isawaitable(_r):
                    await _r
            response = await stream.get_final_message()
        await self._emit_reasoning(reasoning_callback, self._thinking_text(response))
        return response, "".join(collected)

    async def simple_chat(self, *, model, max_tokens, system, prompt):
        with shared._suppress_with_log("anthropic.simple_chat failed; returning None"):
            resp = await self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                **self._headers_kwarg(),
            )
            if resp.content and hasattr(resp.content[0], "text"):
                return resp.content[0].text.strip()
        return None

    def parse_response(self, response, model=None):
        stop_reason = response.stop_reason  # "end_turn" | "tool_use"
        text_blocks = [b for b in response.content if hasattr(b, "text")]
        text = " ".join(b.text for b in text_blocks)
        tool_calls = [
            {"name": b.name, "id": b.id, "input": b.input}
            for b in response.content
            if b.type == "tool_use"
        ]
        return stop_reason, text, tool_calls

    def completion_error(self, response, model=None):
        if getattr(response, "stop_reason", None) == "max_tokens":
            return "Model response was truncated (stop_reason=max_tokens)"
        return None

    def build_assistant_message(self, response, text, model=None):
        return {"role": "assistant", "content": response.content}

    def build_tool_result_messages(self, tool_calls, results, model=None):
        return [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tc["id"], "content": r}
                    for tc, r in zip(tool_calls, results)
                ],
            }
        ]

    def tool_result_rollback_count(self, tool_call_count, model=None):
        return 1  # All tool results live in a single user message

    def build_final_message(self, response, text, model=None):
        # No tool_use blocks to preserve — plain text entry is canonical.
        return {"role": "assistant", "content": text}

    def image_content_block(self, mime_type, base64_data):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": base64_data,
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI-compatible
# ─────────────────────────────────────────────────────────────────────────────


class OpenAITransport(ModelTransport):
    api_format = "openai"

    #: The wire word for "do not think" on an OpenAI-compatible gateway.
    #: Measured against the active provider: `reasoning_effort: "none"` cut a
    #: trivial turn's reasoning from ~180 characters to zero and its completion
    #: from ~80 tokens to 1, while omitting the parameter left the model
    #: thinking in full.  So "off" has to say this word to mean anything —
    #: and a config that never mentions thinking still says nothing at all.
    _NO_THINKING = "none"

    def convert_tools(
        self, tools: list[dict], model: Optional[str] = None
    ) -> Any:
        if not tools:
            return None
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

    @classmethod
    def _inject_system(cls, messages: list[dict], system_prompt: str) -> list[dict]:
        return [{"role": "system", "content": system_prompt}] + cls._sanitize_messages(
            messages
        )

    def _reasoning_effort_kwarg(self, effort_override=None) -> dict:
        """``reasoning_effort=...`` for this call's effort, if any.

        ``effort_override`` is a per-model effort replacing the provider's own
        for this call (see ``RoutingTransport``); ``None`` means the call made
        no override and the configured effort stands.  A provider the config
        never spoke about sends nothing — the parameter is non-standard, so a
        gateway that does not know it answers 400.  The effective word goes
        over the wire unchanged, because the levels are the provider's own;
        only "off" is translated, to the word that actually silences the model
        rather than merely declining to ask.
        """
        effort = (
            shared.normalize_thinking_effort(effort_override)
            if effort_override is not None
            else self.thinking_effort
        )
        if effort is None:
            return {}
        return {"reasoning_effort": self._NO_THINKING if effort == "off" else effort}

    def _create_kwargs(
        self, *, model, max_tokens, system, messages, tools, stream=False,
        thinking_effort=None,
    ):
        kwargs: dict = dict(
            model=model,
            max_tokens=max_tokens,
            messages=self._inject_system(messages, system),
        )
        api_tools = self.convert_tools(tools)
        if api_tools:
            kwargs["tools"] = api_tools
        if stream:
            kwargs["stream"] = True
            if self.stream_usage:
                # Without this a streamed call reports no usage at all, so a
                # streamed turn can be neither recorded nor used to calibrate
                # the token estimator — which is why the interactive path had
                # no rows in `usage_events` while every non-streaming call had
                # them.  The provider answers in a final chunk carrying an
                # empty `choices` list and the usage object; `stream` reads it.
                kwargs["stream_options"] = {"include_usage": True}
        kwargs.update(self._reasoning_effort_kwarg(thinking_effort))
        kwargs.update(self._headers_kwarg())
        return kwargs

    async def create(
        self, *, model, max_tokens, system, messages, tools,
        thinking_effort=None,
    ):
        return await self.client.chat.completions.create(
            **self._create_kwargs(
                model=model, max_tokens=max_tokens,
                system=system, messages=messages, tools=tools,
                thinking_effort=thinking_effort,
            )
        )

    async def stream(
        self, *, model, max_tokens, system, messages, tools, callback,
        reasoning_callback=None, thinking_effort=None,
    ):
        kwargs = self._create_kwargs(
            model=model, max_tokens=max_tokens,
            system=system, messages=messages, tools=tools, stream=True,
            thinking_effort=thinking_effort,
        )
        collected: list[str] = []
        finish_reason = "stop"
        tool_calls_acc: dict[int, dict] = {}
        provider_extras_acc: dict[str, Any] = {}
        #: The provider's own count for this call, when it sent one.  Kept
        #: apart from `provider_extras_acc` because that dict is merged into
        #: the assistant message that goes back into the history, and usage is
        #: a property of the call rather than content of the reply.
        usage: Any = None
        # Reasoning is reported as often as the gateway produces it — one
        # fragment per chunk, before the first answer token — so the UI can
        # show it growing instead of dumping it at the end of the turn.
        # Gateways disagree about whether a chunk carries the next fragment or
        # everything so far.  A cumulative chunk is, by definition, *longer*
        # than what has arrived and starts with it; anything else is the next
        # fragment, including a chunk identical to the accumulation — which is
        # just the same token twice, and must not be dropped.  Comparing
        # against the previous fragment instead of the accumulation would
        # swallow every token that extends its predecessor, which token streams
        # do constantly.
        accumulated_reasoning = ""
        # AsyncOpenAI.chat.completions.create() returns a coroutine that
        # awaits to an AsyncStream — must await before iterating.
        async for chunk in await self.client.chat.completions.create(**kwargs):
            # A usage-only chunk arrives last and carries an *empty* `choices`
            # list, so it must be read before the `continue` that skips it —
            # otherwise the one chunk that says what the call cost is the one
            # chunk this loop discards.  Guarded on `is not None` rather than
            # truthiness so a gateway that reports a zeroed usage is read as
            # "reported zero" instead of "reported nothing".
            if getattr(chunk, "usage", None) is not None:
                usage = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if reasoning_callback is not None:
                seen = self._reasoning_text(delta)
                if seen:
                    if (
                        len(seen) > len(accumulated_reasoning)
                        and seen.startswith(accumulated_reasoning)
                    ):
                        piece = seen[len(accumulated_reasoning):]
                        accumulated_reasoning = seen
                    else:
                        piece = seen
                        accumulated_reasoning += seen
                    await self._emit_reasoning(reasoning_callback, piece)
            if delta.content:
                collected.append(delta.content)
                _r = callback(delta.content)
                if inspect.isawaitable(_r):
                    await _r
            delta_extras = self._message_extras(delta)
            if delta_extras:
                provider_extras_acc = self._merge_extras(
                    provider_extras_acc, delta_extras
                )
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": tc_delta.id or "",
                            "name": (
                                tc_delta.function.name if tc_delta.function else ""
                            ) or "",
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
                    shared._OAIMsg(
                        "".join(collected),
                        oi_tool_calls,
                        provider_extras_acc or None,
                    ),
                )
            ],
            usage=usage,
        )
        return response, "".join(collected)

    async def simple_chat(self, *, model, max_tokens, system, prompt):
        with shared._suppress_with_log("openai.simple_chat failed; returning None"):
            resp = await self.client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                **self._headers_kwarg(),
            )
            if resp.choices and resp.choices[0].message.content:
                return resp.choices[0].message.content.strip()
        return None

    def parse_response(self, response, model=None):
        choice = response.choices[0]
        finish = choice.finish_reason
        msg = choice.message
        text = msg.content or ""
        if finish == "tool_calls" and msg.tool_calls:
            tool_calls = []
            for tc in msg.tool_calls:
                inp = self._parse_tool_arguments(tc.function.arguments)
                tool_calls.append(
                    {"name": tc.function.name, "id": tc.id, "input": inp}
                )
            return "tool_use", text, tool_calls
        return "end_turn", text, []

    def completion_error(self, response, model=None):
        try:
            finish = response.choices[0].finish_reason
        except Exception:
            return None
        if finish == "length":
            return "Model response was truncated (finish_reason=length)"
        return None

    def has_incomplete_tool_calls(
        self, response: Any, model: Optional[str] = None
    ) -> bool:
        try:
            choice = response.choices[0]
            return choice.finish_reason == "length" and bool(choice.message.tool_calls)
        except Exception:
            return False

    def build_assistant_message(self, response, text, model=None):
        msg = response.choices[0].message
        entry: dict = {"role": "assistant", "content": text}
        entry.update(self._message_extras(msg))
        if msg.tool_calls:
            entry["tool_calls"] = [
                self._sanitize_tool_call(
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                )
                for tc in msg.tool_calls
            ]
        return entry

    def build_tool_result_messages(self, tool_calls, results, model=None):
        return [
            {"role": "tool", "tool_call_id": tc["id"], "content": r}
            for tc, r in zip(tool_calls, results)
        ]

    def tool_result_rollback_count(self, tool_call_count, model=None):
        return tool_call_count  # One tool message per call

    def build_final_message(self, response, text, model=None):
        # Reuse the tool-batch entry shape so model_extra fields survive.
        return self.build_assistant_message(response, text)

    def image_content_block(self, mime_type, base64_data):
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{base64_data}"},
        }

    # ── Tool-call argument hardening ────────────────────────────────────

    @classmethod
    def _parse_tool_arguments(cls, arguments: Any) -> dict[str, Any]:
        if isinstance(arguments, dict):
            return copy.deepcopy(arguments)
        raw = "" if arguments is None else str(arguments)
        try:
            parsed = json.loads(raw)
        except Exception:
            return {"_malformed_arguments": raw}
        if isinstance(parsed, dict):
            return parsed
        return {"_malformed_arguments": raw}

    @classmethod
    def _sanitize_tool_call(cls, tool_call: Any) -> Any:
        if not isinstance(tool_call, dict):
            return tool_call
        cleaned = copy.deepcopy(tool_call)
        function = cleaned.get("function")
        if not isinstance(function, dict):
            return cleaned
        arguments = function.get("arguments")
        parsed = cls._parse_tool_arguments(arguments)
        function["arguments"] = json.dumps(parsed, ensure_ascii=False)
        return cleaned

    @classmethod
    def _sanitize_messages(cls, messages: list[dict]) -> list[dict]:
        # Shallow-copy the list and copy only the assistant messages whose
        # tool_calls need normalising.  deepcopy(messages) copied the entire
        # history — tool results, image base64, everything — on every OpenAI
        # create/stream call; _sanitize_tool_call already deep-copies each
        # individual tool_call, so the surrounding message only needs a
        # shallow dict copy before its tool_calls field is replaced.
        sanitized: list[dict] = []
        for message in messages:
            if not isinstance(message, dict):
                sanitized.append(message)
                continue
            if message.get("role") != "assistant":
                sanitized.append(message)
                continue
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                sanitized.append(message)
                continue
            copied = dict(message)
            copied["tool_calls"] = [
                cls._sanitize_tool_call(tc) for tc in tool_calls
            ]
            sanitized.append(copied)
        return sanitized

    # ── Provider-extras handling (model_extra fields the API echoes back) ──

    @classmethod
    def _message_extras(cls, message: Any) -> dict[str, Any]:
        if message is None:
            return {}
        extras: dict[str, Any] = {}
        if isinstance(message, dict):
            for key, value in message.items():
                if key in _OPENAI_MESSAGE_RESERVED_FIELDS:
                    continue
                sanitized = cls._sanitize_extra(value)
                if sanitized is not _SKIP_OPENAI_EXTRA:
                    extras[key] = sanitized
            model_extra = message.get("model_extra")
            if isinstance(model_extra, dict):
                for key, value in model_extra.items():
                    if key in _OPENAI_MESSAGE_RESERVED_FIELDS:
                        continue
                    sanitized = cls._sanitize_extra(value)
                    if sanitized is not _SKIP_OPENAI_EXTRA:
                        extras[key] = sanitized
            return extras
        raw_fields = getattr(message, "__dict__", None)
        if isinstance(raw_fields, dict):
            for key, value in raw_fields.items():
                if key.startswith("_") or key in _OPENAI_MESSAGE_RESERVED_FIELDS:
                    continue
                sanitized = cls._sanitize_extra(value)
                if sanitized is not _SKIP_OPENAI_EXTRA:
                    extras[key] = sanitized
        model_extra = getattr(message, "model_extra", None)
        if isinstance(model_extra, dict):
            for key, value in model_extra.items():
                if key in _OPENAI_MESSAGE_RESERVED_FIELDS:
                    continue
                sanitized = cls._sanitize_extra(value)
                if sanitized is not _SKIP_OPENAI_EXTRA:
                    extras[key] = sanitized
        return extras

    @classmethod
    def _sanitize_extra(cls, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    continue
                sanitized = cls._sanitize_extra(item)
                if sanitized is _SKIP_OPENAI_EXTRA:
                    continue
                cleaned[key] = sanitized
            return cleaned
        if isinstance(value, (list, tuple)):
            cleaned_list: list[Any] = []
            for item in value:
                sanitized = cls._sanitize_extra(item)
                if sanitized is _SKIP_OPENAI_EXTRA:
                    continue
                cleaned_list.append(sanitized)
            return cleaned_list
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return cls._sanitize_extra(model_dump(mode="python"))
            except Exception:
                return _SKIP_OPENAI_EXTRA
        return _SKIP_OPENAI_EXTRA

    @classmethod
    def _merge_extra_value(cls, current: Any, incoming: Any) -> Any:
        incoming = cls._sanitize_extra(incoming)
        if incoming is _SKIP_OPENAI_EXTRA:
            return copy.deepcopy(current)
        current = cls._sanitize_extra(current)
        if current is _SKIP_OPENAI_EXTRA:
            current = None
        if current is None:
            return copy.deepcopy(incoming)
        if incoming is None:
            return copy.deepcopy(current)
        if isinstance(current, str) and isinstance(incoming, str):
            if incoming == current:
                return current
            if incoming.startswith(current):
                return incoming
            if current.startswith(incoming) or current.endswith(incoming):
                return current
            return current + incoming
        if isinstance(current, dict) and isinstance(incoming, dict):
            merged = copy.deepcopy(current)
            for key, value in incoming.items():
                merged[key] = cls._merge_extra_value(merged.get(key), value)
            return merged
        return copy.deepcopy(incoming)

    @classmethod
    def _merge_extras(
        cls, current: dict[str, Any], incoming: dict[str, Any]
    ) -> dict[str, Any]:
        merged = copy.deepcopy(current)
        for key, value in incoming.items():
            merged[key] = cls._merge_extra_value(merged.get(key), value)
        return merged


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────


def build_transport(
    api_format: str,
    client: Any,
    thinking_effort: Any = None,
    stream_usage: Any = True,
    headers: Optional[dict[str, str]] = None,
) -> ModelTransport:
    """Single dispatch point — adding a provider here is the only place to edit.

    ``thinking_effort`` is the provider's configured effort (see
    ``shared.THINKING_EFFORTS``); omitted means "no opinion", which is what
    keeps an unconfigured provider's requests byte-for-byte what they were.

    ``stream_usage`` reaches only the OpenAI transport.  Anthropic's SDK reports
    usage on the finished streamed message with no parameter to ask for it, so
    there is nothing there to configure and passing the value on would be
    carrying a name that is never read.

    ``headers`` is the provider's configured extra request headers, already
    environment-expanded by :func:`shared.resolve_provider_headers`.  It
    reaches both formats: the need is a property of the gateway, not of the
    wire format it speaks.
    """
    if api_format == "anthropic":
        return AnthropicTransport(client, thinking_effort, headers=headers)
    if api_format == "openai":
        return OpenAITransport(
            client, thinking_effort, stream_usage, headers=headers
        )
    raise ValueError(f"unsupported api_format: {api_format!r}")


class RoutingTransport(ModelTransport):
    """Route each call to the transport of the provider owning the model.

    ``model_override`` carries only a model id, so without this layer a model
    from another provider would be sent to the active provider's client and
    fail (or worse, silently hit a same-named model there). Routing keeps the
    override a plain string for callers while dispatching to the right SDK
    client per call.

    Every method that is per-call (create/stream/simple_chat and the
    format-aware message builders) routes on the model. Methods that only
    depend on the agent's own state (image blocks, tool-result rollback
    counts for messages this transport built) delegate to the transport that
    produced those messages — pass the model, or omit it for the default.

    Routing table: model id → transport. Models absent from the table go to
    the default transport (the active provider). When a model id exists under
    several providers, the active provider wins — that is also what the
    model dropdown offers, so the visible option and the routing agree.
    """

    def __init__(
        self,
        default: ModelTransport,
        routes: dict[str, ModelTransport],
        thinking_overrides: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(
            default.client,
            default.thinking_effort,
            default.stream_usage,
            headers=default.headers,
        )
        self.default = default
        self.routes = routes
        # A per-model effort wins over the owning provider's, keyed by model
        # id — the same key the routes answer to.  Held here rather than in
        # per-model transports because the effort is a property of the model
        # within the group, not of a client.
        self.thinking_overrides = dict(thinking_overrides or {})
        # The format of an unrouted call, for callers that ask the transport
        # rather than a transport's endpoint.
        self.api_format = default.api_format

    def _for(self, model: Optional[str]) -> ModelTransport:
        if model is None:
            return self.default
        return self.routes.get(model, self.default)

    def _effort_override(self, model: Optional[str]) -> Optional[str]:
        """The effort this call carries, when a model was told to differ.

        ``None`` means "no override": the transport that owns the model sends
        the effort it was configured with.  An override replaces it for this
        call only — the transport is shared by every model in its group, so
        mutating it would change them all.
        """
        if model is None or model not in self.thinking_overrides:
            return None
        return self.thinking_overrides[model]

    def endpoint_for(self, model: Optional[str] = None) -> ModelEndpoint:
        """The client and format owned by *model*.

        Background consumers (memory consolidation, session-end flushes, the
        evolution engine) make their own LLM calls and historically paired the
        active provider's client with whatever model the config named — so a
        `consolidation.model` from another provider's group was posted to the
        active provider's endpoint and rejected with the same 400 the agent
        loop used to produce.  Asking the router resolves the pair instead.
        """
        transport = self._for(model)
        return ModelEndpoint(transport.client, transport.api_format)

    def convert_tools(self, tools: list[dict], model: Optional[str] = None) -> Any:
        return self._for(model).convert_tools(tools)

    async def create(self, *, model, max_tokens, system, messages, tools):
        transport = self._for(model)
        return await transport.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            thinking_effort=self._effort_override(model),
        )

    async def stream(
        self, *, model, max_tokens, system, messages, tools, callback,
        reasoning_callback=None,
    ):
        # The routed transport carries its own provider's thinking effort, so
        # the effort follows the model and never has to be passed alongside it
        # -- except where a per-model override said otherwise, which arrives
        # as an explicit effort for this call.
        transport = self._for(model)
        return await transport.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            callback=callback,
            reasoning_callback=reasoning_callback,
            thinking_effort=self._effort_override(model),
        )

    async def simple_chat(self, *, model, max_tokens, system, prompt):
        transport = self._for(model)
        return await transport.simple_chat(
            model=model, max_tokens=max_tokens, system=system, prompt=prompt
        )

    def parse_response(
        self, response: Any, model: Optional[str] = None
    ) -> tuple[str, str, list[dict]]:
        return self._for(model).parse_response(response)

    def completion_error(
        self, response: Any, model: Optional[str] = None
    ) -> Optional[str]:
        return self._for(model).completion_error(response)

    def has_incomplete_tool_calls(
        self, response: Any, model: Optional[str] = None
    ) -> bool:
        return self._for(model).has_incomplete_tool_calls(response, model=model)

    def build_final_message(
        self, response: Any, text: str, model: Optional[str] = None
    ) -> dict:
        return self._for(model).build_final_message(response, text)

    def build_assistant_message(
        self, response: Any, text: str, model: Optional[str] = None
    ) -> dict:
        return self._for(model).build_assistant_message(response, text)

    def build_tool_result_messages(
        self, tool_calls: list[dict], results: list[str], model: Optional[str] = None
    ) -> list[dict]:
        return self._for(model).build_tool_result_messages(tool_calls, results)

    def tool_result_rollback_count(
        self, tool_call_count: int, model: Optional[str] = None
    ) -> int:
        return self._for(model).tool_result_rollback_count(tool_call_count)

    def image_content_block(self, mime_type: str, data: str) -> dict:
        # Attachment blocks are built from agent-local state before a
        # transport is chosen; use the default provider's shape.
        return self.default.image_content_block(mime_type, data)


def provider_model_ids(provider_cfg: dict) -> list[str]:
    """The model ids a provider offers.  Also read by the settings page.

    Its ``models`` list, plus the model it declares as its own
    ``default_model``.  The default is included even when a list exists: a
    group that names a default it does not also list is still claiming that
    model, and omitting it left the id out of the routing table — so a
    selection or a config value naming it was sent to whichever provider
    happened to be active instead of the one that declares it.  Whitespace is
    stripped so the routing key, the validation set, and the id sent to the
    provider are the same token.
    """
    declared = provider_cfg.get("models")
    if isinstance(declared, str):
        declared = [declared]
    models = [
        model.strip()
        for model in (declared or [])
        if isinstance(model, str) and model.strip()
    ]
    default = provider_cfg.get("default_model")
    if isinstance(default, str) and default.strip():
        stripped = default.strip()
        if stripped not in models:
            models.append(stripped)
    return models


def provider_thinking_effort(provider_cfg: dict) -> str | None:
    """The thinking effort one provider's config asks for, or None.

    ``providers.<name>.thinking.effort`` — the setting belongs to the provider
    because the wire parameter does: the word is the provider's own, and a
    level that means "think more" on one gateway means a rejected request on
    another.  Absent (or unrecognised) yields None, so a provider that was
    never configured sends nothing.  The ``thinking`` block is a dict rather
    than a bare string so later knobs have somewhere to live.
    """
    thinking = provider_cfg.get("thinking")
    if isinstance(thinking, str):
        # Tolerate the short form (`"thinking": "high"`) because it is the
        # obvious thing to write and the intent is unambiguous.
        return shared.normalize_thinking_effort(thinking)
    if not isinstance(thinking, dict):
        return None
    return shared.normalize_thinking_effort(thinking.get("effort"))


#: Spellings a JSON config uses for "no".  Only the explicit ones count, so a
#: typo leaves the measurement working rather than silently switching it off.
_FALSY_WORDS = frozenset({"false", "no", "off", "none", "0"})


def provider_stream_usage(provider_cfg: dict) -> bool:
    """Whether one provider's streaming calls should ask for usage.

    ``providers.<name>.stream_usage``, defaulting to **on** — the opposite
    default from ``thinking.effort``'s, and deliberately so.  ``reasoning_effort``
    is a non-standard parameter, so a provider the config never mentioned must
    not receive it; ``stream_options`` is part of the OpenAI chat-completions
    format this transport already speaks, and a stream that reports nothing
    cannot be recorded or used to calibrate the estimator, which is exactly the
    state the interactive path was stuck in.

    The escape hatch exists for a gateway that rejects the parameter: it names
    ``stream_options`` in its own error, and one line of provider config turns
    the request back into what it was.
    """
    if not isinstance(provider_cfg, dict):
        return True
    value = provider_cfg.get("stream_usage")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in _FALSY_WORDS
    if isinstance(value, int):
        return value != 0
    return True


def model_thinking_overrides(provider_cfg: dict) -> dict[str, str]:
    """Per-model efforts that win over the provider's own, keyed by model id.

    ``providers.<name>.thinking.models`` — a provider's group is allowed to be
    mixed (a reasoning model next to one that answers ``reasoning_effort``
    with a 400), and the routing table already says which model is which.  The
    values are normalised here, so an override that names no recognised word
    is simply absent rather than a bad word travelling to the API.
    """
    thinking = provider_cfg.get("thinking")
    if not isinstance(thinking, dict):
        return {}
    models = thinking.get("models")
    if not isinstance(models, dict):
        return {}
    overrides: dict[str, str] = {}
    for model, effort in models.items():
        normalised = shared.normalize_thinking_effort(effort)
        if normalised is not None:
            overrides[str(model)] = normalised
    return overrides


def routing_table(cfg: dict) -> dict[str, str]:
    """Model id → the provider that owns it.

    The single definition of "which group does this model belong to".
    ``build_routing_transport`` turns it into transports and
    ``routable_model_ids`` re-exports its keys, so the set of ids a caller may
    request and the set the router can dispatch are the same set by
    construction — when they drifted, an id could pass validation and still be
    posted to the wrong provider's endpoint.

    The active provider wins a contested id: that is what the model dropdown
    shows (its group is listed first) and which client the id should reach.
    """
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        return {}
    active = str(cfg.get("active_provider") or "")
    table: dict[str, str] = {}
    for name, provider_cfg in providers.items():
        if not isinstance(provider_cfg, dict):
            continue
        for model in provider_model_ids(provider_cfg):
            if model in table and name != active:
                continue
            table[model] = name
    return table


def routable_model_ids(cfg: dict) -> set[str]:
    """Model ids a model_override may carry.

    Every id the routing table can dispatch, plus the configured top-level
    ``model`` — which names the *active* provider's model, because that is
    what the client factory resolves it to.  Anything else is an id no group
    owns: requesting it could only reach the wrong endpoint.
    """
    ids: set[str] = set(routing_table(cfg))
    top_level = cfg.get("model")
    if isinstance(top_level, str) and top_level.strip():
        ids.add(top_level.strip())
    return ids


def provider_client_cache_key(
    provider_cfg: dict, api_format: str
) -> tuple[str, str, str]:
    """The identity of one SDK client: who it talks to, and how.

    Credentials *and* base_url, so a config edit that changes either yields a
    fresh client rather than reusing a stale connection.  Its own function
    because both the routing loop and the default-client resolution below
    must agree about when two providers are the same client.
    """
    return (
        str(provider_cfg.get("api_key", "") or ""),
        str(provider_cfg.get("base_url", "") or ""),
        api_format,
    )


def _client_for_provider(
    provider_cfg: dict,
    api_format: str,
    client_factory: Callable[[dict, str], Any],
    client_cache: Optional[dict[tuple[str, str, str], Any]],
) -> Any:
    """The one client for this provider, building it on first use."""
    if client_cache is None:
        return client_factory(provider_cfg, api_format)
    key = provider_client_cache_key(provider_cfg, api_format)
    client = client_cache.get(key)
    if client is None:
        client = client_factory(provider_cfg, api_format)
        client_cache[key] = client
    return client


def build_routing_transport(
    cfg: dict,
    default_format: str,
    default_client: Any,
    client_factory: Callable[[dict, str], Any],
    client_cache: Optional[dict[tuple[str, str, str], Any]] = None,
    *,
    default_client_cache_key: Optional[tuple[str, str, str]] = None,
) -> RoutingTransport:
    """Build a RoutingTransport from provider config.

    ``client_factory(provider_cfg, api_format)`` constructs one SDK client.
    Web sessions rebuild their routing transport on every session-runtime
    creation (config is re-read there), while SDK clients each own a
    connection pool that must not be leaked per session — pass a shared
    ``client_cache`` so one provider configuration maps to one client for
    the process lifetime. The cache key includes the api key/base_url so a
    config edit yields a fresh client instead of reusing a stale one. The
    active provider reuses ``default_client`` so its routed and default
    paths share one client.

    ``default_client_cache_key`` records the provider configuration that
    built ``default_client``.  The active provider's models are routed through
    the default transport, so trusting a client built from an older endpoint,
    credential, or wire format posts, say,
    ``glm-5.3-flash`` to whichever endpoint happened to be active when the
    client was built:

        400 The supported API model names are ..., but you passed glm-5.3-flash.

    That is reachable without anyone lying: a web session runtime is rebuilt
    from the config on disk, but the client it inherits was built when the
    process started, and editing provider settings in between leaves the two
    generations disagreeing.  Comparing the complete client identity lets
    this function detect it and build the client for the config it was given —
    through the same cache, so the process still holds one client per
    provider rather than one per session.  Omitted, the client is trusted as
    before, which is what the process's own build (client and cfg from the
    same call) passes.

    The routes come from ``routing_table``: one transport per provider, no
    second opinion about who owns a model.
    """
    providers = cfg.get("providers", {}) or {}
    active = str(cfg.get("active_provider") or "")
    active_cfg = providers.get(active)
    if not isinstance(active_cfg, dict):
        active_cfg = None
    active_format = (
        str(active_cfg.get("api_format") or default_format)
        if active_cfg is not None
        else default_format
    )
    active_cache_key = (
        provider_client_cache_key(active_cfg, active_format)
        if active_cfg is not None
        else None
    )

    if client_cache is not None:
        # Keep the inherited client under the identity that actually built it.
        # The process bootstrap omits the identity because its client and cfg
        # were resolved together, so the active key is the honest fallback.
        inherited_key = default_client_cache_key or active_cache_key
        if default_client is not None and inherited_key is not None:
            client_cache.setdefault(inherited_key, default_client)

    if (
        default_client_cache_key is not None
        and default_client_cache_key != active_cache_key
        and active_cfg is not None
    ):
        default_format = active_format
        default_client = _client_for_provider(
            active_cfg, default_format, client_factory, client_cache
        )

    default_transport = build_transport(
        default_format,
        default_client,
        provider_thinking_effort(active_cfg) if active_cfg else None,
        provider_stream_usage(active_cfg) if active_cfg else True,
        headers=shared.resolve_provider_headers(
            active_cfg, provider_name=active
        )
        if active_cfg
        else None,
    )
    routes: dict[str, ModelTransport] = {}
    transports: dict[str, ModelTransport] = {}

    def _client_for(provider_cfg: dict, api_format: str) -> Any:
        return _client_for_provider(
            provider_cfg, api_format, client_factory, client_cache
        )

    overrides: dict[str, str] = {}
    for model, name in routing_table(cfg).items():
        provider_cfg = providers.get(name)
        if not isinstance(provider_cfg, dict):
            continue
        # A model's own effort wins over its provider's, so a mixed group (a
        # reasoning model next to one that 400s on the parameter) can be
        # configured in one place.
        model_override = model_thinking_overrides(provider_cfg).get(model)
        if model_override is not None:
            overrides[model] = model_override
        api_format = str(provider_cfg.get("api_format", "openai"))
        if name == active and api_format == default_format:
            # One instance for the active provider, so its models resolve to
            # the very transport that handles unrouted calls.
            transport = default_transport
        else:
            transport = transports.get(name)
            if transport is None:
                transport = build_transport(
                    api_format,
                    _client_for(provider_cfg, api_format),
                    provider_thinking_effort(provider_cfg),
                    provider_stream_usage(provider_cfg),
                    headers=shared.resolve_provider_headers(
                        provider_cfg, provider_name=name
                    ),
                )
                transports[name] = transport
        routes[model] = transport
    return RoutingTransport(default_transport, routes, overrides)
