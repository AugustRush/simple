"""Built-in evolution plugin — wraps EvolutionEngine and adds first-principles
improvement logic (CorrectionDetector + RuleStore).

Plugin lifecycle
----------------
1. ``register()`` returns an ``EvolutionPlugin`` instance (no heavy init).
2. ``on_session_start(components)`` wires up the EvolutionEngine using the
   real LLM client/model/memory that were resolved by _build_components_async.
3. ``on_turn_end(event)`` runs CorrectionDetector; logs failures; calls
   ``record_application`` for every active rule so the feedback loop closes;
   triggers async rule extraction when the failure threshold is met.
4. ``on_session_end(event)`` calls EvolutionEngine.score_session() and
   prints the score exactly as before.
5. ``compose_system_prompt(current_prompt)`` appends active behavioral rules
   so the agent is guided by what was learned from past corrections.
   The returned string is a **suffix** — not a replacement.
6. ``register_slash_commands()`` exposes /evolve, /generate-tool, /stats.

Slash command handlers receive (raw_cmd: str, components: dict).
``components["ctx"]`` is the live AgentContext so handlers can update the
running system prompt without restarting the session.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_RL_DIR = Path.home() / ".agent" / "rl"
_FAILURES_FILE = _RL_DIR / "failures.jsonl"
_RULE_EXTRACTION_THRESHOLD = 3  # corrections before we try to extract a rule


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class EvolutionPlugin:
    """AgentPlugin implementation: wraps EvolutionEngine + new learning logic."""

    name = "evolution"
    version = "2.0.0"

    def __init__(self) -> None:
        # Heavy resources are populated lazily in on_session_start().
        self._engine: Any = None
        self._components: dict = {}
        self._prev_response: str = ""
        self._rule_store: Any = (
            None  # initialised in on_session_start when engine is active
        )
        self._pending_failures: list[dict] = []

        # Lazy-imported to avoid circular deps at module load time.
        from .detector import CorrectionDetector

        self._detector = CorrectionDetector()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_session_start(self, components: dict) -> None:
        """Wire up EvolutionEngine; prefer the pre-built instance in components."""
        self._components = components
        # Reuse the EvolutionEngine already in components (built by
        # _build_components_async) so tests that inject _FakeEvolution work
        # without modification.
        self._engine = components.get("evolution")
        if self._engine is None:
            try:
                import agent as _agent_mod

                self._engine = _agent_mod.EvolutionEngine(
                    components["client"],
                    components["model"],
                    components["memory"],
                    api_format=components.get("api_format", "anthropic"),
                )
            except Exception as exc:
                _console().print(
                    f"[dim]Evolution plugin: engine init failed: {exc}[/dim]"
                )

        # Initialise RuleStore lazily here so that ~/.agent/rl/ is only created
        # when the plugin is actually active (not at plugin-discovery time).
        if self._rule_store is None:
            from .rules import RuleStore

            self._rule_store = RuleStore()

    async def on_turn_end(self, event: Any) -> None:  # event: TurnEvent
        """Detect corrections; record rule applications; trigger extraction."""
        signal = self._detector.detect(event.user_input, self._prev_response)

        # P1-2: record every turn as an application for all active rules so
        # the promotion/retirement lifecycle actually runs.
        if self._rule_store is not None:
            active_rule_ids = self._rule_store.get_active_rule_ids()
            for rule_id in active_rule_ids:
                self._rule_store.record_application(
                    rule_id, was_corrected=signal.is_correction
                )

        if signal.is_correction:
            failure = {
                "id": _new_id(),
                "timestamp": _now(),
                "type": "user_correction",
                "confidence": signal.confidence,
                "context_summary": self._prev_response[:200],
                "user_correction": event.user_input[:200],
            }
            self._pending_failures.append(failure)
            try:
                with open(_FAILURES_FILE, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(failure, ensure_ascii=False) + "\n")
            except Exception as exc:
                _console().print(
                    f"[dim]Evolution plugin: failure log write error: {exc}[/dim]"
                )
            # Trigger rule extraction once we've accumulated enough failures.
            if len(self._pending_failures) >= _RULE_EXTRACTION_THRESHOLD:
                await self._try_extract_rule()

        # Remember the agent's response for next turn's comparison.
        self._prev_response = event.agent_response

    async def on_session_end(self, event: Any) -> None:  # event: SessionEvent
        """Score the session; display result; persist to sessions.jsonl."""
        if self._engine is None:
            return
        try:
            import agent as _agent_mod

            # Derive prompt version from filesystem (backward compat).
            prompts_dir = _agent_mod.PROMPTS_DIR
            prompt_files = sorted(prompts_dir.glob("system_v*.md"))
            prompt_version = prompt_files[-1].stem if prompt_files else "default"

            _agent_mod.CONSOLE.print("\n[dim]Scoring session...[/dim]")
            score_result = await self._engine.score_session(
                event.messages, prompt_version, event.tools_used
            )
            score = score_result.get("score", "?")
            critique = score_result.get("critique", "")
            _agent_mod.CONSOLE.print(
                f"[dim]Session score: {score}/10 — {critique[:100]}[/dim]"
            )
        except Exception as exc:
            _console().print(
                f"[dim]Evolution plugin: session scoring error: {exc}[/dim]"
            )

    # ── Prompt composition ────────────────────────────────────────────────────

    def compose_system_prompt(self, current_prompt: str) -> str:
        """Return a **suffix** to append to the system prompt.

        *current_prompt* is provided for context only — the return value
        is appended by PluginCatalog, NOT used as a replacement.
        Returns ``""`` when there are no active rules.
        """
        rules = self._rule_store.get_active_rules()
        if not rules:
            return ""
        lines = ["## Learned Behavioral Rules"]
        lines += [f"- {r}" for r in rules]
        return "\n".join(lines)

    # ── Slash commands ────────────────────────────────────────────────────────

    def register_slash_commands(self) -> dict:
        return {
            "evolve": self._handle_evolve,
            "generate-tool": self._handle_generate_tool,
            "stats": self._handle_stats,
        }

    async def _handle_evolve(self, raw_cmd: str, components: dict) -> None:
        if self._engine is None:
            _console().print("[yellow]Evolution engine not available.[/yellow]")
            return
        _console().print("[yellow]Running evolution engine...[/yellow]")
        new_prompt = await self._engine.rewrite_system_prompt()
        components["base_system_prompt"] = new_prompt
        import agent as _agent_mod

        components["system_prompt"] = _agent_mod._compose_system_prompt(
            new_prompt,
            components["registry"],
            _agent_mod.Path.cwd().resolve(),
            components["output_dir"],
            skill_catalog=components["skill_catalog"],
            plugin_catalog=components["plugin_catalog"],
        )
        ctx = components.get("ctx")
        if ctx is not None:
            ctx.system_prompt = components["system_prompt"]
        _console().print("[green]System prompt updated.[/green]")

    async def _handle_generate_tool(self, raw_cmd: str, components: dict) -> None:
        if self._engine is None:
            _console().print("[yellow]Evolution engine not available.[/yellow]")
            return
        parts = raw_cmd.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            _console().print("[yellow]Usage: /generate-tool <description>[/yellow]")
            return
        description = parts[1].strip()
        _console().print("[dim]Generating tool...[/dim]")
        await self._engine.generate_tool(description, components["registry"])
        user_tool_catalog = components.get("user_tool_catalog")
        if user_tool_catalog is not None:
            user_tool_catalog.load_into_registry(components["registry"])
        import agent as _agent_mod

        components["system_prompt"] = _agent_mod._compose_system_prompt(
            components["base_system_prompt"],
            components["registry"],
            _agent_mod.Path.cwd().resolve(),
            components["output_dir"],
            skill_catalog=components["skill_catalog"],
            plugin_catalog=components["plugin_catalog"],
        )
        ctx = components.get("ctx")
        if ctx is not None:
            ctx.system_prompt = components["system_prompt"]

    async def _handle_stats(self, raw_cmd: str, components: dict) -> None:
        import agent as _agent_mod

        table = _agent_mod.Table(title="Evolution Statistics")
        table.add_column("Metric")
        table.add_column("Value")
        if self._engine is not None:
            rl_stats = self._engine.get_stats()
            for k, v in rl_stats.items():
                table.add_row(k, str(v))
        if self._rule_store is not None:
            rule_stats = self._rule_store.get_stats()
            table.add_row("rules_total", str(rule_stats["total"]))
            for status, count in rule_stats.get("by_status", {}).items():
                table.add_row(f"rules_{status}", str(count))
        _console().print(table)

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _try_extract_rule(self) -> None:
        """Ask the LLM to extract a single behavioral rule from recent failures."""
        if self._engine is None:
            return
        failures_text = "\n".join(
            f'- User corrected: "{f["user_correction"]}" '
            f'(agent had said: "{f["context_summary"]}")'
            for f in self._pending_failures[-_RULE_EXTRACTION_THRESHOLD:]
        )
        prompt = (
            "The AI assistant received these user corrections in a recent session:\n"
            f"{failures_text}\n\n"
            "Identify the single most actionable behavioral rule that would prevent "
            "these corrections in future sessions. Respond with ONLY the rule text "
            "(one sentence, imperative form, under 120 characters). "
            "Example: 'Always show a diff before modifying existing files.'"
        )
        try:
            # P2-4: use public API instead of private _generate_text.
            rule_text = await self._engine.generate_text(prompt, max_tokens=100)
            rule_text = rule_text.strip().strip('"').strip("'")
            if rule_text:
                self._rule_store.add_rule(
                    rule_text,
                    source_failures=[f["id"] for f in self._pending_failures],
                )
                _console().print(
                    f"[dim]New behavioral rule learned: {rule_text[:80]}[/dim]"
                )
            # P1-4: only clear on success — failures are preserved for retry.
            self._pending_failures.clear()
        except Exception as exc:
            _console().print(
                f"[dim]Evolution plugin: rule extraction failed: {exc}[/dim]"
            )
            # Keep self._pending_failures for the next attempt.


def _console() -> Any:
    """Lazy access to agent.CONSOLE to avoid import-time side effects."""
    import agent as _agent_mod

    return _agent_mod.CONSOLE


def register() -> EvolutionPlugin:
    """Entry point called by PluginCatalog.discover_and_load()."""
    return EvolutionPlugin()
