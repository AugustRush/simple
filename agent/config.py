from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
from typing import Any, Optional

import anthropic
from rich.panel import Panel
from rich.prompt import Prompt

import agent as agent_module
from agent import shared

DEFAULT_SYSTEM_PROMPT = agent_module.DEFAULT_SYSTEM_PROMPT

# ── Default config.json template ─────────────────────────────────────────────
DEFAULT_CONFIG: dict = {
    # ── Active provider ───────────────────────────────────────────────────
    "active_provider": "anthropic",
    # ── Provider definitions ──────────────────────────────────────────────
    # api_format: "anthropic" | "openai"
    # models: optional list for /model command; falls back to [default_model]
    "providers": {
        "anthropic": {
            "api_format": "anthropic",
            "supports_vision": True,
            "api_key": "$ANTHROPIC_API_KEY",
            "default_model": "claude-opus-4-5",
            "models": ["claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-3-5"],
            "max_tokens": 8192,
        },
        "openai": {
            "api_format": "openai",
            "supports_vision": True,
            "api_key": "$OPENAI_API_KEY",
            "default_model": "gpt-4o",
            "models": ["gpt-4o", "gpt-4o-mini", "o1-preview"],
            "max_tokens": 4096,
        },
        "deepseek": {
            "api_format": "openai",
            "supports_vision": False,
            "api_key": "$DEEPSEEK_API_KEY",
            "base_url": "https://api.deepseek.com/v1",
            "default_model": "deepseek-chat",
            "models": ["deepseek-chat", "deepseek-reasoner"],
            "max_tokens": 8192,
        },
        "ollama": {
            "api_format": "openai",
            "supports_vision": False,
            "api_key": "ollama",
            "base_url": "http://localhost:11434/v1",
            "default_model": "qwen2.5:14b",
            "models": ["qwen2.5:14b", "qwen2.5:7b", "llama3.2"],
            "max_tokens": 4096,
        },
    },
    # ── Memory settings ───────────────────────────────────────────────────
    "memory": {
        "tidy_interval_seconds": shared.MEMORY_TIDY_INTERVAL,
        "tidy_file_threshold": shared.MEMORY_TIDY_FILE_THRESHOLD,
    },
    # ── Multi-agent orchestration ─────────────────────────────────────────
    "orchestration": {
        "max_parallel_agents": shared.DEFAULT_MAX_PARALLEL_AGENTS,
        "sub_agent_timeout_seconds": shared.DEFAULT_SUB_AGENT_TIMEOUT_SECONDS,
        "turn_hook_timeout_seconds": shared.DEFAULT_TURN_HOOK_TIMEOUT_SECONDS,
    },
    # ── MCP servers ───────────────────────────────────────────────────────
    "mcp_servers": [],
    # ── Evolution / self-improvement ──────────────────────────────────────
    "evolution": {
        "enabled": True,  # set to false to disable session scoring and rule learning
    },
    "scheduler": {
        "poll_seconds": 30,
        "lease_seconds": 300,
        "max_concurrent_runs": 3,
    },
    "audio": {
        "transcription_command": "$SIMPLE_AUDIO_TRANSCRIBE_COMMAND",
    },
    # ── Context manager ──────────────────────────────────────────────────
    "context": {
        "storage": {
            "max_categories": 15,
            "decay_factor": 0.95,
        },
        "consolidation": {
            "token_ratio": 0.70,
            "keep_last_messages": 6,
            "idle_seconds": 300,
            "min_messages": 4,
            "token_estimation": {
                "chars_per_token": 4,
                "cjk_chars_per_token": 1,
            },
        },
    },
    # ── System prompt ─────────────────────────────────────────────────────
    "system_prompt_file": None,  # null = use built-in prompt
    # ── Output directory ──────────────────────────────────────────────────
    "output_dir": None,  # null = ~/.agent/output
}


def _ensure_config_file() -> bool:
    """Write default config.json if it doesn't exist yet.

    Returns True if this is the first run (file was just created).
    """
    shared.AGENT_HOME.mkdir(parents=True, exist_ok=True)
    if not shared.CONFIG_FILE.exists():
        shared._atomic_write_text(
            shared.CONFIG_FILE,
            json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False),
        )
        return True  # first run
    return False


