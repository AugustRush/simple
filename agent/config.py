from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
from typing import TYPE_CHECKING, Any, Optional

import anthropic
from rich.panel import Panel
from rich.prompt import Prompt

import agent as agent_module
from agent import shared

if TYPE_CHECKING:
    from agent.plugins.catalog import PluginCatalog
    from agent.skills.catalog import SkillCatalog
    from agent.tools.runtime import ToolRegistry

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
        "session_end_flush_timeout_seconds": (
            shared.DEFAULT_SESSION_END_FLUSH_TIMEOUT_SECONDS
        ),
    },
    # ── Multi-agent orchestration ─────────────────────────────────────────
    "orchestration": {
        "max_parallel_agents": shared.DEFAULT_MAX_PARALLEL_AGENTS,
        # Total sub-agents one turn may run, independent of concurrency.
        # 0 derives it from max_parallel_agents.
        "max_agents_per_turn": 0,
        # Total wall-clock budget for one sub-agent, retries included.
        "sub_agent_timeout_seconds": shared.DEFAULT_SUB_AGENT_TIMEOUT_SECONDS,
        "turn_hook_timeout_seconds": shared.DEFAULT_TURN_HOOK_TIMEOUT_SECONDS,
    },
    # ── Runtime guardrails ────────────────────────────────────────────────
    # Maximum steps (model request + the tools it calls) in one turn before
    # treating it as a loop.  "max_steps" is the preferred spelling;
    # "max_tool_call_iterations" remains accepted for existing configs.
    "max_steps": shared.MAX_TOOL_CALL_ITERATIONS,
    # Bounded follow-up calls used to finish a response stopped by the output cap.
    "max_truncation_continuations": shared.DEFAULT_MAX_TRUNCATION_CONTINUATIONS,
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
        # How far a signal may travel from what started it before delivery is
        # refused.  The task graph that signals can form is never declared, so
        # it cannot be checked for cycles when it is built -- this is the bound
        # that stands in for that check.
        "signal_max_depth": 10,
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
            "staging_turn_threshold": shared.STAGING_TURN_THRESHOLD,
            "staging_token_threshold": shared.STAGING_TOKEN_THRESHOLD,
            "max_source_tokens": shared.CONSOLIDATION_MAX_SOURCE_TOKENS,
            "max_chunks_per_run": 2,
            "output_tokens": 512,
            "model": None,
            "flush_on_session_end": False,
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
    # ── File access policy (startup-only, immutable) ─────────────────────
    "file_access": {
        "workspace": {
            "read": True,
            "write": False,
        },
        "max_read_lines": 400,
        "max_read_bytes": 65536,
        "max_snapshot_bytes": 16777216,
        "max_write_bytes": 4194304,
        "max_replacements": 100,
        "max_list_results": 1000,
    },
    # ── Shell permission level (default for every session) ───────────────
    "permissions": {
        "shell_level": "ask",
        "shell_sandbox": "read_all",
        "shell_devices": True,
        # Extra home-relative paths the sandboxed shell may neither read nor
        # write, on top of the built-in secret set.  Add ".ssh", ".docker" or
        # ".kube" here when this instance does not need git-over-ssh, docker
        # or kubectl — they hold credentials but are excluded by default
        # because denying them breaks those tools outright.
        "shell_secret_paths": [],
    },
}


def _ensure_config_file() -> bool:
    """Write the default config file if no config exists yet.

    Named sessions share the default agent config unless they already have
    their own ``config.json``.  Returns True when a new file was created.
    """
    shared.AGENT_HOME.mkdir(parents=True, exist_ok=True)
    config_path = shared.resolve_config_file()
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        shared._atomic_write_text(
            config_path,
            json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False),
        )
        return True  # first run
    return False


@dataclass(frozen=True)
class ProviderField:
    """One key a provider entry may carry, described once for every consumer.

    The three consumers are config validation, the settings page's form, and
    the CLI wizard, and they must not each keep their own copy of the
    vocabulary: a key added to one and forgotten in another is a setting the
    user can write but not see, or see but not write.  `thinking_efforts` set
    the precedent for publishing this backend-side ("the page renders what
    this backend accepts instead of keeping its own copy of the set"); this
    widens it from a list of words to the fields themselves.

    ``kind`` is what a renderer switches on:

    ``string``      free text
    ``secret``      free text that is never returned in the clear
    ``int``         a number
    ``bool``        a switch
    ``choice``      one of ``choices``
    ``string_list`` a list of strings (models)
    ``string_map``  name → value pairs (thinking, headers)
    """

    key: str
    kind: str
    label: str
    help: str
    default: Any = None
    required: bool = False
    choices: tuple[str, ...] = ()
    #: For ``string_map``: whether values under secret-looking names are hidden
    #: when the config is handed to a client.  True for headers, because an
    #: Authorization value is a credential that happens to be spelled as a
    #: header -- and the settings page round-trips the whole provider block.
    secret_values: bool = False


