"""Tests for the fixed-loci memory palace store and JSONL user export."""

import inspect


def make_store(tmp_path):
    from agent import LTMStore

    return LTMStore(
        context_dir=tmp_path / "context",
        memory_dir=tmp_path / "memory",
    )


def make_entry(
    cid="pref-1",
    content="Prefers concise responses",
    importance=0.9,
    category="identity",
    entity="user",
    memory_type="preference",
    source_session="session-1",
):
    from agent import LTMEntry

    return LTMEntry(
        id=cid,
        content=content,
        importance=importance,
        category=category,
        entity=entity,
        memory_type=memory_type,
        source_session=source_session,
        created_at="2026-04-11",
        updated_at="2026-04-11",
    )


def test_store_persists_to_sqlite_without_markdown_projection(tmp_path):
    store = make_store(tmp_path)
    store.add_entry(make_entry())

    assert (tmp_path / "context" / "palace.db").exists()

    entries = store.read_entries("identity")
    assert len(entries) == 1
    assert entries[0].entity == "user"
    assert entries[0].memory_type == "preference"

    projection = tmp_path / "memory" / "identity" / "user.md"
    assert not projection.exists()


def test_memory_palace_exports_user_jsonl(tmp_path):
    from agent import MemoryPalace

    palace = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
    )
    palace.write("identity", "user", "Prefers concise responses")

    path = palace.export_jsonl()

    assert path == tmp_path / "memory" / "memory.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert '"category": "identity"' in lines[0]
    assert '"content": "Prefers concise responses"' in lines[0]


def test_search_entries_can_be_filtered_by_locus(tmp_path):
    store = make_store(tmp_path)
    store.add_entry(
        make_entry(
            cid="identity-1",
            category="identity",
            entity="user",
            content="Prefers concise responses",
        )
    )
    store.add_entry(
        make_entry(
            cid="concept-1",
            category="concepts",
            entity="lambda_calculus",
            memory_type="concept",
            content="Concise responses are different from concise notation.",
        )
    )

    results = store.search_entries("concise responses", categories=["identity"])

    assert [e.id for e in results] == ["identity-1"]


def test_memory_palace_legacy_chapter_alias_maps_to_fixed_locus(tmp_path):
    from agent import MemoryPalace

    palace = MemoryPalace(base_dir=tmp_path / "memory")
    palace.write("knowledge", "python", "Async notes")

    assert palace.read("concepts", "python") == "Async notes"
    assert not (tmp_path / "memory" / "concepts" / "python.md").exists()


def test_memory_palace_reads_from_store_when_projection_is_missing(tmp_path):
    from agent import MemoryPalace

    palace = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
    )
    palace.write("identity", "user", "Prefers concise responses")

    projection = tmp_path / "memory" / "identity" / "user.md"
    assert not projection.exists()

    assert "Prefers concise responses" in palace.read("identity", "user")


def test_memory_palace_search_uses_structured_store_as_source_of_truth(tmp_path):
    from agent import MemoryPalace

    palace = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
    )
    palace.write("identity", "user", "Prefers concise responses")

    results = palace.search("concise")

    assert results
    assert results[0]["path"] == "identity/user"
    assert "Prefers concise responses" in results[0]["snippet"]


def test_memory_palace_tidy_accepts_generic_client_annotation():
    from agent import MemoryPalace

    annotation = inspect.signature(MemoryPalace.tidy).parameters["client"].annotation

    assert annotation is not inspect._empty
    assert "anthropic.AsyncAnthropic" not in str(annotation)


def test_memory_palace_force_tidy_marks_state_dirty(tmp_path):
    from agent import MemoryPalace

    palace = MemoryPalace(
        tidy_interval=3600,
        tidy_threshold=5,
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
    )

    assert palace.should_tidy() is False

    palace.force_tidy()

    assert palace.should_tidy() is True
