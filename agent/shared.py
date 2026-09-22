from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from rich.console import Console

def _resolve_agent_home() -> Path:
    raw = os.environ.get("SIMPLE_AGENT_HOME", "")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".agent"


DEFAULT_AGENT_HOME = _resolve_agent_home()
DEFAULT_CONFIG_FILE = DEFAULT_AGENT_HOME / "config.json"


def session_home(name: str) -> Path:
    """Return the data home for a named session.

    Named CLI sessions keep the original ``--name`` layout as siblings of the
    user-level default (``~/.agent-prod`` for ``--name prod``).  An empty name
    means the default agent home, which honours ``SIMPLE_AGENT_HOME``.
    """
    clean = str(name or "").strip()
    if not clean:
        return DEFAULT_AGENT_HOME
    return Path.home() / f".agent-{clean}"


def web_session_root() -> Path:
    """Return the root directory for isolated Web session runtimes.

    Web sessions intentionally keep independent context, memory, skills and
    tools. Keeping them below the active agent home makes that isolation
    explicit without scattering ``.agent-<id>`` siblings across the home.
    """
    return AGENT_HOME / "web" / "sessions"


def web_session_home(session_id: str) -> Path:
    """Return the filesystem home for one Web session."""
    clean = str(session_id or "").strip()
    if not clean or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", clean) is None:
        raise ValueError("invalid web session id")
    return web_session_root() / clean


def iter_session_homes():
    """Yield ``(session_name, home)`` for discoverable sessions.

    The default home is always included as ``"default"``; named sessions are
    discovered by the ``.agent-<name>`` pattern under the user home directory.
    Missing or non-directory matches are ignored.
    """
    yield "default", DEFAULT_AGENT_HOME
    home_root = Path.home()
    if not home_root.is_dir():
        return
    for path in home_root.glob(".agent-*"):
        if not path.is_dir():
            continue
        yield path.name[len(".agent-"):], path


def _is_named_session_home() -> bool:
    """Return True when the active home is a ``.agent-<name>`` session."""
    if AGENT_HOME == DEFAULT_AGENT_HOME:
        return False
    return (
        AGENT_HOME.parent == Path.home()
        and AGENT_HOME.name.startswith(".agent-")
    )


def resolve_config_file() -> Path:
    """Return the config file to read/write for the active session.

    A session-specific ``config.json`` always wins when present.  For a named
    session (``~/.agent-<name>``) without its own config, the default shared
    config is used.  Any other home keeps using its own ``CONFIG_FILE`` even
    when the file does not exist yet (first run, tests, custom
    ``SIMPLE_AGENT_HOME``).
    """
    if _is_named_session_home():
        if CONFIG_FILE.exists():
            return CONFIG_FILE
        return DEFAULT_CONFIG_FILE
    return CONFIG_FILE


def _set_agent_home(home: Path) -> None:
    """Override AGENT_HOME and all derived paths (for CLI --home support).

    Prefer ``agent._set_agent_home`` from CLI entry points: it also refreshes
    the module-level ``agent.TASKS_DIR`` mirror for Ralph tasks.
    """
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

# How much of the window a request holds back for the answer it is about to ask
# for.  This is deliberately *not* `DEFAULT_MAX_TOKENS`: the configured cap is a
# ceiling on one response, while this is the room the input budget must leave
# free.  Deriving the input budget from the cap conflated the two, so a provider
# configured for long answers got the smallest usable context -- deepseek's
# 64000-token cap against a 128000 window left roughly 57k for the whole
# conversation.  Sized to cover a full default-length answer.
DEFAULT_OUTPUT_RESERVE = 8192

# ── Thinking effort ───────────────────────────────────────────────────────
# Our own vocabulary for "how hard should the model think".  It lives here
# rather than in a transport because three layers have to agree on it: config
# validation, the settings page's options, and the transport that translates it
# into whatever word the provider actually accepts (the OpenAI-compatible
# gateway this was measured against takes none/minimal/low/medium/high/xhigh/
# max).  The tuple is the contract; the translation is the transport's business.
THINKING_EFFORTS = ("off", "low", "medium", "high")


def normalize_thinking_effort(value: object) -> str | None:
    """Our effort word for *value*, or None when the config has no opinion.

    None is a real state, not a failure: it means "this provider was never
    told", which is what every config without a `thinking` key says — and every
    provider the agent ships with must keep saying it, because the wire
    parameter that turns thinking off is not one every gateway knows.
    A recognised word is returned lowercase; anything else (absent key, typo,
    number) also has no opinion, and `_validate_config` names the accepted
    words so the typo is visible rather than silently obeyed.
    """
    text = str(value or "").strip().lower()
    return text if text in THINKING_EFFORTS else None