#: Every key a provider entry may carry, in the order the form should show
#: them.  ``test_provider_fields_are_the_keys_the_code_reads`` holds this
#: against what the loader actually reads, so the table cannot drift into
#: describing a config that no longer exists.
PROVIDER_FIELDS: tuple[ProviderField, ...] = (
    ProviderField(
        "api_format",
        "choice",
        "接口格式",
        "这个分组说哪种协议。OpenAI 兼容的网关（含本地 vLLM/Ollama）选 openai。",
        default="openai",
        required=True,
        choices=("openai", "anthropic"),
    ),
    ProviderField(
        "api_key",
        "secret",
        "API Key",
        "密钥本身，或 $ENV_VAR 形式从环境变量读。",
        required=True,
    ),
    ProviderField(
        "base_url",
        "string",
        "Base URL",
        "接口地址。留空用官方默认；OpenAI 兼容网关通常以 /v1 结尾。",
    ),
    ProviderField(
        "default_model",
        "string",
        "默认模型",
        "这个分组的默认模型 id，也是路由表认领的 id。",
        required=True,
    ),
    ProviderField(
        "models",
        "string_list",
        "可选模型",
        "下拉里列出的模型 id。default_model 即使不在这里也照样可用。",
    ),
    ProviderField(
        "max_tokens",
        "int",
        "输出上限",
        "一次回答最多多长。写在这里就只作用于这个分组。",
    ),
    ProviderField(
        "context_window",
        "int",
        "上下文窗口",
        "这个分组的模型能吃多少 token，压缩按它判断。",
    ),
    ProviderField(
        "output_reserve",
        "int",
        "输出预留",
        "输入预算要为回答留出的空间。不是输出上限：上限是「最多多长」，预留是"
        "「窗口要空多少出来」。",
    ),
    ProviderField(
        "supports_vision",
        "bool",
        "支持图片",
        "为真时图片直接发给模型，否则转成文字描述。",
        default=False,
    ),
    ProviderField(
        "stream_usage",
        "bool",
        "流式上报用量",
        "让流式请求带回 token 用量，用于记账与估算校准。拒绝 stream_options 的"
        "网关才需要关掉。",
        default=True,
    ),
    ProviderField(
        "thinking",
        "string_map",
        "思考强度",
        "effort 取 off/low/medium/high，或 models 子表按模型分别指定。",
    ),
    ProviderField(
        "headers",
        "string_map",
        "额外请求头",
        "随每个请求发出的头。值用 $ENV_VAR 读环境变量；{session} 会在每个请求上"
        "替换成当前会话 id（一个会话一个稳定值）。",
        secret_values=True,
    ),
)

def provider_fields_payload() -> list[dict[str, Any]]:
    """The provider vocabulary as JSON, for a client that renders a form.

    One shape, defined once: the settings page, and any other client, reads
    this instead of hard-coding field names.  ``kind`` is what a renderer
    switches on (see :class:`ProviderField`), and ``secret`` says a value must
    never be echoed back in the clear.
    """
    return [
        {
            "key": field.key,
            "kind": field.kind,
            "label": field.label,
            "help": field.help,
            "default": field.default,
            "required": bool(field.required),
            "choices": list(field.choices),
            "secret": field.kind == "secret",
            "secret_values": bool(field.secret_values),
        }
        for field in PROVIDER_FIELDS
    ]


#: Who owns the shape of a ``string_map`` field's contents.
#:
#: ``scalar``     the generic checker: a dict whose values are strings.
#: ``own_checker`` a dedicated validator instead -- ``thinking`` accepts a bare
#:                 effort word as shorthand and names the accepted values in
#:                 its own message, so a generic "must be a dict" on top of it
#:                 would be a second, worse answer to the same question.
PROVIDER_MAP_VALUE_KINDS: dict[str, str] = {
    "thinking": "own_checker",
    "headers": "scalar",
}


class ModelClientFactory:
    """Build the right async API client from provider config."""

    @staticmethod
    def active_model_and_tokens(cfg: dict) -> tuple[str, int]:
        """The (model, output budget) ``cfg`` names for its active provider.

        Split out of :meth:`from_config` because a caller that already has a
        client still needs these two numbers, and reading them anywhere else
        would be a second answer to "which model does this config name".  The
        web session runtime is exactly that caller: it is rebuilt from the
        config on disk while its SDK client comes from the process's shared
        provider cache, so it takes the client from the routing transport and
        the model from here.  Skipping this is how a config edit ends up
        applied to the routing table but not to the id the session sends.
        """
        providers = cfg.get("providers", {})
        active_name = cfg.get("active_provider", "anthropic")
        provider_cfg = providers.get(active_name, {}) or {}
        model = cfg.get("model") or provider_cfg.get(
            "default_model", shared.DEFAULT_MODEL
        )
        max_tokens = cfg.get("max_tokens") or provider_cfg.get(
            "max_tokens", shared.DEFAULT_MAX_TOKENS
        )
        return str(model), int(max_tokens)

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
        model, max_tokens = ModelClientFactory.active_model_and_tokens(cfg)

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


