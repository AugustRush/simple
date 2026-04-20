from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

AGENT_HOME = Path.home() / ".agent"
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
DEFAULT_SUB_AGENT_TIMEOUT_SECONDS = 300
MAX_TOOL_CALL_ITERATIONS = 40
REGULAR_TOOL_TIMEOUT = 120

CONTEXT_DIR = AGENT_HOME / "context"
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

CONSOLE = Console()


def _new_id() -> str:
    return uuid.uuid4().hex


def _atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(content, encoding=encoding)
    tmp.replace(path)


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
    "MAX_TOOL_CALL_ITERATIONS",
    "REGULAR_TOOL_TIMEOUT",
    "CONTEXT_DIR",
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
    "_new_id",
    "_atomic_write_text",
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
