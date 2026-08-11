"""Facade over the memory subsystem's user-facing operations."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any, Optional

import agent as agent_module
from agent import shared

from ._helpers import _new_id, _now, normalize_memory_chapter
from .models import LTMEntry
from .store import LTMStore

class MemoryPalace:
    """Facade for all memory operations."""

    def __init__(
        self,
        tidy_interval: int = shared.MEMORY_TIDY_INTERVAL,
        tidy_threshold: int = shared.MEMORY_TIDY_FILE_THRESHOLD,
        base_dir: Optional[Path] = None,
        context_dir: Optional[Path] = None,
        store: Optional["LTMStore"] = None,
    ):
        # Resolve at call time so a ``--name`` home switch (via
        # ``_set_agent_home``) is honoured even though shared was imported long
        # before the CLI command ran.
        base_dir = base_dir or shared.MEMORY_DIR
        context_dir = context_dir or shared.CONTEXT_DIR
        self.store = store or LTMStore(context_dir=context_dir, memory_dir=base_dir)
        self.base_dir = self.store.memory_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._export_path = self.base_dir / "memory.jsonl"
        self._export_dirty = True
        self._last_tidy: float = 0
        self._files_since_tidy: int = 0
        self._tidy_interval = tidy_interval
        self._tidy_threshold = tidy_threshold

    def write(self, chapter: str, name: str, content: str, append: bool = False):
        chapter = normalize_memory_chapter(chapter, shared.LEGACY_MEMORY_ALIASES)
        self.store.upsert_manual_note(chapter, name, content, append=append)
        self._files_since_tidy += 1
        self._export_dirty = True

    def set_identity(
        self,
        *,
        subject: str = "assistant",
        name: Optional[str] = None,
        role: Optional[str] = None,
        persona: Optional[str] = None,
    ) -> dict[str, str]:
        """Set who the assistant (or the user) is. Later settings override earlier."""
        return self.store.set_identity(
            subject=subject, name=name, role=role, persona=persona
        )

    def read(self, chapter: str, name: str) -> str:
        chapter = normalize_memory_chapter(chapter, shared.LEGACY_MEMORY_ALIASES)
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
            anchor = (
                f"{entry.category}/{entry.entity}" if entry.entity else entry.category
            )
            results.append({"path": anchor, "snippet": entry.content[:120]})
        return results

    def read_index(self) -> str:
        path = self.export_jsonl()
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def clear(self) -> dict[str, int]:
        """Clear durable, model-retrievable memory through the storage API."""
        deleted = self.store.clear_persistent_memory()
        self._export_dirty = True
        self.export_jsonl()
        self._files_since_tidy = 0
        return deleted

    def export_jsonl(self, path: Optional[Path] = None) -> Path:
        path = path or self._export_path
        if path == self._export_path and path.exists() and not self._export_dirty:
            return path
        entries = sorted(
            self.store.all_entries(),
            key=lambda entry: (str(entry.updated_at), str(entry.id)),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(entry.to_dict(), ensure_ascii=False)
            for entry in entries
        ]
        # Durable primitive, not write_text: this replaces a complete export in
        # place, so a truncating write leaves a reader a short memory file that
        # parses fine and is simply missing entries.
        agent_module._atomic_write_text(
            path, ("\n".join(lines) + "\n") if lines else "", encoding="utf-8"
        )
        if path == self._export_path:
            self._export_dirty = False
        return path

    def should_tidy(self) -> bool:
        if self._files_since_tidy >= self._tidy_threshold:
            return True
        if self._tidy_interval > 0 and self._last_tidy > 0:
            if time.time() - self._last_tidy >= self._tidy_interval:
                return True
        return False

    def force_tidy(self) -> None:
        """Mark the palace as due for maintenance without exposing internals."""
        self._last_tidy = 0
        self._files_since_tidy = self._tidy_threshold

    async def tidy(self, client: Any, model: str):
        """Local maintenance pass: apply retention and refresh JSONL export."""
        shared.CONSOLE.print("[dim]Tidying memory palace...[/dim]")
        self.store.apply_retention()
        snapshot = self.store.maintenance_snapshot(limit=20)
        if snapshot:
            self.store.add_entry(
                LTMEntry(
                    id=_new_id(),
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
        self.export_jsonl()
        self._last_tidy = time.time()
        self._files_since_tidy = 0
        shared.CONSOLE.print("[dim]Memory tidy complete.[/dim]")
