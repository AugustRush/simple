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
from dataclasses import dataclass
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
    DEFAULT_SUB_AGENT_RETRIES,
    DEFAULT_RESULT_CONTENT_MAX_CHARS,
    DEFAULT_TURN_HOOK_TIMEOUT_SECONDS,
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
from .security.shell import (
    SHELL_BLOCKED_COMMANDS as _SHELL_BLOCKED_COMMANDS,
    SHELL_BLOCKED_PATTERNS as _SHELL_BLOCKED_PATTERNS,
    shell_command_check as _shell_command_check,
    shell_command_is_blocked as _shell_command_is_blocked,
)

# Shared constants/helpers are defined in agent.shared and re-exported here.

# ── Ralph Loop ────────────────────────────────────────────────────────────────
TASKS_DIR = AGENT_HOME / "tasks"
from .ralph import (
    RALPH_COMPLETION_PROMISE,
    RALPH_DEFAULT_MAX_ITERATIONS,
    RALPH_MAX_ITERATIONS,
    RalphIterationResult,
    RalphParseError,
    RalphStoreError,
    RalphTask,
    RalphTaskStore,
    RalphTaskStatus,
    RalphValidationError,
    RalphVerifier,
    VerificationResult,
    VerificationStatus,
    parse_ralph_command,
)


def _save_ralph_task(task: RalphTask) -> None:
    """Atomically persist task state to disk."""
    RalphTaskStore(TASKS_DIR).save(task)


def _load_ralph_task(task_id: str) -> Optional[RalphTask]:
    """Load a previously persisted task, or None if not found."""
    try:
        return RalphTaskStore(TASKS_DIR).load(task_id)
    except (RalphStoreError, RalphValidationError):
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

from .memory.system import (
    AgentRuntimeEvent,
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
    SessionWorkingState,
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
from .tools.builtin_tools import BuiltinTools
from .tools.runtime import MCPClient, ToolDef, ToolRegistry, UserToolCatalog


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
        "DEFAULT_SUB_AGENT_RETRIES",
        "DEFAULT_RESULT_CONTENT_MAX_CHARS",
        "DEFAULT_TURN_HOOK_TIMEOUT_SECONDS",
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
        "main_callback",
        "_missing_feishu_dependency_hint",
        "memory_tidy",
    }:
        from . import cli as cli_module

        return getattr(cli_module, name)
    raise AttributeError(name)


sys.modules[__name__].__class__ = _AgentModule
