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
import importlib
import importlib.util
import math
import inspect
import json
import os
import re
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import anthropic
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

# Optional MCP import
try:
    import mcp

    HAS_MCP = True
except ImportError:
    HAS_MCP = False

# ── Constants ─────────────────────────────────────────────────────────────────
AGENT_HOME = Path.home() / ".agent"
MEMORY_DIR = AGENT_HOME / "memory"
SKILLS_DIR = AGENT_HOME / "skills"
PROMPTS_DIR = AGENT_HOME / "prompts"
RL_DIR = AGENT_HOME / "rl"
CONFIG_FILE = AGENT_HOME / "config.json"
INDEX_FILE = MEMORY_DIR / "INDEX.md"
SESSIONS_FILE = RL_DIR / "sessions.jsonl"

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
STAGING_FILE = CONTEXT_DIR / "_staging.jsonl"  # raw conversation buffer

CONSOLE = Console()

DEFAULT_SYSTEM_PROMPT = """You are a powerful personal AI agent with tools, memory, and the ability to spawn sub-agents.

## Tools
You have access to shell, file, web, and memory tools. Use them proactively.

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


@dataclass
class ProviderConfig:
    """Configuration for a single model provider."""

    name: str  # arbitrary label, e.g. "anthropic", "openai", "deepseek"
    api_format: str  # "anthropic" | "openai"
    api_key: str  # or env-var name if starts with "$"
    base_url: Optional[str] = None  # custom endpoint (OpenAI-compat)
    default_model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    extra: dict = field(default_factory=dict)  # future: timeout, headers, etc.

    def resolve_api_key(self) -> str:
        """Resolve key value: literal string or $ENV_VAR reference."""
        k = self.api_key
        if k.startswith("$"):
            env_val = os.environ.get(k[1:], "")
            if not env_val:
                CONSOLE.print(
                    f"[red]Env var {k[1:]} not set for provider '{self.name}'[/red]"
                )
                raise typer.Exit(1)
            return env_val
        return k


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
    # ── System prompt ─────────────────────────────────────────────────────
    "system_prompt_file": None,  # null = use built-in prompt
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


class MemoryIndex:
    """Manages the INDEX.md directory tree."""

    def __init__(self):
        self.path = INDEX_FILE
        self._ensure_dirs()

    def _ensure_dirs(self):
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        for chapter in ["projects", "knowledge", "people", "tasks", "archive"]:
            (MEMORY_DIR / chapter).mkdir(exist_ok=True)
            idx = MEMORY_DIR / chapter / "_index.md"
            if not idx.exists():
                idx.write_text(
                    f"# {chapter.capitalize()} Index\n\n_updated: {_now()}_\n\n"
                )
        if not self.path.exists():
            self._write_default_index()

    def _write_default_index(self):
        content = f"""# Memory Palace Index
_updated: {_now()}_

## Chapters
| Chapter | Files | Last Updated | Summary |
|---------|-------|--------------|---------|
| projects | 0 | {_now()} | Current projects |
| knowledge | 0 | {_now()} | Technical/conceptual notes |
| people | 0 | {_now()} | People and contacts |
| tasks | 0 | {_now()} | Tasks and todos |
| archive | 0 | {_now()} | Archived old content |
"""
        self.path.write_text(content)

    def read(self) -> str:
        if self.path.exists():
            return self.path.read_text()
        return ""

    def update(self):
        """Rewrite INDEX.md by scanning chapters."""
        rows = []
        for chapter in ["projects", "knowledge", "people", "tasks", "archive"]:
            chapter_dir = MEMORY_DIR / chapter
            files = [f for f in chapter_dir.glob("*.md") if f.name != "_index.md"]
            last_updated = max((f.stat().st_mtime for f in files), default=0)
            last_str = (
                datetime.fromtimestamp(last_updated).strftime("%Y-%m-%d")
                if last_updated
                else "—"
            )
            # Read summary from _index.md first line after heading
            idx_file = chapter_dir / "_index.md"
            summary = ""
            if idx_file.exists():
                lines = idx_file.read_text().splitlines()
                for line in lines[2:]:
                    if line.strip() and not line.startswith("_"):
                        summary = line.strip()[:60]
                        break
            rows.append(f"| {chapter} | {len(files)} | {last_str} | {summary} |")

        content = f"""# Memory Palace Index
_updated: {_now()}_

