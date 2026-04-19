#!/usr/bin/env python3
"""
Skill Initializer - Creates a new skill from template.

Usage:
    init_skill.py <skill-name> --path <path> [--resources scripts,references,assets] [--examples]

Examples:
    init_skill.py code-review --path ~/.agent/skills
    init_skill.py quality/lint --path ~/.agent/skills --resources scripts,references
    init_skill.py my-tool --path ~/.agent/skills --resources scripts --examples
"""

import argparse
import re
import sys
from pathlib import Path

MAX_SKILL_NAME_LENGTH = 64
ALLOWED_RESOURCES = {"scripts", "references", "assets"}

SKILL_TEMPLATE = """---
name: {name}
description: {description}
user-invocable: true
disable-model-invocation: false
---

# {title}

{body}
"""

EXAMPLE_SCRIPT = '''#!/usr/bin/env python3
"""
Example helper script for {name}.

Replace with actual implementation or delete if not needed.
"""

def main():
    print("Example script for {name}")
    # TODO: Add actual script logic

if __name__ == "__main__":
    main()
'''

EXAMPLE_REFERENCE = """# Reference Documentation for {title}

Replace with actual reference content or delete if not needed.

## When to Use

Reference docs are ideal for:
- API documentation and schemas
- Detailed workflow guides
- Domain-specific knowledge
- Information too lengthy for SKILL.md
"""

EXAMPLE_ASSET = """# Example Asset

This placeholder represents where asset files would be stored.
Replace with actual asset files (templates, images, fonts, etc.) or delete.

Asset files are NOT loaded into context -- they are used in the output the agent produces.
"""


def normalize_skill_name(raw: str) -> str:
    """Normalize a skill name to lowercase hyphen-case, preserving '/' for nesting."""
    parts = raw.strip().split("/")
    normalized_parts = []
    for part in parts:
        part = part.strip().lower()
        part = re.sub(r"[^a-z0-9]+", "-", part).strip("-")
        part = re.sub(r"-{2,}", "-", part)
        if part:
            normalized_parts.append(part)
    return "/".join(normalized_parts)


def title_case_name(name: str) -> str:
    """Convert hyphenated name to Title Case."""
    leaf = name.rsplit("/", 1)[-1]
    return " ".join(word.capitalize() for word in leaf.split("-"))


def parse_resources(raw: str) -> list[str]:
    if not raw:
        return []
    resources = [item.strip() for item in raw.split(",") if item.strip()]
    invalid = sorted({r for r in resources if r not in ALLOWED_RESOURCES})
    if invalid:
        print(f"[ERROR] Unknown resource type(s): {', '.join(invalid)}")
        print(f"   Allowed: {', '.join(sorted(ALLOWED_RESOURCES))}")
        sys.exit(1)
    seen: set[str] = set()
    deduped = []
    for r in resources:
        if r not in seen:
            deduped.append(r)
            seen.add(r)
    return deduped


def init_skill(
    skill_name: str,
    path: str,
    resources: list[str],
    include_examples: bool,
    description: str = "",
) -> Path | None:
    skill_dir = Path(path).expanduser().resolve() / skill_name
    if skill_dir.exists():
        print(f"[ERROR] Skill directory already exists: {skill_dir}")
        return None

    try:
        skill_dir.mkdir(parents=True, exist_ok=False)
    except Exception as e:
        print(f"[ERROR] Error creating directory: {e}")
        return None

    title = title_case_name(skill_name)
    content = SKILL_TEMPLATE.format(
        name=title,
        description=description
        or "[TODO: Describe what this skill does and when to use it]",
        title=title,
        body="[TODO: Add instructions for the agent when this skill is activated]",
    )
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    print(f"[OK] Created {skill_dir / 'SKILL.md'}")

    for resource in resources:
        res_dir = skill_dir / resource
        res_dir.mkdir(exist_ok=True)
        if include_examples:
            if resource == "scripts":
                script = res_dir / "example.py"
                script.write_text(EXAMPLE_SCRIPT.format(name=skill_name))
                script.chmod(0o755)
                print(f"[OK] Created {resource}/example.py")
            elif resource == "references":
                (res_dir / "api_reference.md").write_text(
                    EXAMPLE_REFERENCE.format(title=title)
                )
                print(f"[OK] Created {resource}/api_reference.md")
            elif resource == "assets":
                (res_dir / "example_asset.txt").write_text(EXAMPLE_ASSET)
                print(f"[OK] Created {resource}/example_asset.txt")
        else:
            print(f"[OK] Created {resource}/")

    print(f"\n[OK] Skill '{skill_name}' initialized at {skill_dir}")
    return skill_dir


def main():
    parser = argparse.ArgumentParser(
        description="Create a new skill directory with a SKILL.md template."
    )
    parser.add_argument(
        "skill_name", help="Skill name (e.g., 'code-review' or 'quality/lint')"
    )
    parser.add_argument("--path", required=True, help="Parent directory for the skill")
    parser.add_argument("--description", default="", help="One-line skill description")
    parser.add_argument(
        "--resources", default="", help="Comma-separated: scripts,references,assets"
    )
    parser.add_argument(
        "--examples", action="store_true", help="Create example files in resource dirs"
    )
    args = parser.parse_args()

    skill_name = normalize_skill_name(args.skill_name)
    if not skill_name:
        print("[ERROR] Skill name must include at least one letter or digit.")
        sys.exit(1)
    leaf = skill_name.rsplit("/", 1)[-1]
    if len(leaf) > MAX_SKILL_NAME_LENGTH:
        print(f"[ERROR] Leaf name '{leaf}' exceeds {MAX_SKILL_NAME_LENGTH} characters.")
        sys.exit(1)
    if skill_name != args.skill_name:
        print(f"Note: Normalized '{args.skill_name}' -> '{skill_name}'")

    resources = parse_resources(args.resources)
    if args.examples and not resources:
        print("[ERROR] --examples requires --resources to be set.")
        sys.exit(1)

    result = init_skill(
        skill_name, args.path, resources, args.examples, args.description
    )
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