_KNOWN_SECTIONS = frozenset({
    "active_provider", "providers", "model", "max_tokens", "output_reserve",
    "memory", "orchestration", "evolution", "scheduler", "audio",
    "mcp_servers", "context", "plugins", "skills", "channels", "user_tools",
    "system_prompt_file", "output_dir", "tavily_api_key",
    "assistant_identity", "shell_blocked_commands",
    "shell_allowed_commands",
    "permissions",
    "llm_max_retries", "llm_retry_base_delay", "max_tool_call_iterations",
    "max_steps",
    "max_truncation_continuations", "file_access",
    "web_proxy",
})


def _check_web_proxy(cfg: dict, warnings: list[str]) -> None:
    # A proxy URL, or a word that turns proxying off.  Anything a bare string
    # cannot express is a typo, and the fallback is the previous behaviour
    # (read the environment) rather than a broken proxy URL.
    web_proxy = cfg.get("web_proxy")
    if web_proxy is not None:
        if not isinstance(web_proxy, str) or not web_proxy.strip():
            warnings.append("'web_proxy' must be a non-empty string (a proxy URL, or 'none' to disable)")


def _check_shell_allowed_commands(cfg: dict, warnings: list[str]) -> None:
    allowed_commands = cfg.get("shell_allowed_commands")
    if allowed_commands is not None:
        if not isinstance(allowed_commands, list) or not all(
            isinstance(item, str) and item.strip() for item in allowed_commands
        ):
            warnings.append(
                "'shell_allowed_commands' must be a list of non-empty command strings"
            )


def _check_permissions(cfg: dict, warnings: list[str]) -> None:
    permissions_cfg = cfg.get("permissions")
    if permissions_cfg is None:
        return
    if not isinstance(permissions_cfg, dict):
        warnings.append("'permissions' must be a dict")
        return
    from agent.security.shell import PERMISSION_LEVELS

    shell_level = permissions_cfg.get("shell_level", "ask")
    if shell_level not in PERMISSION_LEVELS:
        warnings.append(
            f"'permissions.shell_level' must be one of "
            f"{', '.join(PERMISSION_LEVELS)}, got '{shell_level}'"
        )
    from agent.security.filesystem_sandbox import SANDBOX_MODES

    sandbox_mode = permissions_cfg.get("shell_sandbox", "read_all")
    if sandbox_mode not in SANDBOX_MODES:
        warnings.append(
            f"'permissions.shell_sandbox' must be one of "
            f"{', '.join(SANDBOX_MODES)}, got '{sandbox_mode}'"
        )
    elif sandbox_mode == "none" and shell_level != "full":
        warnings.append(
            "'permissions.shell_sandbox' 'none' requires "
            "'permissions.shell_level' 'full'; falling back to 'read_all'"
        )
    shell_devices = permissions_cfg.get("shell_devices", True)
    if not isinstance(shell_devices, bool):
        warnings.append("'permissions.shell_devices' must be a boolean")


def _check_unknown_keys(cfg: dict, warnings: list[str]) -> None:
    # Keys starting with '_' are documentation companions by the example
    # config's own convention (`_tavily_api_key_readme`), not settings.
    for key in cfg:
        if key.startswith("_"):
            continue
        if key not in _KNOWN_SECTIONS:
            warnings.append(f"Unknown config key '{key}' — ignored")


def _check_active_provider(cfg: dict, warnings: list[str]) -> None:
    active = cfg.get("active_provider", "")
    providers = cfg.get("providers", {})
    if not isinstance(providers, dict):
        warnings.append("'providers' must be a dict")
        # Deliberately keeps going with an empty mapping: a bad `providers`
        # reports both that it is not a dict *and* that `active_provider`
        # cannot be found in it.
        providers = {}
    if active and active not in providers:
        warnings.append(
            f"active_provider '{active}' not found in providers. "
            f"Available: {', '.join(providers.keys()) or '(none)'}"
        )


