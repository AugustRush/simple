"""Tests for LTMEntry, LTMCategory, LTMStore."""

import pytest


def make_store(tmp_path):
    from agent import LTMStore

    return LTMStore(context_dir=tmp_path / "context")


def make_entry(
    cid="abc", content="Python async patterns", importance=0.8, category="code_context"
):
    from agent import LTMEntry

    return LTMEntry(
        id=cid,
        content=content,
        importance=importance,
        category=category,
        created_at="2026-01-01",
        updated_at="2026-01-01",
    )


def test_add_and_read_entry(tmp_path):
    store = make_store(tmp_path)
    entry = make_entry()
    store.add_entry(entry)
    entries = store.read_entries("code_context")
    assert len(entries) == 1
    assert entries[0].content == "Python async patterns"
    assert entries[0].importance == 0.8


def test_entry_to_dict_round_trip(tmp_path):
    from agent import LTMEntry

    entry = make_entry()
    d = entry.to_dict()
    restored = LTMEntry.from_dict(d)
    assert restored.id == entry.id
    assert restored.content == entry.content
    assert restored.importance == entry.importance


def test_category_count(tmp_path):
    store = make_store(tmp_path)
    for cat in ["a", "b", "c"]:
        store.add_entry(make_entry(cid=cat, category=cat))
    assert store.category_count() == 3


def test_list_categories(tmp_path):
    store = make_store(tmp_path)
    store.add_entry(make_entry(cid="1", category="alpha"))
    store.add_entry(make_entry(cid="2", category="beta"))
    names = [c.name for c in store.list_categories()]
    assert "alpha" in names
    assert "beta" in names


def test_all_entries(tmp_path):
    store = make_store(tmp_path)
    store.add_entry(make_entry(cid="1", category="alpha"))
    store.add_entry(make_entry(cid="2", category="beta"))
    store.add_entry(make_entry(cid="3", category="alpha"))
    assert len(store.all_entries()) == 3


def test_decay_reduces_importance(tmp_path):
    store = make_store(tmp_path)
    store.add_entry(make_entry(importance=1.0))
    store.apply_decay(factor=0.5)
    entries = store.read_entries("code_context")
    assert entries[0].importance == pytest.approx(0.5)


def test_decay_prunes_low_importance(tmp_path):
    store = make_store(tmp_path)
    store.add_entry(make_entry(importance=0.06))
    for _ in range(20):
        store.apply_decay(factor=0.5)
    entries = store.read_entries("code_context")
    assert len(entries) == 0  # pruned below MIN_IMPORTANCE (0.05)


def test_decay_removes_empty_category_metadata_and_file(tmp_path):
    store = make_store(tmp_path)
    store.add_entry(make_entry(importance=0.06))

    store.apply_decay(factor=0.5)

    assert store.category_count() == 0
    assert store.list_categories() == []
    assert not (tmp_path / "context" / "code_context.json").exists()


def test_merge_categories(tmp_path):
    store = make_store(tmp_path)
    store.add_entry(make_entry(cid="1", category="cat_a"))
    store.add_entry(make_entry(cid="2", category="cat_b"))
    store.merge_categories("cat_a", "cat_b", "merged")
    assert store.category_count() == 1
    merged_entries = store.read_entries("merged")
    assert len(merged_entries) == 2
    assert all(e.category == "merged" for e in merged_entries)


def test_merge_removes_old_files(tmp_path):
    store = make_store(tmp_path)
    store.add_entry(make_entry(cid="1", category="cat_a"))
    store.add_entry(make_entry(cid="2", category="cat_b"))
    store.merge_categories("cat_a", "cat_b", "merged")
    assert not (tmp_path / "context" / "cat_a.json").exists()
    assert not (tmp_path / "context" / "cat_b.json").exists()
    assert not (tmp_path / "context" / "merged.json").exists()


