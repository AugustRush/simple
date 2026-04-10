# Context Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 agent.py 中实现分层上下文管理系统，包括本地 BM25 检索、LLM 驱动的"睡眠"整理机制和动态分类长期记忆。

**Architecture:** WorkingMemory (RAM 中的 ctx.messages) + LongTermMemory (本地 JSON 文件，动态分类，上限 15 个) + LocalRetriever (BM25-lite) + ConsolidationEngine (LLM 驱动的睡眠整理，token 阈值触发)。

**Tech Stack:** Python 3.10+, math/re/collections (stdlib only), anthropic SDK, 已有的 agent.py 结构。

---

## 文件结构

| 文件 | 操作 | 说明 |
|------|------|------|
| `agent.py` | Modify | 新增 Section 2.5 (约 400 行新代码) + 修改 Section 4/8 |
| `tests/test_ltm_store.py` | Create | LTMStore 单元测试 |
| `tests/test_retriever.py` | Create | LocalRetriever 单元测试 |
| `tests/test_consolidation.py` | Create | ConsolidationEngine 单元测试 |

## 新增常量（插入 Section 1 末尾）

```python
CONTEXT_DIR = AGENT_HOME / "context"
MAX_CATEGORIES = 15
MIN_IMPORTANCE = 0.05
CHARS_PER_TOKEN = 4
SLEEP_TOKEN_RATIO = 0.70
DECAY_FACTOR = 0.95
RETRIEVAL_TOP_K = 5
```

---

## Task 1: 数据结构 — LTMEntry / LTMCategory / LTMStore

**Files:**
- Modify: `agent.py` — 在 `MemoryPalace` 类之后、`ToolRegistry` 之前插入新 Section 2.5

- [ ] **Step 1: 写失败测试**

```python
# tests/test_ltm_store.py
import pytest, json
from pathlib import Path
import sys; sys.path.insert(0, str(Path(__file__).parent.parent))

def make_store(tmp_path):
    from agent import LTMStore
    return LTMStore(context_dir=tmp_path / "context")

def test_add_and_read_entry(tmp_path):
    from agent import LTMStore, LTMEntry
    store = make_store(tmp_path)
    entry = LTMEntry(id="abc", content="Python async patterns", importance=0.8,
                     category="code_context", created_at="2026-01-01", updated_at="2026-01-01")
    store.add_entry(entry)
    entries = store.read_entries("code_context")
    assert len(entries) == 1
    assert entries[0].content == "Python async patterns"

def test_category_count(tmp_path):
    from agent import LTMStore, LTMEntry
    store = make_store(tmp_path)
    for cat in ["a", "b", "c"]:
        entry = LTMEntry(id=cat, content="test", importance=0.5,
                         category=cat, created_at="now", updated_at="now")
        store.add_entry(entry)
    assert store.category_count() == 3

def test_decay_prunes_low_importance(tmp_path):
    from agent import LTMStore, LTMEntry
    store = make_store(tmp_path)
    entry = LTMEntry(id="x", content="old info", importance=0.06,
                     category="misc", created_at="now", updated_at="now")
    store.add_entry(entry)
    # Apply many decay cycles
    for _ in range(20):
        store.apply_decay(0.5)
    entries = store.read_entries("misc")
    assert len(entries) == 0  # pruned below MIN_IMPORTANCE

def test_merge_categories(tmp_path):
    from agent import LTMStore, LTMEntry
    store = make_store(tmp_path)
    for cat, cid in [("cat_a", "1"), ("cat_b", "2")]:
        entry = LTMEntry(id=cid, content=f"content {cid}", importance=0.7,
                         category=cat, created_at="now", updated_at="now")
        store.add_entry(entry)
    store.merge_categories("cat_a", "cat_b", "merged")
    assert store.category_count() == 1
    merged_entries = store.read_entries("merged")
    assert len(merged_entries) == 2
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_ltm_store.py -v 2>&1 | head -20
```
期望: `ImportError` 或 `ModuleNotFoundError` (LTMStore 未定义)