def _check_thinking(pname: str, pcfg: dict, warnings: list[str]) -> None:
    """Validate one provider's thinking-effort settings.

    Thinking effort is a word the provider has to recognise, so a typo must be
    reported rather than passed through: the level is sent to the API verbatim,
    and the API answers a wrong word with a 400 that names its own vocabulary —
    a confusing way to learn about a config typo.
    """
    thinking = pcfg.get("thinking")
    if thinking is None:
        return
    allowed = ", ".join(shared.THINKING_EFFORTS)
    if isinstance(thinking, str):
        effort = thinking
    elif isinstance(thinking, dict):
        effort = thinking.get("effort")
    else:
        effort = None
        warnings.append(
            f"providers.{pname}.thinking: must be a dict like "
            f'{{"effort": "off"}} or one of {allowed}'
        )
    if effort is not None and shared.normalize_thinking_effort(effort) is None:
        warnings.append(
            f"providers.{pname}.thinking.effort: must be one of "
            f"{allowed}, got '{effort}'"
        )
    # Per-model overrides get the same check, one warning per bad word,
    # because each names a model the person can act on.
    if not isinstance(thinking, dict):
        return
    per_model = thinking.get("models")
    if per_model is None:
        return
    if not isinstance(per_model, dict):
        warnings.append(
            f"providers.{pname}.thinking.models: must be a "
            f'mapping of model id to effort, like '
            f'{{"some-model": "high"}}'
        )
        return
    for model, model_effort in per_model.items():
        if shared.normalize_thinking_effort(model_effort) is None:
            warnings.append(
                f"providers.{pname}.thinking.models."
                f"{model}: must be one of {allowed}, "
                f"got '{model_effort}'"
            )


def _check_providers(cfg: dict, warnings: list[str]) -> None:
    providers = cfg.get("providers", {})
    if not isinstance(providers, dict):
        providers = {}
    known = {field.key for field in PROVIDER_FIELDS}
    for pname, pcfg in providers.items():
        # `_readme` companions sit beside the real providers in the example
        # config, the same convention the top-level unknown-key check honours.
        if pname.startswith("_"):
            continue
        if not isinstance(pcfg, dict):
            warnings.append(f"providers.{pname}: must be a dict, got {type(pcfg).__name__}")
            continue
        # A key the loader never reads is a setting the user believes in and
        # the agent ignores -- the same failure the top-level unknown-key check
        # exists for, one level down.  PROVIDER_FIELDS is the vocabulary, so a
        # key that is missing from it is either a typo here or a field nobody
        # declared.
        for key in pcfg:
            if key.startswith("_") or key in known:
                continue
            warnings.append(
                f"providers.{pname}.{key}: unknown provider key — ignored "
                f"(known: {', '.join(sorted(known))})"
            )
        fmt = pcfg.get("api_format", "")
        if fmt not in ("anthropic", "openai"):
            warnings.append(
                f"providers.{pname}.api_format: must be 'anthropic' or 'openai', got '{fmt}'"
            )
        if not isinstance(pcfg.get("api_key"), str):
            warnings.append(f"providers.{pname}.api_key: must be a string")
        if not isinstance(pcfg.get("default_model"), str) or not pcfg.get("default_model"):
            warnings.append(f"providers.{pname}.default_model: must be a non-empty string")
        _check_provider_types(pname, pcfg, warnings)
        _check_thinking(pname, pcfg, warnings)
        _check_provider_headers(pname, pcfg, warnings)


def _check_provider_types(pname: str, pcfg: dict, warnings: list[str]) -> None:
    """Type-check the provider fields whose kind is not free text.

    The kinds are read off :data:`PROVIDER_FIELDS`, so a field added to the
    table is checked from the moment it is declared -- which is the whole point
    of keeping one vocabulary.  Free text and secrets are left to the checks
    that know more about them (``api_key``, ``default_model``,
    ``_check_provider_headers``).
    """
    for field in PROVIDER_FIELDS:
        if field.key not in pcfg or field.kind in ("string", "secret"):
            continue
        value = pcfg[field.key]
        if field.kind == "choice":
            if isinstance(value, str) and value in field.choices:
                continue
            warnings.append(
                f"providers.{pname}.{field.key}: must be one of "
                f"{', '.join(field.choices)}, got {value!r}"
            )
        elif field.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                warnings.append(
                    f"providers.{pname}.{field.key}: must be an integer, got {value!r}"
                )
        elif field.kind == "bool":
            if not isinstance(value, bool):
                warnings.append(
                    f"providers.{pname}.{field.key}: must be true or false, got {value!r}"
                )
        elif field.kind == "string_list":
            if isinstance(value, str):
                continue
            if isinstance(value, list) and all(isinstance(i, str) for i in value):
                continue
            warnings.append(
                f"providers.{pname}.{field.key}: must be a list of strings, got {value!r}"
            )
        elif field.kind == "string_map":
            if PROVIDER_MAP_VALUE_KINDS.get(field.key) == "own_checker":
                continue
            if not isinstance(value, dict):
                warnings.append(
                    f"providers.{pname}.{field.key}: must be a dict, got {value!r}"
                )
                continue
            if PROVIDER_MAP_VALUE_KINDS.get(field.key) == "scalar":
                for name, item in value.items():
                    if isinstance(item, bool) or not isinstance(item, (str, int, float)):
                        warnings.append(
                            f"providers.{pname}.{field.key}.{name}: must be a string"
                        )


