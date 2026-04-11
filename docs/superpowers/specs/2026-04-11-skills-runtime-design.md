# Skills Runtime Design

**Goal:** Replace the current Python `~/.agent/skills/*.py` loader with a standard skill-bundle runtime while keeping the existing `~/.agent/skills` path for user-installed skills.

## Scope

This change standardizes skills around bundle directories:

- `SKILL.md` is the required entrypoint
- optional supporting files may live alongside it
- user skills live under `~/.agent/skills`
- built-in skills live under the repo `skills/` directory

The runtime must support:

- automatic skill discovery
- progressive disclosure
- explicit invocation
- automatic skill activation by the model
- supporting-file access for active skills

## Design

### Skill Sources

Two sources are scanned recursively for `SKILL.md`:

1. `~/.agent/skills`
2. `<repo>/skills`

When two skills resolve to the same id, the user skill overrides the built-in skill.

### Skill Identity

Each skill is represented as a bundle rooted at the directory containing `SKILL.md`.

- default id: bundle path relative to its root, POSIX-style
- default display name: frontmatter `name` or the bundle directory name
- description: frontmatter `description` or empty string

Nested skills are supported because discovery is recursive.

### Frontmatter

`SKILL.md` may start with YAML-style frontmatter. The runtime only needs a small supported subset:

- `name`
- `description`
- `user-invocable`
- `disable-model-invocation`

Unknown keys are preserved in metadata but do not affect runtime behavior yet.

### Progressive Disclosure

The system prompt only exposes a compact catalog:

- skill id
- display name
- description
- invocation flags

Full skill instructions are not injected at startup.

When a skill is activated, the runtime returns:

- the `SKILL.md` body
- bundle root path
- a list of supporting files relative to the bundle root

Supporting files are read only on demand.

### Runtime Interface

The runtime exposes bundle-aware tools:

- `activate_skill`
- `list_skill_files`
- `read_skill_file`

These are generic runtime hooks, not one tool per skill.

### Explicit Invocation

Explicit invocation is supported in two forms:

- slash syntax: `/skill <id>`, `/skill <id> <task>`, `/<id> <task>`
- natural language: `use <id>`, `activate <id>`, `使用 <id>`, `启用 <id>`

Explicit invocation forces the requested skill for the current turn.

### Automatic Activation

The model sees the compact skill catalog in the system prompt and may call `activate_skill` when a skill is relevant.

This keeps the runtime close to standard skill behavior while fitting the agent's existing tool loop.

### Supporting Files

After activation, the model may inspect bundle assets via `list_skill_files` and `read_skill_file`.

Scripts under `scripts/` may be executed with the existing `shell` tool using the returned absolute bundle path.

## Risks

- Frontmatter parsing must be permissive enough for common bundle metadata without bringing in a new dependency.
- Exposing bundle files must not weaken workspace file protections for unrelated paths.
- Explicit invocation parsing must avoid hijacking normal user text.
