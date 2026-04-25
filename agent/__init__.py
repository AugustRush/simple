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
import types
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
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

import mcp

from . import shared as _shared
from .channels import Channel, ChannelRunner, CliChannel, IncomingMessage, _build_gateway_channels
from .core.output import CliOutputSink, OutputSink, _active_sink, _fmt_tool_inputs
from .shared import (
    AGENT_HOME,
    BUILTIN_SKILLS_DIR,
    CHARS_PER_TOKEN,
    CONFIG_FILE,
    CONSOLIDATION_MAX_SOURCE_TOKENS,
    CONSOLE,
    CONTEXT_DIR,
    DECAY_FACTOR,
    DEFAULT_MAX_PARALLEL_AGENTS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_ROUTE_KEYWORDS,
    DEFAULT_SUB_AGENT_TIMEOUT_SECONDS,
    INDEX_FILE,
    LEGACY_MEMORY_ALIASES,
    MAX_CATEGORIES,
    MAX_TOOL_CALL_ITERATIONS,
    MEMORY_DIR,
    MEMORY_TIDY_FILE_THRESHOLD,
    MEMORY_TIDY_INTERVAL,
    MIN_IMPORTANCE,
    PACKAGE_ROOT,
    PALACE_DB_FILE,
    PALACE_LOCI,
    PALACE_LOCUS_SUMMARIES,
    PLUGINS_DIR,
    PROMPTS_DIR,
    RECENT_SESSION_TURNS,
    REGULAR_TOOL_TIMEOUT,
    RETRIEVAL_TOP_K,
    RL_DIR,
    SCHEDULER_DB_FILE,
    SCHEDULER_DIR,
    SESSIONS_FILE,
    SKILLS_DIR,
    SLEEP_TOKEN_RATIO,
    STAGING_DIR,
    STAGING_TOKEN_THRESHOLD,
    STAGING_TURN_THRESHOLD,
    TOOLS_DIR,
    USER_PLUGINS_DIR,
    _AnthropicFallbackResponse,
    _AnthropicTextBlock,
    _OAIChoice,
    _OAIFunc,
    _OAIMsg,
    _OAIResponse,
    _OAITC,
    _atomic_write_text,
    _is_safe_prompt_version,
    _new_id,
    _with_task_context,
)

# Shared constants/helpers are defined in agent.shared and re-exported here.

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


DEFAULT_SYSTEM_PROMPT = """You are a powerful personal AI agent with tools, memory, and the ability to spawn sub-agents.

## Tools
Your exact tool capabilities are appended later in this prompt. Use only the tools explicitly listed for this agent instance.

## spawn_agent — multi-agent orchestration

Use `spawn_agent` when the task benefits from specialised sub-agents. Two core patterns:
Prefer lead-controlled coordination over free-form sub-agent debate.

### Pattern 1 — Parallel (independent subtasks)
Call `spawn_agent` **multiple times in ONE turn** when subtasks are fully independent.
They run concurrently; you synthesise the results afterward.
Example: "summarise these 3 articles" → spawn 3 summarisers simultaneously.

### Pattern 2 — Pipeline / Lead-Controlled Rendezvous
Call `spawn_agent` **one at a time across multiple turns**, passing only the minimum
summary needed for the next step.
Use when role B needs role A's output, OR when you need a bounded second round on
important disagreements.

**Lead-controlled rendezvous example:**
- Round 1: spawn(proposer, task=question) and/or spawn(critic, task=question)
- Lead: summarize the main disagreements yourself
- Round 2: spawn(follow-up worker, task=lead_summary) only if another round is justified
- Final: synthesise the answer yourself

Default to a bounded number of rounds. Prefer concise summaries over full raw histories.

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
    ConversationTurn,
    FactAssertion,
    LTMCategory,
    LTMEntry,
    LTMStore,
    LocalRetriever,
    MemoryPalace,
    ResolvedFact,
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
from .orchestration import SubtaskResult, SubtaskSpec



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
from .scheduler import (
    ClaimedTask,
    DailyTrigger,
    DeliveryResult,
    DeliveryTarget,
    ExecutionResult,
    IntervalTrigger,
    NewScheduledTask,
    OnceTrigger,
    ScheduledTask,
    SchedulerDelivery,
    SchedulerService,
    SchedulerStore,
    TaskRun,
    TriggerSpec,
    WeeklyTrigger,
)


class _AgentModule(types.ModuleType):
    _FORWARDED = {
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
    }

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._FORWARDED:
            setattr(_shared, name, value)
        super().__setattr__(name, value)


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


sys.modules[__name__].__class__ = _AgentModule