#: A header name is an HTTP token: printable ASCII minus separators.  Checked
#: because a name the SDK cannot send would otherwise sit in a config that
#: looks fine and quietly never arrive.
_HEADER_NAME_RE = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")


def _check_provider_headers(pname: str, pcfg: dict, warnings: list[str]) -> None:
    """Validate one provider's extra request headers.

    ``providers.<name>.headers`` reaches the wire verbatim, so the two things
    worth catching are a value that cannot be a header (a list, a nested dict)
    and a placeholder that will never be substituted -- the second matters
    because it *looks* like it works while sending a literal ``{session}`` to
    the gateway.
    """
    headers = pcfg.get("headers")
    if headers is None:
        return
    if not isinstance(headers, dict):
        warnings.append(
            f"providers.{pname}.headers: must be a dict of header name → value"
        )
        return
    for name, value in headers.items():
        if not _HEADER_NAME_RE.fullmatch(str(name).strip()):
            warnings.append(
                f"providers.{pname}.headers: '{name}' is not a valid header name"
            )
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            warnings.append(f"providers.{pname}.headers.{name}: must be a string")
            continue
        for token in re.findall(r"\{[^}]*\}", str(value)):
            if token != shared.SESSION_HEADER_PLACEHOLDER:
                warnings.append(
                    f"providers.{pname}.headers.{name}: unknown placeholder "
                    f"'{token}'; only {shared.SESSION_HEADER_PLACEHOLDER} is "
                    f"substituted (one value per conversation)"
                )


def _check_numeric_ranges(cfg: dict, warnings: list[str]) -> None:
    def _check_int(key: str, min_val: int, max_val: int) -> None:
        val = cfg.get(key)
        if val is not None:
            try:
                ival = int(val)
                if ival < min_val or ival > max_val:
                    warnings.append(f"'{key}': {ival} out of range [{min_val}, {max_val}]")
            except (TypeError, ValueError):
                warnings.append(f"'{key}': must be an integer, got {val!r}")

    def _check_float(key: str, min_val: float) -> None:
        val = cfg.get(key)
        if val is not None:
            try:
                fval = float(val)
                if fval < min_val:
                    warnings.append(f"'{key}': {fval} must be >= {min_val}")
            except (TypeError, ValueError):
                warnings.append(f"'{key}': must be a number, got {val!r}")

    _check_int("max_tokens", 1, 2_000_000)
    _check_int(
        "max_tool_call_iterations",
        1,
        shared.MAX_CONFIGURABLE_TOOL_CALL_ITERATIONS,
    )
    _check_int(
        "max_steps",
        1,
        shared.MAX_CONFIGURABLE_TOOL_CALL_ITERATIONS,
    )
    _check_int(
        "max_truncation_continuations",
        0,
        shared.MAX_CONFIGURABLE_TRUNCATION_CONTINUATIONS,
    )
    _check_int("llm_max_retries", 0, 20)
    _check_float("llm_retry_base_delay", 0.1)
    # Bounded by the largest window any provider here offers, because a reserve
    # bigger than the window would leave the input budget negative.
    _check_int("output_reserve", 1, 1_000_000)


def _check_scheduler(cfg: dict, warnings: list[str]) -> None:
    scheduler = cfg.get("scheduler", {})
    if not isinstance(scheduler, dict):
        return
    for skey, smin in (
        ("poll_seconds", 1),
        ("lease_seconds", 10),
        # Zero is allowed and means "the first hop only": a signal emitted
        # by a task is refused, but a signal a person or a clock raises
        # still runs its subscribers.  Anything below zero is a mistake.
        ("signal_max_depth", 0),
    ):
        sv = scheduler.get(skey)
        if sv is not None:
            try:
                if int(sv) < smin:
                    warnings.append(f"scheduler.{skey}: must be >= {smin}")
            except (TypeError, ValueError):
                warnings.append(f"scheduler.{skey}: must be an integer")


def _check_channels(cfg: dict, warnings: list[str]) -> None:
    channels = cfg.get("channels", {})
    if isinstance(channels, dict):
        feishu = channels.get("feishu", {})
        if isinstance(feishu, dict) and feishu.get("enabled"):
            if not feishu.get("app_id") or not feishu.get("app_secret"):
                warnings.append(
                    "channels.feishu is enabled but app_id or app_secret is missing"
                )


# The order here is the order warnings are reported in.  The sections are
# otherwise independent, so adding one is a one-line change.
_SECTION_CHECKS = (
    _check_web_proxy,
    _check_shell_allowed_commands,
    _check_permissions,
    _check_unknown_keys,
    _check_active_provider,
    _check_providers,
    _check_numeric_ranges,
    _check_scheduler,
    _check_channels,
)


