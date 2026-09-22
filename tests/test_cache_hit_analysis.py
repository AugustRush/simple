"""The prompt-cache hit-rate analysis must agree with what the agent records.

Two things are being held here, and only the first is obvious:

* the aggregation is correct — bands partition, unrecorded rows are not folded
  into a clean bucket, and a head that moved is reported;
* the *contract* holds — the metadata keys the analysis reads are the ones
  `agent.core.payload_shape` writes.  A rename on either side would leave a
  report that still prints, still looks plausible, and silently says "no row
  carries this" forever.  That is the failure mode worth a test.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.analyze_cache_hits import (  # noqa: E402
    BANDS,
    HEAD_KEY,
    INTERACTIVE_PHASES,
    REWRITE_KEY,
    UsageRow,
    _connect_readonly,
    band_of,
    band_totals,
    group_by,
    head_history,
    read_events,
    render,
    rewrite_totals,
)

SCHEMA = """
CREATE TABLE usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
)
"""


def _seed(path, rows):
    """A store with the real schema and the given rows, oldest first."""
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    conn.executemany(
        "INSERT INTO usage_events (session_id, phase, input_tokens, "
        "cached_input_tokens, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def _flat(text: str) -> str:
    """Collapse the report's layout so a prose assertion tests the words.

    The report wraps its notes to a terminal width, so a phrase the test cares
    about can straddle a line break.  Comparing on collapsed whitespace keeps
    the assertion about the message rather than about the wrap column.
    """
    return " ".join(text.split())


def _row(
    session_id="s1",
    phase="foreground",
    input_tokens=1000,
    cached=100,
    metadata=None,
    created_at="2026-09-21 10:00:00 UTC",
):
    return UsageRow(
        session_id=session_id,
        phase=phase,
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        created_at=created_at,
        metadata=metadata or {},
    )


# ── The contract with the payload recorder ─────────────────────────────────


def test_the_keys_the_analysis_reads_are_the_ones_the_payload_records():
    from agent.core.payload_shape import describe_payload

    shape = describe_payload("sys", [], [{"role": "user", "content": "hi"}])
    assert HEAD_KEY in shape, f"{HEAD_KEY} is read here but not written by describe_payload"
    assert REWRITE_KEY in shape, (
        f"{REWRITE_KEY} is read here but not written by describe_payload"
    )


def test_the_rewrite_flag_is_what_describe_payload_calls_it():
    """The value must be a bool, because the analysis switches on its type."""
    from agent.core.payload_shape import describe_payload

    shape = describe_payload("sys", [], [], compacted=True)
    assert isinstance(shape[REWRITE_KEY], bool)
    assert shape[REWRITE_KEY] is True


# ── Reading ────────────────────────────────────────────────────────────────


def test_a_seeded_store_reads_back_as_rows(tmp_path):
    db = _seed(
        tmp_path / "palace.db",
        [
            ("s1", "foreground", 1000, 100, '{"head_fingerprint": "aa"}', "2026-09-21 10:00:00 UTC"),
            ("s1", "tool_step", 2000, 1900, "{}", "2026-09-21 10:01:00 UTC"),
        ],
    )
    rows = read_events(db)
    assert [row.phase for row in rows] == ["foreground", "tool_step"]
    assert rows[0].metadata[HEAD_KEY] == "aa"
    # Oldest first, which is what makes first-seen order meaningful downstream.
    assert rows[0].created_at < rows[1].created_at


def test_since_is_a_prefix_comparison_so_a_bare_date_selects_that_day(tmp_path):
    db = _seed(
        tmp_path / "palace.db",
        [
            ("s1", "foreground", 10, 1, "{}", "2026-09-19 23:59:59 UTC"),
            ("s1", "foreground", 20, 2, "{}", "2026-09-20 00:00:00 UTC"),
            ("s1", "foreground", 30, 3, "{}", "2026-09-21 12:00:00 UTC"),
        ],
    )
    assert [row.input_tokens for row in read_events(db, "2026-09-20")] == [20, 30]


def test_a_row_whose_metadata_is_not_json_does_not_break_the_read(tmp_path):
    db = _seed(
        tmp_path / "palace.db",
        [("s1", "foreground", 10, 1, "not json at all", "2026-09-21 10:00:00 UTC")],
    )
    rows = read_events(db)
    assert rows[0].metadata == {}
    assert rows[0].hit_rate == pytest.approx(0.1)


def test_the_reader_cannot_write_to_the_store(tmp_path):
    """The whole point of `mode=ro`: a report must not be able to damage what
    it reports on, whatever a future edit to the script does."""
    db = _seed(tmp_path / "palace.db", [("s1", "foreground", 10, 1, "{}", "2026-09-21 10:00:00 UTC")])
    conn = _connect_readonly(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM usage_events")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO usage_events (session_id, phase, created_at) VALUES ('x', 'y', 'z')")
    finally:
        conn.close()
    # And the row is still there afterwards.
    assert len(read_events(db)) == 1


# ── Aggregation ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "input_tokens, cached, expected",
    [
        (1000, 0, "<20%"),
        (1000, 199, "<20%"),
        (1000, 200, "20-50%"),      # low-inclusive
        (1000, 499, "20-50%"),
        (1000, 500, "50-80%"),      # low-inclusive
        (1000, 800, "80-95%"),      # low-inclusive
        (1000, 950, "95%+"),        # low-inclusive
        (1000, 1000, "95%+"),
    ],
)
def test_a_row_lands_in_exactly_one_band_with_inclusive_lower_bounds(
    input_tokens, cached, expected
):
    assert band_of(_row(input_tokens=input_tokens, cached=cached)) == expected


def test_the_bands_partition_the_range_so_nothing_is_double_counted():
    """Every band boundary is another band's start, which is what makes the
    band table's totals add up to the overall totals."""
    bounds = [low for low, _, _ in BANDS]
    assert bounds == sorted(bounds)
    for (_, high, _), (next_low, _, _) in zip(BANDS, BANDS[1:]):
        assert high == next_low


