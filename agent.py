#!/usr/bin/env python3
"""
Personal Agent - Single-file implementation
Architecture: Memory Palace + Multi-Agent Orchestration + MCP + Self-Evolution
"""

# ─────────────────────────────────────────────────────────────────────────────
# 1. IMPORTS & CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import asyncio
import ast
from contextlib import AsyncExitStack
import math
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import anthropic
import typer
from memory_projection import MemoryIndex, normalize_memory_chapter
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from tool_runtime import BuiltinTools, ToolDef, ToolRegistry

import mcp

# ── Constants ─────────────────────────────────────────────────────────────────
AGENT_HOME = Path.home() / ".agent"
MEMORY_DIR = AGENT_HOME / "memory"
SKILLS_DIR = AGENT_HOME / "skills"
BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
PROMPTS_DIR = AGENT_HOME / "prompts"
RL_DIR = AGENT_HOME / "rl"
CONFIG_FILE = AGENT_HOME / "config.json"
INDEX_FILE = MEMORY_DIR / "INDEX.md"
SESSIONS_FILE = RL_DIR / "sessions.jsonl"
DEFAULT_OUTPUT_DIR = AGENT_HOME / "output"

DEFAULT_MODEL = "claude-opus-4-5"
DEFAULT_MAX_TOKENS = 8192
MEMORY_TIDY_INTERVAL = 3600  # seconds
MEMORY_TIDY_FILE_THRESHOLD = 5
MAX_ORCHESTRATION_DEPTH = 3

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
STAGING_TOKEN_THRESHOLD = 300
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

CONSOLE = Console()

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

# ─────────────────────────────────────────────────────────────────────────────
# 1.5 MODEL CONFIG — provider abstraction (Claude / OpenAI-compatible)
# ─────────────────────────────────────────────────────────────────────────────


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
            "api_key": "$ANTHROPIC_API_KEY",
            "default_model": "claude-opus-4-5",
            "models": ["claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-3-5"],
            "max_tokens": 8192,
        },
        "openai": {
            "api_format": "openai",
            "api_key": "$OPENAI_API_KEY",
            "default_model": "gpt-4o",
            "models": ["gpt-4o", "gpt-4o-mini", "o1-preview"],
            "max_tokens": 4096,
        },
        "deepseek": {
            "api_format": "openai",
            "api_key": "$DEEPSEEK_API_KEY",
            "base_url": "https://api.deepseek.com/v1",
            "default_model": "deepseek-chat",
            "models": ["deepseek-chat", "deepseek-reasoner"],
            "max_tokens": 8192,
        },
        "ollama": {
            "api_format": "openai",
            "api_key": "ollama",
            "base_url": "http://localhost:11434/v1",
            "default_model": "qwen2.5:14b",
            "models": ["qwen2.5:14b", "qwen2.5:7b", "llama3.2"],
            "max_tokens": 4096,
        },
    },
    # ── Memory settings ───────────────────────────────────────────────────
    "memory": {
        "tidy_interval_seconds": MEMORY_TIDY_INTERVAL,
        "tidy_file_threshold": MEMORY_TIDY_FILE_THRESHOLD,
    },
    # ── MCP servers ───────────────────────────────────────────────────────
    "mcp_servers": [],
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
    AGENT_HOME.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False))
        return True  # first run
    return False


