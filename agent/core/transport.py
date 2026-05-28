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
    def convert_tools(self, tools: list[dict]) -> Any:
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
    def parse_response(self, response: Any) -> tuple[str, str, list[dict]]:
        """Return ``(stop_reason, text, tool_calls)``.

        ``stop_reason`` is normalised to ``"tool_use"`` or ``"end_turn"``.
        Each tool call is ``{"name", "id", "input"}``.
        """

    @abc.abstractmethod
    def completion_error(self, response: Any) -> Optional[str]:
        """Describe a non-clean completion (truncation, refusal) or None."""

    # ── Message-history shaping ────────────────────────────────────────

    @abc.abstractmethod
    def build_assistant_message(self, response: Any, text: str) -> dict:
        """Construct the assistant turn entry to append to messages."""

    @abc.abstractmethod
    def build_tool_result_messages(
        self, tool_calls: list[dict], results: list[str]
    ) -> list[dict]:
        """Build the tool-result message(s) to append after a tool batch."""

    @abc.abstractmethod
    def tool_result_rollback_count(self, tool_call_count: int) -> int:
        """How many trailing messages a tool batch added, for rollback math."""

    @abc.abstractmethod
    def build_final_message(self, response: Any, text: str) -> dict:
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
    def convert_tools(self, tools: list[dict]) -> Any:
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

    def parse_response(self, response):
        stop_reason = response.stop_reason  # "end_turn" | "tool_use"
        text_blocks = [b for b in response.content if hasattr(b, "text")]
        text = " ".join(b.text for b in text_blocks)
        tool_calls = [
            {"name": b.name, "id": b.id, "input": b.input}
            for b in response.content
            if b.type == "tool_use"
        ]
        return stop_reason, text, tool_calls

    def completion_error(self, response):
        return None  # Anthropic surfaces truncation via stop_reason already

    def build_assistant_message(self, response, text):
        return {"role": "assistant", "content": response.content}

    def build_tool_result_messages(self, tool_calls, results):
        return [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tc["id"], "content": r}
                    for tc, r in zip(tool_calls, results)
                ],
            }
        ]

    def tool_result_rollback_count(self, tool_call_count):
        return 1  # All tool results live in a single user message

    def build_final_message(self, response, text):
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
    def convert_tools(self, tools: list[dict]) -> Any:
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

    @staticmethod
    def _inject_system(messages: list[dict], system_prompt: str) -> list[dict]:
        return [{"role": "system", "content": system_prompt}] + messages

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

    def parse_response(self, response):
        choice = response.choices[0]
        finish = choice.finish_reason
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

    def completion_error(self, response):
        try:
            finish = response.choices[0].finish_reason
        except Exception:
            return None
        if finish == "length":
            return "Model response was truncated (finish_reason=length)"
        return None

    def build_assistant_message(self, response, text):
        msg = response.choices[0].message
        entry: dict = {"role": "assistant", "content": text}
        entry.update(self._message_extras(msg))
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

    def build_tool_result_messages(self, tool_calls, results):
        return [
            {"role": "tool", "tool_call_id": tc["id"], "content": r}
            for tc, r in zip(tool_calls, results)
        ]

    def tool_result_rollback_count(self, tool_call_count):
        return tool_call_count  # One tool message per call

    def build_final_message(self, response, text):
        # Reuse the tool-batch entry shape so model_extra fields survive.
        return self.build_assistant_message(response, text)

    def image_content_block(self, mime_type, base64_data):
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{base64_data}"},
        }

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