def test_a_row_with_no_input_gets_its_own_label_not_a_total_miss():
    """A provider that reported a zero input count did not report a miss; it
    reported nothing, and counting it as 0% would drag the band down."""
    assert band_of(_row(input_tokens=0, cached=0)) == "no input"


def test_bands_come_back_in_ascending_order_so_the_table_reads_as_a_distribution():
    rows = [
        _row(input_tokens=1000, cached=990),
        _row(input_tokens=1000, cached=10),
        _row(input_tokens=1000, cached=600),
    ]
    assert list(band_totals(rows)) == ["<20%", "50-80%", "95%+"]


def test_grouping_keeps_first_seen_order():
    rows = [
        _row(phase="subagent"),
        _row(phase="foreground"),
        _row(phase="subagent"),
    ]
    grouped = group_by(rows, lambda row: row.phase)
    assert list(grouped) == ["subagent", "foreground"]
    assert grouped["subagent"].calls == 2


def test_uncached_is_clamped_when_the_provider_reports_more_cached_than_input():
    """The two counts come from the provider and are not guaranteed to agree;
    a negative would corrupt every sum it entered."""
    row = _row(input_tokens=100, cached=120)
    assert row.uncached == 0


# ── The rewrite split ──────────────────────────────────────────────────────


def test_unrecorded_rows_are_counted_separately_not_folded_into_appended():
    """A pre-Task-0 row cannot answer "was the body rewritten".  Counting it as
    "appended" would make the clean half look bigger than it is."""
    rows = [
        _row(metadata={REWRITE_KEY: False}),
        _row(metadata={REWRITE_KEY: True}),
        _row(metadata={}),  # written before the shape was recorded
    ]
    grouped, unrecorded = rewrite_totals(rows)
    assert unrecorded == 1
    assert grouped["appended"].calls == 1
    assert grouped["rewritten"].calls == 1