# Default model input context window in tokens.  Used by the compaction
# trigger to decide when working memory (ctx.messages) must be trimmed
# before the next LLM call.  Override per-provider in config.json.
DEFAULT_CONTEXT_WINDOW = 128_000

MEMORY_TIDY_INTERVAL = 3600
MEMORY_TIDY_FILE_THRESHOLD = 5
DEFAULT_MAX_PARALLEL_AGENTS = 3
DEFAULT_SUB_AGENT_TIMEOUT_SECONDS = 1800
DEFAULT_SUB_AGENT_RETRIES = 0
DEFAULT_RESULT_CONTENT_MAX_CHARS = 4000
MIN_RESULT_CONTENT_CHARS = 500
MAX_RESULT_CONTENT_CHARS = 16000
DEFAULT_TURN_HOOK_TIMEOUT_SECONDS = 2.0
MAX_TOOL_CALL_ITERATIONS = 200
MAX_CONFIGURABLE_TOOL_CALL_ITERATIONS = 500
DEFAULT_MAX_TRUNCATION_CONTINUATIONS = 6
MAX_CONFIGURABLE_TRUNCATION_CONTINUATIONS = 20
REGULAR_TOOL_TIMEOUT = 1800
DEFAULT_LLM_MAX_RETRIES = 3
DEFAULT_LLM_RETRY_BASE_DELAY = 1.0
DEFAULT_SESSION_END_FLUSH_TIMEOUT_SECONDS = 30.0

CONTEXT_DIR = AGENT_HOME / "context"
LATENCY_TRACE_ENV_VAR = "SIMPLE_TRACE_LATENCY"
MAX_CATEGORIES = 15
MIN_IMPORTANCE = 0.05
CHARS_PER_TOKEN = 4
SLEEP_TOKEN_RATIO = 0.70
DECAY_FACTOR = 0.95
RETRIEVAL_TOP_K = 5
# Share of the usable window automatic retrieval may occupy.  top_k bounds the
# entry *count*, which says nothing about size; this bounds the actual cost.
RETRIEVAL_BUDGET_FRACTION = 0.15
STAGING_DIR = CONTEXT_DIR / "_staging"
RECENT_SESSION_TURNS = 6
PALACE_DB_FILE = CONTEXT_DIR / "palace.db"
STAGING_TURN_THRESHOLD = 12
CONSOLIDATION_MAX_SOURCE_TOKENS = 8000
STAGING_TOKEN_THRESHOLD = 16000
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

# The conversation the current turn belongs to, for the same reason: a provider
# request is built deep below the turn, and a gateway that wants one stable id
# per conversation (`x-opencode-session` and its like) has to get it from the
# turn that is asking rather than from the process that is serving.
_active_session_id: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("active_session_id", default=None)
)

#: The token a provider's ``headers`` value uses to ask for one value per
#: conversation.  A placeholder rather than a callback because the value has to
#: survive being written into a JSON config file, and the substitution happens
#: per request -- see ``ModelTransport._headers_kwarg``.
SESSION_HEADER_PLACEHOLDER = "{session}"

#: Stable for the life of this process.  Two jobs: the fallback for
#: :func:`current_session_id` when a request is made outside any turn (a
#: background consolidation, say), and the reason a provider that asks for a
#: per-session value never receives an empty one.
_INSTANCE_ID = uuid.uuid4().hex


def instance_id() -> str:
    """This process's own id, stable for as long as it runs."""
    return _INSTANCE_ID


def current_session_id() -> str:
    """The conversation the current turn belongs to, or the process id.

    The fallback is deliberate rather than an error state: work that runs
    outside a turn has no conversation to name, and a header that must be
    present is better served by the process's own stable id than by an empty
    string or by a value that changes on every request.
    """
    session = _active_session_id.get()
    return str(session) if session else _INSTANCE_ID