def _validate_config(cfg: dict) -> list[str]:
    """Validate config.json structure and types. Returns list of warning messages.

    Does NOT abort — warnings are printed but the agent still starts with
    best-effort defaults so a typo doesn't brick the agent.
    """
    warnings: list[str] = []
    for check in _SECTION_CHECKS:
        check(cfg, warnings)
    return warnings


def load_config() -> tuple[dict, bool]:
    """Load config from disk, creating it on first run.

    Returns (cfg, is_first_run).

    Merge strategy:
    - User file is the source of truth for active_provider / model / providers.
    - DEFAULT_CONFIG only fills in completely missing structural sub-sections
      (memory, orchestration, evolution) so the agent always has safe defaults.
    """
    first_run = _ensure_config_file()
    config_path = shared.resolve_config_file()
    try:
        raw = json.loads(config_path.read_text())
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
            "file_access",
            "permissions",
        ):
            if section not in raw and section in DEFAULT_CONFIG:
                raw[section] = DEFAULT_CONFIG[section]
        # Validate and warn (never block startup)
        config_warnings = _validate_config(raw)
        for w in config_warnings:
            shared.CONSOLE.print(f"[yellow]Config: {w}[/yellow]")
        return raw, first_run
    except Exception as e:
        shared.CONSOLE.print(f"[yellow]Config parse error: {e} — using defaults[/yellow]")
        return dict(DEFAULT_CONFIG), first_run


def save_config(cfg: dict):
    config_path = shared.resolve_config_file()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    shared._atomic_write_text(config_path, json.dumps(cfg, indent=2, ensure_ascii=False))


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
            f"  [bold]{shared.resolve_config_file()}[/bold]\n\n"
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


def _load_system_prompt(cfg: dict, *, prompts_dir: Optional[Path] = None) -> str:
    best = (prompts_dir or shared.PROMPTS_DIR) / "best.md"
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


# Module-level cache for the static portion of the system prompt.
#
# The cache key used to be a hand-listed subset of the inputs, and it had
# already drifted: `supports_vision` (read from the registry context, which
# does not bump `_prompt_generation`) and `shared.TOOLS_DIR`/`SKILLS_DIR`
# (rewritten by `--name`) were both absent, so a cached block could outlive
# the values it was rendered from.  `filesystem_sandbox` hit the same bug
# and fixed it by keying on content.
#
# The fix here is structural rather than "add the missing fields": the
# renderer below takes a `_StaticPromptInputs` and may read nothing else, so
# the key cannot drift from the body without failing to compile.  Adding a
# new input means adding a field, which puts it in the key by construction.
_system_prompt_cache_key: "_StaticPromptInputs | None" = None
_system_prompt_cache_value: str = ""


@dataclass(frozen=True)
class _StaticPromptInputs:
    """Every value the cached static block is a function of.

    Hashable and compared by value, so it doubles as the cache key.
    """

    base_prompt: str
    #: (name, description, source) per tool — the registry's own
    #: `_prompt_generation` counter is not enough, because `set_context`
    #: mutates prompt-visible state without bumping it.
    tools: tuple[tuple[str, str, str], ...]
    skill_lines: tuple[str, ...]
    workspace_root: Optional[Path]
    output_dir: Optional[Path]
    supports_vision: bool
    tools_dir: str
    skills_dir: str
    default_output_dir: str


def _static_prompt_inputs(
    base_prompt: str,
    registry: "ToolRegistry",
    workspace_root: Optional[Path],
    output_dir: Optional[Path],
    skill_catalog: Optional["SkillCatalog"],
) -> _StaticPromptInputs:
    return _StaticPromptInputs(
        base_prompt=base_prompt,
        tools=tuple(
            (name, tool.description, tool.source)
            for name, tool in sorted(registry._tools.items())
        ),
        skill_lines=tuple(skill_catalog.summary_lines()) if skill_catalog else (),
        workspace_root=workspace_root,
        output_dir=output_dir,
        supports_vision=bool(registry.get_context("supports_vision")),
        tools_dir=str(registry.get_context("user_tools_dir") or shared.TOOLS_DIR),
        skills_dir=str(registry.get_context("user_skills_dir") or shared.SKILLS_DIR),
        default_output_dir=str(output_dir or shared.DEFAULT_OUTPUT_DIR),
    )