## Chapters
| Chapter | Files | Last Updated | Summary |
|---------|-------|--------------|---------|
{chr(10).join(rows)}
"""
        self.path.write_text(content)

    def list_chapters(self) -> list[dict]:
        chapters = []
        for chapter in ["projects", "knowledge", "people", "tasks", "archive"]:
            chapter_dir = MEMORY_DIR / chapter
            files = [f for f in chapter_dir.glob("*.md") if f.name != "_index.md"]
            chapters.append({"name": chapter, "files": [f.name for f in files]})
        return chapters


class MemoryChapter:
    """Read/write a single .md chapter file."""

    def __init__(self, chapter: str, name: str):
        self.chapter = chapter
        self.name = name
        self.path = MEMORY_DIR / chapter / f"{name}.md"

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> str:
        if self.path.exists():
            return self.path.read_text()
        return ""

    def write(self, content: str):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(content)
        # Update chapter index
        idx_file = self.path.parent / "_index.md"
        self._update_chapter_index(idx_file)

    def append(self, content: str):
        existing = self.read()
        if existing:
            self.write(existing + "\n" + content)
        else:
            self.write(f"# {self.name}\n_created: {_now()}_\n\n" + content)

    def _update_chapter_index(self, idx_file: Path):
        files = [f for f in self.path.parent.glob("*.md") if f.name != "_index.md"]
        lines = [f"- [{f.stem}]({f.name})" for f in sorted(files)]
        idx_file.write_text(
            f"# {self.chapter.capitalize()} Index\n\n_updated: {_now()}_\n\n"
            + "\n".join(lines)
            + "\n"
        )


class MemoryPalace:
    """Facade for all memory operations."""

    def __init__(
        self,
        tidy_interval: int = MEMORY_TIDY_INTERVAL,
        tidy_threshold: int = MEMORY_TIDY_FILE_THRESHOLD,
    ):
        self.index = MemoryIndex()
        self._last_tidy: float = 0
        self._files_since_tidy: int = 0
        self._tidy_interval = tidy_interval
        self._tidy_threshold = tidy_threshold

    def write(self, chapter: str, name: str, content: str, append: bool = False):
        ch = MemoryChapter(chapter, name)
        if append:
            ch.append(content)
        else:
            ch.write(content)
        self._files_since_tidy += 1
        self.index.update()

    def read(self, chapter: str, name: str) -> str:
        return MemoryChapter(chapter, name).read()

    def search(self, query: str) -> list[dict]:
        results = []
        query_lower = query.lower()
        for md_file in MEMORY_DIR.rglob("*.md"):
            if md_file.name in ("INDEX.md", "_index.md"):
                continue
            text = md_file.read_text()
            if query_lower in text.lower():
                # Get first matching line
                for line in text.splitlines():
                    if query_lower in line.lower():
                        snippet = line.strip()[:120]
                        break
                else:
                    snippet = text[:120]
                rel = md_file.relative_to(MEMORY_DIR)
                results.append({"path": str(rel), "snippet": snippet})
        return results

    def list_chapters(self) -> list[dict]:
        return self.index.list_chapters()

    def read_index(self) -> str:
        return self.index.read()

    def should_tidy(self) -> bool:
        now = time.time()
        return (
            now - self._last_tidy
        ) > self._tidy_interval and self._files_since_tidy >= self._tidy_threshold

    async def tidy(self, client: anthropic.AsyncAnthropic, model: str):
        """AI-assisted memory reorganization."""
        CONSOLE.print("[dim]Tidying memory palace...[/dim]")
        # Collect all memory summaries
        summaries = []
        for md_file in MEMORY_DIR.rglob("*.md"):
            if md_file.name in ("INDEX.md", "_index.md"):
                continue
            text = md_file.read_text()
            rel = str(md_file.relative_to(MEMORY_DIR))
            summaries.append(f"## {rel}\n{text[:300]}\n")

        if not summaries:
            return

        prompt = (
            "Review these memory files and suggest:\n"
            "1. Files to merge (too similar)\n"
            "2. Files to archive (outdated)\n"
            "3. Reclassification suggestions\n\n"
            + "\n".join(summaries[:20])  # limit context
        )

        response = await client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        suggestion = response.content[0].text
        # Save tidy report to archive
        self.write("archive", f"tidy_{_datestamp()}", suggestion)
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

    File: ~/.agent/context/_staging.jsonl
    """

    def __init__(self, path: Path = STAGING_FILE):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

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
        return len(self.read_all())

    def clear_all(self) -> None:
        """Delete the staging file after successful consolidation."""
        self.path.unlink(missing_ok=True)