class ModelClientFactory:
    """Build the right async API client from provider config."""

    @staticmethod
    def from_config(cfg: dict, announce: bool = True) -> tuple[Any, str, int]:
        """
        Returns (client, active_model, max_tokens).

        client is either:
          - anthropic.AsyncAnthropic        (api_format == "anthropic")
          - openai.AsyncOpenAI              (api_format == "openai")
        """
        providers = cfg.get("providers", {})
        active_name = cfg.get("active_provider", "anthropic")
        provider_cfg = providers.get(active_name, {})

        # Validate provider exists
        if not provider_cfg:
            available = ", ".join(providers.keys()) or "(none)"
            raise RuntimeError(
                f"Provider '{active_name}' not found in config.json. "
                f"Available providers: {available}. "
                    f"Run: python -m agent config models"
            )

        api_format = provider_cfg.get("api_format", "openai")
        raw_key = provider_cfg.get("api_key", "")
        base_url = provider_cfg.get("base_url", None)
        model = cfg.get("model") or provider_cfg.get("default_model", shared.DEFAULT_MODEL)
        max_tokens = cfg.get("max_tokens") or provider_cfg.get(
            "max_tokens", shared.DEFAULT_MAX_TOKENS
        )

        # Resolve api key:
        #   "$ENV_VAR" → read from environment (optional fallback)
        #   anything else → use as literal value (including empty string for no-auth)
        if raw_key.startswith("$"):
            env_name = raw_key[1:]
            api_key = os.environ.get(env_name, "")
            if not api_key:
                raise RuntimeError(
                    f"API key env var '{env_name}' not set "
                    f"(provider: {active_name}). "
                    f"Run: export {env_name}=..."
                )
        else:
            api_key = raw_key

        if api_format == "anthropic":
            kwargs: dict = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            client = anthropic.AsyncAnthropic(**kwargs)
        elif api_format == "openai":
            try:
                import openai as openai_lib
            except ImportError:
                raise RuntimeError(
                    "openai package not installed. Run: pip install openai"
                )
            kwargs = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            client = openai_lib.AsyncOpenAI(**kwargs)
        else:
            raise RuntimeError(
                f"Unknown api_format '{api_format}' for provider '{active_name}'"
            )

        if announce:
            shared.CONSOLE.print(
                f"[dim]Provider: {active_name} | format: {api_format} | model: {model}[/dim]"
            )
        return client, model, int(max_tokens)

    @staticmethod
    def list_providers(cfg: dict) -> list[dict]:
        providers = cfg.get("providers", {})
        active = cfg.get("active_provider", "anthropic")
        result = []
        for name, p in providers.items():
            result.append(
                {
                    "name": name,
                    "format": p.get("api_format", "?"),
                    "model": p.get("default_model", "?"),
                    "base_url": p.get("base_url", "(default)"),
                    "active": name == active,
                }
            )
        return result


def load_config() -> tuple[dict, bool]:
    """Load config from disk, creating it on first run.

    Returns (cfg, is_first_run).

    Merge strategy:
    - User file is the source of truth for active_provider / model / providers.
    - DEFAULT_CONFIG only fills in completely missing structural sub-sections
      (memory, orchestration, evolution) so the agent always has safe defaults.
    """
    first_run = _ensure_config_file()
    try:
        raw = json.loads(shared.CONFIG_FILE.read_text())
        # Only backfill structural sections the user hasn't touched;
        # never overwrite top-level identity keys.
        for section in (
            "memory",
            "orchestration",
            "evolution",
            "scheduler",
            "audio",
            "mcp_servers",
            "context",
        ):
            if section not in raw and section in DEFAULT_CONFIG:
                raw[section] = DEFAULT_CONFIG[section]
        return raw, first_run
    except Exception as e:
        shared.CONSOLE.print(f"[yellow]Config parse error: {e} — using defaults[/yellow]")
        return dict(DEFAULT_CONFIG), first_run


def save_config(cfg: dict):
    shared.AGENT_HOME.mkdir(parents=True, exist_ok=True)
    shared._atomic_write_text(shared.CONFIG_FILE, json.dumps(cfg, indent=2, ensure_ascii=False))


def provider_supports_vision(cfg: dict, provider_name: str) -> bool:
    providers = cfg.get("providers", {})
    provider_cfg = providers.get(provider_name, {})
    if "supports_vision" in provider_cfg:
        return bool(provider_cfg.get("supports_vision"))
    default_cfg = DEFAULT_CONFIG.get("providers", {}).get(provider_name, {})
    return bool(default_cfg.get("supports_vision", False))


