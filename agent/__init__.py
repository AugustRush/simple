#!/usr/bin/env python3
"""
Personal Agent package runtime.
Architecture: Memory Palace + Multi-Agent Orchestration + MCP + Self-Evolution
"""

# ─────────────────────────────────────────────────────────────────────────────
# 1. IMPORTS & CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import asyncio
import ast
import copy
from contextlib import AsyncExitStack
import importlib.util
import inspect
import math
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional
import urllib.parse
import urllib.request
import html
from zoneinfo import ZoneInfo

import anthropic
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

import mcp

from .channels import Channel, ChannelRunner, CliChannel, IncomingMessage, _build_gateway_channels
from .core.output import CliOutputSink, OutputSink, _active_sink, _fmt_tool_inputs

# ── Constants ─────────────────────────────────────────────────────────────────
AGENT_HOME = Path.home() / ".agent"
MEMORY_DIR = AGENT_HOME / "memory"
SKILLS_DIR = AGENT_HOME / "skills"
TOOLS_DIR = AGENT_HOME / "tools"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILTIN_SKILLS_DIR = PROJECT_ROOT / "skills"
PROMPTS_DIR = AGENT_HOME / "prompts"
RL_DIR = AGENT_HOME / "rl"
CONFIG_FILE = AGENT_HOME / "config.json"
INDEX_FILE = MEMORY_DIR / "INDEX.md"
SESSIONS_FILE = RL_DIR / "sessions.jsonl"
DEFAULT_OUTPUT_DIR = AGENT_HOME / "output"
PLUGINS_DIR = PROJECT_ROOT / "plugins"
USER_PLUGINS_DIR = AGENT_HOME / "plugins"

DEFAULT_MODEL = "claude-opus-4-5"
DEFAULT_MAX_TOKENS = 8192
MEMORY_TIDY_INTERVAL = 3600  # seconds
MEMORY_TIDY_FILE_THRESHOLD = 5
DEFAULT_MAX_PARALLEL_AGENTS = 3
DEFAULT_SUB_AGENT_TIMEOUT_SECONDS = (
    300  # must be > REGULAR_TOOL_TIMEOUT to allow multi-tool sub-agents
)
MAX_TOOL_CALL_ITERATIONS = 40  # hard ceiling on tool-call rounds per send_message call
REGULAR_TOOL_TIMEOUT = 120  # wall-clock timeout (s) for any single non-spawn tool call

# ── Context Manager constants ──────────────────────────────────────────────────
CONTEXT_DIR = AGENT_HOME / "context"
MAX_CATEGORIES = 15  # upper limit on dynamic LTM categories
MIN_IMPORTANCE = 0.05  # entries below this are pruned after decay
CHARS_PER_TOKEN = 4  # rough estimate: 4 chars ≈ 1 token
SLEEP_TOKEN_RATIO = 0.70  # trigger sleep when working memory > 70% of max_tokens
DECAY_FACTOR = 0.95  # per-sleep importance multiplier
RETRIEVAL_TOP_K = 5  # top-K entries injected per turn
STAGING_DIR = CONTEXT_DIR / "_staging"  # per-session raw conversation buffers
RECENT_SESSION_TURNS = 6  # recent staged turns exposed to explicit context lookup
PALACE_DB_FILE = CONTEXT_DIR / "palace.db"
STAGING_TURN_THRESHOLD = 6
CONSOLIDATION_MAX_SOURCE_TOKENS = 1200
# Token threshold for the slow-path staging consolidation check.
# Must be large enough to avoid firing on a single verbose turn.
# CJK text costs ~1 estimated token/char, so 2100 fired on every response
# over ~2100 chars. 8000 tokens ≈ 8 typical turns of mixed CJK/English
# conversation, matching the intent of "consolidate after meaningful context
# has accumulated" rather than "consolidate after one long reply".
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

# ── Ralph Loop ────────────────────────────────────────────────────────────────
TASKS_DIR = AGENT_HOME / "tasks"
RALPH_COMPLETION_PROMISE = "<promise>COMPLETE</promise>"
RALPH_DEFAULT_MAX_ITERATIONS = 10


@dataclass
class RalphTask:
    """State for a Ralph-mode autonomous task iteration loop.

    Persisted to ~/.agent/tasks/<id>.json after every iteration so the task
    survives process restarts and provides an audit trail.
    """

    id: str
    goal: str
    completion_criteria: list
    verify_command: Optional[str]
    completion_promise: str
    max_iterations: int
    current_iteration: int = 0
    status: str = "running"  # running | complete | max_iterations_reached
    progress: list = field(default_factory=list)  # append-only per-iteration log
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()