@dataclass
class LTMEntry:
    """A single long-term memory entry with importance scoring."""

    id: str
    content: str
    importance: float  # 0.0 – 1.0
    category: str
    created_at: str
    updated_at: str

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
    """File-based long-term memory with dynamic categories and upper limit.

    Storage layout: ~/.agent/context/
        _meta.json          — category registry
        <category>.json     — list of LTMEntry objects
    """

    def __init__(
        self,
        context_dir: Path = CONTEXT_DIR,
        max_categories: int = MAX_CATEGORIES,
    ):
        self.dir = context_dir
        self.max_categories = max_categories
        self._meta_path = context_dir / "_meta.json"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._meta = self._load_meta()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_meta(self) -> dict:
        if self._meta_path.exists():
            try:
                return json.loads(self._meta_path.read_text())
            except Exception:
                pass
        return {"categories": [], "total_entries": 0}

    def _save_meta(self) -> None:
        self._meta_path.write_text(json.dumps(self._meta, indent=2, ensure_ascii=False))

    def _category_path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    # ── Category helpers ──────────────────────────────────────────────────────

    def list_categories(self) -> list[LTMCategory]:
        return [LTMCategory.from_dict(c) for c in self._meta.get("categories", [])]

    def category_count(self) -> int:
        return len(self._meta.get("categories", []))

    # ── Entry CRUD ────────────────────────────────────────────────────────────

    def read_entries(self, category: str) -> list[LTMEntry]:
        path = self._category_path(category)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            return [LTMEntry.from_dict(e) for e in data]
        except Exception:
            return []

    def write_entries(self, category: str, entries: list[LTMEntry]) -> None:
        path = self._category_path(category)
        path.write_text(
            json.dumps([e.to_dict() for e in entries], indent=2, ensure_ascii=False)
        )
        avg_imp = sum(e.importance for e in entries) / len(entries) if entries else 0.0
        cats = self._meta.setdefault("categories", [])
        for c in cats:
            if c["name"] == category:
                c["entry_count"] = len(entries)
                c["avg_importance"] = avg_imp
                c["last_updated"] = _now()
                break
        else:
            cats.append(
                {
                    "name": category,
                    "entry_count": len(entries),
                    "avg_importance": avg_imp,
                    "last_updated": _now(),
                }
            )
        self._meta["total_entries"] = sum(c["entry_count"] for c in cats)
        self._save_meta()

    def add_entry(self, entry: LTMEntry) -> None:
        entries = self.read_entries(entry.category)
        entries.append(entry)
        self.write_entries(entry.category, entries)

    def all_entries(self) -> list[LTMEntry]:
        result: list[LTMEntry] = []
        for cat in self.list_categories():
            result.extend(self.read_entries(cat.name))
        return result

    # ── Maintenance ───────────────────────────────────────────────────────────

    def apply_decay(self, factor: float = DECAY_FACTOR) -> None:
        """Decay importance of all entries; prune those below MIN_IMPORTANCE."""
        for cat in self.list_categories():
            entries = self.read_entries(cat.name)
            for e in entries:
                e.decay(factor)
                e.updated_at = _now()
            entries = [e for e in entries if e.importance >= MIN_IMPORTANCE]
            self.write_entries(cat.name, entries)

    def merge_categories(self, cat_a: str, cat_b: str, merged_name: str) -> None:
        """Merge cat_a and cat_b into merged_name, delete originals."""
        entries_a = self.read_entries(cat_a)
        entries_b = self.read_entries(cat_b)
        for e in entries_a + entries_b:
            e.category = merged_name
        self.write_entries(merged_name, entries_a + entries_b)
        # Remove original files / meta rows if they differ from merged_name
        for old in (cat_a, cat_b):
            if old != merged_name:
                self._category_path(old).unlink(missing_ok=True)
                self._meta["categories"] = [
                    c for c in self._meta["categories"] if c["name"] != old
                ]
        self._save_meta()


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
        staged = staging.read_all() if (staging and staging.count() > 0) else []
        source = staged if staged else messages
        conv_text = self._format_messages_for_llm(source)
        source_label = (
            f"staging ({len(staged)} turns)"
            if staged
            else f"messages ({len(messages)})"
        )

        existing = [c.name for c in self.store.list_categories()]
        cat_list = ", ".join(existing) if existing else "none yet"

        prompt = (
            f"Analyze this conversation and extract important facts worth remembering.\n"
            f"Existing categories: {cat_list}\n\n"
            f"For each item output JSON on its own line (no markdown fences):\n"
            f'{{"category": "<name>", "content": "<fact>", "importance": <0.1-1.0>}}\n\n'
            f"Rules:\n"
            f"- importance: 1.0=critical decisions/preferences, 0.5=useful context, 0.1=minor\n"
            f"- Reuse existing categories when possible; create new ones only when necessary\n"
            f"- Be selective: max 10 items, 1-3 sentences each\n\n"
            f"Conversation ({source_label}):\n{conv_text[:3000]}"
        )

        try:
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

            entries = self._parse_entries(raw)
            for entry in entries:
                await self._ensure_category_fits(
                    entry.category, client, model, api_format
                )
                self.store.add_entry(entry)

            self.store.apply_decay(self.decay_factor)

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
                entries.append(
                    LTMEntry(
                        id=str(uuid.uuid4())[:8],
                        content=content,
                        importance=float(data.get("importance", 0.5)),
                        category=data.get("category", "general").strip(),
                        created_at=_now(),
                        updated_at=_now(),
                    )
                )
            except Exception:
                continue
        return entries

    async def _ensure_category_fits(
        self, category: str, client: Any, model: str, api_format: str
    ) -> None:
        """If adding new category exceeds limit, ask LLM to merge two existing."""
        existing = [c.name for c in self.store.list_categories()]
        if category in existing or self.store.category_count() < self.max_categories:
            return

        summaries = []
        for c in self.store.list_categories():
            entries = self.store.read_entries(c.name)
            snippets = "; ".join(e.content[:50] for e in entries[:3])
            summaries.append(f"- {c.name}: {snippets}")

        merge_prompt = (
            f"Memory has {len(existing)} categories (max {self.max_categories}). "
            f"Need to add '{category}'. Choose two categories to merge.\n\n"
            f"Categories:\n" + "\n".join(summaries) + "\n\n"
            f'Respond with JSON only: {{"merge_a": "<cat1>", "merge_b": "<cat2>", "merged_name": "<name>"}}'
        )
        try:
            if api_format == "anthropic":
                resp = await client.messages.create(
                    model=model,
                    max_tokens=256,
                    messages=[{"role": "user", "content": merge_prompt}],
                )
                raw = resp.content[0].text
            else:
                resp = await client.chat.completions.create(
                    model=model,
                    max_tokens=256,
                    messages=[{"role": "user", "content": merge_prompt}],
                )
                raw = resp.choices[0].message.content or ""

            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                data = json.loads(m.group())
                self.store.merge_categories(
                    data["merge_a"], data["merge_b"], data["merged_name"]
                )
                CONSOLE.print(
                    f"[dim]Merged: {data['merge_a']} + {data['merge_b']} "
                    f"→ {data['merged_name']}[/dim]"
                )
        except Exception as e:
            CONSOLE.print(f"[dim]Category merge error: {e}[/dim]")