def test_category_stats_are_derived_from_sqlite_across_instances(tmp_path):
    store1 = make_store(tmp_path)
    store1.add_entry(make_entry(cid="1", category="persistent"))
    assert not (tmp_path / "context" / "_meta.json").exists()

    # New instance derives stats from SQLite, not _meta.json.
    from agent import LTMStore

    store2 = LTMStore(context_dir=tmp_path / "context")
    assert store2.category_count() == 1
    assert store2.read_entries("persistent")[0].id == "1"


def test_category_names_are_normalized_inside_context_dir(tmp_path):
    store = make_store(tmp_path)
    entry = make_entry(cid="1", category="../../Tmp Dir/Unsafe Name")

    store.add_entry(entry)

    categories = store.list_categories()
    assert len(categories) == 1
    assert categories[0].name == "tmp_dir_unsafe_name"
    assert (
        store.read_entries("tmp_dir_unsafe_name")[0].category == "tmp_dir_unsafe_name"
    )
    assert not (tmp_path / "Tmp Dir").exists()


def test_add_entry_upserts_identity_preference(tmp_path):
    from agent import LTMEntry, LTMStore

    store = LTMStore(
        context_dir=tmp_path / "context",
        memory_dir=tmp_path / "memory",
    )
    first = LTMEntry(
        id="pref-a",
        category="identity",
        entity="user",
        memory_type="preference",
        content="Prefers concise responses",
        importance=0.8,
        status="active",
        created_at="2026-04-11",
        updated_at="2026-04-11",
    )
    second = LTMEntry(
        id="pref-b",
        category="identity",
        entity="user",
        memory_type="preference",
        content="Prefers concise responses",
        importance=0.9,
        status="active",
        created_at="2026-04-11",
        updated_at="2026-04-11",
    )

    store.add_entry(first)
    store.add_entry(second)

    entries = store.read_entries("identity")
    assert len([e for e in entries if e.content == "Prefers concise responses"]) == 1
    assert entries[0].importance == 0.9


def test_add_entry_upserts_task_status(tmp_path):
    from agent import LTMEntry, LTMStore

    store = LTMStore(
        context_dir=tmp_path / "context",
        memory_dir=tmp_path / "memory",
    )
    open_task = LTMEntry(
        id="task-open",
        category="tasks",
        entity="fix_auth_bug",
        memory_type="task",
        content="Fix the auth bug",
        importance=0.9,
        status="open",
        created_at="2026-04-11",
        updated_at="2026-04-11",
    )
    done_task = LTMEntry(
        id="task-done",
        category="tasks",
        entity="fix_auth_bug",
        memory_type="task",
        content="Fix the auth bug",
        importance=0.9,
        status="done",
        created_at="2026-04-11",
        updated_at="2026-04-11",
    )

    store.add_entry(open_task)
    store.add_entry(done_task)

    entries = store.read_entries("tasks")
    assert len([e for e in entries if e.entity == "fix_auth_bug"]) == 1
    assert entries[0].status == "done"


def test_add_entry_keeps_distinct_tasks_for_same_entity(tmp_path):
    from agent import LTMEntry, LTMStore

    store = LTMStore(
        context_dir=tmp_path / "context",
        memory_dir=tmp_path / "memory",
    )
    store.add_entry(
        LTMEntry(
            id="task-1",
            category="tasks",
            entity="backend",
            memory_type="task",
            content="Fix the auth bug",
            importance=0.9,
            status="open",
            created_at="2026-04-11",
            updated_at="2026-04-11",
        )
    )
    store.add_entry(
        LTMEntry(
            id="task-2",
            category="tasks",
            entity="backend",
            memory_type="task",
            content="Ship the retry worker",
            importance=0.8,
            status="open",
            created_at="2026-04-12",
            updated_at="2026-04-12",
        )
    )

    entries = [e for e in store.read_entries("tasks") if e.entity == "backend"]

    assert len(entries) == 2
    assert {e.content for e in entries} == {
        "Fix the auth bug",
        "Ship the retry worker",
    }