class ModelClientFactory:
    """Build the right async API client from provider config."""

    @staticmethod
    def from_config(cfg: dict) -> tuple[Any, str, int]:
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
            CONSOLE.print(
                f"[red]Provider '{active_name}' not found in config.json.\n"
                f"Available providers: {available}\n"
                f"Run: python agent.py config models[/red]"
            )
            raise typer.Exit(1)

        api_format = provider_cfg.get("api_format", "openai")
        raw_key = provider_cfg.get("api_key", "")
        base_url = provider_cfg.get("base_url", None)
        model = cfg.get("model") or provider_cfg.get("default_model", DEFAULT_MODEL)
        max_tokens = cfg.get("max_tokens") or provider_cfg.get(
            "max_tokens", DEFAULT_MAX_TOKENS
        )

        # Resolve api key:
        #   "$ENV_VAR" → read from environment (optional fallback)
        #   anything else → use as literal value (including empty string for no-auth)
        if raw_key.startswith("$"):
            env_name = raw_key[1:]
            api_key = os.environ.get(env_name, "")
            if not api_key:
                CONSOLE.print(
                    f"[red]API key env var '{env_name}' not set "
                    f"(provider: {active_name}). "
                    f"Run: export {env_name}=...[/red]"
                )
                raise typer.Exit(1)
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
                CONSOLE.print(
                    "[red]openai package not installed. Run: pip install openai[/red]"
                )
                raise typer.Exit(1)
            kwargs = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            client = openai_lib.AsyncOpenAI(**kwargs)
        else:
            CONSOLE.print(
                f"[red]Unknown api_format '{api_format}' for provider '{active_name}'[/red]"
            )
            raise typer.Exit(1)

        CONSOLE.print(
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


# ─────────────────────────────────────────────────────────────────────────────
# 2. MEMORY LAYER
# ─────────────────────────────────────────────────────────────────────────────


class MemoryPalace:
    """Facade for all memory operations."""

    def __init__(
        self,
        tidy_interval: int = MEMORY_TIDY_INTERVAL,
        tidy_threshold: int = MEMORY_TIDY_FILE_THRESHOLD,
        base_dir: Path = MEMORY_DIR,
        context_dir: Path = CONTEXT_DIR,
        store: Optional["LTMStore"] = None,
    ):
        self.store = store or LTMStore(context_dir=context_dir, memory_dir=base_dir)
        self.base_dir = self.store.memory_dir
        self.index = MemoryIndex(
            base_dir=self.base_dir,
            loci=PALACE_LOCI,
            aliases=LEGACY_MEMORY_ALIASES,
            summaries=PALACE_LOCUS_SUMMARIES,
            now_fn=_now,
        )
        self._last_tidy: float = 0
        self._files_since_tidy: int = 0
        self._tidy_interval = tidy_interval
        self._tidy_threshold = tidy_threshold

    def write(self, chapter: str, name: str, content: str, append: bool = False):
        chapter = normalize_memory_chapter(chapter, LEGACY_MEMORY_ALIASES)
        self.store.upsert_manual_note(chapter, name, content, append=append)
        self._files_since_tidy += 1
        self.index.update()

    def read(self, chapter: str, name: str) -> str:
        chapter = normalize_memory_chapter(chapter, LEGACY_MEMORY_ALIASES)
        note = self.store.read_manual_note(chapter, name)
        if note:
            return note.content
        entries = self.store.read_entries_for_entity(chapter, name)
        if not entries:
            return ""
        lines = [f"# {chapter}/{name}", ""]
        for entry in entries:
            lines.append(f"- ({entry.memory_type}) {entry.content}")
        return "\n".join(lines)

    def search(self, query: str) -> list[dict]:
        results = []
        for entry in self.store.search_entries(query, limit=20):
            anchor = f"{entry.category}/{entry.entity}" if entry.entity else entry.category
            results.append({"path": anchor, "snippet": entry.content[:120]})
        return results

    def list_chapters(self) -> list[dict]:
        return self.index.list_chapters()

    def read_index(self) -> str:
        self.index.update()
        return self.index.read()

    def should_tidy(self) -> bool:
        if self._files_since_tidy >= self._tidy_threshold:
            return True
        if self._tidy_interval > 0 and self._last_tidy > 0:
            if time.time() - self._last_tidy >= self._tidy_interval:
                return True
        return False

    async def tidy(self, client: anthropic.AsyncAnthropic, model: str):
        """Local maintenance pass: apply retention and rebuild projections."""
        CONSOLE.print("[dim]Tidying memory palace...[/dim]")
        self.store.apply_retention()
        snapshot = self.store.maintenance_snapshot(limit=20)
        if snapshot:
            self.store.add_entry(
                LTMEntry(
                    id=str(uuid.uuid4())[:8],
                    content=snapshot,
                    importance=0.4,
                    category="archive",
                    entity="maintenance",
                    memory_type="maintenance_report",
                    source_session="manual_tidy",
                    confidence=1.0,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
        self.index.update()
        self._last_tidy = time.time()
        self._files_since_tidy = 0
        CONSOLE.print("[dim]Memory tidy complete.[/dim]")


# ─────────────────────────────────────────────────────────────────────────────
# 2.5. CONTEXT MANAGER — LTM + Retrieval + Consolidation
# ─────────────────────────────────────────────────────────────────────────────


class StagingBuffer:
    """Append-only JSONL buffer that persists raw conversation turns to disk.

    Stores only user/assistant plain-text messages (skips tool calls and
    tool results to avoid noise and oversized entries).

    Lifecycle:
      append()        — called after each user input + assistant reply
      read_all()      — returns all staged messages (for LLM extraction)
      clear_all()     — called after successful consolidation
      count()         — number of staged messages

    File: ~/.agent/context/_staging/<session>.jsonl
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        context_dir: Path = CONTEXT_DIR,
        session_id: Optional[str] = None,
    ):
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self.path = path or (context_dir / "_staging" / f"{self.session_id}.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._count = self._load_count()

    def _load_count(self) -> int:
        if not self.path.exists():
            return 0
        with open(self.path, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())

    def append(self, role: str, content: str) -> None:
        """Append a plain-text turn (user or assistant only)."""
        if not content or not content.strip():
            return
        entry = {
            "role": role,
            "content": content.strip(),
            "ts": _now(),
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._count += 1

    def read_all(self) -> list[dict]:
        """Return all staged messages in order."""
        if not self.path.exists():
            return []
        msgs = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except Exception:
                continue
        return msgs

    def count(self) -> int:
        return self._count

    def clear_all(self) -> None:
        """Delete the staging file after successful consolidation."""
        self.path.unlink(missing_ok=True)
        self._count = 0


@dataclass
class LTMEntry:
    """A single long-term memory entry with importance scoring."""

    id: str
    content: str
    importance: float  # 0.0 – 1.0
    category: str
    created_at: str
    updated_at: str
    entity: str = ""
    memory_type: str = "fact"
    scope: str = "global"
    status: str = "active"
    source_session: str = ""
    confidence: float = 1.0

    def decay(self, factor: float = DECAY_FACTOR) -> None:
        self.importance = max(0.0, self.importance * factor)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "importance": self.importance,
            "category": self.category,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "entity": self.entity,
            "memory_type": self.memory_type,
            "scope": self.scope,
            "status": self.status,
            "source_session": self.source_session,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LTMEntry":
        return cls(**d)


@dataclass
class LTMCategory:
    """Metadata for a long-term memory category."""

    name: str
    entry_count: int = 0
    avg_importance: float = 0.0
    last_updated: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "entry_count": self.entry_count,
            "avg_importance": self.avg_importance,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LTMCategory":
        return cls(**d)


class LTMStore:
    """SQLite-backed long-term memory with JSON and markdown projections."""

    def __init__(
        self,
        context_dir: Path = CONTEXT_DIR,
        max_categories: int = MAX_CATEGORIES,
        memory_dir: Path = MEMORY_DIR,
    ):
        self.dir = context_dir
        self.max_categories = max_categories
        self.memory_dir = memory_dir
        self._meta_path = context_dir / "_meta.json"
        self._db_path = context_dir / "palace.db"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        self._meta = {"categories": [], "total_entries": 0}
        self._refresh_indexes()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_items (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    importance REAL NOT NULL,
                    category TEXT NOT NULL,
                    entity TEXT NOT NULL DEFAULT '',
                    memory_type TEXT NOT NULL DEFAULT 'fact',
                    scope TEXT NOT NULL DEFAULT 'global',
                    status TEXT NOT NULL DEFAULT 'active',
                    source_session TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_category_status
                    ON memory_items(category, status);
                CREATE INDEX IF NOT EXISTS idx_memory_entity_status
                    ON memory_items(entity, status);
                """
            )

    def _save_meta(self) -> None:
        self._meta_path.write_text(json.dumps(self._meta, indent=2, ensure_ascii=False))

    @staticmethod
    def normalize_category_name(name: str) -> str:
        """Collapse external category input into a safe, stable storage key."""
        normalized = re.sub(r"[\\/]+", " ", str(name).strip().lower())
        normalized = normalized.replace("..", " ")
        normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "_", normalized)
        normalized = re.sub(r"_+", "_", normalized).strip("._")
        return normalized or "general"

    def _category_path(self, name: str) -> Path:
        safe_name = self.normalize_category_name(name)
        path = (self.dir / f"{safe_name}.json").resolve()
        root = self.dir.resolve()
        if root not in path.parents:
            raise ValueError(f"Category path escaped context dir: {name}")
        return path

    def _is_palace_locus(self, category: str) -> bool:
        return self.normalize_category_name(category) in PALACE_LOCI

    def _normalize_entity(self, entity: str, category: str) -> str:
        entity = self.normalize_category_name(entity)
        if entity:
            return entity
        if self._is_palace_locus(category):
            return "user" if self.normalize_category_name(category) == "identity" else "general"
        return self.normalize_category_name(category)

    def _projection_path(self, category: str, entity: str) -> Path:
        category = self.normalize_category_name(category)
        entity = self._normalize_entity(entity, category)
        return self.memory_dir / category / f"{entity}.md"

    def _row_to_entry(self, row: sqlite3.Row) -> LTMEntry:
        return LTMEntry(
            id=row["id"],
            content=row["content"],
            importance=row["importance"],
            category=row["category"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            entity=row["entity"],
            memory_type=row["memory_type"],
            scope=row["scope"],
            status=row["status"],
            source_session=row["source_session"],
            confidence=row["confidence"],
        )

    @staticmethod
    def _normalize_content_key(content: str) -> str:
        normalized = re.sub(r"\s+", " ", content.strip().lower())
        return normalized

    def _stable_merge_key(self, entry: LTMEntry) -> Optional[str]:
        category = self.normalize_category_name(entry.category)
        entity = self._normalize_entity(entry.entity, category)
        memory_type = (entry.memory_type or "fact").strip().lower()

        if category in {"episodes", "archive", "concepts"}:
            return None
        if category == "tasks":
            return f"{category}|{entity}|{memory_type}"
        if category not in {"identity", "projects", "people", "procedures"}:
            return None

        normalized_content = self._normalize_content_key(entry.content)
        return f"{category}|{entity}|{memory_type}|{normalized_content}"

    def _match_existing_entry_id(
        self, conn: sqlite3.Connection, entry: LTMEntry
    ) -> Optional[str]:
        merge_key = self._stable_merge_key(entry)
        if not merge_key:
            return None

        category = self.normalize_category_name(entry.category)
        entity = self._normalize_entity(entry.entity, category)
        memory_type = (entry.memory_type or "fact").strip().lower()

        if category == "tasks":
            row = conn.execute(
                """
                SELECT id FROM memory_items
                WHERE category = ? AND entity = ? AND memory_type = ?
                  AND status NOT IN ('archived', 'superseded')
                ORDER BY updated_at DESC, id ASC
                LIMIT 1
                """,
                (category, entity, memory_type),
            ).fetchone()
            return row["id"] if row else None

        normalized_content = self._normalize_content_key(entry.content)
        rows = conn.execute(
            """
            SELECT * FROM memory_items
            WHERE category = ? AND entity = ? AND memory_type = ?
              AND status NOT IN ('archived', 'superseded')
            ORDER BY updated_at DESC, id ASC
            """,
            (category, entity, memory_type),
        ).fetchall()
        for row in rows:
            if self._normalize_content_key(row["content"]) == normalized_content:
                return row["id"]
        return None

    def _write_entry_row(self, conn: sqlite3.Connection, entry: LTMEntry) -> None:
        entry.category = self.normalize_category_name(entry.category)
        entry.entity = self._normalize_entity(entry.entity, entry.category)
        entry.memory_type = entry.memory_type or "fact"
        entry.scope = entry.scope or "global"
        entry.status = entry.status or "active"
        entry.source_session = entry.source_session or ""
        entry.confidence = float(entry.confidence or 1.0)
        existing_id = self._match_existing_entry_id(conn, entry)
        if existing_id:
            entry.id = existing_id
        conn.execute(
            """
            INSERT OR REPLACE INTO memory_items (
                id, content, importance, category, entity, memory_type, scope,
                status, source_session, confidence, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.id,
                entry.content,
                float(entry.importance),
                entry.category,
                entry.entity,
                entry.memory_type,
                entry.scope,
                entry.status,
                entry.source_session,
                float(entry.confidence),
                entry.created_at,
                entry.updated_at,
            ),
        )

    def _refresh_indexes(self) -> None:
        previous = {c["name"] for c in self._meta.get("categories", [])}
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT category, COUNT(*) AS entry_count,
                       AVG(importance) AS avg_importance,
                       MAX(updated_at) AS last_updated
                FROM memory_items
                WHERE status = 'active'
                GROUP BY category
                ORDER BY category
                """
            ).fetchall()
        self._meta = {
            "categories": [
                {
                    "name": row["category"],
                    "entry_count": int(row["entry_count"]),
                    "avg_importance": float(row["avg_importance"] or 0.0),
                    "last_updated": row["last_updated"] or "",
                }
                for row in rows
            ],
            "total_entries": sum(int(row["entry_count"]) for row in rows),
        }
        self._save_meta()
        current = {c["name"] for c in self._meta.get("categories", [])}
        for category in previous | current:
            self._sync_category_snapshot(category)
            if self._is_palace_locus(category):
                self._sync_projection(category)

    def _sync_category_snapshot(self, category: str) -> None:
        category = self.normalize_category_name(category)
        entries = self.read_entries(category)
        path = self._category_path(category)
        if not entries:
            path.unlink(missing_ok=True)
            return
        path.write_text(
            json.dumps([e.to_dict() for e in entries], indent=2, ensure_ascii=False)
        )

    def _sync_projection(self, category: str) -> None:
        category = self.normalize_category_name(category)
        if not self._is_palace_locus(category):
            return
        entries = self.read_entries(category)
        grouped: dict[str, list[LTMEntry]] = {}
        for entry in entries:
            grouped.setdefault(entry.entity, []).append(entry)
        for entity, entity_entries in grouped.items():
            path = self._projection_path(category, entity)
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                f"# {category}/{entity}",
                f"_updated: {_now()}_",
                "",
            ]
            for entry in sorted(
                entity_entries, key=lambda e: (e.importance, e.updated_at), reverse=True
            ):
                lines.append(
                    f"- ({entry.memory_type}) {entry.content} "
                    f"[importance={entry.importance:.2f}, status={entry.status}]"
                )
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _remove_category(self, category: str) -> None:
        category = self.normalize_category_name(category)
        with self._connect() as conn:
            conn.execute("DELETE FROM memory_items WHERE category = ?", (category,))
        self._refresh_indexes()

    # ── Category helpers ──────────────────────────────────────────────────────

    def list_categories(self) -> list[LTMCategory]:
        return [LTMCategory.from_dict(c) for c in self._meta.get("categories", [])]

    def category_count(self) -> int:
        return len(self._meta.get("categories", []))

    # ── Entry CRUD ────────────────────────────────────────────────────────────

    def read_entries(self, category: str) -> list[LTMEntry]:
        category = self.normalize_category_name(category)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_items
                WHERE category = ? AND status NOT IN ('archived', 'superseded')
                ORDER BY importance DESC, updated_at DESC, id ASC
                """,
                (category,),
            ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def read_entries_for_entity(self, category: str, entity: str) -> list[LTMEntry]:
        category = self.normalize_category_name(category)
        entity = self._normalize_entity(entity, category)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_items
                WHERE category = ? AND entity = ?
                  AND status NOT IN ('archived', 'superseded')
                ORDER BY importance DESC, updated_at DESC, id ASC
                """,
                (category, entity),
            ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def read_manual_note(self, category: str, entity: str) -> Optional[LTMEntry]:
        category = self.normalize_category_name(category)
        entity = self._normalize_entity(entity, category)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM memory_items
                WHERE category = ? AND entity = ? AND memory_type = 'note'
                  AND status NOT IN ('archived', 'superseded')
                ORDER BY updated_at DESC, id ASC
                LIMIT 1
                """,
                (category, entity),
            ).fetchone()
        return self._row_to_entry(row) if row else None

    def write_entries(self, category: str, entries: list[LTMEntry]) -> None:
        category = self.normalize_category_name(category)
        if not entries:
            self._remove_category(category)
            return

        with self._connect() as conn:
            conn.execute("DELETE FROM memory_items WHERE category = ?", (category,))
            for entry in entries:
                entry.category = category
                self._write_entry_row(conn, entry)
        self._refresh_indexes()

    def add_entry(self, entry: LTMEntry) -> None:
        entry.category = self.normalize_category_name(entry.category)
        entry.entity = self._normalize_entity(entry.entity, entry.category)
        with self._connect() as conn:
            self._write_entry_row(conn, entry)
        self._refresh_indexes()

    def upsert_manual_note(
        self, category: str, entity: str, content: str, append: bool = False
    ) -> LTMEntry:
        category = self.normalize_category_name(category)
        entity = self._normalize_entity(entity, category)
        existing = self.read_manual_note(category, entity)
        if existing:
            existing.content = (
                f"{existing.content.rstrip()}\n{content.strip()}" if append else content
            ).strip()
            existing.updated_at = _now()
            entry = existing
        else:
            entry = LTMEntry(
                id=str(uuid.uuid4())[:8],
                content=content.strip(),
                importance=0.8,
                category=category,
                entity=entity,
                memory_type="note",
                scope="global",
                status="active",
                source_session="manual_memory_write",
                confidence=1.0,
                created_at=_now(),
                updated_at=_now(),
            )
        with self._connect() as conn:
            self._write_entry_row(conn, entry)
        self._refresh_indexes()
        return entry

    def all_entries(self) -> list[LTMEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_items
                WHERE status NOT IN ('archived', 'superseded')
                ORDER BY importance DESC, updated_at DESC, id ASC
                """
            ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def search_entries(
        self,
        query: str,
        categories: Optional[list[str]] = None,
        limit: int = RETRIEVAL_TOP_K,
    ) -> list[LTMEntry]:
        tokens = [
            t
            for t in re.findall(
                r"\b[a-zA-Z\u4e00-\u9fff][a-zA-Z0-9\u4e00-\u9fff]*\b", query.lower()
            )
            if t
        ]
        with self._connect() as conn:
            sql = "SELECT * FROM memory_items WHERE status NOT IN ('archived', 'superseded')"
            params: list[Any] = []
            if categories:
                cats = [self.normalize_category_name(c) for c in categories]
                sql += f" AND category IN ({','.join('?' for _ in cats)})"
                params.extend(cats)
            rows = conn.execute(sql, params).fetchall()

        entries = [self._row_to_entry(row) for row in rows]
        if not tokens:
            return entries[:limit]

        def score(entry: LTMEntry) -> float:
            haystack = " ".join(
                [entry.content.lower(), entry.entity.lower(), entry.category.lower()]
            )
            matches = sum(haystack.count(tok) for tok in tokens)
            return matches * (1.0 + entry.importance)

        ranked = [entry for entry in sorted(entries, key=score, reverse=True) if score(entry) > 0]
        return ranked[:limit]

    # ── Maintenance ───────────────────────────────────────────────────────────

    def apply_decay(self, factor: float = DECAY_FACTOR) -> None:
        """Decay importance of all entries; prune those below MIN_IMPORTANCE."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_items WHERE status NOT IN ('archived', 'superseded')"
            ).fetchall()
            for row in rows:
                entry = self._row_to_entry(row)
                entry.decay(factor)
                entry.updated_at = _now()
                if entry.importance < MIN_IMPORTANCE:
                    conn.execute("DELETE FROM memory_items WHERE id = ?", (entry.id,))
                else:
                    self._write_entry_row(conn, entry)
        self._refresh_indexes()

    def apply_retention(self) -> None:
        """Apply locus-aware retention instead of globally decaying all memory equally."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_items WHERE status NOT IN ('archived', 'superseded')"
            ).fetchall()
            for row in rows:
                entry = self._row_to_entry(row)
                if entry.category in {"episodes"}:
                    entry.decay(DECAY_FACTOR)
                    entry.updated_at = _now()
                    if entry.importance < MIN_IMPORTANCE:
                        conn.execute(
                            "UPDATE memory_items SET status = 'archived', updated_at = ? WHERE id = ?",
                            (_now(), entry.id),
                        )
                    else:
                        self._write_entry_row(conn, entry)
                elif entry.category in {"identity", "projects", "procedures", "people", "concepts"}:
                    continue
                elif entry.category == "tasks":
                    continue
        self._refresh_indexes()

    def maintenance_snapshot(self, limit: int = 20) -> str:
        """Return a text summary derived from the structured store, not markdown projections."""
        entries = self.all_entries()[:limit]
        lines = []
        for entry in entries:
            anchor = f"{entry.category}/{entry.entity}" if entry.entity else entry.category
            lines.append(f"- [{anchor}] ({entry.memory_type}) {entry.content}")
        return "\n".join(lines)

    def merge_categories(self, cat_a: str, cat_b: str, merged_name: str) -> None:
        """Merge cat_a and cat_b into merged_name, delete originals."""
        cat_a = self.normalize_category_name(cat_a)
        cat_b = self.normalize_category_name(cat_b)
        merged_name = self.normalize_category_name(merged_name)
        with self._connect() as conn:
            conn.execute(
                "UPDATE memory_items SET category = ?, updated_at = ? WHERE category IN (?, ?)",
                (merged_name, _now(), cat_a, cat_b),
            )
        self._refresh_indexes()


