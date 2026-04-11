"""Tests for the fixed-loci memory palace store and markdown projection."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


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


def test_store_persists_to_sqlite_and_projects_markdown(tmp_path):
    store = make_store(tmp_path)
    store.add_entry(make_entry())

    assert (tmp_path / "context" / "palace.db").exists()

    entries = store.read_entries("identity")
    assert len(entries) == 1
    assert entries[0].entity == "user"
    assert entries[0].memory_type == "preference"

    projection = tmp_path / "memory" / "identity" / "user.md"
    assert projection.exists()
    text = projection.read_text()
    assert "Prefers concise responses" in text
    assert "identity/user" in text


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
    assert (tmp_path / "memory" / "concepts" / "python.md").exists()


def test_memory_palace_reads_from_store_when_projection_is_missing(tmp_path):
    from agent import MemoryPalace

    palace = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
    )
    palace.write("identity", "user", "Prefers concise responses")

    projection = tmp_path / "memory" / "identity" / "user.md"
    assert projection.exists()
    projection.unlink()

    assert "Prefers concise responses" in palace.read("identity", "user")


def test_memory_palace_search_uses_structured_store_as_source_of_truth(tmp_path):
    from agent import MemoryPalace

    palace = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
    )
    palace.write("identity", "user", "Prefers concise responses")

    projection = tmp_path / "memory" / "identity" / "user.md"
    projection.unlink()

    results = palace.search("concise")

    assert results
    assert results[0]["path"] == "identity/user"
    assert "Prefers concise responses" in results[0]["snippet"]
