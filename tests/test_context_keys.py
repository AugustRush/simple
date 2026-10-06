"""The context-key vocabulary, and the guard that keeps it honest.

``ToolRegistry._context`` is the seam through which tools resolve what they need,
and it is already the *narrow* mechanism -- the broad version,
``get_context("components")``, was removed in Task 4.  What was missing was the
contract: nothing said which keys exist, so a key a reader asks for and no writer
supplies looks exactly like a key whose value happens to be absent.

The scanner is AST-based and deliberately so.  A regex version of this file's
first draft reported five keys as "read but never written", and three of the five
appearances were *docstrings explaining that the key is gone* -- including
``"components"``.  Blanking string literals is what makes a sentence about a key
distinguishable from a use of one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent.runtime.context_keys import (
    CONTEXT_KEYS,
    ContextKey,
    declared_names,
    entry_for,
    suspicious,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = REPO_ROOT / "agent"


def _string_arg(node: ast.Call) -> str | None:
    """The literal first argument of a call, if it is a plain string."""
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _scan() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """``(writers, readers)`` of context keys, by file.

    String literals are blanked first, so an explanatory sentence cannot be
    mistaken for a use.  Comments do not appear in the AST at all, which is the
    other half of the same protection.
    """
    writers: dict[str, set[str]] = {}
    readers: dict[str, set[str]] = {}
    for path in sorted(AGENT_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            key = _string_arg(node)
            if key is None:
                continue
            if name == "set_context":
                writers.setdefault(key, set()).add(rel)
            elif name == "get_context":
                readers.setdefault(key, set()).add(rel)
    return writers, readers


# ── The ledger is well formed ────────────────────────────────────────────────


def test_every_entry_has_a_key() -> None:
    for entry in CONTEXT_KEYS:
        assert entry.key.strip(), "a blank key cannot be looked up"


def test_no_key_is_declared_twice() -> None:
    names = declared_names()
    assert len(names) == len(set(names)), "a duplicate makes one entry unreachable"


def test_every_key_has_a_reader() -> None:
    """A declared key nothing reads is vocabulary that should be deleted."""
    for entry in CONTEXT_KEYS:
        assert entry.readers, f"{entry.key!r} is declared but nothing reads it"


def test_no_key_is_suspicious() -> None:
    """No writer and no recorded reason -- the silent-default failure mode."""
    bad = suspicious()
    assert not bad, (
        "these context keys have no writer and no recorded reason:\n  "
        + "\n  ".join(entry.key for entry in bad)
    )


def test_a_key_without_a_writer_and_without_a_note_is_flagged() -> None:
    """Control: the check is not vacuous."""
    bad = suspicious([ContextKey(key="orphan", writers=(), readers=("x",))])
    assert [entry.key for entry in bad] == ["orphan"]


def test_a_partial_entry_is_exempt_by_declaration() -> None:
    """Exemptions are explicit, so they cannot be inherited by accident."""
    ok = suspicious(
        [ContextKey(key="k", writers=(), readers=("x",), partial=True, note="why")]
    )
    assert ok == ()


def test_lookup_of_an_undeclared_key_raises() -> None:
    with pytest.raises(KeyError) as caught:
        entry_for("not.a.real.key")
    assert "CONTEXT_KEYS" in str(caught.value), "it says where to add it"


def test_lookup_of_a_declared_key_returns_it() -> None:
    assert entry_for("output_dir").key == "output_dir"


# ── The source agrees with the ledger ────────────────────────────────────────


def test_every_key_read_in_the_source_is_declared() -> None:
    """A new read without a ledger entry is the drift this catches."""
    _writers, readers = _scan()
    undeclared = sorted(set(readers) - set(declared_names()))
    assert not undeclared, (
        "these context keys are read but not declared in CONTEXT_KEYS:\n  "
        + "\n  ".join(undeclared)
        + "\n\nAdd them to agent/runtime/context_keys.py with their writer."
    )


def test_every_declared_key_is_actually_read() -> None:
    """The other direction: a ledger entry for something nothing reads."""
    _writers, readers = _scan()
    unused = sorted(set(declared_names()) - set(readers))
    assert not unused, (
        "these declared context keys are read nowhere:\n  " + "\n  ".join(unused)
    )


def test_every_writer_claimed_by_the_ledger_really_writes() -> None:
    """The assertion that makes the ledger worth keeping.

    A ledger of writers that does not match the code is worse than none: it
    reports a key as filled when nothing fills it, which is the bug it was written
    to expose.
    """
    writers, _readers = _scan()
    wrong: list[str] = []
    for entry in CONTEXT_KEYS:
        actual = writers.get(entry.key, set())
        if entry.partial:
            # The writer is real but the scanner cannot see it; the entry's note
            # says which mechanism fills it.
            assert entry.note, f"{entry.key!r} claims partial and explains nothing"
            continue
        for claimed in entry.writers:
            if claimed not in actual:
                wrong.append(f"{entry.key}: {claimed} does not write it")
        for found in actual:
            if found not in entry.writers:
                wrong.append(f"{entry.key}: {found} writes it but is not listed")
    assert not wrong, "the ledger and the source disagree:\n  " + "\n  ".join(wrong)


def test_every_reader_claimed_by_the_ledger_really_reads() -> None:
    _writers, readers = _scan()
    wrong: list[str] = []
    for entry in CONTEXT_KEYS:
        actual = readers.get(entry.key, set())
        for claimed in entry.readers:
            if claimed not in actual:
                wrong.append(f"{entry.key}: {claimed} does not read it")
        for found in actual:
            if found not in entry.readers:
                wrong.append(f"{entry.key}: {found} reads it but is not listed")
    assert not wrong, "the ledger and the source disagree:\n  " + "\n  ".join(wrong)


def test_the_scan_finds_writes_and_reads_at_all() -> None:
    """Control: a scanner that finds nothing would pass every assertion above."""
    writers, readers = _scan()
    assert writers, "no set_context calls found"
    assert readers, "no get_context calls found"
    assert "output_dir" in readers


def test_prose_about_a_key_is_not_counted_as_a_use() -> None:
    """The false positive that made the first draft of this guard wrong.

    ``"components"`` appears in two docstrings explaining that the container
    backdoor was removed.  A regex reported it as a write; the AST does not.
    """
    writers, readers = _scan()
    assert "components" not in readers, (
        "'components' is only mentioned in prose now; the scanner is reading text"
    )
    assert "components" not in writers