def test_add_entry_preserves_created_at_when_upserting(tmp_path):
    from agent import LTMEntry, LTMStore

    store = LTMStore(
        context_dir=tmp_path / "context",
        memory_dir=tmp_path / "memory",
    )
    first = LTMEntry(
        id="pref-a",
        category="identity",
        entity="user",
        memory_type="preference",
        content="Prefers concise responses",
        importance=0.8,
        status="active",
        created_at="2026-04-10",
        updated_at="2026-04-10",
    )
    second = LTMEntry(
        id="pref-b",
        category="identity",
        entity="user",
        memory_type="preference",
        content="Prefers concise responses",
        importance=0.9,
        status="active",
        created_at="2026-04-12",
        updated_at="2026-04-12",
    )

    store.add_entry(first)
    store.add_entry(second)

    entry = store.read_entries("identity")[0]
    assert entry.id == "pref-a"
    assert entry.created_at == "2026-04-10"
    assert entry.updated_at == "2026-04-12"


def test_write_entries_does_not_project_markdown_files(tmp_path):
    from agent import LTMEntry, LTMStore

    memory_dir = tmp_path / "memory"
    store = LTMStore(
        context_dir=tmp_path / "context",
        memory_dir=memory_dir,
    )
    store.add_entry(
        LTMEntry(
            id="user-note",
            category="identity",
            entity="user",
            memory_type="preference",
            content="Prefers concise responses",
            importance=0.8,
            status="active",
            created_at="2026-04-11",
            updated_at="2026-04-11",
        )
    )

    assert not (memory_dir / "identity" / "user.md").exists()

    store.write_entries(
        "identity",
        [
            LTMEntry(
                id="admin-note",
                category="identity",
                entity="admin",
                memory_type="preference",
                content="Needs detailed traces",
                importance=0.7,
                status="active",
                created_at="2026-04-12",
                updated_at="2026-04-12",
            )
        ],
    )

    assert not (memory_dir / "identity" / "user.md").exists()
    assert not (memory_dir / "identity" / "admin.md").exists()


def test_add_entry_maps_overflow_dynamic_categories_to_concepts(tmp_path):
    from agent import LTMEntry, LTMStore

    store = LTMStore(
        context_dir=tmp_path / "context",
        memory_dir=tmp_path / "memory",
        max_categories=1,
    )
    store.add_entry(
        LTMEntry(
            id="alpha",
            category="alpha",
            content="First dynamic category",
            importance=0.5,
            created_at="2026-04-11",
            updated_at="2026-04-11",
        )
    )
    store.add_entry(
        LTMEntry(
            id="beta",
            category="beta",
            content="Second dynamic category",
            importance=0.6,
            created_at="2026-04-12",
            updated_at="2026-04-12",
        )
    )

    dynamic_categories = [
        category.name
        for category in store.list_categories()
        if category.name not in {"concepts"}
    ]
    assert dynamic_categories == ["alpha"]
    concepts_entries = store.read_entries("concepts")
    assert len(concepts_entries) == 1
    assert concepts_entries[0].entity == "beta"
    assert concepts_entries[0].content == "Second dynamic category"


def test_search_entries_queries_fts_index(tmp_path, monkeypatch):
    from agent import LTMEntry, LTMStore

    store = LTMStore(
        context_dir=tmp_path / "context",
        memory_dir=tmp_path / "memory",
    )
    store.add_entry(
        LTMEntry(
            id="identity-1",
            category="identity",
            entity="user",
            memory_type="preference",
            content="Prefers concise responses",
            importance=0.8,
            status="active",
            created_at="2026-04-11",
            updated_at="2026-04-11",
        )
    )

    real_connect = store._connect
    seen_sql: list[str] = []

    class _ObservedConn:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._conn.__exit__(exc_type, exc, tb)

        def execute(self, sql, params=()):
            seen_sql.append(" ".join(str(sql).split()))
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(store, "_connect", lambda: _ObservedConn(real_connect()))

    results = store.search_entries("concise responses")

    assert [entry.id for entry in results] == ["identity-1"]
    assert any("memory_items_fts" in sql for sql in seen_sql)


