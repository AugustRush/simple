"""Turning a declared output contract into instructions, and checking them.

Why this module exists
----------------------
Four methods on ``BaseAgent`` did this, and one of them --
``_resolve_output_contract_path`` -- was the reason the group could not simply
be moved: it reached into ``self.registry`` for the output directory and into
``self.workspace_root`` for the fallback root.  A validator that resolves paths
by consulting a global registry is a validator whose behaviour depends on what
else is running.

So the dependency is now an argument.  :func:`validate_output_contract` takes a
``resolve_path`` callable, and ``BaseAgent`` supplies its own; a test supplies a
one-liner.  That is the whole change, and it is what makes the group testable
without an agent.

The three declarations stay separate
------------------------------------
``normalize_output_contract`` parses a caller-supplied dict into the canonical
shape, ``with_output_contract`` renders the instructions appended to a sub-agent
task, and ``validate_output_contract`` checks a finished answer against them.
They are one concept with three moments, and merging any two would mean a caller
that only wants to render also pays for validation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from agent.orchestration import contracts
from agent.pathing import resolve_workspace_path

__all__ = [
    "normalize_output_contract",
    "output_contract_path_resolver",
    "string_list",
    "validate_output_contract",
    "with_output_contract",
]

#: Signature of the path resolver a validator is handed.
PathResolver = Callable[[str], Path]


def normalize_output_contract(
    output_contract: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """The canonical dict form of a declared contract."""
    return contracts.OutputContract.parse(output_contract).to_dict()


def with_output_contract(
    task: str,
    expected_output: str,
    output_contract: Optional[dict[str, Any]],
) -> str:
    """Append the contract's instructions to a sub-agent task.

    Returns *task* untouched when there is nothing to say, so a caller can pass
    an absent contract without branching.
    """
    contract = contracts.OutputContract.parse(output_contract)
    if not expected_output and not contract:
        return task
    # Imported here rather than at module scope: bounded_text is used by the
    # prompt-building paths, and importing it at import time would make this
    # module part of every import chain that only wants validation.
    from agent.core.bounded_text import append_named_block

    return append_named_block(
        task,
        "Expected output contract:",
        contracts.contract_instructions(expected_output, contract),
    )


def output_contract_path_resolver(
    *,
    output_dir: Optional[Path],
    workspace_root: Optional[Path],
) -> PathResolver:
    """A resolver bound to one output directory and workspace root.

    Replaces the previous behaviour, which read both out of a tool registry at
    call time.  An absolute path is used as given; a relative one is taken
    against *output_dir* when there is one, and otherwise resolved against the
    workspace.
    """

    def resolve(raw_path: str) -> Path:
        if output_dir is not None:
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                return (output_dir / candidate).resolve(strict=False)
        resolved, _root_kind = resolve_workspace_path(
            raw_path,
            workspace_root=workspace_root or Path.cwd(),
            output_dir=output_dir,
        )
        return resolved

    return resolve


def validate_output_contract(
    content: str,
    *,
    expected_output: str,
    output_contract: Optional[dict[str, Any]],
    resolve_path: PathResolver,
) -> tuple[bool, str, Optional[dict[str, Any]], Optional[str]]:
    """Check *content* against the declared contract.

    Returns ``(ok, content, structured_content, error)``.  ``resolve_path`` is
    the seam that used to be a registry lookup; see the module docstring.
    """
    result = contracts.validate(
        content,
        expected_output=expected_output,
        contract=contracts.OutputContract.parse(output_contract),
        resolve_path=resolve_path,
    )
    return (
        result.ok,
        result.content,
        result.structured_content,
        result.error,
    )


def string_list(value: Any) -> list[str]:
    """Coerce *value* into a list of non-blank strings."""
    return contracts.string_list(value)
