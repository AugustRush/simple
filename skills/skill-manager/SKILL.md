---
name: Skill Manager
description: Create, update, delete, and manage user skill bundles. Use when the user wants to create a new skill, edit an existing skill, remove a skill, or manage skill lifecycle and structure.
user-invocable: true
disable-model-invocation: false
---

# Skill Manager

Manage user skill bundles. Skills are instruction packages that extend the agent's capabilities with specialized knowledge, workflows, and bundled resources. Each skill lives in its own directory under `~/.agent/skills/` and must contain a `SKILL.md` entrypoint.

## Core Principles

**The agent is already smart.** Only include information the agent doesn't already have. Challenge each piece of content: "Does this justify its token cost?"

**Progressive disclosure.** Skills use a three-level loading system:
1. **Metadata** (name + description) -- always in context (~100 words)
2. **SKILL.md body** -- loaded when skill triggers (<5k words, keep under 500 lines)
3. **Bundled resources** -- loaded on demand by the agent (scripts can be executed without reading into context)

**Match freedom to fragility.** High freedom (text instructions) for flexible tasks; low freedom (specific scripts) for fragile, error-prone operations.

## Skill Bundle Structure

```
~/.agent/skills/
└── <skill-id>/
    ├── SKILL.md          # Required: frontmatter + instructions
    ├── scripts/          # Optional: executable code (Python/Bash)
    ├── references/       # Optional: documentation loaded into context as needed
    └── assets/           # Optional: templates, images, fonts used in output
```

### Resource Types

| Directory | Purpose | Context? |
|---|---|---|
| `scripts/` | Deterministic operations, repeated code | Can execute without reading |
| `references/` | API docs, schemas, workflow guides | Loaded on demand |
| `assets/` | Templates, images, boilerplate | Not loaded; used in output |

## SKILL.md Format

```markdown
---
name: My Skill
description: What this skill does AND when to use it. Include triggering scenarios.
user-invocable: true
disable-model-invocation: false
---

Instructions for the agent when this skill is activated.
```

### Frontmatter Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | dir name | Display name |
| `description` | string | "" | Triggering mechanism -- include both what and when |
| `user-invocable` | bool | true | User can explicitly invoke via `/skill` |
| `disable-model-invocation` | bool | false | Prevent model from auto-activating |

**Critical:** `description` is the primary triggering mechanism. "When to use" info must be here, not in the body (the body loads only after triggering).

## Management Tools

- **`create_skill`** -- Create a new skill bundle
- **`update_skill`** -- Update metadata or instructions (user skills only)
- **`delete_skill`** -- Remove a skill bundle (user skills only)
- **`write_skill_file`** -- Write/update a supporting file in a bundle

After any mutation, the catalog hot-reloads automatically -- the new/updated skill is immediately available in the same session.

## Bundled Scripts

Use `read_skill_file` to inspect these before running:

- **`scripts/init_skill.py`** -- Initialize a new skill from template with optional resource directories
- **`scripts/quick_validate.py`** -- Validate skill structure, frontmatter, and naming conventions

### init_skill.py Usage

```bash
python scripts/init_skill.py <skill-name> --path ~/.agent/skills [--resources scripts,references,assets] [--examples]
```

### quick_validate.py Usage

```bash
python scripts/quick_validate.py <path/to/skill-directory>
```

## Workflow

### Creating a Skill

1. Understand the use case with concrete examples -- ask the user how the skill will be triggered
2. Plan reusable contents: which scripts, references, or assets would help?
3. Call `create_skill` with a meaningful ID, clear description (include triggering scenarios), and instructions
4. Add supporting files with `write_skill_file` as needed
5. Validate with `scripts/quick_validate.py` if the skill has complex structure

### Skill Naming

- Use lowercase letters, digits, hyphens, and `/` for nesting
- Prefer short, verb-led phrases: `code-review`, `quality/lint`
- Name must not exceed 64 characters (leaf segment)

### Writing Effective Instructions

- Put "when to use" in the description, not in the body
- Keep SKILL.md under 500 lines; split to `references/` files when larger
- Reference bundled files from SKILL.md with clear "when to read" guidance
- Prefer concise examples over verbose explanations
- Include scripts for operations that are repeated or error-prone

### Progressive Disclosure Patterns

**Pattern 1: Guide with references** -- Core workflow in SKILL.md, detailed docs in `references/`

**Pattern 2: Domain organization** -- One reference file per domain/variant, agent reads only the relevant one

**Pattern 3: Conditional details** -- Basic content in body, advanced content linked to separate files

## Rules

- **Never modify built-in skills.** Only user skills (`~/.agent/skills/`) can be managed.
- **Confirm destructive operations** (delete) with the user.
- **Avoid extraneous files.** No README.md, CHANGELOG.md, or auxiliary docs in skill bundles.
- **Hot-reload is automatic.** After create/update/delete, changes take effect in the current session.