def test_add_entry_does_not_write_user_visible_projections(tmp_path):
    from agent import LTMEntry, LTMStore

    store = LTMStore(
        context_dir=tmp_path / "context",
        memory_dir=tmp_path / "memory",
    )
    store.add_entry(
        LTMEntry(
            id="alpha",
            category="projects",
            entity="demo",
            content="Project alpha",
            importance=0.5,
            created_at="2026-04-11",
            updated_at="2026-04-11",
        )
    )
    store.add_entry(
        LTMEntry(
            id="beta",
            category="identity",
            entity="user",
            content="Prefers concise responses",
            importance=0.8,
            created_at="2026-04-11",
            updated_at="2026-04-11",
        )
    )

    store.add_entry(
        LTMEntry(
            id="task-1",
            category="tasks",
            entity="fix_auth_bug",
            content="Fix the auth bug",
            importance=0.9,
            created_at="2026-04-12",
            updated_at="2026-04-12",
        )
    )

    # The file projection layer is gone, so "nothing user-visible was written"
    # is now a statement about the filesystem: facts live in SQLite only.
    assert not list((tmp_path / "memory").rglob("*.md"))
    assert not list((tmp_path / "memory").rglob("*.json"))
    assert not (tmp_path / "context" / "_meta.json").exists()


def test_ensure_fts_index_repairs_mismatched_rows_even_when_counts_match(tmp_path):
    from agent import LTMEntry, LTMStore

    store = LTMStore(
        context_dir=tmp_path / "context",
        memory_dir=tmp_path / "memory",
    )
    store.add_entry(
        LTMEntry(
            id="identity-1",
            category="identity",
            entity="user",
            memory_type="preference",
            content="Prefers concise responses",
            importance=0.8,
            status="active",
            created_at="2026-04-11",
            updated_at="2026-04-11",
        )
    )

    with store._connect() as conn:
        conn.execute("DELETE FROM memory_items_fts")
        conn.execute(
            """
            INSERT INTO memory_items_fts (memory_id, content, entity, category)
            VALUES (?, ?, ?, ?)
            """,
            ("wrong-id", "unrelated text", "other", "concepts"),
        )

    store._ensure_fts_index()

    results = store.search_entries("concise responses")
    assert [entry.id for entry in results] == ["identity-1"]


def test_upsert_manual_note_uses_non_truncated_generated_ids(tmp_path):
    store = make_store(tmp_path)

    entry = store.upsert_manual_note("identity", "user", "Prefers concise responses")

    assert len(entry.id) > 8