def _save_ralph_task(task: RalphTask) -> None:
    """Atomically persist task state to disk."""
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        TASKS_DIR / f"{task.id}.json",
        json.dumps(asdict(task), indent=2, ensure_ascii=False),
    )


def _load_ralph_task(task_id: str) -> Optional[RalphTask]:
    """Load a previously persisted task, or None if not found."""
    path = TASKS_DIR / f"{task_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return RalphTask(**data)
    except Exception:
        return None


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


# ── OpenAI streaming synthetic response types ─────────────────────────────────
# These replace the inline anonymous classes that were previously defined inside
# _stream_response(), making the code testable and eliminating duplication.


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
    tool_calls: Optional[list]


@dataclass
class _OAIChoice:
    finish_reason: str
    message: _OAIMsg


@dataclass
class _OAIResponse:
    choices: list


@dataclass
class _AnthropicTextBlock:
    text: str
    type: str = "text"


@dataclass
class _AnthropicFallbackResponse:
    stop_reason: str
    content: list


DEFAULT_SYSTEM_PROMPT = """You are a powerful personal AI agent with tools, memory, and the ability to spawn sub-agents.

## Tools
Your exact tool capabilities are appended later in this prompt. Use only the tools explicitly listed for this agent instance.

## spawn_agent — multi-agent orchestration

Use `spawn_agent` when the task benefits from specialised sub-agents. Two core patterns:

### Pattern 1 — Parallel (independent subtasks)
Call `spawn_agent` **multiple times in ONE turn** when subtasks are fully independent.
They run concurrently; you synthesise the results afterward.
Example: "summarise these 3 articles" → spawn 3 summarisers simultaneously.

### Pattern 2 — Pipeline / Debate (dependent or iterative)
Call `spawn_agent` **one at a time across multiple turns**, passing each result forward.
Use when role B needs role A's output, OR when you need multiple debate rounds.

**Multi-round debate example:**
- Round 1, turn 1: spawn(proposer, task=question)           → proposal_1
- Round 1, turn 2: spawn(critic,   task=proposal_1)         → critique_1
- Round 2, turn 3: spawn(proposer, task=critique_1)         → proposal_2  ← refined
- Round 2, turn 4: spawn(critic,   task=proposal_2)         → critique_2
- … repeat until positions converge or you judge it sufficient …
- Final turn:      spawn(judge, task=full_history)           → verdict

**Deciding when to stop**: after each critic turn, assess whether the debate has converged
(positions are close, or further rounds yield diminishing returns). One round is often
insufficient for complex or controversial questions — use your judgement.
The user can also specify a number of rounds explicitly (e.g. "debate for 3 rounds").

The key rule: **if role B needs role A's output, they must be sequential, not parallel.**

### When NOT to use spawn_agent
Answer directly for simple questions, single-domain tasks, and conversational follow-ups.
Default to direct — don't over-orchestrate.

## Memory
Save important facts, decisions, and learnings to memory so they persist across sessions.
"""

TOOL_DEFAULT_MAX_READ_BYTES = 64 * 1024
TOOL_DEFAULT_MAX_WRITE_BYTES = 256 * 1024
TOOL_DEFAULT_MAX_LIST_RESULTS = 100

# ── Shell tool security ────────────────────────────────────────────────────────
# Commands listed here are blocked unconditionally, regardless of arguments.
# Operators can extend this list via config key "shell_blocked_commands".
_SHELL_BLOCKED_COMMANDS: frozenset[str] = frozenset(
    {
        "rm",
        "rmdir",
        "mkfs",
        "dd",
        "shred",
        "fdisk",
        "parted",
    }
)

# Dangerous pipe-idiom substrings – checked as literal substring of the command.
_SHELL_BLOCKED_PATTERNS: tuple[str, ...] = (
    "curl | sh",
    "wget | sh",
    "wget -O- |",
    "curl -s |",
)