def _first_run_setup() -> bool:
    """Interactive first-run setup wizard.
    Guides user to choose a provider, set API key / base_url, and save config.
    Returns True if setup completed and agent should start.
    """
    from rich.prompt import Confirm

    shared.CONSOLE.print(
        Panel(
            f"[bold cyan]Welcome to Personal Agent![/bold cyan]\n\n"
            f"Config file created at:\n"
            f"  [bold]{shared.CONFIG_FILE}[/bold]\n\n"
            f"Let's set up your AI provider. You can change this anytime:\n"
            f"  [dim]python -m agent config use-provider <name>[/dim]\n"
            f"  [dim]python -m agent config edit[/dim]",
            title="[bold green]First Run Setup[/bold green]",
            border_style="green",
        )
    )

    # ── Step 1: choose provider ───────────────────────────────────────────────
    provider_menu = {
        "1": ("anthropic", "anthropic", "ANTHROPIC_API_KEY", None),
        "2": ("openai", "openai", "OPENAI_API_KEY", None),
        "3": ("deepseek", "openai", "DEEPSEEK_API_KEY", "https://api.deepseek.com/v1"),
        "4": ("ollama", "openai", None, "http://localhost:11434/v1"),
        "5": ("other", "openai", None, None),
    }

    shared.CONSOLE.print("\n[bold]Select provider:[/bold]")
    shared.CONSOLE.print("  1. Anthropic Claude  (native SDK)")
    shared.CONSOLE.print("  2. OpenAI            (openai SDK)")
    shared.CONSOLE.print("  3. DeepSeek          (OpenAI-compatible)")
    shared.CONSOLE.print("  4. Ollama            (local, no key needed)")
    shared.CONSOLE.print("  5. Other             (custom OpenAI-compatible endpoint)")

    choice = ""
    while choice not in provider_menu:
        choice = Prompt.ask("\nChoice", default="1").strip()

    provider_name, api_format, env_key, default_url = provider_menu[choice]

    if provider_name == "other":
        provider_name = (
            Prompt.ask("Provider name (e.g. siliconflow, together)").strip() or "custom"
        )

    shared.CONSOLE.print(
        f"\n[dim]Provider: [bold]{provider_name}[/bold] | format: {api_format}[/dim]"
    )

    # ── Step 2: base_url (for OpenAI-compat providers) ────────────────────────
    base_url = default_url
    if api_format == "openai":
        if default_url:
            entered = Prompt.ask("API base URL", default=default_url).strip()
        else:
            entered = Prompt.ask(
                "API base URL (e.g. https://api.siliconflow.cn/v1)"
            ).strip()
        base_url = entered or default_url

    # ── Step 3: API key ───────────────────────────────────────────────────────
    if provider_name == "ollama":
        api_key_val = "ollama"
        shared.CONSOLE.print("[dim]Ollama: no API key needed.[/dim]")
    else:
        existing_key = os.environ.get(env_key, "") if env_key else ""
        if existing_key:
            shared.CONSOLE.print(f"[green]Found {env_key} in environment. ✓[/green]")
            api_key_val = f"${env_key}" if env_key else existing_key
        else:
            shared.CONSOLE.print(
                f"\n[yellow]API key not found in env '{env_key or '?'}'.[/yellow]"
            )
            shared.CONSOLE.print("Options:")
            shared.CONSOLE.print("  a) Enter key now  (stored in config.json — less secure)")
            env_hint = (
                f"export {env_key}=<key>" if env_key else "set your API key env var"
            )
            shared.CONSOLE.print(f"  b) Leave blank    (add '{env_hint}' later and restart)")

            raw = Prompt.ask(
                "API key (enter to skip)", default="", password=True
            ).strip()
            if raw:
                api_key_val = raw
            else:
                api_key_val = f"${env_key}" if env_key else "$API_KEY"
                shared.CONSOLE.print(f"[dim]Stored as reference: {api_key_val}[/dim]")

    # ── Step 4: default model ─────────────────────────────────────────────────
    model_defaults = {
        "anthropic": "claude-opus-4-5",
        "openai": "gpt-4o",
        "deepseek": "deepseek-chat",
        "ollama": "qwen2.5:14b",
    }
    default_model = model_defaults.get(provider_name, "gpt-4o")
    model = Prompt.ask("Default model", default=default_model).strip() or default_model

    # ── Write config ──────────────────────────────────────────────────────────
    cfg, _ = load_config()
    cfg["active_provider"] = provider_name
    cfg["model"] = model

    p = cfg.setdefault("providers", {}).setdefault(provider_name, {})
    p["api_format"] = api_format
    p["supports_vision"] = provider_supports_vision(cfg, provider_name)
    p["api_key"] = api_key_val
    p["default_model"] = model
    if base_url:
        p["base_url"] = base_url

    save_config(cfg)

    shared.CONSOLE.print(
        Panel(
            f"[green]Config saved.[/green]\n\n"
            f"  Provider : [bold]{provider_name}[/bold] ({api_format})\n"
            + (f"  Base URL : {base_url}\n" if base_url else "")
            + f"  Model    : {model}\n\n"
            f"[dim]Edit anytime: python -m agent config edit[/dim]",
            border_style="green",
        )
    )

    return Confirm.ask("Start agent now?", default=True)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _datestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _resolve_output_dir(cfg: dict) -> Path:
    """Resolve output directory from config, creating it if needed."""
    raw = cfg.get("output_dir")
    if raw:
        p = Path(os.path.expandvars(str(raw))).expanduser().resolve()
    else:
        p = shared.DEFAULT_OUTPUT_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_system_prompt(cfg: dict) -> str:
    best = shared.PROMPTS_DIR / "best.md"
    if best.exists():
        content = best.read_text()
        content = re.sub(r"^<!--.*?-->\n", "", content, flags=re.DOTALL)
        return content
    prompt_file = cfg.get("system_prompt_file")
    if prompt_file:
        p = Path(prompt_file)
        if p.exists():
            return p.read_text()
        shared.CONSOLE.print(
            f"[yellow]system_prompt_file '{prompt_file}' not found — using default[/yellow]"
        )
    return DEFAULT_SYSTEM_PROMPT