class ContextManager:
    """Orchestrates LTM storage, retrieval, and consolidation.

    Trigger rules (all require _needs_consolidation == True):
      1. Token-ratio trigger  — working memory > token_ratio × max_tokens
      2. Idle trigger         — no activity for idle_seconds (background task)
      3. Session-end trigger  — explicit call when the interactive loop exits
    After each sleep() the flag is cleared; mark_activity() re-arms it.

    Staging buffer: every user/assistant turn is appended to _staging.jsonl.
    This ensures consolidation always has a complete source even if the session
    ends before the token threshold fires.  Buffer is cleared after each sleep.
    """

    def __init__(
        self,
        store: LTMStore,
        retriever: LocalRetriever,
        consolidation: ConsolidationEngine,
        idle_seconds: int = 300,
        min_messages: int = 4,
        staging: Optional[StagingBuffer] = None,
    ):
        self.store = store
        self.retriever = retriever
        self.consolidation = consolidation
        self.idle_seconds = idle_seconds
        self.min_messages = min_messages
        self.staging: StagingBuffer = staging or StagingBuffer()
        self._needs_consolidation: bool = False
        self._last_activity: float = 0.0

    # ── Activity tracking ─────────────────────────────────────────────────────

    def mark_activity(self) -> None:
        """Call after each user message to arm consolidation and reset idle timer."""
        self._last_activity = time.time()
        self._needs_consolidation = True

    def idle_elapsed(self) -> float:
        """Seconds since last activity (0 if never active)."""
        if self._last_activity == 0.0:
            return 0.0
        return time.time() - self._last_activity

    # ── Trigger checks ────────────────────────────────────────────────────────

    def should_sleep(self, messages: list[dict], max_tokens: int) -> bool:
        """Token-ratio trigger: only fires when dirty and messages are sufficient."""
        if not self._needs_consolidation:
            return False
        if len(messages) < self.min_messages:
            return False
        return self.consolidation.should_sleep(messages, max_tokens)

    def should_idle_sleep(self, messages: list[dict]) -> bool:
        """Idle trigger: fires when dirty, sufficient messages, and idle long enough."""
        if not self._needs_consolidation:
            return False
        if len(messages) < self.min_messages:
            return False
        return self.idle_elapsed() >= self.idle_seconds

    def should_session_end_sleep(self) -> bool:
        """Session-end trigger: fires when staging has unprocessed content."""
        return self._needs_consolidation and self.staging.count() > 0

    # ── Retrieval ─────────────────────────────────────────────────────────────

    def retrieve_context(self, query: str, top_k: int = RETRIEVAL_TOP_K) -> str:
        """Return top-K relevant LTM entries as an injectable string."""
        entries = self.store.all_entries()
        if not entries:
            return ""
        top = self.retriever.retrieve(query, entries, top_k=top_k)
        if not top:
            return ""
        lines = ["## Retrieved Context (from long-term memory)"]
        for e in top:
            lines.append(f"- [{e.category}] {e.content}")
        return "\n".join(lines)

    # ── Consolidation ─────────────────────────────────────────────────────────

    async def sleep(
        self,
        messages: list[dict],
        client: Any,
        model: str,
        api_format: str = "anthropic",
    ) -> list[dict]:
        """Run one sleep cycle (uses staging as source), then clear dirty flag."""
        result = await self.consolidation.consolidate(
            messages, client, model, api_format, staging=self.staging
        )
        self._needs_consolidation = False
        return result

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        cats = self.store.list_categories()
        return {
            "categories": len(cats),
            "total_entries": sum(c.entry_count for c in cats),
            "category_names": [c.name for c in cats],
            "max_categories": self.store.max_categories,
            "needs_consolidation": self._needs_consolidation,
            "staged_turns": self.staging.count(),
            "idle_elapsed_s": round(self.idle_elapsed()),
            "idle_threshold_s": self.idle_seconds,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 3. TOOLS / SKILLS / MCP
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict
    fn: Callable


class ToolRegistry:
    """Central registry for all tools."""

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(self, name: str, description: str, parameters: dict, fn: Callable):
        self._tools[name] = ToolDef(
            name=name, description=description, parameters=parameters, fn=fn
        )

    def tool(self, name: str, description: str, parameters: dict):
        """Decorator for registering tools."""

        def decorator(fn: Callable):
            self.register(name, description, parameters, fn)
            return fn

        return decorator

    def to_anthropic_format(self) -> list[dict]:
        result = []
        for t in self._tools.values():
            result.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
            )
        return result

    async def call(self, tool_name: str, tool_input: dict) -> str:
        if tool_name not in self._tools:
            return f"Error: tool '{tool_name}' not found"
        try:
            fn = self._tools[tool_name].fn
            if asyncio.iscoroutinefunction(fn):
                result = await fn(**tool_input)
            else:
                result = fn(**tool_input)
            return str(result)
        except Exception as e:
            return f"Error calling tool '{tool_name}': {e}\n{traceback.format_exc()}"

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())