class LocalRetriever:
    """BM25-lite retrieval with importance boosting. Pure stdlib, no external deps."""

    K1: float = 1.5
    B: float = 0.75

    @staticmethod
    def tokenize(text: str) -> list[str]:
        """Lowercase tokenizer: splits on non-alphanumeric, keeps CJK chars."""
        return re.findall(
            r"\b[a-zA-Z\u4e00-\u9fff][a-zA-Z0-9\u4e00-\u9fff]*\b", text.lower()
        )

    def score(
        self, query: str, entries: list[LTMEntry]
    ) -> list[tuple[LTMEntry, float]]:
        """Score entries against query using BM25-lite + importance boost."""
        if not entries:
            return []
        query_terms = self.tokenize(query)
        if not query_terms:
            return [(e, e.importance) for e in entries]

        N = len(entries)
        df: dict[str, int] = {}
        tokenized: list[list[str]] = []

        for entry in entries:
            tokens = self.tokenize(entry.content)
            tokenized.append(tokens)
            for term in set(tokens):
                df[term] = df.get(term, 0) + 1

        avg_dl = sum(len(t) for t in tokenized) / N if N else 1.0

        scored: list[tuple[LTMEntry, float]] = []
        for i, entry in enumerate(entries):
            tokens = tokenized[i]
            dl = len(tokens)
            tf_map: dict[str, int] = {}
            for t in tokens:
                tf_map[t] = tf_map.get(t, 0) + 1

            bm25 = 0.0
            for term in query_terms:
                if term not in tf_map:
                    continue
                idf = math.log(
                    (N - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5) + 1
                )
                tf = tf_map[term]
                tf_norm = (
                    tf
                    * (self.K1 + 1)
                    / (tf + self.K1 * (1 - self.B + self.B * dl / avg_dl))
                )
                bm25 += idf * tf_norm

            # Importance acts as a multiplicative boost
            scored.append((entry, bm25 * (1.0 + entry.importance)))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def retrieve(
        self, query: str, entries: list[LTMEntry], top_k: int = RETRIEVAL_TOP_K
    ) -> list[LTMEntry]:
        """Return top-K most relevant entries (score > 0 only)."""
        scored = self.score(query, entries)
        return [entry for entry, s in scored[:top_k] if s > 0]


