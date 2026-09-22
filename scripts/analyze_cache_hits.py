#!/usr/bin/env python3
"""Report provider prompt-cache hit rates from `usage_events`.

Why this exists
---------------
A provider's prompt cache bills a request by how much of its *front* it has
already stored: a cached input token costs roughly a tenth of an uncached one.
So the number that decides what a conversation costs is the share of each
request that hit — and before this script, reading it meant writing ad-hoc SQL
against a table whose shape had to be rediscovered every time.

It prints the three cuts that produced the original diagnosis, so the same
reading is reproducible in one command:

* **by phase** — which path is paying (`subagent` / `foreground` / `tool_step` /
  `consolidation`);
* **by hit band** — how much of the uncached bill the worst requests carry;
* **by body rewrite** — whether compaction is what breaks the prefix.

The head-stability section answers the remaining question, "is the front of the
request the same as it was last call", which a hit rate alone cannot: a session
whose head changes on every call can still show a respectable average.

Usage
-----
    uv run python scripts/analyze_cache_hits.py
    uv run python scripts/analyze_cache_hits.py --db /path/to/palace.db
    uv run python scripts/analyze_cache_hits.py --since 2026-09-20
    uv run python scripts/analyze_cache_hits.py --top 5

Reading the numbers
-------------------
`input_tokens` is the provider's own count for the request and
`cached_input_tokens` its count of how much of that came from cache, so
`hit = cached / input`.  Both are the provider's figures, not estimates.

Read the **uncached** column, not the hit rate.  A high rate on a small request
saves little; the bill is the sum of the uncached column, which is why the band
table prints each band's share of it.  A band holding a fifth of the calls and
half the uncached tokens is where the money is, and it is the only band worth
optimising.

A phase that is **absent** means that path recorded nothing at all.  A row is
written only when the provider reports usage, so a missing `foreground` /
`tool_step` is a measurement gap — not evidence of a cheap conversation.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Mirrors how the agent resolves its home, without importing the package:
#: `$SIMPLE_AGENT_HOME` overrides `~/.agent`, and the store lives under
#: `context/`.
DEFAULT_DB = (
    Path(os.environ.get("SIMPLE_AGENT_HOME") or Path.home() / ".agent")
    / "context"
    / "palace.db"
)

#: The phases a conversation actually runs through.  `subagent` and
#: `consolidation` are background work and are expected to have their own
#: profile, so their absence is not a finding — these two are.
INTERACTIVE_PHASES = ("foreground", "tool_step")

#: Half-open, low-inclusive, so every row lands in exactly one band.  The last
#: bound is above 1.0 because a provider is free to report a cached count that
#: rounds past its input count.
BANDS: tuple[tuple[float, float, str], ...] = (
    (0.00, 0.20, "<20%"),
    (0.20, 0.50, "20-50%"),
    (0.50, 0.80, "50-80%"),
    (0.80, 0.95, "80-95%"),
    (0.95, 1.01, "95%+"),
)

#: Metadata keys written by `agent.core.payload_shape.describe_payload`.
HEAD_KEY = "head_fingerprint"
REWRITE_KEY = "body_compacted"


# ── The data ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UsageRow:
    """One recorded provider call, with its metadata already parsed."""

    session_id: str
    phase: str
    input_tokens: int
    cached_input_tokens: int
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def uncached(self) -> int:
        # Clamped because the two counts come from the provider and are not
        # guaranteed to be consistent with each other.
        return max(0, self.input_tokens - self.cached_input_tokens)

    @property
    def hit_rate(self) -> float:
        return self.cached_input_tokens / self.input_tokens if self.input_tokens else 0.0

    @property
    def head_fingerprint(self) -> str:
        head = self.metadata.get(HEAD_KEY)
        return head if isinstance(head, str) else ""

    @property
    def body_rewritten(self) -> bool | None:
        """True/False when the row recorded it, None for a pre-Task-0 row."""
        value = self.metadata.get(REWRITE_KEY)
        return value if isinstance(value, bool) else None


@dataclass
class Totals:
    calls: int = 0
    input_tokens: int = 0
    cached: int = 0

    def add(self, row: UsageRow) -> None:
        self.calls += 1
        self.input_tokens += row.input_tokens
        self.cached += row.cached_input_tokens

    @property
    def uncached(self) -> int:
        return max(0, self.input_tokens - self.cached)

    @property
    def hit_rate(self) -> float:
        return self.cached / self.input_tokens if self.input_tokens else 0.0


# ── Reading ────────────────────────────────────────────────────────────────


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the store without a way to change it.

    `mode=ro` is the guarantee, but it is not always *available*: a database in
    WAL mode needs its `-shm` index, and a read-only connection may not create
    one, so an uncleanly-closed store refuses it.  The fallback keeps the tool
    usable and is still SELECT-only — `query_only` makes SQLite reject a write
    outright rather than relying on this script never issuing one.
    """
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA query_only = 1")
        return conn