# Global registry
REGISTRY = ToolRegistry()


class BuiltinTools:
    """Built-in tools: shell, file, web, memory_write, context_retrieve."""

    def __init__(
        self,
        memory: MemoryPalace,
        registry: ToolRegistry,
        context_manager: Optional["ContextManager"] = None,
    ):
        self.memory = memory
        self.registry = registry
        self.context_manager = context_manager
        self._register()

    def _register(self):
        r = self.registry

        r.register(
            "shell",
            "Execute a shell command and return stdout/stderr. Use for system operations, running scripts, etc.",
            {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30)",
                        "default": 30,
                    },
                },
                "required": ["command"],
            },
            self._shell,
        )

        r.register(
            "read_file",
            "Read the contents of a file.",
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative file path",
                    },
                },
                "required": ["path"],
            },
            self._read_file,
        )

        r.register(
            "write_file",
            "Write content to a file (creates or overwrites).",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
            self._write_file,
        )

        r.register(
            "list_files",
            "List files in a directory.",
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path (default: current dir)",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern (default: *)",
                    },
                },
                "required": [],
            },
            self._list_files,
        )

        r.register(
            "memory_write",
            "Write or append content to the memory palace.",
            {
                "type": "object",
                "properties": {
                    "chapter": {
                        "type": "string",
                        "description": "Chapter name: projects|knowledge|people|tasks|archive",
                        "enum": ["projects", "knowledge", "people", "tasks", "archive"],
                    },
                    "name": {
                        "type": "string",
                        "description": "File name (without .md)",
                    },
                    "content": {"type": "string", "description": "Content to write"},
                    "append": {
                        "type": "boolean",
                        "description": "Append instead of overwrite",
                        "default": False,
                    },
                },
                "required": ["chapter", "name", "content"],
            },
            self._memory_write,
        )

        r.register(
            "memory_read",
            "Read a memory chapter file.",
            {
                "type": "object",
                "properties": {
                    "chapter": {
                        "type": "string",
                        "description": "Chapter: projects|knowledge|people|tasks|archive",
                    },
                    "name": {
                        "type": "string",
                        "description": "File name (without .md)",
                    },
                },
                "required": ["chapter", "name"],
            },
            self._memory_read,
        )

        r.register(
            "memory_search",
            "Search across all memory files.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
            self._memory_search,
        )

        r.register(
            "memory_index",
            "Show the memory palace index.",
            {
                "type": "object",
                "properties": {},
                "required": [],
            },
            self._memory_index,
        )

        r.register(
            "context_retrieve",
            (
                "Search long-term context memory for relevant information. "
                "Use to recall past facts, user preferences, project context, "
                "or any information consolidated from previous sessions."
            ),
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query to retrieve relevant context",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of results (default 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
            self._context_retrieve,
        )

    async def _shell(self, command: str, timeout: int = 30) -> str:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out = stdout.decode(errors="replace")
            err = stderr.decode(errors="replace")
            result = ""
            if out:
                result += f"STDOUT:\n{out}"
            if err:
                result += f"STDERR:\n{err}"
            result += f"\nExit code: {proc.returncode}"
            return result or "(no output)"
        except asyncio.TimeoutError:
            return f"Error: command timed out after {timeout}s"
        except Exception as e:
            return f"Error: {e}"

    def _read_file(self, path: str) -> str:
        try:
            return Path(path).read_text()
        except Exception as e:
            return f"Error reading file: {e}"

    def _write_file(self, path: str, content: str) -> str:
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return f"Written {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error writing file: {e}"

    def _list_files(self, path: str = ".", pattern: str = "*") -> str:
        try:
            p = Path(path)
            files = list(p.glob(pattern))
            if not files:
                return f"No files matching '{pattern}' in {path}"
            return "\n".join(str(f) for f in sorted(files)[:100])
        except Exception as e:
            return f"Error listing files: {e}"

    def _memory_write(
        self, chapter: str, name: str, content: str, append: bool = False
    ) -> str:
        self.memory.write(chapter, name, content, append=append)
        action = "Appended to" if append else "Wrote"
        return f"{action} memory: {chapter}/{name}.md"

    def _memory_read(self, chapter: str, name: str) -> str:
        content = self.memory.read(chapter, name)
        return content if content else f"No memory file: {chapter}/{name}.md"

    def _memory_search(self, query: str) -> str:
        results = self.memory.search(query)
        if not results:
            return f"No memory found for query: '{query}'"
        lines = [f"- **{r['path']}**: {r['snippet']}" for r in results[:10]]
        return "\n".join(lines)

    def _memory_index(self) -> str:
        return self.memory.read_index()

    def _context_retrieve(self, query: str, top_k: int = 5) -> str:
        if self.context_manager is None:
            return "Context manager not available."
        result = self.context_manager.retrieve_context(query, top_k=top_k)
        return result if result else "No relevant context found."


