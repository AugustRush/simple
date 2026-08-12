"""The output contract a sub-agent's result must satisfy.

A spawned sub-agent returns free text.  When the caller needs something
machine-usable — JSON with known keys, files that must exist on disk — that
expectation has to be (a) stated in the task prompt and (b) checked against
the result.  Those two halves must agree: a prompt that asks for
``<deliverable>`` and a validator that does not look for it produce silent
mismatches, which is why they live in one module rather than as nine private
methods scattered through ``BaseAgent``.

Nothing here touches conversation state, the tool loop, or the LLM.  It is a
pure function of (task, expected_output, contract) on the way out and
(content, contract) on the way back, with one injected path resolver for the
``required_files`` check.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Callable

_DELIVERABLE_RE = re.compile(
    r"<deliverable>\s*(.*?)\s*</deliverable>",
    flags=re.IGNORECASE | re.DOTALL,
)

DELIVERABLE_INSTRUCTIONS = (
    "Return the final deliverable inside this exact block:",
    "<deliverable>",
    "<your deliverable here>",
    "</deliverable>",
)


def mapping_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def string_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


@dataclass(frozen=True)
class OutputContract:
    """A normalized contract: what the deliverable must look like."""

    format: str = ""
    required_keys: tuple[str, ...] = ()
    required_files: tuple[str, ...] = ()

    @classmethod
    def parse(cls, raw: dict[str, Any] | None) -> "OutputContract":
        contract = mapping_dict(raw)
        return cls(
            format=str(contract.get("format", "") or "").strip().lower(),
            required_keys=tuple(
                str(item)
                for item in contract.get("required_keys", [])
                if str(item).strip()
            ),
            required_files=tuple(
                str(item)
                for item in contract.get("required_files", [])
                if str(item).strip()
            ),
        )

    def __bool__(self) -> bool:
        return bool(self.format or self.required_keys or self.required_files)

    def to_dict(self) -> dict[str, Any]:
        """Round-trip back to the wire/tool-input shape."""
        result: dict[str, Any] = {}
        if self.format:
            result["format"] = self.format
        if self.required_keys:
            result["required_keys"] = list(self.required_keys)
        if self.required_files:
            result["required_files"] = list(self.required_files)
        return result

    @property
    def requires_json(self) -> bool:
        return self.format == "json" or bool(self.required_keys)

    def requires_deliverable(self, expected_output: str) -> bool:
        """Whether the sub-agent must wrap its answer in ``<deliverable>``.

        Note ``requires_json`` implies this, which
        :meth:`validate` relies on to know a deliverable was extracted before
        it tries to parse one.
        """
        return bool(str(expected_output or "").strip() or self.requires_json)


@dataclass(frozen=True)
class ContractResult:
    ok: bool
    content: str
    structured_content: dict[str, Any] | None = None
    error: str | None = None


def extract_deliverable(content: str) -> str | None:
    if not content:
        return None
    match = _DELIVERABLE_RE.search(str(content))
    if not match:
        return None
    return match.group(1).strip() or None


def contract_instructions(
    expected_output: str, contract: OutputContract
) -> list[str]:
    """The prompt half of the contract — kept next to the validator half."""
    lines: list[str] = []
    if expected_output:
        lines.append(expected_output)
    if contract.format == "json":
        lines.append("The deliverable inside <deliverable> must be a JSON object.")
    if contract.required_keys:
        lines.append(
            "The JSON deliverable must include these keys: "
            + ", ".join(contract.required_keys)
        )
    if contract.required_files:
        lines.append(
            "These files must exist when you finish: "
            + ", ".join(contract.required_files)
        )
    if contract.requires_deliverable(expected_output):
        lines.extend(DELIVERABLE_INSTRUCTIONS)
    return lines


def validate(
    content: str,
    *,
    expected_output: str,
    contract: OutputContract,
    resolve_path: Callable[[str], Path],
) -> ContractResult:
    """Check *content* against the contract the task prompt announced."""
    if not expected_output and not contract:
        return ContractResult(ok=True, content=content)

    requires_deliverable = contract.requires_deliverable(expected_output)
    deliverable = extract_deliverable(content or "") if requires_deliverable else None
    if requires_deliverable and deliverable is None:
        return ContractResult(
            ok=False,
            content=content,
            error=(
                "Expected output contract not satisfied: missing <deliverable> block"
            ),
        )

    structured_content: dict[str, Any] | None = None
    normalized_content = deliverable if deliverable is not None else content

    if contract.requires_json:
        # requires_json ⇒ requires_deliverable, so the missing-block branch
        # above already returned when there is nothing to parse.
        assert deliverable is not None
        try:
            parsed = json.loads(deliverable)
        except Exception:
            return ContractResult(
                ok=False,
                content=deliverable,
                error=(
                    "Expected output contract not satisfied: "
                    "deliverable is not valid JSON"
                ),
            )
        if not isinstance(parsed, dict):
            return ContractResult(
                ok=False,
                content=deliverable,
                error=(
                    "Expected output contract not satisfied: "
                    "deliverable JSON must be an object"
                ),
            )
        missing_keys = [key for key in contract.required_keys if key not in parsed]
        if missing_keys:
            return ContractResult(
                ok=False,
                content=deliverable,
                structured_content=parsed,
                error=(
                    "Expected output contract not satisfied: "
                    "missing required deliverable keys: " + ", ".join(missing_keys)
                ),
            )
        structured_content = parsed
        normalized_content = json.dumps(parsed, ensure_ascii=False, sort_keys=True)

    if contract.required_files:
        missing_files: list[str] = []
        for raw_path in contract.required_files:
            try:
                resolved_path = resolve_path(raw_path)
            except ValueError as exc:
                return ContractResult(
                    ok=False,
                    content=normalized_content,
                    structured_content=structured_content,
                    error=(
                        "Expected output contract not satisfied: "
                        "invalid required output file path: " + str(exc)
                    ),
                )
            if not resolved_path.exists():
                missing_files.append(raw_path)
        if missing_files:
            return ContractResult(
                ok=False,
                content=normalized_content,
                structured_content=structured_content,
                error=(
                    "Expected output contract not satisfied: "
                    "missing required output file(s): " + ", ".join(missing_files)
                ),
            )

    return ContractResult(
        ok=True,
        content=normalized_content,
        structured_content=structured_content,
    )


__all__ = [
    "DELIVERABLE_INSTRUCTIONS",
    "ContractResult",
    "OutputContract",
    "contract_instructions",
    "extract_deliverable",
    "mapping_dict",
    "string_list",
    "validate",
]