def _compose_system_prompt(
    base_prompt: str,
    registry: ToolRegistry,
    workspace_root: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    skill_catalog: Optional[SkillCatalog] = None,
    plugin_catalog: Optional[PluginCatalog] = None,
) -> str:
    groups: dict[str, list[tuple[str, str]]] = {
        "builtin": [],
        "mcp": [],
        "runtime": [],
    }
    for name, tool in sorted(registry._tools.items()):
        source = tool.source
        if source == "builtin":
            groups["builtin"].append((name, tool.description))
        elif source.startswith("mcp:"):
            groups["mcp"].append((name, tool.description))
        else:
            groups["runtime"].append((name, tool.description))

    def _format_group(items: list[tuple[str, str]]) -> str:
        return "; ".join(f"{name}: {description}" for name, description in items)

    lines = [
        "## Active Capabilities",
        "Use only tools that are actually listed for this agent instance.",
        "When the user asks what you can do, what tools you have, or what capabilities are available, explicitly summarize the active tools below by name and purpose. Mention MCP tools when present.",
    ]
    if groups["builtin"]:
        lines.append("Built-in tools: " + _format_group(groups["builtin"]))
    if groups["mcp"]:
        lines.append("Connected MCP tools: " + _format_group(groups["mcp"]))
    if groups["runtime"]:
        lines.append("Runtime tools: " + _format_group(groups["runtime"]))
    if skill_catalog:
        lines.extend(skill_catalog.summary_lines())
    if workspace_root:
        builtin_names = {n for n, _ in groups["builtin"]}
        if any(n in builtin_names for n in ("read_file", "write_file", "list_files")):
            lines.append(
                f"Workspace root for read_file/write_file/list_files only: {workspace_root}"
            )
        if "schedule_create" in builtin_names:
            lines.append(
                "If the user asks for a reminder, delayed follow-up, or recurring future message, "
                "use the schedule tools instead of saying you cannot act in the future."
            )
            lines.append(
                "Use `action_type=message` for literal future messages, "
                "`action_type=agent_task` for future work to execute later, and "
                "`action_type=system_job` for internal maintenance. "
                "Do not pretend the scheduled action has already run."
            )
        if "shell" in builtin_names:
            lines.append(
                "For shell commands that generate files, direct outputs into the configured output directory "
                "using command-specific output flags or the shell tool's `cwd` parameter."
            )
        if "send_file" in builtin_names:
            lines.append(
                "If the user asks to receive a file in the current channel, use `send_file` with the resolved file path "
                "instead of claiming file delivery is unsupported."
            )
    lines.append(
        "Agent-managed paths are separate from the workspace root: "
        f"user tools live in {shared.TOOLS_DIR}, "
        f"user skills live in {shared.SKILLS_DIR}."
    )
    if output_dir:
        lines.append(
            f"Output directory for generated files (screenshots, exports, temp): {output_dir}"
        )
    lines.append(
        f"Current UTC time: {_now()}. "
        "Use the current_time tool when the user asks about local time or timezone conversions."
    )
    lines.append(
        "You are a personal AI agent with long-term memory, scheduled task support, and "
        "multi-channel delivery. When appropriate, proactively suggest setting reminders, "
        "scheduling daily summaries, or creating recurring check-ins. "
        "After completing significant tasks or generating files, confirm the outcome."
    )
    composed = base_prompt.rstrip() + "\n\n" + "\n".join(lines)
    if plugin_catalog:
        composed = plugin_catalog.compose_all_prompts(composed)
    return composed


async def _close_components(components: dict) -> None:
    mcp_client = components.get("mcp_client")
    if mcp_client is not None:
        await mcp_client.close()
    ctx_mgr = components.get("context_manager")
    if ctx_mgr is not None and hasattr(ctx_mgr, "store"):
        ctx_mgr.store.close()