class ConsolidationEngine:
    """LLM-driven context consolidation — the 'sleep' mechanism.

    Triggered when working memory exceeds SLEEP_TOKEN_RATIO of max_tokens.
    Extracts structured facts from conversation, stores in LTM, applies decay,
    and compresses ctx.messages to the most recent entries.
    """

    def __init__(
        self,
        store: LTMStore,
        max_categories: int = MAX_CATEGORIES,
        decay_factor: float = DECAY_FACTOR,
        sleep_token_ratio: float = SLEEP_TOKEN_RATIO,
        keep_last_messages: int = 6,
    ):
        self.store = store
        self.max_categories = max_categories
        self.decay_factor = decay_factor
        self.sleep_token_ratio = sleep_token_ratio
        self.keep_last_messages = keep_last_messages

    # ── Trigger ───────────────────────────────────────────────────────────────

    def estimate_tokens(self, messages: list[dict]) -> int:
        """Rough token estimate: total chars / CHARS_PER_TOKEN."""
        total_chars = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total_chars += len(
                            str(block.get("text", "") or block.get("content", ""))
                        )
        return total_chars // CHARS_PER_TOKEN

    def should_sleep(self, messages: list[dict], max_tokens: int) -> bool:
        return self.estimate_tokens(messages) >= int(
            max_tokens * self.sleep_token_ratio
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
    ) -> list[dict]:
        """One sleep cycle: extract → classify → store → decay → compress.

        Source priority for LLM extraction:
          1. staging buffer (if non-empty) — full, clean conversation history
          2. ctx.messages fallback          — used only when staging is absent
        After extraction the staging buffer is cleared.
        """
        if keep_last is None:
            keep_last = self.keep_last_messages
        CONSOLE.print("[dim]💤 Context consolidation (sleep)...[/dim]")

        # Choose extraction source
        staged = staging.read_all() if staging else []
        source = staged if staged else messages
        conv_text = self._format_messages_for_llm(source)
        source_label = (
            f"staging ({len(staged)} turns)"
            if staged
            else f"messages ({len(messages)})"
        )

        prompt = (
            f"Analyze this conversation and extract important facts worth remembering.\n"
            f"For each item output JSON on its own line (no markdown fences):\n"
            f'{{"locus": "<one of {", ".join(PALACE_LOCI)}>", "entity": "<anchor>", '
            f'"memory_type": "<type>", "content": "<fact>", "importance": <0.1-1.0>, "confidence": <0.1-1.0>}}\n\n'
            f"Rules:\n"
            f"- Use only the fixed loci listed above; never invent new top-level loci\n"
            f"- identity: user preferences/style/constraints\n"
            f"- projects: project decisions/state/risks\n"
            f"- people: person-specific facts\n"
            f"- concepts: durable domain knowledge\n"
            f"- tasks: commitments, next steps, open loops\n"
            f"- procedures: repeatable workflows and preferred methods\n"
            f"- archive: only if the memory is historical or superseded\n"
            f"- Do not output episodes; session summary is generated separately\n"
            f"- Be selective: max 8 items, 1-2 sentences each\n\n"
            f"Conversation ({source_label}):\n{conv_text[:3000]}"
        )

        try:
            entries = [
                self._build_episode_entry(source, staging.session_id if staging else "")
            ]
            if api_format == "anthropic":
                resp = await client.messages.create(
                    model=model,
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = resp.content[0].text
            else:
                resp = await client.chat.completions.create(
                    model=model,
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = resp.choices[0].message.content or ""

            entries.extend(self._parse_entries(raw))
            for entry in entries:
                await self._ensure_category_fits(entry.category, client, model, api_format)
                self.store.add_entry(entry)

            self.store.apply_retention()

            # Clear staging after successful extraction
            if staging:
                staging.clear_all()

            CONSOLE.print(
                f"[dim]💤 Stored {len(entries)} entries from {source_label}. "
                f"Categories: {self.store.category_count()}/{self.max_categories}[/dim]"
            )
        except Exception as e:
            CONSOLE.print(f"[dim]Sleep extraction error: {e}[/dim]")

        compressed = messages[-keep_last:] if len(messages) > keep_last else messages
        CONSOLE.print(
            f"[dim]💤 Messages compressed: {len(messages)} → {len(compressed)}[/dim]"
        )
        return compressed

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _format_messages_for_llm(self, messages: list[dict]) -> str:
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
        return "\n\n".join(lines)

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
                category = (
                    data.get("locus")
                    or data.get("category")
                    or "concepts"
                )
                normalized_category = self.store.normalize_category_name(category)
                entity = str(data.get("entity", "")).strip()
                if normalized_category not in PALACE_LOCI:
                    entity = entity or normalized_category
                    normalized_category = "concepts"
                entries.append(
                    LTMEntry(
                        id=str(uuid.uuid4())[:8],
                        content=content,
                        importance=float(data.get("importance", 0.5)),
                        category=normalized_category,
                        created_at=_now(),
                        updated_at=_now(),
                        entity=entity,
                        memory_type=str(data.get("memory_type", "fact")).strip() or "fact",
                        source_session=str(data.get("source_session", "")).strip(),
                        confidence=float(data.get("confidence", 1.0)),
                    )
                )
            except Exception:
                continue
        return entries

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
            id=str(uuid.uuid4())[:8],
            content=summary,
            importance=0.7,
            category="episodes",
            entity=session_id or "session",
            memory_type="session_summary",
            source_session=session_id,
            confidence=1.0,
            created_at=_now(),
            updated_at=_now(),
        )

    async def _ensure_category_fits(
        self, category: str, client: Any, model: str, api_format: str
    ) -> None:
        """Normalize free-form categories into the fixed palace loci."""
        normalized = self.store.normalize_category_name(category)
        if normalized in PALACE_LOCI:
            return


class ContextManager:
    """Orchestrates LTM storage, retrieval, and consolidation.

    Trigger rules (all require _needs_consolidation == True):
      1. Token-ratio trigger  — working memory > token_ratio × max_tokens
      2. Idle trigger         — no activity for idle_seconds (background task)
      3. Session-end trigger  — explicit call when the interactive loop exits
    After each sleep() the flag is cleared; mark_activity() re-arms it.

    Staging buffer: every user/assistant turn is appended to a per-session
    JSONL file under _staging/. This ensures consolidation always has a
    complete source even if the session ends before the token threshold fires,
    without mixing raw turns across unrelated sessions. Buffer is cleared after
    each sleep.
    """

    def __init__(
        self,
        store: LTMStore,
        retriever: LocalRetriever,
        consolidation: ConsolidationEngine,
        idle_seconds: int = 300,
        min_messages: int = 4,
        staging: Optional[StagingBuffer] = None,
        staging_turn_threshold: int = STAGING_TURN_THRESHOLD,
        staging_token_threshold: int = STAGING_TOKEN_THRESHOLD,
    ):
        self.store = store
        self.retriever = retriever
        self.consolidation = consolidation
        self.idle_seconds = idle_seconds
        self.min_messages = min_messages
        self.staging_turn_threshold = staging_turn_threshold
        self.staging_token_threshold = staging_token_threshold
        self.staging: StagingBuffer = staging or StagingBuffer()
        self._needs_consolidation: bool = False
        self._last_activity: float = 0.0
        self._lock = threading.RLock()
        self._jobs: deque[dict[str, Any]] = deque()
        self._processing_job = False

    # ── Activity tracking ─────────────────────────────────────────────────────

    def mark_activity(self) -> None:
        """Call after each user message to arm consolidation and reset idle timer."""
        with self._lock:
            self._last_activity = time.time()
            self._needs_consolidation = True

    def idle_elapsed(self) -> float:
        """Seconds since last activity (0 if never active)."""
        with self._lock:
            if self._last_activity == 0.0:
                return 0.0
            return time.time() - self._last_activity

    # ── Trigger checks ────────────────────────────────────────────────────────

    def should_sleep(self, messages: list[dict], max_tokens: int) -> bool:
        """Token-ratio trigger: only fires when dirty and messages are sufficient."""
        with self._lock:
            if not self._needs_consolidation:
                return False
        if len(messages) < self.min_messages:
            return False
        return self.consolidation.should_sleep(messages, max_tokens)

    def should_idle_sleep(self, messages: list[dict]) -> bool:
        """Idle trigger: fires when dirty, sufficient messages, and idle long enough."""
        with self._lock:
            if not self._needs_consolidation:
                return False
        if len(messages) < self.min_messages:
            return False
        return self.idle_elapsed() >= self.idle_seconds

    def should_session_end_sleep(self) -> bool:
        """Session-end trigger: fires when staging has unprocessed content."""
        with self._lock:
            return self._needs_consolidation and self.staging.count() > 0

    def should_enqueue_consolidation(self) -> bool:
        """Queue consolidation based on staged content volume, not working-memory size."""
        with self._lock:
            if not self._needs_consolidation:
                return False
            staged = self.staging.read_all()
        if not staged:
            return False
        if len(staged) >= self.staging_turn_threshold:
            return True
        return self.consolidation.estimate_tokens(staged) >= self.staging_token_threshold

    def enqueue_consolidation(self, reason: str) -> None:
        """Queue one consolidation job if there is staged work pending."""
        with self._lock:
            if self.staging.count() == 0:
                return
            if self._jobs:
                return
            self._jobs.append(
                {
                    "reason": reason,
                    "session_id": self.staging.session_id,
                    "queued_at": _now(),
                }
            )

    def next_job(self, pop: bool = False) -> Optional[dict]:
        with self._lock:
            if not self._jobs:
                if self._needs_consolidation and self.staging.count() > 0:
                    self._jobs.append(
                        {
                            "reason": "idle",
                            "session_id": self.staging.session_id,
                            "queued_at": _now(),
                        }
                    )
                else:
                    return None
            return self._jobs.popleft() if pop else dict(self._jobs[0])

    def pending_jobs(self) -> int:
        with self._lock:
            return len(self._jobs)

    def should_process_jobs(self) -> bool:
        return self.pending_jobs() > 0 and self.idle_elapsed() >= self.idle_seconds

    def should_compact_messages(self, messages: list[dict], max_tokens: int) -> bool:
        """Front-end compaction keeps working memory bounded without network calls."""
        if len(messages) < self.min_messages:
            return False
        return self.consolidation.should_sleep(messages, max_tokens)

    def compact_messages(self, messages: list[dict]) -> list[dict]:
        keep_last = self.consolidation.keep_last_messages
        return messages[-keep_last:] if len(messages) > keep_last else messages

    # ── Retrieval ─────────────────────────────────────────────────────────────

    @staticmethod
    def _route_categories(query: str) -> list[str]:
        q = query.lower()
        routes: list[str] = []
        if any(tok in q for tok in ["刚才", "刚刚", "上次", "之前", "recent", "earlier"]):
            routes.append("episodes")
        if any(tok in q for tok in ["偏好", "喜欢", "风格", "prefer", "preference"]):
            routes.append("identity")
        if any(tok in q for tok in ["项目", "project", "repo", "仓库"]):
            routes.extend(["projects", "tasks"])
        if any(tok in q for tok in ["任务", "todo", "待办", "next step", "open loop"]):
            routes.append("tasks")
        if any(tok in q for tok in ["流程", "通常怎么", "workflow", "procedure"]):
            routes.append("procedures")
        if any(tok in q for tok in ["人", "person", "people", "同事"]):
            routes.append("people")
        if any(tok in q for tok in ["概念", "是什么", "what is", "define", "知识"]):
            routes.append("concepts")
        seen = []
        for cat in routes:
            if cat not in seen:
                seen.append(cat)
        return seen

    def retrieve_ltm_context(self, query: str, top_k: int = RETRIEVAL_TOP_K) -> str:
        """Return top-K relevant LTM entries as an injectable string."""
        categories = self._route_categories(query)
        top = self.store.search_entries(query, categories=categories or None, limit=top_k)
        if not top and categories:
            top = self.store.search_entries(query, categories=None, limit=top_k)
        if not top:
            return ""
        lines = ["## Retrieved Context (from long-term memory)"]
        for e in top:
            anchor = f"{e.category}/{e.entity}" if e.entity else e.category
            lines.append(f"- [{anchor}] {e.content}")
        return "\n".join(lines)

    def _recent_session_context(self, limit: int = RECENT_SESSION_TURNS) -> str:
        """Return the most recent staged turns for explicit current-session recall."""
        staged = self.staging.read_all()
        if not staged:
            return ""
        lines = ["## Current Session (not yet consolidated)"]
        for msg in staged[-limit:]:
            role = str(msg.get("role", "unknown")).upper()
            content = str(msg.get("content", "")).strip()
            if content:
                lines.append(f"- {role}: {content}")
        return "\n".join(lines) if len(lines) > 1 else ""

    def retrieve_context(self, query: str, top_k: int = RETRIEVAL_TOP_K) -> str:
        """Return explicit context lookup results across active session and LTM."""
        sections = []
        recent = self._recent_session_context()
        if recent:
            sections.append(recent)
        ltm = self.retrieve_ltm_context(query, top_k=top_k)
        if ltm:
            sections.append(ltm)
        return "\n\n".join(sections)

    # ── Consolidation ─────────────────────────────────────────────────────────

    async def sleep(
        self,
        messages: list[dict],
        client: Any,
        model: str,
        api_format: str = "anthropic",
    ) -> list[dict]:
        """Run one sleep cycle (uses staging as source), then clear dirty flag."""
        try:
            return await self.consolidation.consolidate(
                messages, client, model, api_format, staging=self.staging
            )
        finally:
            with self._lock:
                self._needs_consolidation = False

    async def process_one_job(
        self,
        client: Any,
        model: str,
        api_format: str = "anthropic",
        extractor: Optional[Callable[..., list[Any]]] = None,
    ) -> bool:
        """Process one queued consolidation job without mutating working memory."""
        with self._lock:
            if self._processing_job:
                return False
            self._processing_job = True
        job = self.next_job(pop=True)
        if job is None:
            with self._lock:
                self._processing_job = False
            return False

        try:
            with self._lock:
                staged = self.staging.read_all()
            if not staged:
                with self._lock:
                    self._needs_consolidation = False
                return False

            if extractor is not None:
                entries = [
                    self.consolidation._build_episode_entry(
                        staged, self.staging.session_id
                    )
                ]
                extracted = extractor(staged, job)
                for item in extracted or []:
                    if isinstance(item, LTMEntry):
                        entries.append(item)
                    elif isinstance(item, dict):
                        lines = json.dumps(item, ensure_ascii=False)
                        entries.extend(self.consolidation._parse_entries(lines))
                for entry in entries:
                    self.store.add_entry(entry)
                self.store.apply_retention()
                with self._lock:
                    self.staging.clear_all()
                    self._needs_consolidation = False
                return True

            await self.consolidation.consolidate(
                [],
                client,
                model,
                api_format,
                staging=self.staging,
            )
            with self._lock:
                self._needs_consolidation = False
            return True
        finally:
            with self._lock:
                self._processing_job = False

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        cats = self.store.list_categories()
        return {
            "categories": len(cats),
            "total_entries": sum(c.entry_count for c in cats),
            "category_names": [c.name for c in cats],
            "max_categories": self.store.max_categories,
            "needs_consolidation": self._needs_consolidation,
            "queued_jobs": self.pending_jobs(),
            "staged_turns": self.staging.count(),
            "idle_elapsed_s": round(self.idle_elapsed()),
            "idle_threshold_s": self.idle_seconds,
        }


class BackgroundMemoryWorker:
    """Threaded worker that processes queued memory jobs off the interactive path."""

    def __init__(
        self,
        ctx_mgr: ContextManager,
        client: Any,
        model: str,
        api_format: str,
        poll_seconds: float = 1.0,
    ):
        self.ctx_mgr = ctx_mgr
        self.client = client
        self.model = model
        self.api_format = api_format
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            while not self._stop.is_set():
                try:
                    if self.ctx_mgr.should_process_jobs():
                        loop.run_until_complete(
                            self.ctx_mgr.process_one_job(
                                self.client,
                                self.model,
                                api_format=self.api_format,
                            )
                        )
                except Exception as e:
                    CONSOLE.print(f"[dim]Background consolidation error: {e}[/dim]")
                self._stop.wait(self.poll_seconds)
        finally:
            loop.close()


# ─────────────────────────────────────────────────────────────────────────────
# 3. TOOLS / SKILLS / MCP
# ─────────────────────────────────────────────────────────────────────────────


class MCPClient:
    """Connect to external MCP servers and inject tools into registry."""

    def __init__(self, registry: ToolRegistry):
        self.registry = registry
        self._sessions = []
        self._stack = AsyncExitStack()
        self._configured_servers = 0
        self._connected_servers = 0
        self._failed_servers = 0
        self._registered_tools = 0

    @staticmethod
    def _safe_name(value: str) -> str:
        name = re.sub(r"[^0-9a-zA-Z_]+", "_", value.strip().lower())
        return name.strip("_") or "mcp"

    async def connect_from_config(self, config: dict, extra_env: dict[str, str] | None = None):
        self._extra_env = extra_env or {}
        self._configured_servers = len(config.get("mcp_servers", []) or [])
        mcp_servers = config.get("mcp_servers", [])
        for server_cfg in mcp_servers:
            try:
                await self._connect_server(server_cfg)
                self._connected_servers += 1
            except Exception as e:
                self._failed_servers += 1
                CONSOLE.print(
                    f"[yellow]MCP server connect failed ({server_cfg.get('name', '?')}): {e}[/yellow]"
                )

    async def _connect_server(self, cfg: dict):
        command = str(cfg.get("command", "")).strip()
        if not command:
            raise ValueError("MCP server config requires 'command'")

        server_name = self._safe_name(
            str(cfg.get("name") or Path(command).name or "mcp")
        )
        # Merge: agent-level env < server-specific env (server wins)
        server_env = dict(cfg.get("env", {}) or {})
        merged_env = {**self._extra_env, **server_env} if self._extra_env or server_env else None
        params = mcp.StdioServerParameters(
            command=command,
            args=list(cfg.get("args", []) or []),
            env=merged_env or None,
            cwd=cfg.get("cwd"),
        )
        read_stream, write_stream = await self._stack.enter_async_context(
            mcp.stdio_client(params)
        )
        session = await self._stack.enter_async_context(
            mcp.ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        self._sessions.append({"name": server_name, "session": session})

        tools_result = await session.list_tools()
        for tool in getattr(tools_result, "tools", []):
            self._register_tool(server_name, session, tool)

    def _register_tool(self, server_name: str, session: Any, tool: Any) -> None:
        original_name = str(getattr(tool, "name", "")).strip()
        if not original_name:
            return
        registered_name = f"mcp_{server_name}_{self._safe_name(original_name)}"
        description = getattr(tool, "description", None) or f"MCP tool {original_name}"
        parameters = getattr(tool, "inputSchema", None) or {
            "type": "object",
            "properties": {},
            "required": [],
        }

        async def _call_mcp_tool(**kwargs):
            result = await session.call_tool(original_name, arguments=kwargs or None)
            text_blocks = []
            for block in getattr(result, "content", []) or []:
                block_type = getattr(block, "type", "")
                if block_type == "text":
                    text_blocks.append(getattr(block, "text", ""))
                else:
                    text_blocks.append(str(block))
            return {
                "ok": not bool(getattr(result, "isError", False)),
                "server": server_name,
                "tool": original_name,
                "text": "\n".join(b for b in text_blocks if b).strip(),
                "structured": getattr(result, "structuredContent", None),
            }

        self.registry.register(
            registered_name,
            description,
            parameters,
            _call_mcp_tool,
            source=f"mcp:{server_name}",
        )
        self._registered_tools += 1

    def status_summary(self) -> dict[str, Any]:
        return {
            "configured_servers": self._configured_servers,
            "connected_servers": self._connected_servers,
            "failed_servers": self._failed_servers,
            "registered_tools": self._registered_tools,
        }

    async def close(self) -> None:
        await self._stack.aclose()


@dataclass
class SkillBundle:
    id: str
    name: str
    description: str
    path: Path
    source: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)
    supporting_files: list[str] = field(default_factory=list)
    user_invocable: bool = True
    disable_model_invocation: bool = False


@dataclass
class ExplicitSkillRequest:
    skill_ref: str
    remaining_text: str = ""


def _parse_frontmatter_value(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""
    low = value.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if value[0] in ('"', "'"):
        try:
            return ast.literal_eval(value)
        except Exception:
            return value.strip("'\"")
    if value[0] in "[{(":
        try:
            return ast.literal_eval(value)
        except Exception:
            return value
    return value


def parse_skill_markdown(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text.strip()

    lines = text.splitlines()
    try:
        closing_index = lines[1:].index("---") + 1
    except ValueError:
        return {}, text.strip()

    metadata: dict[str, Any] = {}
    for line in lines[1:closing_index]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        metadata[key.strip()] = _parse_frontmatter_value(raw_value)
    body = "\n".join(lines[closing_index + 1 :]).strip()
    return metadata, body


def parse_explicit_skill_request(text: str) -> Optional[ExplicitSkillRequest]:
    stripped = text.strip()
    if not stripped:
        return None

    slash_match = re.match(r"^/skill\s+([^\s]+)(?:\s+(.*))?$", stripped, re.IGNORECASE)
    if slash_match:
        return ExplicitSkillRequest(
            skill_ref=slash_match.group(1),
            remaining_text=(slash_match.group(2) or "").strip(),
        )

    slash_direct = re.match(r"^/([^\s/][^\s]*)(?:\s+(.*))?$", stripped)
    if slash_direct:
        return ExplicitSkillRequest(
            skill_ref=slash_direct.group(1),
            remaining_text=(slash_direct.group(2) or "").strip(),
        )

    natural_match = re.match(
        r"^(?:please\s+)?(?:use|activate|run)\s+([^\s,.:：，]+)(?:\s+(.*))?$",
        stripped,
        re.IGNORECASE,
    )
    if natural_match:
        return ExplicitSkillRequest(
            skill_ref=natural_match.group(1),
            remaining_text=(natural_match.group(2) or "").strip(),
        )

    chinese_match = re.match(
        r"^(?:请)?(?:使用|启用)\s*([^\s,.:：，]+)(?:\s+(.*))?$",
        stripped,
    )
    if chinese_match:
        return ExplicitSkillRequest(
            skill_ref=chinese_match.group(1),
            remaining_text=(chinese_match.group(2) or "").strip(),
        )

    return None


def prepare_user_message_for_skills(
    user_message: str, skill_catalog: SkillCatalog
) -> tuple[str, list[str]]:
    parsed = parse_explicit_skill_request(user_message)
    if parsed is None:
        return user_message, []
    bundle = skill_catalog.get(parsed.skill_ref)
    if bundle is None or not bundle.user_invocable:
        return user_message, []
    normalized = parsed.remaining_text.strip()
    if not normalized:
        normalized = (
            f"The user explicitly requested the skill '{bundle.id}'. "
            "Activate it and briefly explain how you will apply it."
        )
    return normalized, [bundle.id]


class SkillCatalog:
    """Load skill bundles from user and built-in skill directories."""

    def __init__(self, user_root: Optional[Path] = None, builtin_root: Optional[Path] = None):
        self.user_root = user_root or SKILLS_DIR
        self.builtin_root = builtin_root or BUILTIN_SKILLS_DIR
        self._skills: dict[str, SkillBundle] = {}
        self._aliases: dict[str, str] = {}

    def load_all(self) -> None:
        self.user_root.mkdir(parents=True, exist_ok=True)
        self._skills.clear()
        self._aliases.clear()
        self._load_root(self.builtin_root, source="builtin")
        self._load_root(self.user_root, source="user")

    def _load_root(self, root: Path, *, source: str) -> None:
        if not root.exists():
            return
        for skill_file in sorted(root.rglob("SKILL.md")):
            bundle = self._read_bundle(skill_file, root=root, source=source)
            if bundle is None:
                continue
            self._skills[bundle.id] = bundle
        self._rebuild_aliases()

    def _read_bundle(self, skill_file: Path, *, root: Path, source: str) -> Optional[SkillBundle]:
        try:
            raw_text = skill_file.read_text(encoding="utf-8")
        except Exception as e:
            CONSOLE.print(f"[yellow]Failed to read skill {skill_file}: {e}[/yellow]")
            return None

        metadata, body = parse_skill_markdown(raw_text)
        bundle_dir = skill_file.parent
        bundle_id = bundle_dir.relative_to(root).as_posix()
        if not bundle_id or bundle_id == ".":
            bundle_id = bundle_dir.name
        supporting_files = sorted(
            p.relative_to(bundle_dir).as_posix()
            for p in bundle_dir.rglob("*")
            if p.is_file() and p.name != "SKILL.md"
        )
        return SkillBundle(
            id=bundle_id,
            name=str(metadata.get("name") or bundle_dir.name),
            description=str(metadata.get("description") or ""),
            path=bundle_dir,
            source=source,
            body=body,
            metadata=metadata,
            supporting_files=supporting_files,
            user_invocable=bool(metadata.get("user-invocable", True)),
            disable_model_invocation=bool(metadata.get("disable-model-invocation", False)),
        )

    def _rebuild_aliases(self) -> None:
        self._aliases.clear()
        counts: dict[str, int] = {}
        for skill_id in self._skills:
            leaf = skill_id.rsplit("/", 1)[-1]
            counts[leaf] = counts.get(leaf, 0) + 1
        for skill_id, bundle in self._skills.items():
            self._aliases[skill_id] = skill_id
            leaf = skill_id.rsplit("/", 1)[-1]
            if counts.get(leaf, 0) == 1:
                self._aliases[leaf] = skill_id
            self._aliases[bundle.name] = skill_id

    def reload(self) -> None:
        self.load_all()

    def get(self, skill_ref: str) -> Optional[SkillBundle]:
        resolved = self.resolve_ref(skill_ref)
        if resolved is None:
            return None
        return self._skills.get(resolved)

    def resolve_ref(self, skill_ref: str) -> Optional[str]:
        ref = skill_ref.strip()
        if not ref:
            return None
        if ref in self._skills:
            return ref
        return self._aliases.get(ref)

    def list_skills(self) -> list[SkillBundle]:
        return [self._skills[key] for key in sorted(self._skills)]

    def summary_lines(self) -> list[str]:
        if not self._skills:
            return []
        lines = [
            "## Available Skills",
            "Available skills:",
            "Skills are instruction bundles loaded on demand. Use activate_skill only when a skill is relevant.",
        ]
        for bundle in self.list_skills():
            lines.append(
                "- "
                f"{bundle.id} ({bundle.source}; user-invocable={'yes' if bundle.user_invocable else 'no'}; "
                f"model-invocable={'no' if bundle.disable_model_invocation else 'yes'}): "
                f"{bundle.description or 'No description'}"
            )
        return lines

    def register_tools(self, registry: ToolRegistry) -> None:
        async def activate_skill(skill_name: str) -> dict[str, Any]:
            bundle = self.get(skill_name)
            if bundle is None:
                return {"ok": False, "error": f"Skill '{skill_name}' not found"}
            if bundle.disable_model_invocation:
                return {
                    "ok": False,
                    "error": f"Skill '{bundle.id}' cannot be activated by the model",
                }
            return self._activation_payload(bundle)

        def list_skill_files(skill_name: str) -> dict[str, Any]:
            bundle = self.get(skill_name)
            if bundle is None:
                return {"ok": False, "error": f"Skill '{skill_name}' not found"}
            return {
                "ok": True,
                "skill": bundle.id,
                "bundle_root": str(bundle.path),
                "files": bundle.supporting_files,
            }

        def read_skill_file(skill_name: str, path: str) -> dict[str, Any]:
            bundle = self.get(skill_name)
            if bundle is None:
                return {"ok": False, "error": f"Skill '{skill_name}' not found"}
            rel_path = Path(path)
            if rel_path.is_absolute():
                return {"ok": False, "error": "Skill file paths must be relative to the skill bundle"}
            target = (bundle.path / rel_path).resolve(strict=False)
            if target != bundle.path and bundle.path not in target.parents:
                return {"ok": False, "error": "Requested path escapes the skill bundle"}
            if not target.exists() or not target.is_file():
                return {"ok": False, "error": f"Skill file '{path}' not found"}
            return {
                "ok": True,
                "skill": bundle.id,
                "path": rel_path.as_posix(),
                "bundle_root": str(bundle.path),
                "content": target.read_text(encoding="utf-8"),
            }

        registry.register(
            "activate_skill",
            "Load a skill bundle's full instructions and supporting-file index.",
            {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Skill id, unique leaf name, or display name",
                    }
                },
                "required": ["skill_name"],
            },
            activate_skill,
            replace=True,
            source="runtime:skill",
        )
        registry.register(
            "list_skill_files",
            "List supporting files inside a skill bundle.",
            {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Skill id, unique leaf name, or display name",
                    }
                },
                "required": ["skill_name"],
            },
            list_skill_files,
            replace=True,
            source="runtime:skill",
        )
        registry.register(
            "read_skill_file",
            "Read a supporting file from a skill bundle.",
            {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Skill id, unique leaf name, or display name",
                    },
                    "path": {
                        "type": "string",
                        "description": "Relative path inside the skill bundle",
                    },
                },
                "required": ["skill_name", "path"],
            },
            read_skill_file,
            replace=True,
            source="runtime:skill",
        )

    def _activation_payload(self, bundle: SkillBundle) -> dict[str, Any]:
        return {
            "ok": True,
            "skill": {
                "id": bundle.id,
                "name": bundle.name,
                "description": bundle.description,
                "source": bundle.source,
                "bundle_root": str(bundle.path),
                "instructions": bundle.body,
                "supporting_files": bundle.supporting_files,
                "metadata": bundle.metadata,
            },
        }

    def activation_text(self, skill_ref: str, *, explicit: bool = False) -> Optional[str]:
        bundle = self.get(skill_ref)
        if bundle is None:
            return None
        lines = [f"Skill `{bundle.id}` ({bundle.name}) is active for this turn."]
        if explicit:
            lines.append("This skill was explicitly requested by the user and must be followed.")
        if bundle.description:
            lines.append(f"Description: {bundle.description}")
        lines.append(f"Bundle root: {bundle.path}")
        if bundle.supporting_files:
            lines.append("Supporting files available on demand:")
            lines.extend(f"- {path}" for path in bundle.supporting_files)
        else:
            lines.append("Supporting files available on demand: none")
        lines.append("")
        lines.append(bundle.body or "(No instructions in SKILL.md body)")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 4. AGENT CORE
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class AgentContext:
    """State for a single agent instance."""

    agent_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    role: str = "assistant"
    messages: list[dict] = field(default_factory=list)
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    tools_enabled: bool = True
    depth: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass
