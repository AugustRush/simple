"""What the memory page can do to a store, and the two invariants it must not break.

Two properties are load-bearing here and neither is visible from the endpoint
tests, which is why they live in their own file:

* **A pin survives everything that rewrites the row.**  Consolidation re-states
  entries through ``add_entry``, which replaces the whole row; retention decays
  ``episodes``.  A pin that either of those can drop is a button that lies --
  it says "keep this" and then the next background sweep throws it away.
* **Provenance is read from the evidence, not invented.**  A fact's author is
  the winning assertion's ``source_*`` columns; a store that cannot join must
  answer empty so the page can say "unknown", never pick a plausible session.

The rest (CRUD field-wise, delete idempotence, retract-then-resolve) is covered
for *both* backends by ``run_store_contract`` in ``test_memory_contract.py``.
"""

from __future__ import annotations

import pytest

from agent import shared
from agent.memory.models import FactAssertion, LTMEntry
from agent.memory.store import LTMStore


def _store(tmp_path) -> LTMStore:
    return LTMStore(context_dir=tmp_path / "context", memory_dir=tmp_path / "memory")


def _entry(identifier: str, **overrides) -> LTMEntry:
    fields = dict(
        id=identifier,
        content=f"memory {identifier}",
        importance=0.5,
        category="episodes",
        created_at="2026-10-26 00:00:00.000000 UTC",
        updated_at="2026-10-26 00:00:00.000000 UTC",
        entity="",
        memory_type="episode",
        source_session="s1",
    )
    fields.update(overrides)
    return LTMEntry(**fields)


def test_a_pinned_episode_survives_retention_and_its_twin_does_not(tmp_path):
    """The pair is the point: the same score, one flag, two different fates.

    A test that only asserted "the pinned one is still there" would pass on a
    retention pass that archived nothing at all.
    """
    store = _store(tmp_path)
    # Just above the floor, so one decay step takes it below.
    importance = shared.MIN_IMPORTANCE * 1.04
    store.add_entry(_entry("pinned", importance=importance, pinned=1))
    store.add_entry(_entry("loose", importance=importance))

    store.apply_retention()

    kept = {item.id: item for item in store.all_entries()}
    assert "pinned" in kept, "a pinned episode must not be archived by retention"
    assert "loose" not in kept, "the unpinned twin proves retention actually ran"
    # The pin exempts from decay, not from being visible: the score it had is
    # the score it keeps, which is what makes it rank where it ranked.
    assert kept["pinned"].importance == pytest.approx(importance)


def test_a_pin_survives_the_rewrite_consolidation_performs(tmp_path):
    """``add_entry`` replaces the row; the operator's flag is not the row's to lose."""
    store = _store(tmp_path)
    store.add_entry(_entry("m1"))
    store.update_entry("m1", pinned=True)

    # The exact shape consolidation writes: same id, freshly built object, no
    # ``pinned`` argument -- because the writer has no business knowing about it.
    store.add_entry(_entry("m1", content="memory m1, restated", importance=0.7))

    [row] = [item for item in store.all_entries() if item.id == "m1"]
    assert row.pinned == 1
    assert row.content == "memory m1, restated", "the rewrite still has to land"


def test_rewriting_an_entry_leaves_columns_it_does_not_name_alone(tmp_path):
    """The structural reason `pinned` was lost: ``INSERT OR REPLACE`` is a delete.

    Consolidation re-states an entry through ``add_entry``, which rewrites the row.
    With REPLACE that rewrite *takes the old row away and puts a new one in its
    place*, so any column the writer does not name returns to its default -- which
    is what silently unpinned a pinned memory (fixed then by carrying the value by
    hand) and what would silently lose every column added later.  An upsert that
    names the mutable columns removes the trap instead of documenting it.

    The column here is added at runtime on purpose: the test must not depend on
    today's schema, and the next person to add one gets this proof for free.
    """
    store = _store(tmp_path)
    store.add_entry(_entry("m1"))

    with store._connect() as conn:
        conn.execute("ALTER TABLE memory_items ADD COLUMN scratch TEXT DEFAULT ''")
        conn.execute("UPDATE memory_items SET scratch = 'kept' WHERE id = 'm1'")

    store.add_entry(_entry("m1", content="memory m1, restated"))

    with store._connect() as conn:
        row = conn.execute(
            "SELECT scratch FROM memory_items WHERE id = 'm1'"
        ).fetchone()
    assert row["scratch"] == "kept", "an unnamed column must survive a rewrite"


