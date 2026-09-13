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


class ModelTransport(abc.ABC):
    """Format-specific dispatch contract for one LLM provider.

    All methods are deliberately stateless w.r.t. agent loop state — the
    caller passes model/messages/tools per call.  The transport's only
    instance state is the SDK client.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

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
    ) -> tuple[Any, str]:
        """Streaming completion; returns (final_response, collected_text)."""

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
    def convert_tools(
        self, tools: list[dict], model: Optional[str] = None
    ) -> Any:
        return tools if tools else anthropic.NOT_GIVEN

    async def create(self, *, model, max_tokens, system, messages, tools):
        return await self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=self.convert_tools(tools),
        )

    async def stream(self, *, model, max_tokens, system, messages, tools, callback):
        collected: list[str] = []
        async with self.client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=self.convert_tools(tools),
        ) as stream:
            async for text in stream.text_stream:
                collected.append(text)
                _r = callback(text)
                if inspect.isawaitable(_r):
                    await _r
            response = await stream.get_final_message()
        return response, "".join(collected)

    async def simple_chat(self, *, model, max_tokens, system, prompt):
        with shared._suppress_with_log(f"anthropic.simple_chat failed; returning None"):
            resp = await self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
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

    def _create_kwargs(self, *, model, max_tokens, system, messages, tools, stream=False):
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
        return kwargs

    async def create(self, *, model, max_tokens, system, messages, tools):
        return await self.client.chat.completions.create(
            **self._create_kwargs(
                model=model, max_tokens=max_tokens,
                system=system, messages=messages, tools=tools,
            )
        )

    async def stream(self, *, model, max_tokens, system, messages, tools, callback):
        kwargs = self._create_kwargs(
            model=model, max_tokens=max_tokens,
            system=system, messages=messages, tools=tools, stream=True,
        )
        collected: list[str] = []
        finish_reason = "stop"
        tool_calls_acc: dict[int, dict] = {}
        provider_extras_acc: dict[str, Any] = {}
        # AsyncOpenAI.chat.completions.create() returns a coroutine that
        # awaits to an AsyncStream — must await before iterating.
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
            ]
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


def build_transport(api_format: str, client: Any) -> ModelTransport:
    """Single dispatch point — adding a provider here is the only place to edit."""
    if api_format == "anthropic":
        return AnthropicTransport(client)
    if api_format == "openai":
        return OpenAITransport(client)
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
    ) -> None:
        super().__init__(default.client)
        self.default = default
        self.routes = routes

    def _for(self, model: Optional[str]) -> ModelTransport:
        if model is None:
            return self.default
        return self.routes.get(model, self.default)

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
        )

    async def stream(
        self, *, model, max_tokens, system, messages, tools, callback
    ):
        transport = self._for(model)
        return await transport.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            callback=callback,
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


def _provider_models(provider_cfg: dict) -> list[str]:
    """The model ids a provider offers.

    Its ``models`` list, or its ``default_model`` alone when no list is
    configured. Whitespace is stripped so the routing key, the validation
    set, and the id sent to the provider are the same token.
    """
    models = provider_cfg.get("models") or []
    if not models and provider_cfg.get("default_model"):
        models = [provider_cfg["default_model"]]
    return [
        model.strip()
        for model in models
        if isinstance(model, str) and model.strip()
    ]


def routable_model_ids(cfg: dict) -> set[str]:
    """Model ids a model_override may carry.

    The composer dropdown offers every provider's models and the routing
    table dispatches on the id, so this is the honest validation set: every
    provider's models via the same definition the router uses, plus the
    configured top-level ``model`` (which resolves to the active provider's
    default anyway). Ambiguous ids belong to the active provider; the set
    does not care about ownership.
    """
    ids: set[str] = set()
    top_level = cfg.get("model")
    if isinstance(top_level, str) and top_level.strip():
        ids.add(top_level.strip())
    providers = cfg.get("providers")
    if isinstance(providers, dict):
        for provider_cfg in providers.values():
            if isinstance(provider_cfg, dict):
                ids.update(_provider_models(provider_cfg))
    return ids


def build_routing_transport(
    cfg: dict,
    default_format: str,
    default_client: Any,
    client_factory: Callable[[dict, str], Any],
    client_cache: Optional[dict[tuple[str, str, str], Any]] = None,
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
    """
    providers = cfg.get("providers", {}) or {}
    active = str(cfg.get("active_provider") or "")
    default_transport = build_transport(default_format, default_client)
    routes: dict[str, ModelTransport] = {}
    transports: dict[tuple[str, str], ModelTransport] = {}

    def _client_for(provider_cfg: dict, api_format: str) -> Any:
        if client_cache is None:
            return client_factory(provider_cfg, api_format)
        key = (
            str(provider_cfg.get("api_key", "") or ""),
            str(provider_cfg.get("base_url", "") or ""),
            api_format,
        )
        client = client_cache.get(key)
        if client is None:
            client = client_factory(provider_cfg, api_format)
            client_cache[key] = client
        return client

    for name, provider_cfg in providers.items():
        if not isinstance(provider_cfg, dict):
            continue
        models = _provider_models(provider_cfg)
        if not models:
            continue
        api_format = str(provider_cfg.get("api_format", "openai"))
        if name == active and api_format == default_format:
            # One instance for the active provider, so its models resolve to
            # the very transport that handles unrouted calls.
            transport = default_transport
        else:
            key = (name, api_format)
            if key not in transports:
                transports[key] = build_transport(
                    api_format, _client_for(provider_cfg, api_format)
                )
            transport = transports[key]
        for model in models:
            # Active provider wins conflicts; iterate it last is not enough
            # when it is not the last in the dict, so guard explicitly.
            if model in routes and name != active:
                continue
            routes[model] = transport
    return RoutingTransport(default_transport, routes)