class MCPClient:
    """Connect to external MCP servers and inject tools into registry."""

    def __init__(self, registry: ToolRegistry):
        self.registry = registry
        self._sessions = []

    async def connect_from_config(self, config: dict):
        if not HAS_MCP:
            return
        mcp_servers = config.get("mcp_servers", [])
        for server_cfg in mcp_servers:
            try:
                await self._connect_server(server_cfg)
            except Exception as e:
                CONSOLE.print(
                    f"[yellow]MCP server connect failed ({server_cfg.get('name', '?')}): {e}[/yellow]"
                )

    async def _connect_server(self, cfg: dict):
        # Placeholder for actual MCP SDK integration
        # When mcp package is available, implement proper connection
        pass


class SkillLoader:
    """Dynamically load skills from ~/.agent/skills/*.py"""

    def __init__(self, registry: ToolRegistry):
        self.registry = registry
        self._loaded: set[str] = set()

    def load_all(self):
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        for skill_file in SKILLS_DIR.glob("*.py"):
            if skill_file.name not in self._loaded:
                self._load_skill(skill_file)

    def _load_skill(self, path: Path):
        try:
            spec = importlib.util.spec_from_file_location(path.stem, path)
            if spec is None or spec.loader is None:
                return
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Look for functions decorated with @register_tool or having _tool_meta attribute
            for name, obj in inspect.getmembers(module, inspect.isfunction):
                if hasattr(obj, "_tool_meta"):
                    meta = obj._tool_meta
                    self.registry.register(
                        meta["name"], meta["description"], meta["parameters"], obj
                    )
                    CONSOLE.print(
                        f"[dim]Loaded skill: {meta['name']} from {path.name}[/dim]"
                    )

            self._loaded.add(path.name)
        except Exception as e:
            CONSOLE.print(f"[yellow]Failed to load skill {path.name}: {e}[/yellow]")

    def reload(self):
        self._loaded.clear()
        self.load_all()


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
                import json as _json

                tool_calls = []
                for tc in msg.tool_calls:
                    try:
                        inp = _json.loads(tc.function.arguments)
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
            retrieved = self.context_manager.retrieve_context(user_message)
            if retrieved:
                ctx.system_prompt = ctx.system_prompt + "\n\n" + retrieved

        ctx.messages.append({"role": "user", "content": user_message})
        tool_calls_made = []
        result_text = ""

        try:
            while True:
                tools = self.registry.to_anthropic_format() if ctx.tools_enabled else []

                try:
                    if stream_callback:
                        # Stream for display; collect text but don't trust it for
                        # tool-use detection — follow up with one non-stream call.
                        result_text = await self._stream_response(
                            ctx, tools, stream_callback
                        )
                        stream_callback = None  # don't double-print on next iter

                    # Single authoritative non-streaming call (also handles post-tool turns)
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
    ) -> str:
        """Stream response text chunk-by-chunk, calling callback per chunk."""
        collected: list[str] = []
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
            else:
                # OpenAI streaming
                kwargs: dict = dict(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    messages=self._inject_system(ctx.messages, ctx.system_prompt),
                    stream=True,
                )
                api_tools = self._tools_for_api(tools)
                if api_tools:
                    kwargs["tools"] = api_tools
                async for chunk in await self.client.chat.completions.create(**kwargs):
                    delta = chunk.choices[0].delta.content if chunk.choices else None
                    if delta:
                        collected.append(delta)
                        callback(delta)
        except Exception as e:
            import traceback

            traceback.print_exc()
        return "".join(collected)

    def register_spawn_capability(self, base_system_prompt: str) -> None:
        """Register the spawn_agent tool.

        The main agent can call spawn_agent one or more times in a single turn.
        Multiple calls are executed in parallel (via asyncio.gather in send_message).
        Sub-agents receive all regular tools but NOT spawn_agent, preventing recursion.
        """
        parent = self  # captured reference to the parent agent

        async def spawn_agent(role: str, task: str, system_suffix: str = "") -> str:
            # Build a leaf registry: all tools except spawn_agent itself
            sub_registry = ToolRegistry()
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
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. SELF-EVOLUTION
# ─────────────────────────────────────────────────────────────────────────────