- [ ] **Step 3: 实现 LTMEntry / LTMCategory / LTMStore**

在 `agent.py` 的 `# 2. MEMORY LAYER` 段末尾（`MemoryPalace` 类之后）、`# 3. TOOLS` 段之前插入：

```python
# ─────────────────────────────────────────────────────────────────────────────
# 2.5. CONTEXT MANAGER — LTM + Retrieval + Consolidation
# ─────────────────────────────────────────────────────────────────────────────


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
    """File-based long-term memory with dynamic categories and upper limit."""

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

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load_meta(self) -> dict:
        if self._meta_path.exists():
            try:
                return json.loads(self._meta_path.read_text())
            except Exception:
                pass
        return {"categories": [], "total_entries": 0}

    def _save_meta(self) -> None:
        self._meta_path.write_text(
            json.dumps(self._meta, indent=2, ensure_ascii=False)
        )

    def _category_path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    # ── Category helpers ─────────────────────────────────────────────────────

    def list_categories(self) -> list[LTMCategory]:
        return [LTMCategory.from_dict(c) for c in self._meta.get("categories", [])]

    def category_count(self) -> int:
        return len(self._meta.get("categories", []))

    # ── Entry CRUD ───────────────────────────────────────────────────────────

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
        cats = self._meta.setdefault("categories", [])
        avg_imp = (
            sum(e.importance for e in entries) / len(entries) if entries else 0.0
        )
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
        result = []
        for cat in self.list_categories():
            result.extend(self.read_entries(cat.name))
        return result

    # ── Maintenance ──────────────────────────────────────────────────────────

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
        # Remove original files/meta if they differ from merged_name
        for old in (cat_a, cat_b):
            if old != merged_name:
                self._category_path(old).unlink(missing_ok=True)
                self._meta["categories"] = [
                    c for c in self._meta["categories"] if c["name"] != old
                ]
        self._save_meta()
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_ltm_store.py -v
```
期望: 4 tests PASSED

- [ ] **Step 5: Commit**

```bash
git add agent.py tests/test_ltm_store.py
git commit -m "feat: add LTMEntry/LTMCategory/LTMStore for context manager"
```

---

## Task 2: LocalRetriever — BM25-lite 本地检索

**Files:**
- Modify: `agent.py` — 在 `LTMStore` 之后追加 `LocalRetriever` 类

- [ ] **Step 1: 写失败测试**

```python
# tests/test_retriever.py
import pytest
from pathlib import Path
import sys; sys.path.insert(0, str(Path(__file__).parent.parent))

def make_entries():
    from agent import LTMEntry
    return [
        LTMEntry("1", "Python async await coroutine patterns", 0.9, "code", "now", "now"),
        LTMEntry("2", "User prefers concise responses without emojis", 0.8, "prefs", "now", "now"),
        LTMEntry("3", "Database connection pooling with asyncpg", 0.7, "code", "now", "now"),
        LTMEntry("4", "Project deadline is end of April 2026", 0.6, "tasks", "now", "now"),
        LTMEntry("5", "Machine learning model training pipeline", 0.5, "code", "now", "now"),
    ]

def test_tokenize(tmp_path):
    from agent import LocalRetriever
    r = LocalRetriever()
    tokens = r.tokenize("Hello World async patterns")
    assert "hello" in tokens
    assert "async" in tokens

def test_retrieve_top_k(tmp_path):
    from agent import LocalRetriever
    r = LocalRetriever()
    entries = make_entries()
    result = r.retrieve("async python patterns", entries, top_k=2)
    assert len(result) == 2
    # The most relevant entry should be about async/Python
    ids = [e.id for e in result]
    assert "1" in ids  # "Python async await coroutine patterns"

def test_retrieve_returns_empty_for_irrelevant_query(tmp_path):
    from agent import LocalRetriever
    r = LocalRetriever()
    entries = make_entries()
    result = r.retrieve("xyzzy foobar nonsense123", entries, top_k=3)
    assert len(result) == 0  # no matching terms

def test_importance_boosts_ranking(tmp_path):
    from agent import LocalRetriever, LTMEntry
    r = LocalRetriever()
    entries = [
        LTMEntry("low",  "python code function", 0.1, "c", "now", "now"),
        LTMEntry("high", "python code function", 0.9, "c", "now", "now"),
    ]
    result = r.retrieve("python code", entries, top_k=2)
    assert result[0].id == "high"  # higher importance wins on equal text score
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_retriever.py -v 2>&1 | head -20
```