def _render_static_prompt(inputs: _StaticPromptInputs) -> str:
    """Render the cached block from *inputs* and nothing else.

    Reading any other state here silently reintroduces the drift this
    structure exists to prevent.
    """
    groups: dict[str, list[tuple[str, str]]] = {
        "builtin": [],
        "mcp": [],
        "runtime": [],
    }
    for name, description, source in inputs.tools:
        if source == "builtin":
            groups["builtin"].append((name, description))
        elif source.startswith("mcp:"):
            groups["mcp"].append((name, description))
        else:
            groups["runtime"].append((name, description))

    workspace_root = inputs.workspace_root
    output_dir = inputs.output_dir

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
    lines.extend(inputs.skill_lines)
    if workspace_root:
        builtin_names = {n for n, _ in groups["builtin"]}
        if any(
            n in builtin_names
            for n in ("read_file", "write_file", "list_files", "edit_file")
        ):
            lines.append(
                "File tools take an explicit `root` (`workspace` or `output_dir`) "
                "and a root-relative `path`; absolute paths and path traversal "
                "are rejected. `read_file` returns a bounded line window plus an "
                "exact `revision`; pass that revision back as `expected_revision` "
                "to `write_file` or `edit_file` before mutating, and reread after "
                "any conflict. The workspace is read-only by default and workspace "
                "writes additionally require the startup `file_access` policy plus "
                "an explicit `write_scope`; `output_dir` is always readable and "
                "writable for generated artifacts. The `file_access` configuration "
                "is loaded only at startup — changing it requires a restart."
            )
        if "context_retrieve" in builtin_names:
            lines.append(
                "Memory search is LEXICAL, not semantic: `context_retrieve` "
                "finds memories that share WORDS with the query, not ones that "
                "merely mean the same thing. When the context already in this "
                "turn does not answer the question, call it with SEVERAL "
                "phrasings in `queries` — including the other language, and the "
                "concrete nouns the memory itself would use — before telling "
                "the user you have no record. A memory written in English will "
                "not match a Chinese question, and one empty search proves "
                "nothing."
            )
        if builtin_names & {"schedule_create", "workflow_create"}:
            lines.append(
                "Creating is something the user asks you to do, not something "
                "you volunteer. A question about how some process works — "
                "「订单都是如何接的，具体流程是什么」 — is answered in this turn, "
                "in words; it is not a request to build a task, and answering "
                "it by creating one leaves behind a schedule nobody asked for "
                "that outlives the conversation. Build only when the user asks "
                "for something to exist and keep running: a cadence "
                "(「每天早上」), a reminder, or an explicit 「建一个 / 创建 / "
                "拆成」. If you think a task would help but were not asked for "
                "one, offer it in your reply and let the user say yes. "
                "The tools check this instead of taking your word for it: their "
                "`intent` argument must quote, verbatim, the words that asked "
                "for it, and a call whose intent cannot be found in this turn's "
                "request is refused. So write the user's own sentence there — "
                "a reason why the task would be useful reads as a paraphrase "
                "and does not pass."
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
            lines.append(
                "Give the task `criteria` saying what has to be true for a run "
                "of it to count as a success, and a `verify_command` whenever "
                "that is mechanically checkable — a test suite, a build, a "
                "script that checks the output. That is not documentation: the "
                "criterion is what decides whether the run is recorded as "
                "having done its job, and a task with no criterion is judged "
                "only on whether its result was delivered."
            )
        if "schedule_runs" in builtin_names:
            lines.append(
                "A scheduled task runs with nobody watching, so read back what "
                "it did: `schedule_runs` gives each run's outcome and, when it "
                "failed, the reason. Use it after creating a task or a "
                "workflow, and whenever the user says one of them failed or "
                "did not produce anything. When the reason is the criterion "
                "itself — a `verify_command` naming a file no step writes, a "
                "path that never exists — the defect is in the definition: fix "
                "the task. Retrying cannot change a check that was impossible "
                "the first time."
            )
        if "workflow_create" in builtin_names:
            lines.append(
                "When a request is really several jobs in an order — fetch, "
                "then transform, then report — build it with `workflow_create` "
                "rather than with several `schedule_create` calls. Hand-chaining "
                "separate tasks through signals produces the same runs with no "
                "recorded edges, and the edges are what tell a failure which "
                "steps below it must not run. Say which steps depend on which "
                "in `depends_on`, and give each step its own `criteria`."
            )
        if "report_outcome" in builtin_names:
            lines.append(
                "This turn is a scheduled run, not a conversation. If you "
                "cannot do what the run was for, call `report_outcome` with "
                "the reason instead of describing the problem in your reply: "
                "the reply is only a summary, and `report_outcome` is what "
                "records the run as failed and stops the steps below it. There "
                "is deliberately no way to report success — that is decided by "
                "the acceptance check, not by you, and claiming it would not "
                "make it so."
            )
        if "shell" in builtin_names:
            lines.append(
                "Every shell tool call must include an `intent` input that explains what that exact command "
                "will do and why it is necessary. Do not rely on surrounding prose as the shell intent."
            )
            lines.append(
                "The shell tool takes a `root` parameter (`output_dir` or "
                "`workspace`); the default root is the selected project "
                f"workspace ({workspace_root}). Relative `cwd` values resolve "
                "inside that root, so downloads and generated artifacts must "
                "explicitly use `root=output_dir`."
            )
            lines.append(
                "Project source changes, dependency commands, tests, and normal "
                "build commands belong in `root=workspace`; for downloads and "
                "user-facing generated artifacts use `root=output_dir` with "
                "relative targets such as `git clone <url> repo-name`. Keep "
                "follow-up commands in the same root and set `cwd` to the "
                "directory you created."
            )
            lines.append(
                "In the Web channel, a folder selected with the project-folder "
                "picker is an explicit read/write grant for this session."
            )
        if "send_file" in builtin_names:
            lines.append(
                "If the user asks to receive a file in the current channel, use `send_file` with the resolved file path "
                "instead of claiming file delivery is unsupported."
            )
            lines.append(
                "For generated images, send each final user-facing image exactly once. "
                "Do not send source grids, contact sheets, previews, or other intermediate images "
                "unless the user explicitly asks to inspect them or requests multiple variants."
            )
        if "set_identity" in builtin_names:
            lines.append(
                "When the user gives you a name, a role, or a persona — or "
                "changes one you already have — call `set_identity` in that "
                "same turn. Agreeing in prose does not persist anything: "
                "identity is read back from `set_identity` on every restart, "
                "and the newest call replaces the previous setting. Use "
                "`subject=user` to record what the user tells you about "
                "themselves."
            )
        if "create_tool" in builtin_names:
            lines.append(
                "When the user asks for a new capability or tool, use "
                "`create_tool` (or `update_tool`). Writing a .py file with "
                "`write_file` or `shell` does not create a tool — only "
                "`create_tool` validates it, checks that it imports, asks "
                "the user to approve it, and loads it into this session."
            )
            lines.append(
                "If a tool you are writing needs a third-party package, use "
                "`install_tool_dependency`. Never install packages for a "
                "tool with `pip`, `uv add`, or `poetry add` from the shell: "
                "those change the user's own project or Python environment, "
                "while `install_tool_dependency` keeps the package in the "
                "agent's private dependency directory."
            )
    lines.append(
        "Agent-managed paths are separate from the workspace root: "
        f"user tools live in {inputs.tools_dir}, "
        f"user skills live in {inputs.skills_dir}."
    )
    if output_dir:
        lines.append(
            f"Output directory for generated files (screenshots, exports, temp): {output_dir}"
        )
    if inputs.supports_vision:
        lines.append(
            "This agent supports vision. When the user sends images, you can see and "
            "analyze them directly — describe what you observe before taking action."
        )
    return inputs.base_prompt.rstrip() + "\n\n" + "\n".join(lines)


def _compose_system_prompt(
    base_prompt: str,
    registry: "ToolRegistry",
    workspace_root: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    skill_catalog: Optional["SkillCatalog"] = None,
    plugin_catalog: Optional["PluginCatalog"] = None,
) -> str:
    global _system_prompt_cache_key, _system_prompt_cache_value

    inputs = _static_prompt_inputs(
        base_prompt, registry, workspace_root, output_dir, skill_catalog
    )
    if inputs != _system_prompt_cache_key:
        _system_prompt_cache_value = _render_static_prompt(inputs)
        _system_prompt_cache_key = inputs

    # No time-dependent footer here on purpose.  This string is the head of
    # every provider request, so a value that changes minute to minute sits in
    # front of the entire conversation: the provider's prefix cache can only
    # reuse what precedes the first difference, so the clock caps every request
    # in the session at the few hundred tokens before it.  The current time now
    # rides in the turn's own message (see ``BaseAgent._prepare_turn``), which
    # is cache-neutral -- the message tail is unique per request anyway -- and
    # strictly more accurate, because it is the time of *this* turn rather than
    # of whichever turn last re-rendered the prompt.
    result = _system_prompt_cache_value
    result += "\n" + (
        "You are a personal AI agent with long-term memory, scheduled task support, and "
        "multi-channel delivery. When appropriate, proactively suggest setting reminders, "
        "scheduling daily summaries, or creating recurring check-ins. "
        "After completing significant tasks or generating files, confirm the outcome."
    )
    if plugin_catalog:
        result = plugin_catalog.compose_all_prompts(result)
    return result


async def _close_components(components: dict) -> None:
    mcp_task = components.get("mcp_task")
    if mcp_task is not None and not mcp_task.done():
        mcp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await mcp_task
    mcp_client = components.get("mcp_client")
    if mcp_client is not None:
        await mcp_client.close()
    ctx_mgr = components.get("context_manager")
    if ctx_mgr is not None:
        staging = getattr(ctx_mgr, "staging", None)
        if staging is not None and hasattr(staging, "close"):
            staging.close()
        if hasattr(ctx_mgr, "store"):
            ctx_mgr.store.close()
    plugin_catalog = components.get("plugin_catalog")
    if plugin_catalog is not None and hasattr(plugin_catalog, "close"):
        plugin_catalog.close()