# 6. SELF-EVOLUTION
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

    async def score_session(
        self, messages: list[dict], prompt_version: str, tools_used: list[str]
    ) -> dict:
        """Let Claude score the session quality."""
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
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text
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

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        new_prompt = response.content[0].text

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

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        code = response.content[0].text

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
        for section in ("memory", "orchestration", "evolution", "mcp_servers"):
            if section not in raw and section in DEFAULT_CONFIG:
                raw[section] = DEFAULT_CONFIG[section]
        return raw, first_run
    except Exception as e:
        CONSOLE.print(f"[yellow]Config parse error: {e} — using defaults[/yellow]")
        return dict(DEFAULT_CONFIG), first_run


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (override wins)."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


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


def get_config_value(cfg: dict, key: str, default: Any = None) -> Any:
    return cfg.get(key, default)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _datestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _load_system_prompt(cfg: dict) -> str:
    best = PROMPTS_DIR / "best.md"
    if best.exists():
        content = best.read_text()
        content = re.sub(r"^<!--.*?-->\n", "", content, flags=re.DOTALL)
        return content
    prompt_file = cfg.get("system_prompt_file")
    if prompt_file and Path(prompt_file).exists():
        return Path(prompt_file).read_text()
    return DEFAULT_SYSTEM_PROMPT


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


def _build_components(cfg: dict):
    """Build all components from config using ModelClientFactory."""
    client, model, max_tokens = ModelClientFactory.from_config(cfg)
    system_prompt = _load_system_prompt(cfg)

    # Sub-config sections
    mem_cfg = cfg.get("memory", {})
    orch_cfg = cfg.get("orchestration", {})

    # Resolve active provider format for format-aware classes
    active_provider = cfg.get("active_provider", "anthropic")
    api_format = (
        cfg.get("providers", {}).get(active_provider, {}).get("api_format", "anthropic")
    )

    memory = MemoryPalace(
        tidy_interval=mem_cfg.get("tidy_interval_seconds", MEMORY_TIDY_INTERVAL),
        tidy_threshold=mem_cfg.get("tidy_file_threshold", MEMORY_TIDY_FILE_THRESHOLD),
    )
    registry = ToolRegistry()

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

    BuiltinTools(memory, registry, context_manager=ctx_manager)

    skill_loader = SkillLoader(registry)
    skill_loader.load_all()

    agent = BaseAgent(
        client, registry, model=model, max_tokens=max_tokens, api_format=api_format
    )
    agent.register_spawn_capability(system_prompt)
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
        "skill_loader": skill_loader,
        "cfg": cfg,
    }