def resolve_provider_headers(
    provider_cfg: object, *, provider_name: str = ""
) -> dict[str, str]:
    """The extra request headers one provider declares, environment expanded.

    ``providers.<name>.headers`` is a name → value map for gateways that need
    more than a URL and a key: a client identifier, a routing or session
    header, an organisation tag.  A value of the exact form ``$NAME`` is read
    from the environment, the same rule ``api_key`` follows -- and with the
    same failure: a missing variable is named and raised rather than sent as an
    empty header, because a gateway that requires the header answers an empty
    one with a rejection that does not say why.

    ``{session}`` is left in place here and substituted per request (see
    :data:`SESSION_HEADER_PLACEHOLDER`), which is the whole point of it: one
    client serves every conversation.

    A pair that is not a name → string pair is dropped rather than guessed at;
    ``_validate_config`` reports those, where the typo is visible.
    """
    if not isinstance(provider_cfg, dict):
        return {}
    raw = provider_cfg.get("headers")
    if not isinstance(raw, dict) or not raw:
        return {}
    resolved: dict[str, str] = {}
    for name, value in raw.items():
        header = str(name).strip()
        if not header or isinstance(value, bool):
            continue
        if not isinstance(value, (str, int, float)):
            continue
        text = str(value)
        if text.startswith("$"):
            variable = text[1:]
            from_env = os.environ.get(variable, "")
            if not from_env:
                where = f" (provider: {provider_name})" if provider_name else ""
                raise RuntimeError(
                    f"Header '{header}' reads env var '{variable}', which is not "
                    f"set{where}. Run: export {variable}=..."
                )
            text = from_env
        resolved[header] = text
    return resolved


CONSOLE = Console()


def _new_id() -> str:
    return uuid.uuid4().hex


def _atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Replace ``path`` with ``content`` as one indivisible, durable step.

    ``rename`` alone buys atomicity — a reader sees the whole old file or the
    whole new one, never a truncation in progress.  It does not buy durability:
    without ``fsync`` the rename can be visible while the bytes it points at are
    still only in the page cache, so a crash yields an empty or short file that
    every reader accepts as authoritative.  Both syncs are needed, and for
    different reasons: the file so its contents survive, the parent directory so
    the *entry* does.

    This is the single durable-write primitive.  Anything persisting state the
    next process start will read must go through it rather than
    ``Path.write_text``, which truncates in place and has neither property.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except BaseException:
        # Never leave the scratch file behind: these live beside real state and
        # a stale one is indistinguishable from a partial write to an operator.
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    """Force a directory entry to disk; best-effort, not all platforms allow it."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, payload: object, *, indent: int | None = None) -> None:
    """Durably persist ``payload`` as JSON. Shares one primitive with text."""
    _atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=indent)
    )


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
    #: Provider usage, when the caller asked for it.  Only the streaming
    #: assembler in `OpenAITransport` sets this: a non-streaming response is the
    #: SDK's own object, which carries `usage` natively.  Optional so every
    #: existing construction site keeps working, and so a gateway that reports
    #: nothing is distinguishable from one that reported zero.
    usage: object | None = None


@dataclass
class _AnthropicTextBlock:
    text: str


@dataclass
class _AnthropicFallbackResponse:
    stop_reason: str
    content: list[object]

__all__ = [
    "AGENT_HOME",
    "DEFAULT_AGENT_HOME",
    "DEFAULT_CONFIG_FILE",
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
    "DEFAULT_OUTPUT_RESERVE",
    "MEMORY_TIDY_INTERVAL",
    "MEMORY_TIDY_FILE_THRESHOLD",
    "DEFAULT_MAX_PARALLEL_AGENTS",
    "DEFAULT_SUB_AGENT_TIMEOUT_SECONDS",
    "DEFAULT_SUB_AGENT_RETRIES",
    "DEFAULT_RESULT_CONTENT_MAX_CHARS",
    "MIN_RESULT_CONTENT_CHARS",
    "MAX_RESULT_CONTENT_CHARS",
    "DEFAULT_TURN_HOOK_TIMEOUT_SECONDS",
    "MAX_TOOL_CALL_ITERATIONS",
    "MAX_CONFIGURABLE_TOOL_CALL_ITERATIONS",
    "DEFAULT_MAX_TRUNCATION_CONTINUATIONS",
    "MAX_CONFIGURABLE_TRUNCATION_CONTINUATIONS",
    "REGULAR_TOOL_TIMEOUT",
    "DEFAULT_LLM_MAX_RETRIES",
    "DEFAULT_LLM_RETRY_BASE_DELAY",
    "DEFAULT_SESSION_END_FLUSH_TIMEOUT_SECONDS",
    "CONTEXT_DIR",
    "LATENCY_TRACE_ENV_VAR",
    "MAX_CATEGORIES",
    "MIN_IMPORTANCE",
    "CHARS_PER_TOKEN",
    "SLEEP_TOKEN_RATIO",
    "DECAY_FACTOR",
    "RETRIEVAL_BUDGET_FRACTION",
    "RETRIEVAL_TOP_K",
    "DEFAULT_CONTEXT_WINDOW",
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
    "session_home",
    "web_session_root",
    "web_session_home",
    "iter_session_homes",
    "resolve_config_file",
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