- [ ] **Step 3: 实现 LocalRetriever**

在 `LTMStore` 类之后追加：

```python
class LocalRetriever:
    """BM25-lite retrieval with importance boosting. Pure stdlib, no external deps."""

    K1: float = 1.5
    B: float = 0.75

    @staticmethod
    def tokenize(text: str) -> list[str]:
        return re.findall(r"\b[a-zA-Z\u4e00-\u9fff][a-zA-Z0-9\u4e00-\u9fff]*\b", text.lower())

    def score(
        self, query: str, entries: list[LTMEntry]
    ) -> list[tuple[LTMEntry, float]]:
        if not entries:
            return []
        query_terms = self.tokenize(query)
        if not query_terms:
            return [(e, e.importance) for e in entries]

        from collections import Counter

        N = len(entries)
        df: dict[str, int] = {}
        tokenized: list[list[str]] = []

        for entry in entries:
            tokens = self.tokenize(entry.content)
            tokenized.append(tokens)
            for term in set(tokens):
                df[term] = df.get(term, 0) + 1

        avg_dl = sum(len(t) for t in tokenized) / N if N else 1.0

        scored = []
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
                tf_norm = tf * (self.K1 + 1) / (
                    tf + self.K1 * (1 - self.B + self.B * dl / avg_dl)
                )
                bm25 += idf * tf_norm

            final = bm25 * (1.0 + entry.importance)
            scored.append((entry, final))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def retrieve(
        self, query: str, entries: list[LTMEntry], top_k: int = RETRIEVAL_TOP_K
    ) -> list[LTMEntry]:
        scored = self.score(query, entries)
        return [entry for entry, s in scored[:top_k] if s > 0]
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_retriever.py -v
```
期望: 4 tests PASSED

- [ ] **Step 5: Commit**

```bash
git add agent.py tests/test_retriever.py
git commit -m "feat: add LocalRetriever with BM25-lite scoring and importance boost"
```

---

## Task 3: ConsolidationEngine — 睡眠整理机制

**Files:**
- Modify: `agent.py` — 在 `LocalRetriever` 之后追加 `ConsolidationEngine`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_consolidation.py
import pytest
from pathlib import Path
import sys; sys.path.insert(0, str(Path(__file__).parent.parent))

def make_engine(tmp_path):
    from agent import LTMStore, ConsolidationEngine
    store = LTMStore(context_dir=tmp_path / "context")
    return ConsolidationEngine(store=store)

def test_estimate_tokens():
    from agent import ConsolidationEngine, LTMStore
    # Using a dummy store path (won't be written)
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        store = LTMStore(pathlib.Path(d) / "c")
        eng = ConsolidationEngine(store=store)
        messages = [
            {"role": "user", "content": "Hello world"},       # 11 chars
            {"role": "assistant", "content": "Hi there"},     # 8 chars
        ]
        tokens = eng.estimate_tokens(messages)
        assert tokens == (11 + 8) // 4

def test_should_sleep_true(tmp_path):
    from agent import ConsolidationEngine, LTMStore
    store = LTMStore(tmp_path / "c")
    eng = ConsolidationEngine(store=store, sleep_token_ratio=0.7)
    # 1000 chars / 4 = 250 tokens; max_tokens=300 → 250/300 = 83% > 70%
    messages = [{"role": "user", "content": "x" * 1000}]
    assert eng.should_sleep(messages, max_tokens=300) is True