async def _interactive_loop(components: dict, cfg: dict):
    """Main interactive chat loop."""
    agent: BaseAgent = components["agent"]
    memory: MemoryPalace = components["memory"]
    evolution: EvolutionEngine = components["evolution"]
    system_prompt = components["system_prompt"]
    ctx_mgr: Optional[ContextManager] = components.get("context_manager")

    # Get prompt version
    prompt_files = sorted(PROMPTS_DIR.glob("system_v*.md"))
    prompt_version = prompt_files[-1].stem if prompt_files else "default"

    ctx = AgentContext(system_prompt=system_prompt)
    tools_used_session: list[str] = []

    # ── Background idle consolidation task ────────────────────────────────────
    async def _idle_consolidation_loop():
        """Poll every 30 s; consolidate when dirty + idle threshold exceeded."""
        while True:
            await asyncio.sleep(30)
            if ctx_mgr and ctx_mgr.should_idle_sleep(ctx.messages):
                CONSOLE.print("\n[dim]💤 Idle consolidation triggered...[/dim]")
                ctx.messages = await ctx_mgr.sleep(
                    ctx.messages,
                    components["client"],
                    components["model"],
                    api_format=agent.api_format,
                )

    idle_task = asyncio.create_task(_idle_consolidation_loop())

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
                    CONSOLE.print(f"[yellow]Unknown command: {user_input}[/yellow]")
                    continue

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

                # Token-ratio consolidation (dirty flag + min_messages already checked)
                if ctx_mgr and ctx_mgr.should_sleep(ctx.messages, agent.max_tokens):
                    ctx.messages = await ctx_mgr.sleep(
                        ctx.messages,
                        components["client"],
                        components["model"],
                        api_format=agent.api_format,
                    )

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
                    components["skill_loader"].reload()

            except Exception as e:
                CONSOLE.print(f"\n[red]Error: {e}[/red]")

            # Idle memory tidy check
            if memory.should_tidy():
                await memory.tidy(components["client"], components["model"])

    finally:
        # Cancel background idle consolidation task on exit
        idle_task.cancel()
        try:
            await idle_task
        except asyncio.CancelledError:
            pass

    # Session-end consolidation: digest any unprocessed staging content
    if ctx_mgr and ctx_mgr.should_session_end_sleep():
        CONSOLE.print("[dim]💤 Session-end consolidation...[/dim]")
        try:
            await ctx_mgr.sleep(
                ctx.messages,
                components["client"],
                components["model"],
                api_format=agent.api_format,
            )
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
        components = _build_components(cfg)
        asyncio.run(_interactive_loop(components, cfg))


@app.command()
def chat(question: str = typer.Argument(..., help="Question or task for the agent")):
    """Single-turn chat with the agent."""
    cfg, first_run = load_config()
    if first_run:
        if not _first_run_setup():
            raise typer.Exit(0)
        cfg, _ = load_config()
    components = _build_components(cfg)
    agent: BaseAgent = components["agent"]
    ctx = AgentContext(system_prompt=components["system_prompt"])

    async def _run():
        CONSOLE.print("[bold blue]Agent[/bold blue]: ", end="")
        result = await agent.send_message(
            ctx,
            question,
            stream_callback=lambda chunk: CONSOLE.print(chunk, end="", markup=False),
        )
        CONSOLE.print()
        if result.error:
            CONSOLE.print(f"[red]Error: {result.error}[/red]")

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
    components = _build_components(cfg)
    evolution: EvolutionEngine = components["evolution"]

    async def _run():
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
        import json as _json

        CONSOLE.print(
            Markdown(f"```json\n{_json.dumps(cfg, indent=2, ensure_ascii=False)}\n```")
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
    components = _build_components(cfg)

    async def _run():
        mem: MemoryPalace = components["memory"]
        # Force tidy by resetting timer
        mem._last_tidy = 0
        mem._files_since_tidy = MEMORY_TIDY_FILE_THRESHOLD
        await mem.tidy(components["client"], components["model"])

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
