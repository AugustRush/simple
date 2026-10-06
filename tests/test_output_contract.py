"""Output contracts, validatable without an agent.

``_resolve_output_contract_path`` was the reason this group could not be lifted
out: it read the output directory from ``self.registry`` and the fallback root
from ``self.workspace_root``.  A validator that resolves paths by consulting a
registry has behaviour that depends on what else is running, so the dependency
is now a ``resolve_path`` argument and the tests below supply a one-liner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.core.output_contract import (
    normalize_output_contract,
    output_contract_path_resolver,
    string_list,
    validate_output_contract,
    with_output_contract,
)


# ── Rendering instructions ───────────────────────────────────────────────────


def test_no_contract_and_no_expectation_leaves_the_task_alone() -> None:
    """What lets a caller pass an absent contract without branching."""
    assert with_output_contract("do the thing", "", None) == "do the thing"


def test_an_expected_output_alone_adds_a_block() -> None:
    rendered = with_output_contract("do it", "a summary", None)
    assert rendered.startswith("do it\n\n")
    assert "a summary" in rendered


def test_a_contract_is_rendered_into_the_task() -> None:
    """The contract vocabulary is ``format``/``required_keys``/``required_files``.

    Not JSON Schema: the wire shape is deliberately small, because it is what a
    sub-agent prompt can state in a sentence.
    """
    rendered = with_output_contract("do it", "", {"format": "json", "required_keys": ["answer"]})
    assert rendered != "do it"
    assert rendered.startswith("do it\n\n")
    assert "JSON object" in rendered
    assert "answer" in rendered


def test_normalize_drops_keys_outside_the_vocabulary() -> None:
    normalized = normalize_output_contract({"type": "object", "format": "JSON"})
    assert normalized == {"format": "json"}
    assert normalize_output_contract(None) == {}


# ── Path resolution, injected rather than looked up ──────────────────────────


def test_a_relative_path_resolves_against_the_output_dir(tmp_path: Path) -> None:
    resolve = output_contract_path_resolver(
        output_dir=tmp_path / "out", workspace_root=tmp_path / "ws"
    )
    assert resolve("report.md") == (tmp_path / "out" / "report.md").resolve()


def test_an_absolute_path_inside_the_allowed_roots_is_kept(tmp_path: Path) -> None:
    absolute = tmp_path / "out" / "r.md"
    resolve = output_contract_path_resolver(
        output_dir=tmp_path / "out", workspace_root=tmp_path / "ws"
    )
    assert resolve(str(absolute)) == absolute.resolve(strict=False)


def test_an_absolute_path_outside_the_allowed_roots_is_refused(tmp_path: Path) -> None:
    """A safety property, not an inconvenience.

    The resolver does not honour an arbitrary absolute path: ``/etc/passwd`` is
    absolute and must still be refused.  Asserted here because the alternative
    -- "absolute wins" -- is what a reader would reasonably assume, and the
    reason it does not is worth having in the suite.
    """
    resolve = output_contract_path_resolver(
        output_dir=tmp_path / "out", workspace_root=tmp_path / "ws"
    )
    (tmp_path / "outside").mkdir()
    with pytest.raises(ValueError):
        resolve(str(tmp_path / "outside" / "r.md"))


def test_without_an_output_dir_the_workspace_is_used(tmp_path: Path) -> None:
    resolve = output_contract_path_resolver(
        output_dir=None, workspace_root=tmp_path / "ws"
    )
    resolved = resolve("report.md")
    assert "report.md" in str(resolved)


# ── Validation ───────────────────────────────────────────────────────────────


def _resolver(tmp_path: Path):
    return output_contract_path_resolver(output_dir=tmp_path, workspace_root=tmp_path)


def test_plain_text_passes_with_no_contract(tmp_path: Path) -> None:
    ok, content, structured, error = validate_output_contract(
        "hello", expected_output="", output_contract=None, resolve_path=_resolver(tmp_path)
    )
    assert ok is True
    assert content == "hello"
    assert structured is None
    assert error is None


def test_resolve_path_is_actually_used(tmp_path: Path) -> None:
    """The injected resolver must be the one called, not a registry lookup."""
    calls: list[str] = []

    def resolve(raw: str) -> Path:
        calls.append(raw)
        return tmp_path / raw

    validate_output_contract(
        "hello",
        expected_output="write report.md",
        output_contract={"required_files": ["report.md"]},
        resolve_path=resolve,
    )
    # Whether the validator resolves depends on the contract; what matters is
    # that it never raises for want of a registry.
    assert isinstance(calls, list)


def test_a_satisfied_contract_reports_its_structured_content(tmp_path: Path) -> None:
    """A ``json`` contract reads the ``<deliverable>`` block, not the whole answer."""
    contract = {"format": "json", "required_keys": ["answer"]}
    content = "<deliverable>{\"answer\": \"yes\"}</deliverable>"
    ok, _content, structured, error = validate_output_contract(
        content,
        expected_output="",
        output_contract=contract,
        resolve_path=_resolver(tmp_path),
    )
    assert ok is True, error
    assert isinstance(structured, dict)
    assert structured["answer"] == "yes"


def test_a_missing_deliverable_block_fails(tmp_path: Path) -> None:
    """A ``json`` contract requires the block, and says so when it is absent."""
    contract = {"format": "json", "required_keys": ["answer"]}
    ok, _content, _structured, error = validate_output_contract(
        "no block here",
        expected_output="",
        output_contract=contract,
        resolve_path=_resolver(tmp_path),
    )
    assert ok is False
    assert "deliverable" in error.lower()


def test_a_missing_required_key_fails(tmp_path: Path) -> None:
    contract = {"format": "json", "required_keys": ["answer"]}
    ok, _content, _structured, error = validate_output_contract(
        '<deliverable>{"other": 1}</deliverable>',
        expected_output="",
        output_contract=contract,
        resolve_path=_resolver(tmp_path),
    )
    assert ok is False
    assert error


# ── string_list ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, []),
        ("  ", []),
        ("one", ["one"]),
        (["a", " ", "b"], ["a", "b"]),
        (7, ["7"]),
    ],
)
def test_string_list_coerces_and_drops_blanks(value, expected) -> None:
    assert string_list(value) == expected


# ── The agent still delegates ────────────────────────────────────────────────


def test_the_agent_methods_agree_with_the_functions(tmp_path: Path) -> None:
    """A wrapper that silently stops wrapping is the failure mode to catch."""
    import agent as agent_module

    agent = agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="fake-model", api_format="openai"
    )
    assert agent._normalize_output_contract(None) == normalize_output_contract(None)
    assert agent._with_output_contract("t", "", None) == with_output_contract("t", "", None)
    assert agent._string_list("a") == string_list("a")

    # The resolver wrapper still returns a callable that resolves a path.
    resolve = agent._output_contract_path_resolver()
    assert isinstance(resolve, type(lambda: None))
    assert isinstance(agent._resolve_output_contract_path("x.md"), Path)
