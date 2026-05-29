from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from rich.console import Console

def _resolve_agent_home() -> Path:
    raw = os.environ.get("SIMPLE_AGENT_HOME", "")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".agent"


def _set_agent_home(home: Path) -> None:
    """Override AGENT_HOME and all derived paths (for CLI --home support)."""
    global AGENT_HOME, MEMORY_DIR, SKILLS_DIR, TOOLS_DIR, PACKAGE_ROOT
    global BUILTIN_SKILLS_DIR, PROMPTS_DIR, RL_DIR, SCHEDULER_DIR
    global SCHEDULER_DB_FILE, CONFIG_FILE, INDEX_FILE, SESSIONS_FILE
    global DEFAULT_OUTPUT_DIR, PLUGINS_DIR, USER_PLUGINS_DIR, CONTEXT_DIR
    global STAGING_DIR, PALACE_DB_FILE
    resolved = Path(home).expanduser().resolve()
    os.environ["SIMPLE_AGENT_HOME"] = str(resolved)
    AGENT_HOME = resolved
    MEMORY_DIR = AGENT_HOME / "memory"
    SKILLS_DIR = AGENT_HOME / "skills"
    TOOLS_DIR = AGENT_HOME / "tools"
    PACKAGE_ROOT = Path(__file__).resolve().parent
    BUILTIN_SKILLS_DIR = PACKAGE_ROOT / "_builtin" / "skills"
    PROMPTS_DIR = AGENT_HOME / "prompts"
    RL_DIR = AGENT_HOME / "rl"
    SCHEDULER_DIR = AGENT_HOME / "tasks"
    SCHEDULER_DB_FILE = SCHEDULER_DIR / "scheduler.db"
    CONFIG_FILE = AGENT_HOME / "config.json"
    INDEX_FILE = MEMORY_DIR / "INDEX.md"
    SESSIONS_FILE = RL_DIR / "sessions.jsonl"
    DEFAULT_OUTPUT_DIR = AGENT_HOME / "output"
    PLUGINS_DIR = PACKAGE_ROOT / "_builtin" / "plugins"
    USER_PLUGINS_DIR = AGENT_HOME / "plugins"
    CONTEXT_DIR = AGENT_HOME / "context"
    STAGING_DIR = CONTEXT_DIR / "_staging"
    PALACE_DB_FILE = CONTEXT_DIR / "palace.db"


AGENT_HOME = _resolve_agent_home()
MEMORY_DIR = AGENT_HOME / "memory"
SKILLS_DIR = AGENT_HOME / "skills"
TOOLS_DIR = AGENT_HOME / "tools"
PACKAGE_ROOT = Path(__file__).resolve().parent
BUILTIN_SKILLS_DIR = PACKAGE_ROOT / "_builtin" / "skills"
PROMPTS_DIR = AGENT_HOME / "prompts"
RL_DIR = AGENT_HOME / "rl"
SCHEDULER_DIR = AGENT_HOME / "tasks"
SCHEDULER_DB_FILE = SCHEDULER_DIR / "scheduler.db"
CONFIG_FILE = AGENT_HOME / "config.json"
INDEX_FILE = MEMORY_DIR / "INDEX.md"
SESSIONS_FILE = RL_DIR / "sessions.jsonl"
DEFAULT_OUTPUT_DIR = AGENT_HOME / "output"
PLUGINS_DIR = PACKAGE_ROOT / "_builtin" / "plugins"
USER_PLUGINS_DIR = AGENT_HOME / "plugins"