def _shell_command_is_blocked(
    command: str, extra_blocked: Optional[list[str]] = None
) -> Optional[str]:
    """Return a human-readable reason if *command* is blocked, or None.

    Two checks are performed:
      1. Dangerous pipe patterns (substring match against the full command).
      2. The effective executable basename, after skipping wrappers such as
         env-var prefixes, ``env``, and ``sudo``, against the built-in and
         caller-supplied blocklists.
    """
    import shlex as _shlex
    import os as _os

    def _is_env_assignment(token: str) -> bool:
        return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", token))

    def _resolve_effective_command(tokens: list[str]) -> Optional[str]:
        idx = 0
        while idx < len(tokens):
            token = tokens[idx]
            if _is_env_assignment(token):
                idx += 1
                continue

            cmd = _os.path.basename(token.strip().lstrip("./"))
            if cmd == "env":
                idx += 1
                while idx < len(tokens):
                    token = tokens[idx]
                    if token == "--":
                        idx += 1
                        break
                    if _is_env_assignment(token):
                        idx += 1
                        continue
                    if token.startswith("-"):
                        idx += 1
                        if token in {
                            "-C",
                            "--chdir",
                            "-S",
                            "--split-string",
                            "-u",
                            "--unset",
                        } and idx < len(tokens):
                            idx += 1
                        continue
                    break
                continue

            if cmd == "sudo":
                idx += 1
                while idx < len(tokens):
                    token = tokens[idx]
                    if token == "--":
                        idx += 1
                        break
                    if token.startswith("-"):
                        idx += 1
                        if token in {
                            "-g",
                            "--group",
                            "-h",
                            "--host",
                            "-p",
                            "--prompt",
                            "-R",
                            "--chroot",
                            "-r",
                            "--role",
                            "-t",
                            "--type",
                            "-u",
                            "--user",
                        } and idx < len(tokens):
                            idx += 1
                        continue
                    break
                continue

            return cmd
        return None

    blocked = _SHELL_BLOCKED_COMMANDS | frozenset(extra_blocked or [])
    for pattern in _SHELL_BLOCKED_PATTERNS:
        if pattern in command:
            return f"command pattern '{pattern}' is blocked for safety"
    try:
        tokens = _shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return None
    argv0 = _resolve_effective_command(tokens)
    if not argv0:
        return None
    if argv0 in blocked:
        return f"command '{argv0}' is blocked for safety"
    return None


from .memory.system import (
    BackgroundMemoryWorker,
    ConsolidationEngine,
    ContextManager,
    LTMCategory,
    LTMEntry,
    LTMStore,
    LocalRetriever,
    MemoryIndex,
    MemoryPalace,
    StagingBuffer,
    normalize_memory_chapter,
)
from .skills.catalog import (
    ExplicitSkillRequest,
    SkillBundle,
    SkillCatalog,
    parse_explicit_skill_request,
    prepare_user_message_for_skills,
)
from .tools.runtime import BuiltinTools, MCPClient, ToolDef, ToolRegistry, UserToolCatalog


# ─────────────────────────────────────────────────────────────────────────────
# 4. AGENT CORE
# ─────────────────────────────────────────────────────────────────────────────


from .core.agent import (
    AgentContext,
    AgentResult,
    BaseAgent,
    SubAgentProgressEvent,
)



from .evolution import EvolutionEngine


# ─────────────────────────────────────────────────────────────────────────────
# 6. PLUGIN SYSTEM
# ─────────────────────────────────────────────────────────────────────────────


from .plugins.catalog import (
    AgentPlugin,
    HookResult,
    PluginCatalog,
    PluginMeta,
    PostToolEvent,
    PreToolEvent,
    SessionEvent,
    TurnEvent,
)

# ─────────────────────────────────────────────────────────────────────────────
# 7. CONFIG
# ─────────────────────────────────────────────────────────────────────────────


from .config import (
    DEFAULT_CONFIG,
    ModelClientFactory,
    _close_components,
    _compose_system_prompt,
    _first_run_setup,
    _load_system_prompt,
    _now,
    _resolve_output_dir,
    load_config,
    save_config,
)
from .bootstrap import _build_components, _build_components_async


# ─────────────────────────────────────────────────────────────────────────────
# 8. CLI
# ─────────────────────────────────────────────────────────────────────────────

def __getattr__(name: str):
    if name in {
        "app",
        "memory_app",
        "_interactive_loop",
        "_ralph_task_loop",
        "main_callback",
        "_missing_feishu_dependency_hint",
        "memory_tidy",
    }:
        from . import cli as cli_module

        return getattr(cli_module, name)
    raise AttributeError(name)
