#!/usr/bin/env python3
"""
Minimal validator for skill bundles.

Usage:
    quick_validate.py <skill_directory>
"""

import re
import sys
from pathlib import Path
from typing import Optional

MAX_SKILL_NAME_LENGTH = 64
ALLOWED_FRONTMATTER_KEYS = {
    "name",
    "description",
    "user-invocable",
    "disable-model-invocation",
}
PLACEHOLDER_MARKERS = ("[todo", "todo:")


def _extract_frontmatter(content: str) -> Optional[str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i])
    return None


def _parse_frontmatter(text: str) -> Optional[dict[str, str]]:
    """Simple key: value parser for frontmatter."""
    parsed: dict[str, str] = {}
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        # Strip surrounding quotes
        if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
            value = value[1:-1]
        parsed[key] = value
    return parsed


def _validate_skill_name(name: str, folder_name: str) -> Optional[str]:
    if not re.fullmatch(r"[a-zA-Z0-9]+(?:[-_][a-zA-Z0-9]+)*", name):
        return (
            f"Name '{name}' should use alphanumerics with hyphens or underscores only"
        )
    if len(name) > MAX_SKILL_NAME_LENGTH:
        return f"Name is too long ({len(name)} characters). Maximum is {MAX_SKILL_NAME_LENGTH}."
    return None


def _validate_description(description: str) -> Optional[str]:
    trimmed = description.strip()
    if not trimmed:
        return "Description cannot be empty"
    lowered = trimmed.lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return "Description still contains TODO placeholder text"
    if len(trimmed) > 1024:
        return f"Description is too long ({len(trimmed)} characters). Maximum is 1024."
    return None


def validate_skill(skill_path: str | Path) -> tuple[bool, str]:
    """Validate a skill folder structure and required frontmatter.

    Returns:
        (is_valid, message) tuple
    """
    skill_path = Path(skill_path).resolve()

    if not skill_path.exists():
        return False, f"Skill folder not found: {skill_path}"
    if not skill_path.is_dir():
        return False, f"Path is not a directory: {skill_path}"

    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md not found"

    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"Could not read SKILL.md: {exc}"

    frontmatter_text = _extract_frontmatter(content)
    if frontmatter_text is None:
        return False, "Invalid frontmatter format (must start with --- delimiter)"

    frontmatter = _parse_frontmatter(frontmatter_text)
    if frontmatter is None:
        return False, "Failed to parse frontmatter"

    unexpected = sorted(set(frontmatter.keys()) - ALLOWED_FRONTMATTER_KEYS)
    if unexpected:
        return (
            False,
            f"Unexpected frontmatter key(s): {', '.join(unexpected)}. "
            f"Allowed: {', '.join(sorted(ALLOWED_FRONTMATTER_KEYS))}",
        )

    if "name" not in frontmatter:
        return False, "Missing 'name' in frontmatter"
    if "description" not in frontmatter:
        return False, "Missing 'description' in frontmatter"

    name = frontmatter["name"]
    name_error = _validate_skill_name(name.strip(), skill_path.name)
    if name_error:
        return False, name_error

    description = frontmatter["description"]
    desc_error = _validate_description(description)
    if desc_error:
        return False, desc_error

    # Check for symlinks
    for child in skill_path.rglob("*"):
        if child.is_symlink():
            return False, f"Symlinks not allowed: {child.relative_to(skill_path)}"

    return True, "Skill is valid!"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: quick_validate.py <skill_directory>")
        sys.exit(1)

    valid, message = validate_skill(sys.argv[1])
    print(message)
    sys.exit(0 if valid else 1)
