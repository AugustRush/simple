# Agent Package Refactor Design

## Problem

`agent.py` currently mixes unrelated responsibilities in a single runtime file:

- domain constants and dataclasses
- output sinks and channel abstractions
- tool registry and built-in tools
- memory palace, retrieval, and consolidation
- skills and plugins
- agent orchestration and sub-agent execution
- CLI commands and gateway bootstrap

This makes change impact hard to predict, encourages implicit coupling, and
causes unrelated edits to collide in the same file.

## Goals

1. Replace the monolith with an `agent/` package shaped by stable reasons to change.
2. Make dependency direction explicit so core orchestration does not depend on
   channels, storage details, or packaging glue.
3. Preserve runnable behavior: CLI commands, gateway startup, tests, and public
   runtime entrypoints should still work after the split.
4. Allow future evolution of channels, memory, skills, and plugins without
   reopening a single giant module.

## Non-Goals

- Rewriting core behavior or changing the user-facing feature set.
- Replacing the memory or tool-call algorithms.
- Redesigning Feishu, plugin, or skill semantics beyond what is needed to
   separate modules cleanly.

## First-Principles Boundaries

The package should be organized so each unit answers one question:

- `agent.domain`: What are the durable data shapes, enums, constants, and events?
- `agent.core`: How does one agent turn execute?
- `agent.tools`: How are tools defined, registered, and sourced?
- `agent.memory`: How is context stored, staged, retrieved, and consolidated?
- `agent.skills`: How are skills discovered and activated?
- `agent.plugins`: How do plugins load and hook into runtime events?
- `agent.channels`: How do transports adapt runtime I/O?
- `agent.bootstrap`: How are concrete implementations wired together?
- `agent.cli`: How is the runtime exposed as a Typer application?

## Proposed Package Layout

```text
agent/
  __init__.py
  bootstrap.py
  cli.py
  config.py

  domain/
    __init__.py
    constants.py
    events.py
    models.py

  core/
    __init__.py
    agent.py
    orchestration.py
    output.py
    tool_loop.py

  tools/
    __init__.py
    builtin.py
    mcp.py
    registry.py
    user_tools.py

  memory/
    __init__.py
    background.py
    consolidation.py
    index.py
    ltm.py
    palace.py
    retrieval.py
    staging.py

  skills/
    __init__.py
    bundles.py
    catalog.py
    runtime.py

  plugins/
    __init__.py
    base.py
    catalog.py
    manifest.py

  channels/
    __init__.py
    base.py
    cli.py
```

## Dependency Direction

The dependency graph must stay one-way:

```text
channels -> bootstrap -> core
bootstrap -> tools / memory / skills / plugins / config
core -> domain + abstract runtime collaborators
tools / memory / skills / plugins -> domain
```

Rules:

- `agent.core` must not import concrete channel implementations.
- `agent.memory` must not emit CLI or transport output directly.
- `agent.plugins` only participates through events and hooks.
- `agent.bootstrap` is the composition root and is the only place where broad
  concrete imports are expected.

## Migration Strategy

### Phase 1: Package skeleton and shared exports

- Create `agent/` package and shared domain/core modules.
- Preserve import stability by re-exporting the main runtime symbols from
  `agent/__init__.py`.
- Update packaging to resolve `simple` from `agent.cli:app`.

### Phase 2: Extract subsystems

- Move tool registry and built-in tool code into `agent.tools`.
- Move memory palace and context components into `agent.memory`.
- Move skills and plugins into their own packages.
- Move channel abstractions into `agent.channels`.

### Phase 3: Assemble and delete monolith

- Move CLI and component assembly into `agent.cli` and `agent.bootstrap`.
- Replace the old `agent.py` implementation with package entrypoints.
- Update tests to assert package boundaries instead of single-file layout.

## Verification Strategy

- Add a package-layout regression test proving `import agent` resolves to the
  package, not the monolithic module.
- Keep focused integration tests around component assembly, channel layer, and
  Feishu behavior green during the migration.
- Run the full test suite after deleting the monolith implementation.

## Risks

1. Shared constants and dataclasses can easily create circular imports if moved
   without a dedicated `domain` layer.
2. `build_components()` currently acts like a service locator; if it is not
   reduced to a composition root, coupling will survive the refactor.
3. Output and progress routing spans CLI, gateway, and sub-agents, so boundary
   mistakes there can create subtle regressions.

## Decision

Proceed with the domain-oriented package split, preserve behavior through
re-exports and focused regression tests, and remove the monolithic `agent.py`
implementation once the package is authoritative.
