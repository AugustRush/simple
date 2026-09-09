"""LLM-driven consolidation — the memory subsystem's 'sleep' mechanism."""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from agent import shared
from agent.lexical import count_cjk_chars
from agent.usage import extract_provider_usage

from ._helpers import _new_id, _now
from .models import ConsolidationResult, LTMEntry
from .store import LTMStore

class ConsolidationEngine:
    """LLM-driven context consolidation — the 'sleep' mechanism.

    Triggered when working memory exceeds shared.SLEEP_TOKEN_RATIO of max_tokens.
    Extracts structured facts from conversation, stores in LTM, applies decay,
    and compresses ctx.messages to the most recent entries.
    """

    def __init__(
        self,
        store: LTMStore,
        max_categories: int = shared.MAX_CATEGORIES,
        decay_factor: float = shared.DECAY_FACTOR,
        sleep_token_ratio: float = shared.SLEEP_TOKEN_RATIO,
        keep_last_messages: int = 6,
        max_source_tokens: int = shared.CONSOLIDATION_MAX_SOURCE_TOKENS,
        max_chunks_per_run: int = 0,
        output_tokens: int = 512,
        chars_per_token: float = float(shared.CHARS_PER_TOKEN),
        cjk_chars_per_token: float = 1.0,
    ):
        self.store = store
        self.max_categories = max_categories
        self.decay_factor = decay_factor
        self.sleep_token_ratio = sleep_token_ratio
        self.keep_last_messages = keep_last_messages
        self.max_source_tokens = max(1, int(max_source_tokens))
        self.max_chunks_per_run = max(0, int(max_chunks_per_run))
        self.output_tokens = max(64, min(2048, int(output_tokens)))
        # Token estimation ratios — configurable for different script systems.
        # chars_per_token:     non-CJK chars per token (Latin/ASCII, default 4)
        # cjk_chars_per_token: CJK chars per token (Hanzi/Kana/Hangul, default 1)
        self.chars_per_token: float = max(0.1, float(chars_per_token))
        self.cjk_chars_per_token: float = max(0.1, float(cjk_chars_per_token))
        # Multiplier correcting the character heuristic toward the provider's
        # actual accounting.  Starts neutral and is adjusted from observed usage
        # (see observe_actual_usage); 1.0 means "heuristic used as-is".
        self.token_calibration: float = 1.0
        self._calibration_samples: int = 0

    # ── Trigger ───────────────────────────────────────────────────────────────

    # Providers charge for an image by its rendered dimensions, not by the
    # length of its base64 transport encoding.  Anthropic caps a single image at
    # roughly 1.6k tokens; OpenAI's high-detail tiling lands in the same order of
    # magnitude.  Estimating `len(base64)/4` instead reports a 1.5MB image as
    # ~512k tokens — 320x its real cost — which alone can exceed any budget and
    # force the whole conversation to be evicted for one screenshot.
    MAX_IMAGE_TOKENS = 1600

    @classmethod
    def _non_text_block_tokens(
        cls, block: dict, count_text: Callable[[str], int]
    ) -> int:
        """Cost of a non-text content block under the provider's price model."""
        block_type = str(block.get("type") or "")
        if block_type in {"image", "input_image", "image_url"}:
            return cls.MAX_IMAGE_TOKENS
        source = block.get("source")
        if isinstance(source, dict) and source.get("data") is not None:
            # An inline base64 payload of unknown type: price it as an image
            # rather than by transport length.
            return cls.MAX_IMAGE_TOKENS
        # Anything else genuinely is text the provider reads verbatim.
        return count_text(json.dumps(block, ensure_ascii=False, default=str))

    def estimate_tokens(self, messages: list[dict]) -> int:
        """Token estimate with CJK-awareness.

        Non-CJK text:    ``len(text) / chars_per_token``   (default 4 chars/token)
        CJK characters:  ``len(cjk) / cjk_chars_per_token`` (default 1 char/token)

        Both ratios are configurable via ``context.consolidation.token_estimation``
        in config.json so they can be tuned for different languages and model
        tokenisers.  Without the CJK distinction the estimate for Chinese
        conversations is ~4x too low, causing the compact trigger to fire far
        later than intended.  Also counts tool_use ``input`` payloads which the
        previous implementation silently ignored.
        """
        def _count(text: str) -> int:
            cjk = count_cjk_chars(text)
            non_cjk = len(text) - cjk
            return int(cjk / self.cjk_chars_per_token) + int(
                non_cjk / self.chars_per_token
            )

        total = 0.0
        for msg in messages:
            # Provider chat protocols charge a small envelope cost per message
            # even when content is empty.
            total += 4
            content = msg.get("content", "")
            if isinstance(content, str):
                total += _count(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        total += _count(str(block))
                        continue
                    text_value = block.get("text", "") or block.get("content", "")
                    if text_value:
                        total += _count(str(text_value))
                    tool_input = block.get("input")
                    if tool_input is not None:
                        total += _count(
                            json.dumps(tool_input, ensure_ascii=False, default=str)
                        )
                    if not text_value and tool_input is None:
                        total += self._non_text_block_tokens(block, _count)
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                total += _count(
                    json.dumps(tool_calls, ensure_ascii=False, default=str)
                )
        return int(total * self.token_calibration)

    # Bounds on the learned multiplier.  A heuristic that needs more than a 4x
    # correction is broken in a way calibration should not paper over, and one
    # below 1.0 would make the estimator claim text is cheaper than the
    # character floor — neither is worth trusting.
    _MIN_CALIBRATION = 1.0
    _MAX_CALIBRATION = 4.0

    def observe_actual_usage(self, estimated: int, actual: int) -> None:
        """Correct the heuristic from the provider's exact count.

        The loss here is asymmetric: underestimating means a payload reaches the
        provider over its limit and the call hard-fails, while overestimating
        only wastes budget.  So an underestimate is corrected immediately and in
        full, and an overestimate is relaxed slowly.
        """
        if estimated <= 0 or actual <= 0:
            return
        observed_ratio = actual / estimated
        # Undo the multiplier already applied, to recover the ratio the raw
        # heuristic would need.
        needed = observed_ratio * self.token_calibration
        if needed > self.token_calibration:
            updated = needed  # underestimate: jump straight to safety
        else:
            updated = self.token_calibration + 0.1 * (needed - self.token_calibration)
        self.token_calibration = min(
            self._MAX_CALIBRATION, max(self._MIN_CALIBRATION, updated)
        )
        self._calibration_samples += 1

    def should_sleep(self, messages: list[dict], max_tokens: int) -> bool:
        return self.estimate_tokens(messages) >= int(
            max_tokens * self.sleep_token_ratio
        )

    def _message_lines_for_llm(self, messages: list[dict]) -> list[str]:
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown").upper()
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = [
                    block.get("text", "") or block.get("content", "")
                    for block in content
                    if isinstance(block, dict)
                ]
                content = " ".join(str(p) for p in parts)
            lines.append(f"{role}: {content}")
        return lines

    def _chunk_messages_for_llm(self, messages: list[dict]) -> list[str]:
        """Return bounded conversation chunks for extraction prompts.

        We use a conservative one-char-per-token upper bound so no chunk can
        exceed the configured budget, even for CJK-heavy inputs.
        """
        max_chars = self.max_source_tokens
        chunks: list[str] = []
        current_lines: list[str] = []
        current_len = 0

        for line in self._message_lines_for_llm(messages):
            segments = [line[i : i + max_chars] for i in range(0, len(line), max_chars)]
            if not segments:
                segments = [line]
            for segment in segments:
                extra_len = len(segment) if not current_lines else len(segment) + 2
                if current_lines and current_len + extra_len > max_chars:
                    chunks.append("\n\n".join(current_lines))
                    current_lines = [segment]
                    current_len = len(segment)
                else:
                    current_lines.append(segment)
                    current_len += extra_len

        if current_lines:
            chunks.append("\n\n".join(current_lines))
        return chunks

    def _build_consolidation_prompt(
        self,
        conversation_text: str,
        source_label: str,
        chunk_index: int,
        chunk_count: int,
    ) -> str:
        chunk_label = (
            f"{source_label}, chunk {chunk_index}/{chunk_count}"
            if chunk_count > 1
            else source_label
        )
        return (
            f"Analyze this conversation and extract important facts worth remembering.\n"
            f"For each item output JSON on its own line (no markdown fences):\n"
            f'{{"locus": "<one of {", ".join(shared.PALACE_LOCI)}>", "entity": "<anchor>", '
            f'"memory_type": "<type>", "content": "<fact>", "importance": <0.1-1.0>, "confidence": <0.1-1.0>}}\n\n'
            f"Rules:\n"
            f"- Use only the fixed loci listed above; never invent new top-level loci\n"
            f"- identity: durable identity facts; use entity='user' for user facts and entity='assistant' for agent self-identity\n"
            f"- projects: project decisions/state/risks\n"
            f"- people: person-specific facts\n"
            f"- concepts: durable domain knowledge\n"
            f"- tasks: commitments, next steps, open loops\n"
            f"- procedures: repeatable workflows and preferred methods\n"
            f"- archive: only if the memory is historical or superseded\n"
            f"- If the user gives the agent a stable name/role/identity, store it under identity with entity='assistant' and memory_type='self_identity'\n"
            f"- Do not output episodes; session summary is generated separately\n"
            f"- Be selective: max 8 items, 1-2 sentences each\n\n"
            f"Conversation ({chunk_label}):\n{conversation_text}"
        )

    # ── Main consolidation ────────────────────────────────────────────────────

    async def consolidate(
        self,
        messages: list[dict],
        client: Any,
        model: str,
        api_format: str = "anthropic",
        keep_last: Optional[int] = None,
        staging: Optional["StagingBuffer"] = None,
        project_scope: str = "",
    ) -> ConsolidationResult:
        """One sleep cycle: extract → classify → store → decay → compress.

        Source priority for LLM extraction:
          1. staging buffer (if non-empty) — full, clean conversation history
          2. ctx.messages fallback          — used only when staging is absent
        After extraction the staging buffer is cleared.
        """
        if keep_last is None:
            keep_last = self.keep_last_messages
        shared.CONSOLE.print("[dim]💤 Context consolidation (sleep)...[/dim]")

        # Choose extraction source
        staged = staging.read_all() if staging else []
        source = staged if staged else messages
        if not source:
            compressed = (
                messages[-keep_last:] if len(messages) > keep_last else messages
            )
            if messages:  # only print if there was something to compress
                shared.CONSOLE.print(
                    f"[dim]💤 Messages compressed: {len(messages)} → {len(compressed)}[/dim]"
                )
            return ConsolidationResult(success=True, compressed_messages=compressed)
        source_label = (
            f"staging ({len(staged)} turns)"
            if staged
            else f"messages ({len(messages)})"
        )
        conversation_chunks = self._chunk_messages_for_llm(source)
        omitted_chunks = 0
        if self.max_chunks_per_run and len(conversation_chunks) > self.max_chunks_per_run:
            omitted_chunks = len(conversation_chunks) - self.max_chunks_per_run
            # Preserve the initial request and the newest outcome. The journal
            # remains authoritative for middle detail omitted from this index.
            if self.max_chunks_per_run == 1:
                conversation_chunks = [conversation_chunks[-1]]
            else:
                head_count = self.max_chunks_per_run // 2
                tail_count = self.max_chunks_per_run - head_count
                conversation_chunks = (
                    conversation_chunks[:head_count]
                    + conversation_chunks[-tail_count:]
                )

        try:
            raw_responses: list[str] = []
            for idx, chunk_text in enumerate(conversation_chunks, start=1):
                prompt = self._build_consolidation_prompt(
                    chunk_text, source_label, idx, len(conversation_chunks)
                )
                if api_format == "anthropic":
                    resp = await client.messages.create(
                        model=model,
                        max_tokens=self.output_tokens,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    raw_responses.append(resp.content[0].text)
                else:
                    resp = await client.chat.completions.create(
                        model=model,
                        max_tokens=self.output_tokens,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    raw_responses.append(resp.choices[0].message.content or "")
                usage = extract_provider_usage(resp)
                record_usage = getattr(self.store, "append_usage_event", None)
                if callable(record_usage) and usage.total_tokens > 0:
                    record_usage(
                        session_id=staging.session_id if staging else "default",
                        phase="consolidation",
                        model=model,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cached_input_tokens=usage.cached_input_tokens,
                        metadata={
                            "chunk_index": idx,
                            "chunk_count": len(conversation_chunks),
                            "omitted_chunks": omitted_chunks,
                        },
                    )

            entries = [
                self._build_episode_entry(source, staging.session_id if staging else "")
            ]
            for raw in raw_responses:
                parsed = self._parse_entries(raw)
                for entry in parsed:
                    entry.scope = self._scope_for_entry(
                        entry.category,
                        session_id=staging.session_id if staging else "",
                        project_scope=project_scope,
                    )
                    entry.source_session = (
                        staging.session_id if staging else entry.source_session
                    )
                entries.extend(parsed)
            self.store.add_entries(entries)

            self.store.apply_retention()

            # Clear staging after successful extraction
            if staging and staged:
                staging.drop_prefix(len(staged))

            shared.CONSOLE.print(
                f"[dim]💤 Stored {len(entries)} entries from {source_label} "
                f"across {len(conversation_chunks)} chunk(s). "
                f"Dynamic categories: {self.store.dynamic_category_count()}/{self.max_categories}[/dim]"
            )
            success = True
        except Exception as e:
            shared.CONSOLE.print(f"[dim]Sleep extraction error: {e}[/dim]")
            success = False

        compressed = messages[-keep_last:] if len(messages) > keep_last else messages
        if messages:
            # Only print when there is actual working memory to compress; skip the
            # "0 → 0" line that appears when consolidate() is called from the
            # background job path (which passes messages=[]).
            shared.CONSOLE.print(
                f"[dim]💤 Messages compressed: {len(messages)} → {len(compressed)}[/dim]"
            )
        return ConsolidationResult(
            success=success,
            compressed_messages=compressed,
            stored_entries=len(entries) if success else 0,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _format_messages_for_llm(self, messages: list[dict]) -> str:
        return "\n\n".join(self._message_lines_for_llm(messages))

    @staticmethod
    def _infer_identity_entity(content: str, memory_type: str, entity: str) -> str:
        explicit = str(entity or "").strip().lower()
        if explicit:
            return explicit
        normalized_type = str(memory_type or "").strip().lower()
        if normalized_type in {"self_identity", "assistant_identity"}:
            return "assistant"

        lowered = str(content or "").strip().lower()
        assistant_markers = (
            "assistant",
            "agent",
            "bot",
            "your name",
            "你叫",
            "你的名字",
            "助手",
            "机器人",
        )
        user_markers = (
            "user",
            "the user",
            "用户",
            "我喜欢",
            "我通常",
            "prefers",
        )
        assistant_hit = any(marker in lowered for marker in assistant_markers)
        user_hit = any(marker in lowered for marker in user_markers)
        if assistant_hit and not user_hit:
            return "assistant"
        return "user"

    def _parse_entries(self, raw: str) -> list[LTMEntry]:
        entries = []
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
                content = data.get("content", "").strip()
                if not content:
                    continue
                category = data.get("locus") or data.get("category") or "concepts"
                normalized_category = self.store.normalize_category_name(category)
                memory_type = str(data.get("memory_type", "fact")).strip() or "fact"
                entity = str(data.get("entity", "")).strip()
                if normalized_category == "identity":
                    entity = self._infer_identity_entity(content, memory_type, entity)
                if normalized_category not in shared.PALACE_LOCI:
                    entity = entity or normalized_category
                    normalized_category = "concepts"
                # Clamp model-specified scores to the documented [0, 1] range:
                # a hallucinated 999 would otherwise pin the entry at the top
                # of retention ordering forever, and a negative value would
                # never survive a single decay pass.
                importance = min(1.0, max(0.0, float(data.get("importance", 0.5))))
                confidence = min(1.0, max(0.0, float(data.get("confidence", 1.0))))
                entries.append(
                    LTMEntry(
                        id=_new_id(),
                        content=content,
                        importance=importance,
                        category=normalized_category,
                        created_at=_now(),
                        updated_at=_now(),
                        entity=entity,
                        memory_type=memory_type,
                        source_session=str(data.get("source_session", "")).strip(),
                        confidence=confidence,
                    )
                )
            except Exception:
                continue
        return entries

    @staticmethod
    def _scope_for_entry(
        category: str,
        *,
        session_id: str = "",
        project_scope: str = "",
    ) -> str:
        normalized = str(category or "").strip().lower()
        if normalized in {"tasks", "episodes", "archive"} and session_id:
            return f"session:{session_id}"
        if normalized == "projects" and project_scope:
            return project_scope
        return "global"

    def _build_episode_entry(
        self, messages: list[dict], session_id: str = ""
    ) -> LTMEntry:
        snippets = []
        for msg in messages[-6:]:
            role = str(msg.get("role", "unknown")).upper()
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(block.get("text", "") or block.get("content", ""))
                    for block in content
                    if isinstance(block, dict)
                )
            content = str(content).strip()
            if content:
                snippets.append(f"{role}: {content[:180]}")
        summary = " | ".join(snippets)[:1200] or "Session summary unavailable."
        return LTMEntry(
            id=_new_id(),
            content=summary,
            importance=0.7,
            category="episodes",
            entity=session_id or "session",
            memory_type="session_summary",
            source_session=session_id,
            scope=f"session:{session_id}" if session_id else "global",
            confidence=1.0,
            created_at=_now(),
            updated_at=_now(),
        )
