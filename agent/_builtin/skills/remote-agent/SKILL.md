---
name: Remote Agent
description: Delegate tasks to remote AI agents (Codex, Claude Code) on other machines via SSH.
user-invocable: false
disable-model-invocation: false
---

# Remote Agent

Use this skill when a task is best executed on a remote machine — the codebase, build environment, or credentials live there.

## Remote execution pattern

```bash
# Delegate to Codex on a remote host
ssh <host> "cd <workdir> && codex -p '<task description>'"

# Delegate to Claude Code on a remote host
ssh <host> "cd <workdir> && claude -p '<task description>'"

# Headless mode for no interactive prompts
ssh <host> "cd <workdir> && codex exec '<task>'"
ssh <host> "cd <workdir> && claude -p '<task>' --output-format text"
```

## When to use

- The codebase is on another machine (not in your workspace)
- A remote environment has unique tools, credentials, or services
- You need multiple remote agents to work in parallel on different machines
- A task requires a specific agent (Codex for code generation, Claude for analysis)

## When NOT to use

- The task can be done locally — no need to SSH
- The remote host is not accessible via SSH
- You don't have a clear task description to delegate

## Chaining agents

The output of one remote agent can feed directly into another —
a Codex review becomes Claude's implementation prompt.

```bash
# Phase 1: Codex reviews (strength: code analysis)
ssh dev "cd /project && codex -p 'find bugs, security issues, and style problems in the PR'"
# → Codex returns a detailed review

# Phase 2: Claude implements (strength: execution)
ssh dev "cd /project && claude -p 'fix all issues from this review: <paste Codex output>'"
# → Claude applies all the fixes
```

Other patterns:

```bash
# Review-reviewer: Claude checks Codex's work
ssh dev "codex -p 'generate tests for api.py' > /tmp/tests.py"
ssh dev "claude -p 'review /tmp/tests.py for edge cases and missing coverage'"

# Parallel then synthesize: Codex + Claude in parallel, then merge
ssh dev "codex -p 'security review'" &    # parallel
ssh dev "claude -p 'performance review'" & # parallel
# Wait for both, then:
ssh dev "codex -p 'merge these two reviews: <security> <performance>'"
```

## Tips

- Set up SSH key authentication in advance (`ssh-copy-id <host>`)
- Use full paths in `cd` — the remote shell starts at the user's home
- Quote the prompt carefully: single quotes avoid local shell expansion
- For long prompts, use a heredoc or write the prompt to a temp file
- Run `codex exec` (not `codex`) when you need the result piped back without TUI
- The remote machine must have `codex` or `claude` on PATH