def test_deleting_an_entry_takes_its_search_row_with_it(tmp_path):
    """A deleted memory that still ranks is worse than one that was never deleted."""
    store = _store(tmp_path)
    store.add_entry(_entry("m1", content="用户只喝手冲咖啡", category="preferences"))
    assert [item.id for item in store.search_entries("手冲")] == ["m1"]

    assert store.delete_entry("m1") is True
    assert store.search_entries("手冲") == []
    assert store.all_entries() == []


def test_fact_provenance_is_the_winning_assertions(tmp_path):
    """The page's "where did this come from" has to be evidence, not a guess."""
    store = _store(tmp_path)
    store.add_fact_assertion(
        FactAssertion(
            id="f-consolidation",
            subject="user",
            predicate="drinks",
            value="filter coffee",
            source_kind="consolidation",
            source_session="session-7",
            channel="web",
        )
    )
    # A second assertion on the same key from a weaker source: it must not
    # overwrite the provenance while it does not win the resolution.
    store.add_fact_assertion(
        FactAssertion(
            id="f-inference",
            subject="user",
            predicate="drinks",
            value="filter coffee",
            source_kind="inference",
            source_session="session-9",
            channel="feishu",
            confidence=0.2,
        )
    )

    [fact] = store.read_resolved_facts(subject="user")
    assert fact.winning_assertion_id == "f-consolidation"
    assert fact.source_kind == "consolidation"
    assert fact.source_session == "session-7"
    assert fact.channel == "web"
    assert fact.asserted_at, "the assertion's own timestamp, not the resolution's"


def test_retracting_a_fact_leaves_its_evidence_readable(tmp_path):
    """Forgetting archives; it does not erase. The ledger keeps what was claimed."""
    store = _store(tmp_path)
    store.add_fact_assertion(
        FactAssertion(
            id="f1",
            subject="user",
            predicate="lives_in",
            value="Shanghai",
            source_kind="manual_write",
            source_session="s1",
        )
    )
    assert store.retract_facts("user", "lives_in") == 1
    assert store.read_resolved_facts(subject="user") == []

    # The evidence survives in the ledger, archived: that is what distinguishes
    # "this was rejected" from "this was never said", and it is the thing a
    # later audit (or a re-consolidation that sees the same sentence) needs.
    from agent.memory import facts as facts_module

    with store._connect() as conn:
        rows = facts_module.read_fact_assertions(conn, subject="user")
    assert [row.id for row in rows] == ["f1"]
    assert rows[0].status == "archived"


def test_a_category_change_re_derives_a_defaulted_entity(tmp_path):
    """An entity that is only a default is a fact about the category, so it moves.

    ``general`` is what an empty entity becomes outside the palace loci and
    ``user`` is what it becomes under ``identity``; carrying the old default
    across would leave the entry filed under a category whose default it is not.
    """
    store = _store(tmp_path)
    store.add_entry(_entry("m1", category="projects", entity=""))
    [row] = store.all_entries()
    assert row.entity == "general"

    updated = store.update_entry("m1", category="identity")
    assert updated is not None
    assert updated.category == "identity"
    assert updated.entity == "user"


def test_a_named_entity_survives_a_category_change(tmp_path):
    """...while an entity the operator typed is not rewritten behind their back."""
    store = _store(tmp_path)
    store.add_entry(_entry("m1", category="projects", entity="Simple Repo"))

    updated = store.update_entry("m1", category="people")
    assert updated is not None
    assert updated.category == "people"
    assert updated.entity == "simple_repo", "only the spelling is normalized"
