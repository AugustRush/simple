from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def normalize_memory_chapter(chapter: str, aliases: dict[str, str]) -> str:
    chapter = str(chapter).strip().lower()
    return aliases.get(chapter, chapter)


class MemoryIndex:
    """Manages the INDEX.md directory tree."""

    def __init__(
        self,
        base_dir: Path,
        loci: tuple[str, ...],
        aliases: dict[str, str],
        summaries: dict[str, str],
        now_fn: Callable[[], str],
    ):
        self.base_dir = base_dir
        self.loci = loci
        self.aliases = aliases
        self.summaries = summaries
        self.now_fn = now_fn
        self.path = self.base_dir / "INDEX.md"
        self._ensure_dirs()

    def normalize_chapter(self, chapter: str) -> str:
        return normalize_memory_chapter(chapter, self.aliases)

    def _ensure_dirs(self):
        self.base_dir.mkdir(parents=True, exist_ok=True)
        for chapter in self.loci:
            (self.base_dir / chapter).mkdir(exist_ok=True)
            idx = self.base_dir / chapter / "_index.md"
            if not idx.exists():
                idx.write_text(
                    f"# {chapter.capitalize()} Index\n\n_updated: {self.now_fn()}_\n\n"
                )
        if not self.path.exists():
            self._write_default_index()

    def _write_default_index(self):
        rows = [
            f"| {chapter} | 0 | {self.now_fn()} | {self.summaries[chapter]} |"
            for chapter in self.loci
        ]
        content = (
            f"# Memory Palace Index\n_updated: {self.now_fn()}_\n\n## Chapters\n"
            "| Chapter | Files | Last Updated | Summary |\n"
            "|---------|-------|--------------|---------|\n"
            f"{chr(10).join(rows)}\n"
        )
        self.path.write_text(content)

    def read(self) -> str:
        if self.path.exists():
            return self.path.read_text()
        return ""

    def update(self):
        rows = []
        for chapter in self.loci:
            chapter_dir = self.base_dir / chapter
            files = [f for f in chapter_dir.glob("*.md") if f.name != "_index.md"]
            last_updated = max((f.stat().st_mtime for f in files), default=0)
            last_str = (
                datetime.fromtimestamp(last_updated, tz=timezone.utc).strftime("%Y-%m-%d")
                if last_updated
                else "—"
            )
            idx_file = chapter_dir / "_index.md"
            summary = ""
            if idx_file.exists():
                lines = idx_file.read_text().splitlines()
                for line in lines[2:]:
                    if line.strip() and not line.startswith("_"):
                        summary = line.strip()[:60]
                        break
            if not summary:
                summary = self.summaries.get(chapter, "")
            rows.append(f"| {chapter} | {len(files)} | {last_str} | {summary} |")

        content = (
            f"# Memory Palace Index\n_updated: {self.now_fn()}_\n\n## Chapters\n"
            "| Chapter | Files | Last Updated | Summary |\n"
            "|---------|-------|--------------|---------|\n"
            f"{chr(10).join(rows)}\n"
        )
        self.path.write_text(content)

    def list_chapters(self) -> list[dict]:
        chapters = []
        for chapter in self.loci:
            chapter_dir = self.base_dir / chapter
            files = [f for f in chapter_dir.glob("*.md") if f.name != "_index.md"]
            chapters.append({"name": chapter, "files": [f.name for f in files]})
        return chapters


class MemoryChapter:
    """Read/write a single .md chapter file."""

    def __init__(
        self,
        chapter: str,
        name: str,
        base_dir: Path,
        normalize_chapter: Callable[[str], str],
        now_fn: Callable[[], str],
    ):
        self.chapter = normalize_chapter(chapter)
        self.name = name
        self.base_dir = base_dir
        self.now_fn = now_fn
        self.path = self.base_dir / self.chapter / f"{name}.md"

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> str:
        if self.path.exists():
            return self.path.read_text()
        return ""

    def write(self, content: str):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(content)
        idx_file = self.path.parent / "_index.md"
        self._update_chapter_index(idx_file)

    def append(self, content: str):
        existing = self.read()
        if existing:
            self.write(existing + "\n" + content)
        else:
            self.write(f"# {self.name}\n_created: {self.now_fn()}_\n\n" + content)

    def _update_chapter_index(self, idx_file: Path):
        files = [f for f in self.path.parent.glob("*.md") if f.name != "_index.md"]
        lines = [f"- [{f.stem}]({f.name})" for f in sorted(files)]
        idx_file.write_text(
            f"# {self.chapter.capitalize()} Index\n\n_updated: {self.now_fn()}_\n\n"
            + "\n".join(lines)
            + "\n"
        )
