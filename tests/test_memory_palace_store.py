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


def test_memory_palace_clear_removes_durable_memory_and_pending_staging(tmp_path):
    from agent import LTMStore, MemoryPalace, StagingBuffer

    context_dir = tmp_path / "context"
    store = LTMStore(context_dir=context_dir, memory_dir=tmp_path / "memory")
    palace = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=context_dir,
        store=store,
    )
    staging = StagingBuffer(context_dir=context_dir, session_id="cli")
    store.add_entry(make_entry())
    store.write_conversation_exchange(
        session_id="cli",
        user_content="Remember this",
        assistant_content="Stored",
    )
    staging.append("user", "pending memory")

    deleted = palace.clear()

    assert deleted["memory_items"] == 1
    assert deleted["conversation_turns"] == 2
    assert deleted["staging_turns"] == 1
    assert store.all_entries() == []
    assert store.recent_conversation_turns(session_id="cli") == []
    assert staging.count() == 0
    assert palace.read_index() == ""


def test_memory_palace_does_not_create_chapter_dirs(tmp_path):
    from agent import MemoryPalace

    palace = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
    )
    palace.write("identity", "user", "Prefers concise responses")

    assert not (tmp_path / "memory" / "identity").exists()
    assert not (tmp_path / "memory" / "projects").exists()
    assert not (tmp_path / "memory" / "INDEX.md").exists()


def test_memory_palace_write_defers_jsonl_export_until_requested(tmp_path):
    from agent import MemoryPalace

    palace = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
    )

    palace.write("identity", "user", "Prefers concise responses")

    assert not (tmp_path / "memory" / "memory.jsonl").exists()
    assert "Prefers concise responses" in palace.read_index()
    assert (tmp_path / "memory" / "memory.jsonl").exists()


def test_memory_palace_exports_jsonl_in_updated_at_order(tmp_path):
    from agent import LTMEntry, LTMStore, MemoryPalace

    store = LTMStore(
        context_dir=tmp_path / "context",
        memory_dir=tmp_path / "memory",
    )
    store.add_entries(
        [
            LTMEntry(
                id="late",
                content="Second item",
                importance=0.7,
                category="projects",
                entity="demo",
                memory_type="decision",
                source_session="session-1",
                created_at="2026-04-21 10:00 UTC",
                updated_at="2026-04-21 11:00 UTC",
            ),
            LTMEntry(
                id="early",
                content="First item",
                importance=0.8,
                category="identity",
                entity="user",
                memory_type="preference",
                source_session="session-1",
                created_at="2026-04-21 08:00 UTC",
                updated_at="2026-04-21 09:00 UTC",
            ),
        ]
    )
    palace = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
        store=store,
    )

    path = palace.export_jsonl()
    lines = path.read_text(encoding="utf-8").splitlines()

    assert len(lines) == 2
    assert '"id": "early"' in lines[0]
    assert '"id": "late"' in lines[1]


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


def test_memory_palace_tidy_takes_no_llm_client():
    """tidy() is a store pass: no client, no model, nothing to mis-pair.

    It used to accept ``(client, model)`` that it never passed to anyone,
    which left callers free to hand it whichever provider's client happened
    to be active.  Declining the parameters is what makes that unreachable.
    """
    from agent import MemoryPalace

    parameters = inspect.signature(MemoryPalace.tidy).parameters

    assert list(parameters) == ["self"]


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


def test_ltm_store_legacy_cleanup_preserves_unrelated_json_and_markdown(tmp_path):
    from agent import LTMStore

    context_dir = tmp_path / "context"
    memory_dir = tmp_path / "memory"
    context_dir.mkdir()
    memory_dir.mkdir()
    custom_json = context_dir / "user-settings.json"
    custom_json.write_text('{"theme":"light"}', encoding="utf-8")
    custom_md = memory_dir / "notes.md"
    custom_md.write_text("keep me", encoding="utf-8")

    LTMStore(context_dir=context_dir, memory_dir=memory_dir)

    assert custom_json.exists()
    assert custom_md.exists()