def test_a_non_boolean_rewrite_value_is_treated_as_unrecorded():
    rows = [_row(metadata={REWRITE_KEY: "yes"})]
    grouped, unrecorded = rewrite_totals(rows)
    assert grouped == {}
    assert unrecorded == 1


# ── Head stability ─────────────────────────────────────────────────────────


def test_head_history_returns_distinct_heads_in_first_seen_order():
    rows = [
        _row(session_id="s1", metadata={HEAD_KEY: "a"}),
        _row(session_id="s1", metadata={HEAD_KEY: "a"}),
        _row(session_id="s1", metadata={HEAD_KEY: "b"}),
        _row(session_id="s1", metadata={HEAD_KEY: "a"}),
    ]
    assert head_history(rows) == {"s1": ["a", "b"]}


def test_head_history_skips_rows_that_recorded_no_head():
    rows = [_row(session_id="s1", metadata={}), _row(session_id="s2", metadata={HEAD_KEY: "a"})]
    assert head_history(rows) == {"s2": ["a"]}


# ── Rendering ──────────────────────────────────────────────────────────────


def test_the_report_names_a_head_that_moved_mid_session():
    rows = [
        _row(session_id="s1", metadata={HEAD_KEY: "a", REWRITE_KEY: False}),
        _row(session_id="s1", metadata={HEAD_KEY: "b", REWRITE_KEY: True}),
    ]
    out = render(rows, source=":memory:")
    assert "sessions whose head changed mid-session: 1" in out
    assert "s1..." in out


def test_the_report_warns_when_the_interactive_path_recorded_nothing():
    """This is the Task 0 finding stated as an artefact: a missing phase is a
    measurement gap, not a cheap conversation."""
    out = _flat(render([_row(phase="subagent")], source=":memory:"))
    assert "'foreground'" in out and "'tool_step'" in out
    assert "recorded nothing" in out
    assert "true hit rate is unknown" in out


def test_the_warning_is_absent_once_the_interactive_phases_have_rows():
    rows = [_row(phase=phase) for phase in INTERACTIVE_PHASES] + [_row(phase="subagent")]
    assert "recorded nothing" not in _flat(render(rows, source=":memory:"))


def test_the_report_says_so_rather_than_printing_empty_tables():
    out = render([], source="/tmp/nothing.db")
    assert "No usage rows" in out


def test_the_report_marks_the_unrecorded_shape_rather_than_showing_zeros():
    """An old table must not render as "0 rewrites" — that reads as a clean
    bill of health when it is really an absent measurement."""
    out = render([_row(metadata={})], source=":memory:")
    assert "No row carries `body_compacted` yet" in out
    assert "No row carries `head_fingerprint` yet" in out


def test_uncached_tokens_are_summed_so_the_bill_can_be_read_off_the_table():
    """The uncached column is the bill.  A rate cannot be summed, so the totals
    must be over tokens and the rate must be token-weighted — the mean of 90%
    and 0% would be 45% too, but only by coincidence."""
    rows = [
        _row(input_tokens=1000, cached=900),   # 100 uncached
        _row(input_tokens=1000, cached=0),     # 1000 uncached
    ]
    totals = group_by(rows, lambda row: "all")["all"]
    assert (totals.calls, totals.input_tokens, totals.cached) == (2, 2000, 900)
    assert totals.uncached == 1100
    assert totals.hit_rate == pytest.approx(0.45)

    # Token-weighted, not the mean of the per-row rates: a big miss beside a
    # small hit must not average out.
    lopsided = [
        _row(input_tokens=10_000, cached=0),
        _row(input_tokens=10, cached=10),
    ]
    assert group_by(lopsided, lambda row: "all")["all"].hit_rate == pytest.approx(
        10 / 10010
    )


def test_the_uncached_column_appears_in_the_rendered_report():
    rows = [_row(input_tokens=1000, cached=900), _row(input_tokens=1000, cached=0)]
    assert "1.1k" in render(rows, source=":memory:")
