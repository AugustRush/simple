"""Tests for structured fact assertions and resolved facts."""


def test_fact_store_creates_fact_tables(tmp_path):
    from agent import LTMStore

    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")

    with store._connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert "fact_assertions" in tables
    assert "resolved_facts" in tables


def test_add_fact_assertion_keeps_append_only_history(tmp_path):
    from agent import FactAssertion, LTMStore

    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")
    store.add_fact_assertion(
        FactAssertion(
            id="fact-1",
            subject="assistant",
            predicate="name",
            value="Afu",
            source_kind="bootstrap",
        )
    )
    store.add_fact_assertion(
        FactAssertion(
            id="fact-2",
            subject="assistant",
            predicate="name",
            value="Afu",
            source_kind="manual_write",
        )
    )

    assertions = store.read_fact_assertions(subject="assistant", predicate="name")

    assert [assertion.id for assertion in assertions] == ["fact-1", "fact-2"]
    assert [assertion.value for assertion in assertions] == ["Afu", "Afu"]


def test_resolve_fact_materializes_latest_winner_for_fact_key(tmp_path):
    from agent import FactAssertion, LTMStore

    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")
    store.add_fact_assertion(
        FactAssertion(
            id="fact-1",
            subject="assistant",
            predicate="name",
            value="Afu",
            source_kind="bootstrap",
            created_at="2026-04-25 12:00 UTC",
            updated_at="2026-04-25 12:00 UTC",
        )
    )

    resolved = store.resolve_fact("assistant", "name", "global")
    stored = store.read_resolved_facts(subject="assistant", predicate="name")

    assert resolved is not None
    assert resolved.subject == "assistant"
    assert resolved.predicate == "name"
    assert resolved.value == "Afu"
    assert len(stored) == 1
    assert stored[0].winning_assertion_id == "fact-1"


def test_resolve_fact_preserves_conflict_without_winner(tmp_path):
    from agent import FactAssertion, LTMStore

    store = LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")
    shared_created_at = "2026-04-25 12:00 UTC"
    store.add_fact_assertion(
        FactAssertion(
            id="fact-1",
            subject="assistant",
            predicate="name",
            value="Afu",
            source_kind="manual_write",
            confidence=0.9,
            created_at=shared_created_at,
            updated_at=shared_created_at,
        )
    )
    store.add_fact_assertion(
        FactAssertion(
            id="fact-2",
            subject="assistant",
            predicate="name",
            value="Buddy",
            source_kind="manual_write",
            confidence=0.9,
            created_at=shared_created_at,
            updated_at=shared_created_at,
        )
    )

    resolved = store.resolve_fact("assistant", "name", "global")
    assertions = store.read_fact_assertions(subject="assistant", predicate="name")
    stored = store.read_resolved_facts(subject="assistant", predicate="name")

    assert resolved is None
    assert stored == []
    assert {assertion.status for assertion in assertions} == {"conflicted"}