def test_writes_do_not_recompute_category_stats(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    scans = 0
    real_stats = store._category_stats

    def counting_stats():
        nonlocal scans
        scans += 1
        return real_stats()

    monkeypatch.setattr(store, "_category_stats", counting_stats)

    for index in range(20):
        store.add_entry(make_entry(cid=f"e{index}", content=f"fact {index}"))

    # Each write used to trigger a full GROUP BY over memory_items.
    assert scans == 0

    # …and the derived view is still correct once something actually asks.
    assert store._meta["total_entries"] == 20
    assert scans == 1

    # Cached until the next mutation, then recomputed on demand.
    assert store._meta["total_entries"] == 20
    assert scans == 1
    store.add_entry(make_entry(cid="e20", content="fact 20"))
    assert store._meta["total_entries"] == 21
    assert scans == 2


# ── Corpus statistics for the re-ranker ────────────────────────────────────


def test_corpus_stats_counts_documents_not_candidates(tmp_path):
    store = make_store(tmp_path)
    for i in range(10):
        store.add_entry(make_entry(cid=f"common-{i}", content="deployment notes"))
    store.add_entry(make_entry(cid="rare", content="kubernetes deployment notes"))

    stats = store.corpus_stats(["kubernetes", "deployment"])

    assert stats.total_documents == 11
    assert stats.document_frequencies["kubernetes"] == 1
    assert stats.document_frequencies["deployment"] == 11
    # The rare term must carry more IDF weight than the ubiquitous one.
    assert stats.idf("kubernetes", fallback_n=11) > stats.idf(
        "deployment", fallback_n=11
    )


def test_corpus_stats_handles_cjk_terms(tmp_path):
    store = make_store(tmp_path)
    store.add_entry(make_entry(cid="zh-1", content="用户偏好简洁回答"))
    store.add_entry(make_entry(cid="zh-2", content="用户喜欢中文"))
    store.add_entry(make_entry(cid="en-1", content="unrelated english note"))

    stats = store.corpus_stats(["用户", "简洁"])

    assert stats.total_documents == 3
    assert stats.document_frequencies["用户"] == 2
    assert stats.document_frequencies["简洁"] == 1


def test_corpus_stats_respects_scope_filter(tmp_path):
    from agent import LTMEntry

    store = make_store(tmp_path)
    store.add_entry(
        LTMEntry(
            id="g",
            content="scoped kubernetes note",
            importance=0.5,
            category="code_context",
            scope="global",
            created_at="2026-01-01",
            updated_at="2026-01-01",
        )
    )
    store.add_entry(
        LTMEntry(
            id="s",
            content="scoped kubernetes note",
            importance=0.5,
            category="code_context",
            scope="session:other",
            created_at="2026-01-01",
            updated_at="2026-01-01",
        )
    )

    scoped = store.corpus_stats(["kubernetes"], scopes=["global"])

    assert scoped.total_documents == 1
    assert scoped.document_frequencies["kubernetes"] == 1


def test_corpus_stats_empty_terms(tmp_path):
    store = make_store(tmp_path)
    stats = store.corpus_stats([])
    assert stats.total_documents == 0
    assert dict(stats.document_frequencies) == {}


def test_corpus_stats_estimates_cjk_df_from_a_bounded_sample(tmp_path, monkeypatch):
    """CJK df is sampled, so its cost does not grow with the store.

    Exactness is not the goal — IDF is a logarithm, so a df/N ratio good to a
    few percent is indistinguishable from an exact count in the score.
    """
    from agent.memory import store as store_mod

    store = make_store(tmp_path)
    # 200 entries; "用户" in exactly half.
    for i in range(200):
        text = "用户偏好记录" if i % 2 == 0 else "项目部署记录"
        store.add_entry(make_entry(cid=f"zh-{i:04d}", content=text))

    monkeypatch.setattr(store_mod, "_CJK_DF_SAMPLE_SIZE", 50)
    stats = store.corpus_stats(["用户"])

    assert stats.total_documents == 200
    # Scaled back up to the corpus: ~100, not the 50-row sample count.
    assert 60 <= stats.document_frequencies["用户"] <= 140


def test_corpus_stats_marks_a_ubiquitous_cjk_term_as_uninformative(tmp_path):
    store = make_store(tmp_path)
    for i in range(50):
        store.add_entry(make_entry(cid=f"zh-{i:04d}", content="记录用户偏好"))

    stats = store.corpus_stats(["记录"])

    assert stats.document_frequencies["记录"] == stats.total_documents
    # df == N ⇒ IDF ≈ 0: the term cannot discriminate between these entries.
    assert stats.idf("记录", fallback_n=50) < 0.05


def test_corpus_stats_caps_latin_counts_for_ubiquitous_terms(tmp_path):
    store = make_store(tmp_path)
    for i in range(200):
        store.add_entry(make_entry(cid=f"en-{i:04d}", content="deployment notes here"))

    stats = store.corpus_stats(["deployment"])

    assert stats.document_frequencies["deployment"] == stats.total_documents
    assert stats.idf("deployment", fallback_n=200) < 0.05