def read_events(db_path: Path, since: str = "") -> list[UsageRow]:
    """Every usage row, oldest first, optionally restricted by `created_at`.

    `since` is compared as a string prefix because `created_at` is stored
    fixed-width UTC, so lexicographic order is time order and a bare date
    (`2026-09-20`) selects that whole day.
    """
    sql = (
        "SELECT session_id, phase, input_tokens, cached_input_tokens, "
        "metadata_json, created_at FROM usage_events"
    )
    params: list[Any] = []
    if since:
        sql += " WHERE created_at >= ?"
        params.append(since)
    sql += " ORDER BY id"
    conn = _connect_readonly(db_path)
    try:
        fetched = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [
        UsageRow(
            session_id=session_id or "",
            phase=phase or "",
            input_tokens=int(input_tokens or 0),
            cached_input_tokens=int(cached or 0),
            created_at=created_at or "",
            metadata=_parse_metadata(metadata_json),
        )
        for session_id, phase, input_tokens, cached, metadata_json, created_at in fetched
    ]


def _parse_metadata(raw: Any) -> dict[str, Any]:
    """Metadata is a JSON object by construction; a bad row is not fatal."""
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ── Aggregation (pure: same rows in, same report out) ──────────────────────


def group_by(rows: Iterable[UsageRow], key: Callable[[UsageRow], str]) -> dict[str, Totals]:
    """Totals per distinct key, in first-seen order.

    First-seen order rather than sorted: the phases and sessions arrive in
    chronological order from the query, and that order is more informative than
    an alphabetical one (which would interleave unrelated sessions).
    """
    grouped: dict[str, Totals] = {}
    for row in rows:
        grouped.setdefault(key(row), Totals()).add(row)
    return grouped


def band_of(row: UsageRow) -> str:
    """The hit band a row falls in. Zero-input rows have no rate, so they get
    their own label rather than being silently counted as a total miss."""
    if row.input_tokens <= 0:
        return "no input"
    for low, high, label in BANDS:
        if low <= row.hit_rate < high:
            return label
    return BANDS[-1][2]


def band_totals(rows: Sequence[UsageRow]) -> dict[str, Totals]:
    """Bands in ascending order, so the table reads as a distribution."""
    grouped = group_by(rows, band_of)
    order = [label for _, _, label in BANDS] + ["no input"]
    return {label: grouped[label] for label in order if label in grouped}


def rewrite_totals(rows: Sequence[UsageRow]) -> tuple[dict[str, Totals], int]:
    """Split by whether the body was rewritten, plus the unrecorded count.

    The third number matters: rows written before the shape was recorded cannot
    answer the question, and folding them into "not rewritten" would make the
    clean half look bigger than it is.
    """
    grouped: dict[str, Totals] = {}
    unrecorded = 0
    for row in rows:
        rewritten = row.body_rewritten
        if rewritten is None:
            unrecorded += 1
            continue
        grouped.setdefault("rewritten" if rewritten else "appended", Totals()).add(row)
    return grouped, unrecorded


def head_history(rows: Iterable[UsageRow]) -> dict[str, list[str]]:
    """Per session, the distinct heads in the order they were first used.

    A list longer than one is the finding: the head is supposed to be a pure
    function of session-stable state, so every entry after the first is a call
    that could not share the previous call's prefix.
    """
    history: dict[str, list[str]] = {}
    for row in rows:
        head = row.head_fingerprint
        if not head:
            continue
        heads = history.setdefault(row.session_id, [])
        if head not in heads:
            heads.append(head)
    return history


# ── Rendering ──────────────────────────────────────────────────────────────


def _tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def _rate(cached: int, total: int) -> str:
    return f"{cached / total * 100:.1f}%" if total else "-"


def _table(
    headers: Sequence[str], rows: Sequence[Sequence[str]], align: str | None = None
) -> str:
    """A plain fixed-width table. The first column is a label, the rest numbers."""
    align = align or ("l" + "r" * (len(headers) - 1))
    widths = [
        max([len(headers[i])] + [len(row[i]) for row in rows])
        for i in range(len(headers))
    ]

    def line(cells: Sequence[str]) -> str:
        return "  ".join(
            cell.ljust(widths[i]) if align[i] == "l" else cell.rjust(widths[i])
            for i, cell in enumerate(cells)
        )

    out = [line(headers), "  ".join("-" * width for width in widths)]
    out.extend(line(row) for row in rows)
    return "\n".join(out)


def _totals_row(label: str, totals: Totals) -> list[str]:
    return [
        label,
        str(totals.calls),
        _tokens(totals.input_tokens),
        _tokens(totals.cached),
        _tokens(totals.uncached),
        _rate(totals.cached, totals.input_tokens),
    ]