class AgentResult:
    agent_id: str
    content: str
    tool_calls_made: list[str] = field(default_factory=list)
    error: Optional[str] = None


class BaseAgent:
    """Core agent: streams Claude, handles tool_use loop."""

    def __init__(
        self,
        client: Any,
        registry: ToolRegistry,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        api_format: str = "anthropic",
    ):
        self.client = client
        self.registry = registry
        self.api_format = api_format
        self.model = model
        self.max_tokens = max_tokens
        self.context_manager: Optional[ContextManager] = None

    def set_model(self, model: str) -> None:
        """Switch the model used for subsequent calls."""
        self.model = model

    # ── Format-aware API helpers ──────────────────────────────────────────

    def _tools_for_api(self, tools: list[dict]) -> Any:
        """Convert tools to the right format; return NOT_GIVEN/None if empty."""
        if not tools:
            return anthropic.NOT_GIVEN if self.api_format == "anthropic" else None
        if self.api_format == "openai":
            # Convert Anthropic tool schema → OpenAI function-calling format
            return [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                }
                for t in tools
            ]
        return tools  # anthropic format as-is

    def _inject_system(self, messages: list[dict], system_prompt: str) -> list[dict]:
        """For OpenAI format, prepend system as first message."""
        if self.api_format == "openai":
            return [{"role": "system", "content": system_prompt}] + messages
        return messages  # Anthropic passes system separately

    async def _create(self, ctx: "AgentContext", tools: list[dict]) -> Any:
        """Non-streaming API call, returns a normalised response object."""
        if self.api_format == "anthropic":
            return await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=ctx.system_prompt,
                messages=ctx.messages,
                tools=self._tools_for_api(tools),
            )
        else:
            # OpenAI-compatible
            kwargs: dict = dict(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=self._inject_system(ctx.messages, ctx.system_prompt),
            )
            api_tools = self._tools_for_api(tools)
            if api_tools:
                kwargs["tools"] = api_tools
            return await self.client.chat.completions.create(**kwargs)

    def _parse_response(self, response: Any) -> tuple[str, str, list[dict]]:
        """
        Parse a response object into (stop_reason, text, tool_calls).
        tool_calls: list of {"name": ..., "id": ..., "input": {...}}
        """
        if self.api_format == "anthropic":
            stop_reason = response.stop_reason  # "end_turn" | "tool_use"
            text_blocks = [b for b in response.content if hasattr(b, "text")]
            text = " ".join(b.text for b in text_blocks)
            tool_calls = [
                {"name": b.name, "id": b.id, "input": b.input}
                for b in response.content
                if b.type == "tool_use"
            ]
            return stop_reason, text, tool_calls
        else:
            # OpenAI
            choice = response.choices[0]
            finish = choice.finish_reason  # "stop" | "tool_calls"
            msg = choice.message
            text = msg.content or ""
            if finish == "tool_calls" and msg.tool_calls:
                tool_calls = []
                for tc in msg.tool_calls:
                    try:
                        inp = json.loads(tc.function.arguments)
                    except Exception:
                        inp = {}
                    tool_calls.append(
                        {"name": tc.function.name, "id": tc.id, "input": inp}
                    )
                return "tool_use", text, tool_calls
            return "end_turn", text, []

    def _assistant_message(self, response: Any, text: str) -> dict:
        """Build the assistant history entry after a tool_use stop."""
        if self.api_format == "anthropic":
            return {"role": "assistant", "content": response.content}
        else:
            # For OpenAI we store the raw message object (or a dict)
            msg = response.choices[0].message
            entry: dict = {"role": "assistant", "content": msg.content}
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            return entry

    def _tool_result_messages(
        self, tool_calls: list[dict], results: list[str]
    ) -> list[dict]:
        """Build tool-result history entries for both formats."""
        if self.api_format == "anthropic":
            return [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tc["id"], "content": r}
                        for tc, r in zip(tool_calls, results)
                    ],
                }
            ]
        else:
            # OpenAI: one message per tool result
            return [
                {"role": "tool", "tool_call_id": tc["id"], "content": r}
                for tc, r in zip(tool_calls, results)
            ]

    async def send_message(
        self,
        ctx: "AgentContext",
        user_message: str,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> "AgentResult":
        # Inject relevant LTM context into system prompt for this turn
        original_system = ctx.system_prompt
        if self.context_manager:
            retrieved = self.context_manager.retrieve_ltm_context(user_message)
            if retrieved:
                ctx.system_prompt = ctx.system_prompt + "\n\n" + retrieved
        skill_catalog: Optional[SkillCatalog] = ctx.metadata.get("skill_catalog")
        required_skills: list[str] = list(ctx.metadata.get("required_skills", []))
        if skill_catalog and required_skills:
            active_blocks = []
            for skill_ref in required_skills:
                activation = skill_catalog.activation_text(skill_ref, explicit=True)
                if activation:
                    active_blocks.append(activation)
            if active_blocks:
                ctx.system_prompt = (
                    ctx.system_prompt
                    + "\n\n## Active Skills\n"
                    + "\n\n".join(active_blocks)
                )

        ctx.messages.append({"role": "user", "content": user_message})
        tool_calls_made = []
        result_text = ""

        try:
            while True:
                tools = self.registry.to_anthropic_format() if ctx.tools_enabled else []

                try:
                    if stream_callback:
                        # Stream for display AND use the full response for tool detection.
                        response, result_text = await self._stream_response(
                            ctx, tools, stream_callback
                        )
                        stream_callback = None  # don't double-print on next iter
                    else:
                        response = await self._create(ctx, tools)
                    stop_reason, text, tool_uses = self._parse_response(response)

                    if stop_reason == "tool_use" and tool_uses:
                        result_text = text or result_text
                        ctx.messages.append(self._assistant_message(response, text))

                        spawn_calls = [
                            tu for tu in tool_uses if tu["name"] == "spawn_agent"
                        ]
                        if len(spawn_calls) > 1:
                            roles = ", ".join(
                                tu["input"].get("role", "?") for tu in spawn_calls
                            )
                            CONSOLE.print(
                                f"\n[bold magenta]⟳ Spawning {len(spawn_calls)} agents in parallel:[/bold magenta] {roles}"
                            )

                        async def _exec_tool(tu: dict) -> str:
                            if tu["name"] == "spawn_agent":
                                # spawn_agent handles its own rich output
                                return await self.registry.call(tu["name"], tu["input"])
                            CONSOLE.print(f"\n[cyan]→ {tu['name']}[/cyan]")
                            res = await self.registry.call(tu["name"], tu["input"])
                            CONSOLE.print(
                                f"[dim]{res[:200]}{'...' if len(res) > 200 else ''}[/dim]"
                            )
                            return res

                        tool_calls_made.extend(tu["name"] for tu in tool_uses)
                        results = list(
                            await asyncio.gather(*[_exec_tool(tu) for tu in tool_uses])
                        )
                        ctx.messages.extend(
                            self._tool_result_messages(tool_uses, results)
                        )
                        continue
                    else:
                        result_text = text or result_text
                        ctx.messages.append(
                            {"role": "assistant", "content": result_text}
                        )
                        break

                except Exception as e:
                    return AgentResult(
                        agent_id=ctx.agent_id,
                        content="",
                        tool_calls_made=tool_calls_made,
                        error=str(e),
                    )
        finally:
            # Always restore the original system prompt
            ctx.system_prompt = original_system

        return AgentResult(
            agent_id=ctx.agent_id,
            content=result_text,
            tool_calls_made=tool_calls_made,
        )

    async def _stream_response(
        self,
        ctx: "AgentContext",
        tools: list[dict],
        callback: Callable[[str], None],
    ) -> tuple[Any, str]:
        """Stream response text chunk-by-chunk and return (full_response, collected_text).

        For Anthropic: uses stream.get_final_message() to obtain the complete response.
        For OpenAI: accumulates tool_call deltas and rebuilds a synthetic response.
        """
        collected: list[str] = []
        response = None
        try:
            if self.api_format == "anthropic":
                async with self.client.messages.stream(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=ctx.system_prompt,
                    messages=ctx.messages,
                    tools=self._tools_for_api(tools),
                ) as stream:
                    async for text in stream.text_stream:
                        collected.append(text)
                        callback(text)
                    response = await stream.get_final_message()
            else:
                # OpenAI streaming — accumulate tool_call deltas as well
                kwargs: dict = dict(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    messages=self._inject_system(ctx.messages, ctx.system_prompt),
                    stream=True,
                )
                api_tools = self._tools_for_api(tools)
                if api_tools:
                    kwargs["tools"] = api_tools
                finish_reason = "stop"
                tool_calls_acc: dict[int, dict] = {}  # index -> {id, name, arguments}
                async for chunk in await self.client.chat.completions.create(**kwargs):
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    if delta.content:
                        collected.append(delta.content)
                        callback(delta.content)
                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {
                                    "id": tc_delta.id or "",
                                    "name": (tc_delta.function.name if tc_delta.function else "") or "",
                                    "arguments": "",
                                }
                            acc = tool_calls_acc[idx]
                            if tc_delta.id:
                                acc["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    acc["name"] = tc_delta.function.name
                                if tc_delta.function.arguments:
                                    acc["arguments"] += tc_delta.function.arguments
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason

                # Build a synthetic response object matching _parse_response expectations
                class _Func:
                    def __init__(self, name, arguments):
                        self.name = name
                        self.arguments = arguments

                class _TC:
                    def __init__(self, id, function):
                        self.id = id
                        self.function = function

                class _Msg:
                    def __init__(self, content, tool_calls):
                        self.content = content
                        self.tool_calls = tool_calls

                class _Choice:
                    def __init__(self, finish_reason, message):
                        self.finish_reason = finish_reason
                        self.message = message

                class _Response:
                    def __init__(self, choices):
                        self.choices = choices

                oi_tool_calls = [
                    _TC(v["id"], _Func(v["name"], v["arguments"]))
                    for _, v in sorted(tool_calls_acc.items())
                ] if tool_calls_acc else None

                response = _Response([
                    _Choice(
                        finish_reason,
                        _Msg("".join(collected), oi_tool_calls),
                    )
                ])
        except Exception as e:
            traceback.print_exc()
            # Return a minimal end_turn response on error
            if self.api_format == "anthropic":
                # Return a fake minimal response
                class _FakeBlock:
                    def __init__(self, text):
                        self.text = text
                        self.type = "text"
                class _FakeResp:
                    def __init__(self, text):
                        self.stop_reason = "end_turn"
                        self.content = [_FakeBlock(text)]
                response = _FakeResp("".join(collected))
            else:
                class _Func2:
                    pass
                class _Msg2:
                    def __init__(self, content):
                        self.content = content
                        self.tool_calls = None
                class _Choice2:
                    def __init__(self, msg):
                        self.finish_reason = "stop"
                        self.message = msg
                class _Response2:
                    def __init__(self, choices):
                        self.choices = choices
                response = _Response2([_Choice2(_Msg2("".join(collected)))])
        return response, "".join(collected)

    def register_spawn_capability(
        self, base_system_prompt: str, workspace_root: Optional[Path] = None
    ) -> None:
        """Register the spawn_agent tool.

        The main agent can call spawn_agent one or more times in a single turn.
        Multiple calls are executed in parallel (via asyncio.gather in send_message).
        Sub-agents receive all regular tools but NOT spawn_agent, preventing recursion.
        """
        parent = self  # captured reference to the parent agent

        async def spawn_agent(role: str, task: str, system_suffix: str = "") -> str:
            # Build a leaf registry: all tools except spawn_agent itself
            sub_registry = ToolRegistry(console=CONSOLE)
            for name, tool_def in parent.registry._tools.items():
                if name != "spawn_agent":
                    sub_registry._tools[name] = tool_def

            sub_agent = BaseAgent(
                parent.client,
                sub_registry,
                model=parent.model,
                max_tokens=parent.max_tokens,
                api_format=parent.api_format,
            )
            sys_prompt = base_system_prompt
            if system_suffix:
                sys_prompt += f"\n\n{system_suffix}"
            sys_prompt = _compose_system_prompt(sys_prompt, sub_registry, workspace_root)
            sub_ctx = AgentContext(role=role, system_prompt=sys_prompt)
            CONSOLE.print(f"\n[bold magenta]▶ [{role}][/bold magenta] {task[:120]}")
            result = await sub_agent.send_message(sub_ctx, task)
            CONSOLE.print(
                Panel(
                    result.content,
                    title=f"[magenta]{role}[/magenta]",
                    border_style="magenta",
                    padding=(0, 1),
                )
            )
            return result.content

        self.registry.register(
            "spawn_agent",
            (
                "Spawn a specialized sub-agent to handle a task from a particular perspective. "
                "Call this tool multiple times in a single response to run sub-agents in PARALLEL. "
                "Each sub-agent has a fresh context and all regular tools."
            ),
            {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "description": (
                            "Role / persona of the sub-agent "
                            "(e.g. 'researcher', 'critic', 'implementer', 'devil's advocate')"
                        ),
                    },
                    "task": {
                        "type": "string",
                        "description": "The specific task or question for this sub-agent.",
                    },
                    "system_suffix": {
                        "type": "string",
                        "description": (
                            "Optional extra instructions appended to the system prompt "
                            "to shape this sub-agent's behavior."
                        ),
                    },
                },
                "required": ["role", "task"],
            },
            spawn_agent,
            source="runtime:spawn",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. SELF-EVOLUTION
# ─────────────────────────────────────────────────────────────────────────────


class EvolutionEngine:
    """Self-evolution: scoring, prompt rewriting, tool generation."""

    def __init__(
        self,
        client: Any,
        model: str,
        memory: MemoryPalace,
        api_format: str = "anthropic",
    ):
        self.client = client
        self.model = model
        self.memory = memory
        self.api_format = api_format
        RL_DIR.mkdir(parents=True, exist_ok=True)
        PROMPTS_DIR.mkdir(parents=True, exist_ok=True)

    async def _generate_text(self, prompt: str, max_tokens: int) -> str:
        if self.api_format == "anthropic":
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        response = await self.client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content or ""

    async def score_session(
        self, messages: list[dict], prompt_version: str, tools_used: list[str]
    ) -> dict:
        """Let the active provider score the session quality."""
        if len(messages) < 2:
            return {"score": 5, "critique": "Session too short to evaluate"}

        # Sample last N exchanges
        sample = messages[-10:]
        convo_text = "\n".join(
            f"{m['role'].upper()}: {str(m['content'])[:300]}"
            for m in sample
            if isinstance(m.get("content"), str)
        )

        prompt = (
            "Rate this AI assistant conversation on a scale of 1-10.\n"
            "Criteria: accuracy, helpfulness, conciseness, tool use appropriateness.\n"
            f"Conversation:\n{convo_text}\n\n"
            'Respond in JSON: {"score": N, "critique": "brief analysis", "improvements": ["..."]}'
        )

        try:
            text = await self._generate_text(prompt, max_tokens=512)
            json_match = re.search(r"\{.*\}", text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                result = {"score": 5, "critique": text[:200]}
        except Exception as e:
            result = {"score": 5, "critique": f"Scoring failed: {e}"}

        # Save to RL log
        record = {
            "session_id": str(uuid.uuid4())[:8],
            "timestamp": _now(),
            "score": result.get("score", 5),
            "prompt_version": prompt_version,
            "tools_used": tools_used,
            "critique": result.get("critique", ""),
            "improvements": result.get("improvements", []),
        }
        with open(SESSIONS_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")

        return result

    def _load_sessions(self) -> list[dict]:
        if not SESSIONS_FILE.exists():
            return []
        sessions = []
        with open(SESSIONS_FILE) as f:
            for line in f:
                try:
                    sessions.append(json.loads(line.strip()))
                except Exception:
                    pass
        return sessions

    def _get_current_prompt_version(self) -> tuple[str, str]:
        best = PROMPTS_DIR / "best.md"
        if best.exists():
            content = best.read_text()
            # Extract version from filename reference or default
            v_match = re.search(r"version:\s*(\w+)", content)
            version = v_match.group(1) if v_match else "best"
            return version, content
        # Find latest version
        versions = sorted(PROMPTS_DIR.glob("system_v*.md"))
        if versions:
            latest = versions[-1]
            return latest.stem, latest.read_text()
        return "default", DEFAULT_SYSTEM_PROMPT

    async def rewrite_system_prompt(self) -> str:
        """Analyze history and rewrite system prompt."""
        sessions = self._load_sessions()
        if not sessions:
            return "No sessions to analyze"

        # Get low-score sessions
        low_sessions = [s for s in sessions if s.get("score", 10) < 6]
        critiques = "\n".join(
            f"- Score {s['score']}: {s['critique']}" for s in sessions[-20:]
        )
        improvements = []
        for s in sessions[-20:]:
            improvements.extend(s.get("improvements", []))

        version, current_prompt = self._get_current_prompt_version()

        prompt = (
            f"Current system prompt:\n{current_prompt}\n\n"
            f"Recent session critiques:\n{critiques}\n\n"
            f"Suggested improvements:\n"
            + "\n".join(f"- {i}" for i in improvements[:10])
            + "\n\n"
            "Rewrite the system prompt to address these issues. "
            "Make it more effective while keeping it concise."
        )

        new_prompt = await self._generate_text(prompt, max_tokens=2048)

        # Save new version
        existing = list(PROMPTS_DIR.glob("system_v*.md"))
        new_version_num = len(existing) + 1
        new_path = PROMPTS_DIR / f"system_v{new_version_num}.md"
        new_path.write_text(f"<!-- version: v{new_version_num} -->\n{new_prompt}")

        CONSOLE.print(
            f"[green]New prompt version saved: system_v{new_version_num}.md[/green]"
        )
        return new_prompt

    async def generate_tool(self, description: str, registry: ToolRegistry) -> str:
        """Let Claude generate a new tool and save it as a skill."""
        prompt = (
            f"Generate a Python tool function for: {description}\n\n"
            "Requirements:\n"
            "1. Use this decorator pattern:\n"
            "```python\n"
            "def register_tool(name, description, parameters):\n"
            "    def decorator(fn):\n"
            "        fn._tool_meta = {'name': name, 'description': description, 'parameters': parameters}\n"
            "        return fn\n"
            "    return decorator\n"
            "\n"
            "@register_tool(\n"
            "    name='tool_name',\n"
            "    description='What this tool does',\n"
            "    parameters={'type': 'object', 'properties': {...}, 'required': [...]}\n"
            ")\n"
            "async def tool_function(**kwargs):\n"
            "    # implementation\n"
            "    return result\n"
            "```\n"
            "2. The function must be async\n"
            "3. Include the register_tool helper at the top\n"
            "4. Add proper error handling\n"
            "5. Return a string result\n\n"
            "Output ONLY the Python code, no explanation."
        )

        code = await self._generate_text(prompt, max_tokens=2048)

        # Extract code from markdown code block if present
        code_match = re.search(r"```python\n(.*?)```", code, re.DOTALL)
        if code_match:
            code = code_match.group(1)

        # Generate safe filename
        safe_name = re.sub(r"[^a-z0-9_]", "_", description.lower()[:30])
        skill_path = SKILLS_DIR / f"auto_{safe_name}.py"
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(code)

        CONSOLE.print(f"[green]Tool saved to {skill_path}[/green]")
        return f"Tool generated and saved to {skill_path}"

    def apply_best_prompt(self) -> str:
        """Load the best prompt from history."""
        sessions = self._load_sessions()
        if not sessions:
            return DEFAULT_SYSTEM_PROMPT

        # Find best performing prompt version
        version_scores: dict[str, list[float]] = {}
        for s in sessions:
            v = s.get("prompt_version", "default")
            version_scores.setdefault(v, []).append(s.get("score", 5))

        best_version = max(
            version_scores,
            key=lambda v: sum(version_scores[v]) / len(version_scores[v]),
        )

        # Load that prompt
        prompt_file = PROMPTS_DIR / f"{best_version}.md"
        if prompt_file.exists():
            content = prompt_file.read_text()
            # Strip version comment
            content = re.sub(r"^<!--.*?-->\n", "", content, flags=re.DOTALL)
            (PROMPTS_DIR / "best.md").write_text(content)
            return content

        return DEFAULT_SYSTEM_PROMPT

    def get_stats(self) -> dict:
        sessions = self._load_sessions()
        if not sessions:
            return {"total": 0, "avg_score": 0}
        scores = [s.get("score", 5) for s in sessions]
        return {
            "total": len(sessions),
            "avg_score": round(sum(scores) / len(scores), 2),
            "min_score": min(scores),
            "max_score": max(scores),
            "recent_score": scores[-1] if scores else 0,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 7. CONFIG
# ─────────────────────────────────────────────────────────────────────────────


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
        raw = json.loads(CONFIG_FILE.read_text())
        # Only backfill structural sections the user hasn't touched;
        # never overwrite top-level identity keys.
        for section in ("memory", "orchestration", "evolution", "mcp_servers", "context"):
            if section not in raw and section in DEFAULT_CONFIG:
                raw[section] = DEFAULT_CONFIG[section]
        return raw, first_run
    except Exception as e:
        CONSOLE.print(f"[yellow]Config parse error: {e} — using defaults[/yellow]")
        return dict(DEFAULT_CONFIG), first_run




def save_config(cfg: dict):
    AGENT_HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def _first_run_setup() -> bool:
    """Interactive first-run setup wizard.
    Guides user to choose a provider, set API key / base_url, and save config.
    Returns True if setup completed and agent should start.
    """
    from rich.prompt import Confirm

    CONSOLE.print(
        Panel(
            f"[bold cyan]Welcome to Personal Agent![/bold cyan]\n\n"
            f"Config file created at:\n"
            f"  [bold]{CONFIG_FILE}[/bold]\n\n"
            f"Let's set up your AI provider. You can change this anytime:\n"
            f"  [dim]python agent.py config use-provider <name>[/dim]\n"
            f"  [dim]python agent.py config edit[/dim]",
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

    CONSOLE.print("\n[bold]Select provider:[/bold]")
    CONSOLE.print("  1. Anthropic Claude  (native SDK)")
    CONSOLE.print("  2. OpenAI            (openai SDK)")
    CONSOLE.print("  3. DeepSeek          (OpenAI-compatible)")
    CONSOLE.print("  4. Ollama            (local, no key needed)")
    CONSOLE.print("  5. Other             (custom OpenAI-compatible endpoint)")

    choice = ""
    while choice not in provider_menu:
        choice = Prompt.ask("\nChoice", default="1").strip()

    provider_name, api_format, env_key, default_url = provider_menu[choice]

    if provider_name == "other":
        provider_name = (
            Prompt.ask("Provider name (e.g. siliconflow, together)").strip() or "custom"
        )

    CONSOLE.print(
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
        CONSOLE.print("[dim]Ollama: no API key needed.[/dim]")
    else:
        existing_key = os.environ.get(env_key, "") if env_key else ""
        if existing_key:
            CONSOLE.print(f"[green]Found {env_key} in environment. ✓[/green]")
            api_key_val = f"${env_key}" if env_key else existing_key
        else:
            CONSOLE.print(
                f"\n[yellow]API key not found in env '{env_key or '?'}'.[/yellow]"
            )
            CONSOLE.print("Options:")
            CONSOLE.print("  a) Enter key now  (stored in config.json — less secure)")
            env_hint = (
                f"export {env_key}=<key>" if env_key else "set your API key env var"
            )
            CONSOLE.print(f"  b) Leave blank    (add '{env_hint}' later and restart)")

            raw = Prompt.ask(
                "API key (enter to skip)", default="", password=True
            ).strip()
            if raw:
                api_key_val = raw
            else:
                api_key_val = f"${env_key}" if env_key else "$API_KEY"
                CONSOLE.print(f"[dim]Stored as reference: {api_key_val}[/dim]")

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
    p["api_key"] = api_key_val
    p["default_model"] = model
    if base_url:
        p["base_url"] = base_url

    save_config(cfg)

    CONSOLE.print(
        Panel(
            f"[green]Config saved.[/green]\n\n"
            f"  Provider : [bold]{provider_name}[/bold] ({api_format})\n"
            + (f"  Base URL : {base_url}\n" if base_url else "")
            + f"  Model    : {model}\n\n"
            f"[dim]Edit anytime: python agent.py config edit[/dim]",
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
        p = DEFAULT_OUTPUT_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_system_prompt(cfg: dict) -> str:
    best = PROMPTS_DIR / "best.md"
    if best.exists():
        content = best.read_text()
        content = re.sub(r"^<!--.*?-->\n", "", content, flags=re.DOTALL)
        return content
    prompt_file = cfg.get("system_prompt_file")
    if prompt_file:
        p = Path(prompt_file)
        if p.exists():
            return p.read_text()
        CONSOLE.print(
            f"[yellow]system_prompt_file '{prompt_file}' not found — using default[/yellow]"
        )
    return DEFAULT_SYSTEM_PROMPT


def _compose_system_prompt(
    base_prompt: str,
    registry: ToolRegistry,
    workspace_root: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    skill_catalog: Optional[SkillCatalog] = None,
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
            lines.append(f"Workspace root for file tools: {workspace_root}")
    if output_dir:
        lines.append(f"Output directory for generated files (screenshots, exports, temp): {output_dir}")
    return base_prompt.rstrip() + "\n\n" + "\n".join(lines)


async def _close_components(components: dict) -> None:
    mcp_client = components.get("mcp_client")
    if mcp_client is not None:
        await mcp_client.close()


# ─────────────────────────────────────────────────────────────────────────────
# 8. CLI
# ─────────────────────────────────────────────────────────────────────────────

app = typer.Typer(
    name="agent",
    help="Personal AI Agent with Memory Palace, Multi-Agent Orchestration, and Self-Evolution",
    add_completion=False,
)
memory_app = typer.Typer(help="Memory palace commands")
app.add_typer(memory_app, name="memory")


async def _build_components_async(cfg: dict):
    """Build all components from config using ModelClientFactory."""
    client, model, max_tokens = ModelClientFactory.from_config(cfg)
    system_prompt = _load_system_prompt(cfg)

    # Sub-config sections
    mem_cfg = cfg.get("memory", {})
    orch_cfg = cfg.get("orchestration", {})

    workspace_root = Path.cwd().resolve()
    output_dir = _resolve_output_dir(cfg)

    # Resolve active provider format for format-aware classes
    active_provider = cfg.get("active_provider", "anthropic")
    api_format = (
        cfg.get("providers", {}).get(active_provider, {}).get("api_format", "anthropic")
    )

    registry = ToolRegistry(console=CONSOLE)

    # Context Manager — build first so BuiltinTools can reference it
    # Config is split into two sub-sections:
    #   context.storage       — LTM store settings (what to keep)
    #   context.consolidation — trigger settings (when/how to consolidate)
    ctx_cfg = cfg.get("context", {})
    storage_cfg = ctx_cfg.get("storage", ctx_cfg)  # fallback: flat cfg for compat
    cons_cfg = ctx_cfg.get("consolidation", ctx_cfg)  # fallback: flat cfg for compat

    ctx_store = LTMStore(
        context_dir=CONTEXT_DIR,
        max_categories=storage_cfg.get("max_categories", MAX_CATEGORIES),
        memory_dir=MEMORY_DIR,
    )
    memory = MemoryPalace(
        tidy_interval=mem_cfg.get("tidy_interval_seconds", MEMORY_TIDY_INTERVAL),
        tidy_threshold=mem_cfg.get("tidy_file_threshold", MEMORY_TIDY_FILE_THRESHOLD),
        base_dir=MEMORY_DIR,
        context_dir=CONTEXT_DIR,
        store=ctx_store,
    )
    ctx_manager = ContextManager(
        store=ctx_store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(
            store=ctx_store,
            max_categories=storage_cfg.get("max_categories", MAX_CATEGORIES),
            decay_factor=storage_cfg.get("decay_factor", DECAY_FACTOR),
            sleep_token_ratio=cons_cfg.get("token_ratio", SLEEP_TOKEN_RATIO),
            keep_last_messages=cons_cfg.get("keep_last_messages", 6),
        ),
        idle_seconds=cons_cfg.get("idle_seconds", 300),
        min_messages=cons_cfg.get("min_messages", 4),
    )

    BuiltinTools(
        memory,
        registry,
        context_manager=ctx_manager,
        workspace_root=workspace_root,
        chapter_normalizer=lambda chapter: normalize_memory_chapter(
            chapter, LEGACY_MEMORY_ALIASES
        ),
        output_dir=output_dir,
    )

    # Share output_dir with skills via registry context
    registry.set_context("output_dir", str(output_dir))

    skill_catalog = SkillCatalog()
    skill_catalog.load_all()
    skill_catalog.register_tools(registry)

    mcp_client = None
    mcp_status = {
        "configured_servers": 0,
        "connected_servers": 0,
        "failed_servers": 0,
        "registered_tools": 0,
    }
    if cfg.get("mcp_servers"):
        mcp_client = MCPClient(registry)
        mcp_extra_env = {"AGENT_OUTPUT_DIR": str(output_dir)}
        await mcp_client.connect_from_config(cfg, extra_env=mcp_extra_env)
        mcp_status = mcp_client.status_summary()
        if mcp_status["connected_servers"]:
            CONSOLE.print(
                "[green]MCP active:[/green] "
                f"{mcp_status['connected_servers']} server(s), "
                f"{mcp_status['registered_tools']} tool(s) registered"
            )
        else:
            CONSOLE.print(
                "[yellow]MCP configured, but no servers connected successfully.[/yellow]"
            )

    agent = BaseAgent(
        client, registry, model=model, max_tokens=max_tokens, api_format=api_format
    )
    agent.register_spawn_capability(system_prompt, workspace_root=workspace_root)
    system_prompt = _compose_system_prompt(
        system_prompt,
        registry,
        workspace_root,
        output_dir,
        skill_catalog=skill_catalog,
    )
    agent.context_manager = ctx_manager
    evolution = EvolutionEngine(client, model, memory, api_format=api_format)

    return {
        "client": client,
        "model": model,
        "max_tokens": max_tokens,
        "system_prompt": system_prompt,
        "memory": memory,
        "registry": registry,
        "agent": agent,
        "evolution": evolution,
        "context_manager": ctx_manager,
        "skill_catalog": skill_catalog,
        "mcp_client": mcp_client,
        "mcp_status": mcp_status,
        "output_dir": output_dir,
        "cfg": cfg,
    }


def _build_components(cfg: dict):
    """Synchronous compatibility wrapper for commands that do not need async setup."""
    return asyncio.run(_build_components_async(cfg))


async def _interactive_loop(components: dict, cfg: dict):
    """Main interactive chat loop."""
    agent: BaseAgent = components["agent"]
    memory: MemoryPalace = components["memory"]
    evolution: EvolutionEngine = components["evolution"]
    system_prompt = components["system_prompt"]
    ctx_mgr: Optional[ContextManager] = components.get("context_manager")
    skill_catalog: SkillCatalog = components["skill_catalog"]

    # Get prompt version
    prompt_files = sorted(PROMPTS_DIR.glob("system_v*.md"))
    prompt_version = prompt_files[-1].stem if prompt_files else "default"

    ctx = AgentContext(system_prompt=system_prompt)
    tools_used_session: list[str] = []
    memory_worker = (
        BackgroundMemoryWorker(
            ctx_mgr,
            components["client"],
            components["model"],
            agent.api_format,
        )
        if ctx_mgr
        else None
    )
    if memory_worker:
        memory_worker.start()

    CONSOLE.print(
        Panel(
            "[bold cyan]Personal Agent[/bold cyan]\n"
            "[dim]Commands: /memory, /context, /evolve, /tools, /model [name], /quit[/dim]",
            title="Agent Ready",
            border_style="cyan",
        )
    )

    try:
        while True:
            try:
                user_input = Prompt.ask("\n[bold green]You[/bold green]")
            except (EOFError, KeyboardInterrupt):
                break

            if not user_input.strip():
                continue

            # Handle slash commands
            if user_input.startswith("/"):
                cmd = user_input[1:].strip().lower()
                if cmd in ("quit", "exit", "q"):
                    break
                elif cmd == "memory":
                    chapters = memory.list_chapters()
                    table = Table(title="Memory Palace")
                    table.add_column("Chapter")
                    table.add_column("Files")
                    for ch in chapters:
                        table.add_row(ch["name"], str(len(ch["files"])))
                    CONSOLE.print(table)
                    continue
                elif cmd == "context":
                    if ctx_mgr:
                        stats = ctx_mgr.stats()
                        table = Table(title="Context Manager (LTM)")
                        table.add_column("Metric")
                        table.add_column("Value")
                        table.add_row(
                            "Categories",
                            f"{stats['categories']}/{stats['max_categories']}",
                        )
                        table.add_row("Total Entries", str(stats["total_entries"]))
                        table.add_row(
                            "Category Names",
                            ", ".join(stats["category_names"]) or "—",
                        )
                        table.add_row(
                            "Staged Turns",
                            str(stats["staged_turns"]),
                        )
                        table.add_row(
                            "Needs Consolidation",
                            "yes" if stats["needs_consolidation"] else "no",
                        )
                        table.add_row(
                            "Idle",
                            f"{stats['idle_elapsed_s']}s / {stats['idle_threshold_s']}s",
                        )
                        CONSOLE.print(table)
                    else:
                        CONSOLE.print("[yellow]Context manager not available.[/yellow]")
                    continue
                elif cmd == "evolve":
                    CONSOLE.print("[yellow]Running evolution engine...[/yellow]")
                    new_prompt = await evolution.rewrite_system_prompt()
                    ctx.system_prompt = new_prompt
                    CONSOLE.print("[green]System prompt updated.[/green]")
                    continue
                elif cmd == "tools":
                    tools = components["registry"].list_tools()
                    CONSOLE.print("Available tools: " + ", ".join(tools))
                    continue
                elif cmd == "skills":
                    skills = skill_catalog.list_skills()
                    if not skills:
                        CONSOLE.print("[yellow]No skills found.[/yellow]")
                    else:
                        table = Table(title="Available Skills")
                        table.add_column("ID")
                        table.add_column("Source")
                        table.add_column("Description")
                        for bundle in skills:
                            table.add_row(
                                bundle.id,
                                bundle.source,
                                bundle.description or "—",
                            )
                        CONSOLE.print(table)
                    continue
                elif cmd.startswith("mode "):
                    # Kept as a hidden override for debugging; not advertised
                    CONSOLE.print(
                        "[dim](manual mode override removed — routing is automatic)[/dim]"
                    )
                    continue
                elif cmd == "model" or cmd.startswith("model "):
                    parts = cmd.split(None, 1)
                    provider_cfg = cfg.get("providers", {}).get(
                        cfg.get("active_provider", ""), {}
                    )
                    available = provider_cfg.get(
                        "models", [provider_cfg.get("default_model", agent.model)]
                    )
                    if len(parts) == 1:
                        # List available models
                        table = Table(title="Models")
                        table.add_column("Model")
                        table.add_column("Active")
                        for m in available:
                            mark = (
                                "[bold green]✓[/bold green]" if m == agent.model else ""
                            )
                            table.add_row(m, mark)
                        CONSOLE.print(table)
                    else:
                        new_model = parts[1].strip()
                        agent.set_model(new_model)
                        CONSOLE.print(f"[green]Switched to model: {new_model}[/green]")
                    continue
                elif cmd == "stats":
                    stats = evolution.get_stats()
                    CONSOLE.print(
                        f"Sessions: {stats['total']}, Avg score: {stats['avg_score']}"
                    )
                    continue
                else:
                    normalized_input, required_skills = prepare_user_message_for_skills(
                        user_input, skill_catalog
                    )
                    if required_skills:
                        user_input = normalized_input
                        ctx.metadata["required_skills"] = required_skills
                    else:
                        CONSOLE.print(f"[yellow]Unknown command: {user_input}[/yellow]")
                        continue
            else:
                normalized_input, required_skills = prepare_user_message_for_skills(
                    user_input, skill_catalog
                )
                if required_skills:
                    user_input = normalized_input
                    ctx.metadata["required_skills"] = required_skills
                else:
                    ctx.metadata.pop("required_skills", None)

            # Mark activity so idle timer resets and dirty flag is set
            if ctx_mgr:
                ctx_mgr.mark_activity()

            # The agent decides how to handle the request
            CONSOLE.print()
            collected_text: list[str] = []

            def stream_cb(chunk: str):
                CONSOLE.print(chunk, end="", markup=False)
                collected_text.append(chunk)

            try:
                CONSOLE.print("[bold blue]Agent[/bold blue]: ", end="")
                ctx.metadata["skill_catalog"] = skill_catalog
                result = await agent.send_message(
                    ctx, user_input, stream_callback=stream_cb
                )
                if not collected_text:
                    CONSOLE.print(Markdown(result.content))
                CONSOLE.print()
                if result.error:
                    CONSOLE.print(f"[red]Error: {result.error}[/red]")
                tools_used_session.extend(result.tool_calls_made)

                # Stage this turn (user input + assistant reply) for consolidation
                if ctx_mgr:
                    ctx_mgr.staging.append("user", user_input)
                    if result.content:
                        ctx_mgr.staging.append("assistant", result.content)
                    if ctx_mgr.should_enqueue_consolidation():
                        ctx_mgr.enqueue_consolidation("staged_turns")

                # Keep working memory bounded without blocking on LLM consolidation.
                if ctx_mgr and ctx_mgr.should_compact_messages(ctx.messages, agent.max_tokens):
                    ctx.messages = ctx_mgr.compact_messages(ctx.messages)

                # Check for tool generation request
                if any(
                    kw in user_input.lower()
                    for kw in [
                        "create a tool",
                        "write a tool",
                        "generate a tool",
                        "帮我写一个工具",
                    ]
                ):
                    CONSOLE.print("[dim]Generating tool...[/dim]")
                    await evolution.generate_tool(user_input, components["registry"])
                    components["skill_catalog"].reload()
                    components["skill_catalog"].register_tools(components["registry"])
                    components["system_prompt"] = _compose_system_prompt(
                        system_prompt,
                        components["registry"],
                        Path.cwd().resolve(),
                        components["output_dir"],
                        skill_catalog=components["skill_catalog"],
                    )
                    ctx.system_prompt = components["system_prompt"]

            except Exception as e:
                CONSOLE.print(f"\n[red]Error: {e}[/red]")

    finally:
        if memory_worker:
            memory_worker.stop()

    # Session-end consolidation: digest any unprocessed staging content
    if ctx_mgr and ctx_mgr.should_session_end_sleep():
        CONSOLE.print("[dim]💤 Session-end consolidation...[/dim]")
        try:
            ctx_mgr.enqueue_consolidation("session_end")
            while ctx_mgr.pending_jobs():
                await ctx_mgr.process_one_job(
                    components["client"],
                    components["model"],
                    api_format=agent.api_format,
                )
            ctx.messages = ctx_mgr.compact_messages(ctx.messages)
        except Exception as e:
            CONSOLE.print(f"[dim]Session-end consolidation error: {e}[/dim]")

    # End of session scoring
    if len(ctx.messages) >= 2:
        CONSOLE.print("\n[dim]Scoring session...[/dim]")
        try:
            score_result = await evolution.score_session(
                ctx.messages, prompt_version, tools_used_session
            )
            score = score_result.get("score", "?")
            critique = score_result.get("critique", "")
            CONSOLE.print(f"[dim]Session score: {score}/10 — {critique[:100]}[/dim]")
        except Exception:
            pass

    CONSOLE.print("\n[dim]Goodbye.[/dim]")


@app.callback(invoke_without_command=True)
def main_callback(ctx: typer.Context):
    """Enter interactive chat when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        cfg, first_run = load_config()
        if first_run:
            if not _first_run_setup():
                raise typer.Exit(0)
            # Reload after potential edits
            cfg, _ = load_config()
        async def _run():
            components = await _build_components_async(cfg)
            try:
                await _interactive_loop(components, cfg)
            finally:
                await _close_components(components)

        asyncio.run(_run())


@app.command()
def chat(question: str = typer.Argument(..., help="Question or task for the agent")):
    """Single-turn chat with the agent."""
    cfg, first_run = load_config()
    if first_run:
        if not _first_run_setup():
            raise typer.Exit(0)
        cfg, _ = load_config()
    async def _run():
        components = await _build_components_async(cfg)
        agent: BaseAgent = components["agent"]
        ctx = AgentContext(system_prompt=components["system_prompt"])
        skill_catalog: SkillCatalog = components["skill_catalog"]
        normalized_question, required_skills = prepare_user_message_for_skills(
            question, skill_catalog
        )
        if required_skills:
            ctx.metadata["required_skills"] = required_skills
        ctx.metadata["skill_catalog"] = skill_catalog
        CONSOLE.print("[bold blue]Agent[/bold blue]: ", end="")
        try:
            result = await agent.send_message(
                ctx,
                normalized_question,
                stream_callback=lambda chunk: CONSOLE.print(chunk, end="", markup=False),
            )
            CONSOLE.print()
            if result.error:
                CONSOLE.print(f"[red]Error: {result.error}[/red]")
        finally:
            await _close_components(components)

    asyncio.run(_run())


@app.command()
def evolve(
    rewrite: bool = typer.Option(
        False, "--rewrite", help="Rewrite system prompt from session history"
    ),
    apply_best: bool = typer.Option(
        False, "--apply-best", help="Apply best-scoring prompt"
    ),
    stats: bool = typer.Option(False, "--stats", help="Show RL statistics"),
):
    """Self-evolution: analyze history and optimize the agent."""
    cfg, _ = load_config()
    async def _run():
        components = await _build_components_async(cfg)
        evolution: EvolutionEngine = components["evolution"]
        try:
            if stats:
                s = evolution.get_stats()
                table = Table(title="RL Statistics")
                table.add_column("Metric")
                table.add_column("Value")
                for k, v in s.items():
                    table.add_row(k, str(v))
                CONSOLE.print(table)
            elif apply_best:
                prompt = evolution.apply_best_prompt()
                CONSOLE.print("[green]Applied best prompt.[/green]")
                CONSOLE.print(f"[dim]{prompt[:200]}...[/dim]")
            else:
                CONSOLE.print("[yellow]Rewriting system prompt...[/yellow]")
                new_prompt = await evolution.rewrite_system_prompt()
                CONSOLE.print("[green]Done. New prompt:[/green]")
                CONSOLE.print(Markdown(new_prompt[:500]))
        finally:
            await _close_components(components)

    asyncio.run(_run())


@app.command()
def config(
    action: str = typer.Argument(..., help="Action: list | models | get"),
    key: Optional[str] = typer.Argument(
        None, help="Config key (dot-notation supported, e.g. providers.qwen.base_url)"
    ),
    value: Optional[str] = typer.Argument(None, help="(unused)"),
):
    """View agent configuration (read-only).

    Examples:
      config list                              # show current config
      config models                            # list configured providers
      config get providers.qwen.default_model  # read a specific key
    """
    cfg, _ = load_config()

    if action == "list":
        CONSOLE.print(
            Markdown(f"```json\n{json.dumps(cfg, indent=2, ensure_ascii=False)}\n```")
        )

    elif action == "models":
        providers = ModelClientFactory.list_providers(cfg)
        table = Table(title="Configured Providers")
        table.add_column("Name")
        table.add_column("Format")
        table.add_column("Default Model")
        table.add_column("Base URL")
        table.add_column("Active")
        for p in providers:
            mark = "[bold green]✓[/bold green]" if p["active"] else ""
            table.add_row(p["name"], p["format"], p["model"], p["base_url"], mark)
        CONSOLE.print(table)

    elif action == "get":
        if not key:
            CONSOLE.print("[red]Key required for 'get'[/red]")
            raise typer.Exit(1)
        parts = key.split(".")
        cur: Any = cfg
        for p in parts:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(p)
        if cur is None:
            CONSOLE.print(f"[yellow]Key '{key}' not found[/yellow]")
        else:
            CONSOLE.print(f"{key} = {cur}")

    else:
        CONSOLE.print(f"[red]Unknown action '{action}'. Use: list | models | get[/red]")
        raise typer.Exit(1)


# ── Memory subcommands ────────────────────────────────────────────────────────


@memory_app.command("ls")
def memory_ls():
    """List all memory chapters and file counts."""
    memory = MemoryPalace()
    table = Table(title="Memory Palace")
    table.add_column("Chapter")
    table.add_column("Files")
    table.add_column("File Names")
    for ch in memory.list_chapters():
        table.add_row(ch["name"], str(len(ch["files"])), ", ".join(ch["files"][:5]))
    CONSOLE.print(table)


@memory_app.command("show")
def memory_show(
    path: str = typer.Argument(..., help="chapter/name (e.g. projects/myproject)"),
):
    """Show contents of a memory file."""
    parts = path.strip("/").split("/", 1)
    if len(parts) != 2:
        CONSOLE.print("[red]Path must be chapter/name[/red]")
        raise typer.Exit(1)
    chapter, name = parts
    memory = MemoryPalace()
    content = memory.read(chapter, name)
    if content:
        CONSOLE.print(Markdown(content))
    else:
        CONSOLE.print(f"[yellow]No memory at {path}[/yellow]")


@memory_app.command("search")
def memory_search(query: str = typer.Argument(..., help="Search query")):
    """Search across all memory files."""
    memory = MemoryPalace()
    results = memory.search(query)
    if not results:
        CONSOLE.print(f"[yellow]No results for '{query}'[/yellow]")
        return
    table = Table(title=f"Search: {query}")
    table.add_column("Path")
    table.add_column("Snippet")
    for r in results:
        table.add_row(r["path"], r["snippet"][:80])
    CONSOLE.print(table)


@memory_app.command("tidy")
def memory_tidy():
    """Manually trigger AI-assisted memory reorganization."""
    cfg, _ = load_config()
    async def _run():
        components = await _build_components_async(cfg)
        mem: MemoryPalace = components["memory"]
        # Force tidy by resetting timer
        mem._last_tidy = 0
        mem._files_since_tidy = MEMORY_TIDY_FILE_THRESHOLD
        try:
            await mem.tidy(components["client"], components["model"])
        finally:
            await _close_components(components)

    asyncio.run(_run())


@memory_app.command("index")
def memory_index():
    """Show the memory palace index."""
    memory = MemoryPalace()
    CONSOLE.print(Markdown(memory.read_index()))


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()