def test_export_jsonl_replaces_the_file_durably(tmp_path, monkeypatch):
    """The export is a full in-place rewrite.

    A truncating write leaves a reader a short memory file that parses fine and
    is simply missing entries — silent, partial memory loss.  So the export goes
    through the durable primitive, and a reader never observes a shortened file.
    """
    from pathlib import Path

    from agent import MemoryPalace

    palace = MemoryPalace(
        base_dir=tmp_path / "memory",
        context_dir=tmp_path / "context",
    )
    # Distinct loci: write() upserts one note per (chapter, name), so reusing a
    # name would overwrite rather than accumulate.
    for index in range(12):
        palace.write("identity", f"user-{index}", f"fact number {index}")

    observed: list[int] = []
    real_replace = Path.replace

    def observing_replace(self, target):
        # The only instant the reader's view can change.
        if Path(target).name == "memory.jsonl" and Path(target).exists():
            observed.append(len(Path(target).read_text(encoding="utf-8").splitlines()))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", observing_replace)

    first = palace.export_jsonl()
    before = len(first.read_text(encoding="utf-8").splitlines())
    palace.write("identity", "user-extra", "one more fact")
    second = palace.export_jsonl()
    after = len(second.read_text(encoding="utf-8").splitlines())

    assert before == 12
    assert after == before + 1
    # Proves it went through the durable primitive, and every observed
    # intermediate state was a complete file.
    assert observed == [before]
    assert [p.name for p in (tmp_path / "memory").iterdir() if p.name.startswith(".")] == []


# ── Interjections are part of the turn that answered them ───────────────────


def _interjection_store(tmp_path):
    from agent import LTMStore

    return LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")


def test_an_interjection_is_journaled_between_the_request_and_the_answer(tmp_path):
    """The model answers it in this turn, so the record has to show the words.

    Before this, only the message that *opened* a turn was written: the
    transcript held an answer to a question that appeared nowhere, and the
    user's own sentence was gone -- from the display and from the memory
    pipeline alike.
    """
    store = _interjection_store(tmp_path)

    result = store.write_conversation_exchange(
        session_id="cli",
        user_content="列出文件",
        assistant_content="收到，先不总结",
        message_id="m1",
        interjections=[
            {"text": "这些文件里有 markdown 吗？", "message_id": "i1", "urgency": "now"}
        ],
    )

    assert result.interjections_created == ("这些文件里有 markdown 吗？",)
    turns = store.recent_conversation_turns(session_id="cli")
    assert [(t.role, t.content) for t in turns] == [
        ("user", "列出文件"),
        ("user", "这些文件里有 markdown 吗？"),
        ("assistant", "收到，先不总结"),
    ]
    # The flag is what lets a client render it as in-turn speech rather than
    # as the start of another turn.
    assert turns[1].metadata.get("interjection") is True
    assert turns[1].metadata.get("urgency") == "now"


def test_replaying_the_same_turn_does_not_duplicate_an_interjection(tmp_path):
    """The write is idempotent on the message id, like the user row beside it."""
    store = _interjection_store(tmp_path)
    entry = {"text": "补一句", "message_id": "i1"}

    for _ in range(3):
        result = store.write_conversation_exchange(
            session_id="cli",
            user_content="原始请求",
            assistant_content="回答",
            message_id="m1",
            interjections=[entry],
        )

    contents = [t.content for t in store.recent_conversation_turns(session_id="cli")]
    assert contents.count("补一句") == 1
    # Only the call that created the row reports it, so nothing stages the
    # same words into the memory pipeline twice.
    assert result.interjections_created == ()


def test_an_interjection_without_an_id_is_still_recorded_once_per_turn(tmp_path):
    """A channel that cannot name its messages must not lose them."""
    store = _interjection_store(tmp_path)

    store.write_conversation_exchange(
        session_id="cli",
        user_content="原始请求",
        assistant_content="回答",
        interjections=[{"text": "没有 id 的插话"}],
    )

    contents = [t.content for t in store.recent_conversation_turns(session_id="cli")]
    assert contents == ["原始请求", "没有 id 的插话", "回答"]