def test_should_sleep_false(tmp_path):
    from agent import ConsolidationEngine, LTMStore
    store = LTMStore(tmp_path / "c")
    eng = ConsolidationEngine(store=store, sleep_token_ratio=0.7)
    messages = [{"role": "user", "content": "hello"}]  # 5 chars = 1 token
    assert eng.should_sleep(messages, max_tokens=8192) is False

def test_parse_entries(tmp_path):
    from agent import ConsolidationEngine, LTMStore
    store = LTMStore(tmp_path / "c")
    eng = ConsolidationEngine(store=store)
    raw = '''
Here are the extracted facts:
{"category": "code_context", "content": "User uses Python 3.11", "importance": 0.8}
{"category": "user_prefs", "content": "Prefers concise responses", "importance": 0.7}
Some non-JSON text here.
{"category": "tasks", "content": "Fix the auth bug", "importance": 0.9}
'''
    entries = eng._parse_entries(raw)
    assert len(entries) == 3
    assert entries[0].category == "code_context"
    assert entries[1].importance == 0.7
    assert entries[2].content == "Fix the auth bug"

def test_format_messages(tmp_path):
    from agent import ConsolidationEngine, LTMStore
    store = LTMStore(tmp_path / "c")
    eng = ConsolidationEngine(store=store)
    messages = [
        {"role": "user", "content": "What is async?"},
        {"role": "assistant", "content": "Async allows..."},
    ]
    text = eng._format_messages_for_llm(messages)
    assert "USER: What is async?" in text
    assert "ASSISTANT: Async allows..." in text
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_consolidation.py -v 2>&1 | head -20
```

- [ ] **Step 3: 实现 ConsolidationEngine**

在 `LocalRetriever` 之后追加：

```python
class ConsolidationEngine:
    """LLM-driven context consolidation — the 'sleep' mechanism."""

    def __init__(
        self,
        store: LTMStore,
        max_categories: int = MAX_CATEGORIES,
        decay_factor: float = DECAY_FACTOR,
        sleep_token_ratio: float = SLEEP_TOKEN_RATIO,
    ):
        self.store = store
        self.max_categories = max_categories
        self.decay_factor = decay_factor
        self.sleep_token_ratio = sleep_token_ratio

    # ── Trigger condition ─────────────────────────────────────────────────────

    def estimate_tokens(self, messages: list[dict]) -> int:
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
        return self.estimate_tokens(messages) >= int(max_tokens * self.sleep_token_ratio)

    # ── Main consolidation ────────────────────────────────────────────────────

    async def consolidate(
        self,
        messages: list[dict],
        client: Any,
        model: str,
        api_format: str = "anthropic",
        keep_last: int = 6,
    ) -> list[dict]:
        """Run one sleep cycle: extract → classify → decay → compress messages."""
        CONSOLE.print("[dim]💤 Context consolidation (sleep)...[/dim]")
        conv_text = self._format_messages_for_llm(messages)
        existing = [c.name for c in self.store.list_categories()]
        cat_list = ", ".join(existing) if existing else "none yet"

        prompt = (
            f"Analyze this conversation and extract important facts worth remembering.\n"
            f"Existing categories: {cat_list}\n\n"
            f"For each item output JSON on its own line (no markdown fences):\n"
            f'{{\"category\": \"<name>\", \"content\": \"<fact>\", \"importance\": <0.1-1.0>}}\n\n'
            f"Rules:\n"
            f"- importance: 1.0=critical decisions/preferences, 0.5=useful context, 0.1=minor\n"
            f"- Reuse existing categories when possible; create new only when necessary\n"
            f"- Be selective: max 10 items, 1-3 sentences each\n\n"
            f"Conversation:\n{conv_text[:3000]}"
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
            CONSOLE.print(
                f"[dim]💤 Stored {len(entries)} entries. "
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
                content = " ".join(parts)
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
                entry = LTMEntry(
                    id=str(uuid.uuid4())[:8],
                    content=data.get("content", "").strip(),
                    importance=float(data.get("importance", 0.5)),
                    category=data.get("category", "general").strip(),
                    created_at=_now(),
                    updated_at=_now(),
                )
                if entry.content:
                    entries.append(entry)
            except Exception:
                continue
        return entries

    async def _ensure_category_fits(
        self, category: str, client: Any, model: str, api_format: str
    ) -> None:
        """If adding new category would exceed limit, ask LLM to merge two existing ones."""
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
                    f"[dim]Merged: {data['merge_a']} + {data['merge_b']} → {data['merged_name']}[/dim]"
                )
        except Exception as e:
            CONSOLE.print(f"[dim]Category merge error: {e}[/dim]")
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_consolidation.py -v
```
期望: 5 tests PASSED

- [ ] **Step 5: Commit**

```bash
git add agent.py tests/test_consolidation.py
git commit -m "feat: add ConsolidationEngine with sleep/decay/category-merge"
```

---

## Task 4: ContextManager — 编排层

**Files:**
- Modify: `agent.py` — 在 `ConsolidationEngine` 之后追加 `ContextManager`

- [ ] **Step 1: 实现 ContextManager**

```python
class ContextManager:
    """Orchestrates LTM storage, retrieval, and consolidation."""

    def __init__(
        self,
        store: LTMStore,
        retriever: LocalRetriever,
        consolidation: ConsolidationEngine,
    ):
        self.store = store
        self.retriever = retriever
        self.consolidation = consolidation

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

    def should_sleep(self, messages: list[dict], max_tokens: int) -> bool:
        return self.consolidation.should_sleep(messages, max_tokens)

    async def sleep(
        self,
        messages: list[dict],
        client: Any,
        model: str,
        api_format: str = "anthropic",
    ) -> list[dict]:
        return await self.consolidation.consolidate(
            messages, client, model, api_format
        )

    def stats(self) -> dict:
        cats = self.store.list_categories()
        return {
            "categories": len(cats),
            "total_entries": sum(c.entry_count for c in cats),
            "category_names": [c.name for c in cats],
            "max_categories": self.store.max_categories,
        }
```

- [ ] **Step 2: 添加新常量到 Section 1**

在 `MEMORY_TIDY_INTERVAL` 常量附近添加：

```python
CONTEXT_DIR = AGENT_HOME / "context"
MAX_CATEGORIES = 15
MIN_IMPORTANCE = 0.05
CHARS_PER_TOKEN = 4
SLEEP_TOKEN_RATIO = 0.70
DECAY_FACTOR = 0.95
RETRIEVAL_TOP_K = 5
```

- [ ] **Step 3: 运行全部测试确认通过**

```bash
pytest tests/ -v
```
期望: 全部 PASSED

- [ ] **Step 4: Commit**

```bash
git add agent.py
git commit -m "feat: add ContextManager orchestration layer"
```

---

## Task 5: 集成 BaseAgent + _interactive_loop

**Files:**
- Modify: `agent.py:895-1119` (BaseAgent)
- Modify: `agent.py:1735+` (_interactive_loop)

- [ ] **Step 1: 给 BaseAgent 添加 context_manager 属性**

在 `BaseAgent.__init__` 末尾添加：
```python
self.context_manager: Optional["ContextManager"] = None
```

- [ ] **Step 2: 修改 send_message() 注入检索上下文**

在 `send_message` 函数体开头（`ctx.messages.append` 之前）插入：

```python
# Inject retrieved context from LTM
original_system = ctx.system_prompt
if self.context_manager:
    retrieved = self.context_manager.retrieve_context(user_message)
    if retrieved:
        ctx.system_prompt = ctx.system_prompt + "\n\n" + retrieved
```

在函数末尾 `return AgentResult(...)` 之前恢复：

```python
ctx.system_prompt = original_system
```

- [ ] **Step 3: 修改 _interactive_loop() 触发睡眠**

在 `result = await agent.send_message(...)` 之后、下一轮循环之前添加：

```python
# Trigger sleep consolidation if context is getting large
ctx_mgr = components.get("context_manager")
if ctx_mgr and ctx_mgr.should_sleep(ctx.messages, agent.max_tokens):
    ctx.messages = await ctx_mgr.sleep(
        ctx.messages, components["client"], components["model"],
        api_format=agent.api_format,
    )
```

- [ ] **Step 4: 修改 _build_components() 实例化 ContextManager**

在 `evolution = EvolutionEngine(...)` 之后添加：

```python
ctx_store = LTMStore(
    context_dir=CONTEXT_DIR,
    max_categories=cfg.get("context", {}).get("max_categories", MAX_CATEGORIES),
)
ctx_manager = ContextManager(
    store=ctx_store,
    retriever=LocalRetriever(),
    consolidation=ConsolidationEngine(
        store=ctx_store,
        decay_factor=cfg.get("context", {}).get("decay_factor", DECAY_FACTOR),
        sleep_token_ratio=cfg.get("context", {}).get("sleep_token_ratio", SLEEP_TOKEN_RATIO),
    ),
)
agent.context_manager = ctx_manager
```

在 `return {...}` 中添加 `"context_manager": ctx_manager`。

- [ ] **Step 5: 运行 agent 冒烟测试**

```bash
python agent.py --help
```
期望: 无 import error，正常显示帮助

- [ ] **Step 6: Commit**

```bash
git add agent.py
git commit -m "feat: integrate ContextManager into BaseAgent and interactive loop"
```

---

## Task 6: context_retrieve 工具 + /context 命令

**Files:**
- Modify: `agent.py` — BuiltinTools._register() + _interactive_loop()

- [ ] **Step 1: 注册 context_retrieve 工具**

在 `BuiltinTools.__init__` 中接收 `context_manager` 参数并在 `_register()` 注册工具：

```python
r.register(
    "context_retrieve",
    "Search long-term memory context for relevant information. Use when you need to recall past facts, user preferences, or project context.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "top_k": {"type": "integer", "description": "Max results (default 5)", "default": 5},
        },
        "required": ["query"],
    },
    lambda query, top_k=5: (
        self.context_manager.retrieve_context(query, top_k=top_k)
        if self.context_manager
        else "Context manager not available."
    ),
)
```

- [ ] **Step 2: 添加 /context 斜杠命令**

在 `_interactive_loop()` 的斜杠命令处理区块添加：

```python
elif cmd == "context":
    ctx_mgr = components.get("context_manager")
    if ctx_mgr:
        stats = ctx_mgr.stats()
        table = Table(title="Context Manager")
        table.add_column("Metric")
        table.add_column("Value")
        table.add_row("Categories", f"{stats['categories']}/{stats['max_categories']}")
        table.add_row("Total Entries", str(stats["total_entries"]))
        table.add_row("Category Names", ", ".join(stats["category_names"]) or "—")
        CONSOLE.print(table)
    continue
```

- [ ] **Step 3: 更新帮助提示**

在 `_interactive_loop()` 的启动 Panel 中的 Commands 行添加 `/context`：

```
[dim]Commands: /memory, /context, /evolve, /tools, /model [name], /quit[/dim]
```

- [ ] **Step 4: 运行冒烟测试**

```bash
python agent.py --help && echo "OK"
```

- [ ] **Step 5: 运行全部测试**

```bash
pytest tests/ -v
```
期望: 全部 PASSED

- [ ] **Step 6: Commit**

```bash
git add agent.py
git commit -m "feat: add context_retrieve tool and /context slash command"
```

---

## 验收标准

1. `pytest tests/ -v` 全部通过
2. `python agent.py --help` 无报错
3. 长对话（>70% token 阈值）自动触发睡眠整理
4. 每轮对话前自动检索 LTM 并注入 system prompt
5. 类别数量不超过 `MAX_CATEGORIES=15`
6. `/context` 命令显示当前 LTM 统计信息