DEFAULT_MODEL = "claude-opus-4-5"
DEFAULT_MAX_TOKENS = 8192
MEMORY_TIDY_INTERVAL = 3600
MEMORY_TIDY_FILE_THRESHOLD = 5
DEFAULT_MAX_PARALLEL_AGENTS = 3
DEFAULT_SUB_AGENT_TIMEOUT_SECONDS = 1800
DEFAULT_SUB_AGENT_RETRIES = 0
DEFAULT_RESULT_CONTENT_MAX_CHARS = 4000
DEFAULT_TURN_HOOK_TIMEOUT_SECONDS = 2.0
MAX_TOOL_CALL_ITERATIONS = 200
MAX_CONFIGURABLE_TOOL_CALL_ITERATIONS = 500
REGULAR_TOOL_TIMEOUT = 1800
DEFAULT_LLM_MAX_RETRIES = 3
DEFAULT_LLM_RETRY_BASE_DELAY = 1.0

CONTEXT_DIR = AGENT_HOME / "context"
LATENCY_TRACE_ENV_VAR = "SIMPLE_TRACE_LATENCY"
MAX_CATEGORIES = 15
MIN_IMPORTANCE = 0.05
CHARS_PER_TOKEN = 4
SLEEP_TOKEN_RATIO = 0.70
DECAY_FACTOR = 0.95
RETRIEVAL_TOP_K = 5
STAGING_DIR = CONTEXT_DIR / "_staging"
RECENT_SESSION_TURNS = 6
PALACE_DB_FILE = CONTEXT_DIR / "palace.db"
STAGING_TURN_THRESHOLD = 6
CONSOLIDATION_MAX_SOURCE_TOKENS = 1200
STAGING_TOKEN_THRESHOLD = 8000
PALACE_LOCI = (
    "identity",
    "projects",
    "people",
    "concepts",
    "episodes",
    "tasks",
    "procedures",
    "archive",
)
LEGACY_MEMORY_ALIASES = {
    "knowledge": "concepts",
}
PALACE_LOCUS_SUMMARIES = {
    "identity": "User identity, preferences, communication style, and durable constraints",
    "projects": "Project background, decisions, risks, and current state",
    "people": "People-specific facts, relationships, and collaboration context",
    "concepts": "Stable concepts, definitions, and domain knowledge",
    "episodes": "Session and event summaries",
    "tasks": "Open loops, commitments, and next actions",
    "procedures": "Reusable workflows and preferred methods",
    "archive": "Superseded or historical memory items",
}
DEFAULT_ROUTE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "episodes": (),
    "identity": ("偏好", "喜欢", "风格", "prefer", "preference"),
    "projects": ("项目", "project", "repo", "仓库"),
    "tasks": ("任务", "todo", "待办", "next step", "open loop"),
    "procedures": ("流程", "通常怎么", "workflow", "procedure"),
    "people": ("人", "person", "people", "同事"),
    "concepts": ("概念", "是什么", "what is", "define", "知识"),
}