def _section(title: str, body: str) -> str:
    return f"\n-- {title} " + "-" * max(0, 56 - len(title)) + "\n" + body


def render(rows: Sequence[UsageRow], source: Path, top: int = 5) -> str:
    """The whole report as one string, so it can be printed or asserted on."""
    if not rows:
        return (
            f"Database: {source}\n"
            "No usage rows.  A row is written only when the provider reports "
            "usage,\nso an empty table means no call has reported one yet."
        )

    spans = [row.created_at for row in rows if row.created_at]
    header = (
        f"Database: {source}\n"
        f"Rows: {len(rows)}   Range: {min(spans) if spans else '?'} "
        f".. {max(spans) if spans else '?'}"
    )
    parts = [header]

    by_phase = group_by(rows, lambda row: row.phase or "(unnamed)")
    parts.append(
        _section(
            "By phase",
            _table(
                ("phase", "calls", "input", "cached", "uncached", "hit"),
                [_totals_row(phase, totals) for phase, totals in by_phase.items()],
            ),
        )
    )

    overall = Totals()
    for row in rows:
        overall.add(row)

    bands = band_totals(rows)
    band_rows = []
    for label, totals in bands.items():
        share = f"{totals.uncached / overall.uncached * 100:.1f}%" if overall.uncached else "-"
        band_rows.append(_totals_row(label, totals) + [share])
    parts.append(
        _section(
            "By hit band",
            _table(
                ("band", "calls", "input", "cached", "uncached", "hit", "of uncached"),
                band_rows,
            ),
        )
    )

    rewrites, unrecorded = rewrite_totals(rows)
    if rewrites:
        parts.append(
            _section(
                "By body rewrite",
                _table(
                    ("body", "calls", "input", "cached", "uncached", "hit"),
                    [
                        _totals_row(label, totals)
                        for label, totals in rewrites.items()
                    ],
                ),
            )
        )
    else:
        parts.append(
            _section(
                "By body rewrite",
                "No row carries `body_compacted` yet; rows written before the\n"
                "shape was recorded cannot say whether compaction cut the body.",
            )
        )

    history = head_history(rows)
    if history:
        unstable = {
            session: heads for session, heads in history.items() if len(heads) > 1
        }
        lines = [
            f"sessions with a recorded head: {len(history)}",
            f"sessions whose head changed mid-session: {len(unstable)}",
        ]
        if unstable:
            lines.append("")
            lines.append(
                _table(
                    ("session", "distinct heads", "calls"),
                    [
                        [
                            session[:12] + "...",
                            str(len(heads)),
                            str(sum(1 for row in rows if row.session_id == session)),
                        ]
                        for session, heads in sorted(
                            unstable.items(), key=lambda item: -len(item[1])
                        )
                    ],
                )
            )
        parts.append(_section("Head stability", "\n".join(lines)))
    else:
        parts.append(
            _section(
                "Head stability",
                "No row carries `head_fingerprint` yet, so whether the head of\n"
                "the request stayed put cannot be read from this table.",
            )
        )

    sessions = group_by(rows, lambda row: row.session_id or "(unnamed)")
    worst = sorted(sessions.items(), key=lambda item: -item[1].uncached)[:top]
    parts.append(
        _section(
            f"Worst {len(worst)} sessions by uncached input",
            _table(
                ("session", "calls", "input", "cached", "uncached", "hit"),
                [
                    [session[:12] + "..."] + _totals_row("", totals)[1:]
                    for session, totals in worst
                ],
            ),
        )
    )

    parts.append(_section("Overall", _table(
        ("scope", "calls", "input", "cached", "uncached", "hit"),
        [_totals_row("all rows", overall)],
    )))

    present = {row.phase for row in rows}
    missing = [phase for phase in INTERACTIVE_PHASES if phase not in present]
    if missing:
        note = (
            f"note: no {' / '.join(repr(phase) for phase in missing)} rows. "
            "The interactive path recorded nothing, so its true hit rate is "
            "unknown -- rows appear only once a provider reports usage for a "
            "streamed call."
        )
        parts.append("\n" + textwrap.fill(note, width=78, subsequent_indent="      "))
    return "\n".join(parts)


# ── Entry point ────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report provider prompt-cache hit rates from usage_events."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"path to palace.db (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--since",
        default="",
        help="only rows at or after this UTC date/time prefix, e.g. 2026-09-20",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=5,
        help="how many sessions to list by uncached input (default: 5)",
    )
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"No database at {args.db}", file=sys.stderr)
        print(
            "Pass --db PATH, or set SIMPLE_AGENT_HOME if the agent's home is "
            "not ~/.agent.",
            file=sys.stderr,
        )
        return 1
    try:
        rows = read_events(args.db, args.since)
    except sqlite3.DatabaseError as exc:
        print(f"Could not read {args.db}: {exc}", file=sys.stderr)
        return 1
    print(render(rows, args.db, top=max(1, args.top)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