class CancelToken:
    """Per-turn cancellation token with two-level cleanup callbacks.

    A bare ``cancel()`` is *graceful*: cleanups run (e.g. SIGTERM to a
    shell subprocess), the current step gets a chance to finish whatever
    it can, and the next tool-loop boundary picks up the signal cleanly.
    ``cancel(level="force")`` is *hard*: cleanups run in force mode
    (SIGKILL, asyncio.Task.cancel), abandoning in-flight work.

    Tools register themselves via ``register_cleanup(name, fn)`` and MUST
    call the returned deregister callback when they finish normally —
    otherwise the cancel handler would try to clean up a stale resource.

    Reading ``is_cancelled`` is the existing cooperative-cancel API and
    still works at every tool-loop boundary.
    """

    def __init__(self) -> None:
        self._cancelled = False
        self._level = "none"  # "none" | "graceful" | "force"
        # Insertion-ordered list of (name, callback) entries so cleanups
        # fire in registration order — innermost (most-recent) op cleans
        # first, then unwinds outward.
        self._cleanups: list[tuple[str, "Callable[[str], None]"]] = []

    def cancel(self, level: str = "graceful") -> None:
        """Mark the token cancelled and fire all registered cleanups.

        Re-calling with a higher level (``"force"`` after ``"graceful"``)
        upgrades the signal: cleanups fire again in force mode so e.g. a
        process that ignored SIGTERM now gets SIGKILL.
        """
        was_cancelled = self._cancelled
        self._cancelled = True
        if level not in ("graceful", "force"):
            level = "graceful"
        # Upgrade only forward (graceful → force; never force → graceful).
        if not was_cancelled or (level == "force" and self._level != "force"):
            self._level = level
            # Iterate over a snapshot so cleanups can deregister themselves.
            for name, cb in list(self._cleanups):
                try:
                    cb(level)
                except Exception as exc:
                    logging.getLogger("agent").warning(
                        "CancelToken cleanup '%s' raised: %s", name, exc
                    )

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    @property
    def level(self) -> str:
        return self._level

    def register_cleanup(
        self, name: str, callback: "Callable[[str], None]"
    ) -> "Callable[[], None]":
        """Register a cleanup fired when ``cancel()`` is called.

        ``callback`` receives the cancel level (``"graceful"`` or
        ``"force"``) and should be **fast** and **non-throwing** — typical
        impl is sending a signal to a subprocess or cancelling an
        asyncio.Task.

        Returns a deregister function the caller MUST call when the
        resource completes normally (use try/finally).  If the token is
        already cancelled when registering, the callback fires immediately.
        """
        entry = (name, callback)
        if self._cancelled:
            try:
                callback(self._level)
            except Exception as exc:
                logging.getLogger("agent").warning(
                    "CancelToken late cleanup '%s' raised: %s", name, exc
                )
            return lambda: None
        self._cleanups.append(entry)

        def _deregister() -> None:
            try:
                self._cleanups.remove(entry)
            except ValueError:
                pass

        return _deregister


# Published via ContextVar so any tool deep in the call stack can grab
# the active cancellation token and register a cleanup without us having
# to thread the token through every API.
_active_cancel_token: contextvars.ContextVar[Optional[CancelToken]] = (
    contextvars.ContextVar("active_cancel_token", default=None)
)


CONSOLE = Console()


def _new_id() -> str:
    return uuid.uuid4().hex


def _atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(content, encoding=encoding)
    tmp.replace(path)


def _latency_trace_enabled() -> bool:
    raw = os.environ.get(LATENCY_TRACE_ENV_VAR, "")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _trace_fields(**fields: object) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value).replace("\n", "\\n")
        if not text:
            continue
        if any(ch.isspace() for ch in text):
            text = repr(text)
        parts.append(f"{key}={text}")
    return " ".join(parts)


def _trace_latency(component: str, stage: str, **fields: object) -> None:
    if not _latency_trace_enabled():
        return
    payload = _trace_fields(**fields)
    message = f"latency_trace component={component} stage={stage}"
    if payload:
        message += f" {payload}"
    logging.getLogger("agent").warning(message)


def _preview_text(text: object, limit: int = 80) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _looks_like_chinese(text: object) -> bool:
    return any("一" <= ch <= "鿿" for ch in str(text or ""))


def _cancelled_by_user_text(reference_text: object = "") -> str:
    """Localized fallback shown when a turn is cancelled by the user."""
    if _looks_like_chinese(reference_text):
        return "[任务已被用户中断]"
    return "[Task cancelled by user]"


@contextlib.contextmanager
def _suppress_with_log(reason: str, *, logger_name: str = "agent", level: int = logging.WARNING):
    """Swallow exceptions but always leave a debuggable trace.

    First-principles replacement for bare ``except Exception: pass``.
    Forces every silent-failure site to articulate why and what is being lost,
    so a future operator can grep the log instead of grepping for ``pass``.
    """
    try:
        yield
    except Exception:
        logging.getLogger(logger_name).log(level, "suppressed: %s", reason, exc_info=True)


def _interaction_log(component: str, event: str, **fields: object) -> None:
    payload = _trace_fields(**fields)
    message = f"interaction component={component} event={event}"
    if payload:
        message += f" {payload}"
    logging.getLogger("agent").info(message)


def _is_safe_prompt_version(version: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", version))


def _with_task_context(system_prompt: str, task_context: str) -> str:
    task_context = str(task_context or "").strip()
    if not task_context:
        return system_prompt
    return (
        system_prompt
        + "\n\n## Current Task Context (original request)\n"
        + task_context
    )


@dataclass
class _OAIFunc:
    name: str
    arguments: str


@dataclass
class _OAITC:
    id: str
    function: _OAIFunc


@dataclass
class _OAIMsg:
    content: str
    tool_calls: list[_OAITC] | None = None
    model_extra: dict[str, object] | None = None


@dataclass
class _OAIChoice:
    finish_reason: str
    message: _OAIMsg


@dataclass
class _OAIResponse:
    choices: list[_OAIChoice]


@dataclass
class _AnthropicTextBlock:
    text: str


@dataclass
class _AnthropicFallbackResponse:
    stop_reason: str
    content: list[object]


__all__ = [
    "AGENT_HOME",
    "MEMORY_DIR",
    "SKILLS_DIR",
    "TOOLS_DIR",
    "PACKAGE_ROOT",
    "BUILTIN_SKILLS_DIR",
    "PROMPTS_DIR",
    "RL_DIR",
    "SCHEDULER_DIR",
    "SCHEDULER_DB_FILE",
    "CONFIG_FILE",
    "INDEX_FILE",
    "SESSIONS_FILE",
    "DEFAULT_OUTPUT_DIR",
    "PLUGINS_DIR",
    "USER_PLUGINS_DIR",
    "DEFAULT_MODEL",
    "DEFAULT_MAX_TOKENS",
    "MEMORY_TIDY_INTERVAL",
    "MEMORY_TIDY_FILE_THRESHOLD",
    "DEFAULT_MAX_PARALLEL_AGENTS",
    "DEFAULT_SUB_AGENT_TIMEOUT_SECONDS",
    "DEFAULT_SUB_AGENT_RETRIES",
    "DEFAULT_RESULT_CONTENT_MAX_CHARS",
    "DEFAULT_TURN_HOOK_TIMEOUT_SECONDS",
    "MAX_TOOL_CALL_ITERATIONS",
    "MAX_CONFIGURABLE_TOOL_CALL_ITERATIONS",
    "REGULAR_TOOL_TIMEOUT",
    "DEFAULT_LLM_MAX_RETRIES",
    "DEFAULT_LLM_RETRY_BASE_DELAY",
    "CONTEXT_DIR",
    "LATENCY_TRACE_ENV_VAR",
    "MAX_CATEGORIES",
    "MIN_IMPORTANCE",
    "CHARS_PER_TOKEN",
    "SLEEP_TOKEN_RATIO",
    "DECAY_FACTOR",
    "RETRIEVAL_TOP_K",
    "STAGING_DIR",
    "RECENT_SESSION_TURNS",
    "PALACE_DB_FILE",
    "STAGING_TURN_THRESHOLD",
    "CONSOLIDATION_MAX_SOURCE_TOKENS",
    "STAGING_TOKEN_THRESHOLD",
    "PALACE_LOCI",
    "LEGACY_MEMORY_ALIASES",
    "PALACE_LOCUS_SUMMARIES",
    "DEFAULT_ROUTE_KEYWORDS",
    "CONSOLE",
    "_resolve_agent_home",
    "_set_agent_home",
    "_new_id",
    "_atomic_write_text",
    "_latency_trace_enabled",
    "_trace_fields",
    "_trace_latency",
    "_preview_text",
    "_interaction_log",
    "_is_safe_prompt_version",
    "_with_task_context",
    "_OAIFunc",
    "_OAITC",
    "_OAIMsg",
    "_OAIChoice",
    "_OAIResponse",
    "_AnthropicTextBlock",
    "_AnthropicFallbackResponse",
]
