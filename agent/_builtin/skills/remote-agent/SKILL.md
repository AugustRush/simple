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

When the user asks for a multi-phase workflow (review → implement,
generate → review, etc.), ask them which agent should handle each phase
and on which host. Never assume — the user knows their tools best.

```bash
# Pattern: output of phase 1 feeds into phase 2
ssh <host> "<agent1> -p '<phase 1 task>'"
ssh <host> "<agent2> -p '<phase 2 task>: <output from phase 1>'"
```

Example: user says "Claude review my code, then Codex implement the fixes"

```bash
ssh dev "claude -p 'review the PR for bugs, security, and style issues'"
# → Claude returns review

ssh dev "codex -p 'implement all fixes from this review: <paste Claude output>'"
# → Codex applies fixes
```

Other patterns — same idea, different agents:

```bash
# Codex generates tests → Claude reviews them
ssh dev "codex -p 'generate unit tests for api.py' > /tmp/tests.py"
ssh dev "claude -p 'review /tmp/tests.py for missing edge cases'"

# Two agents in parallel → third synthesizes
ssh host-a "<agent1> -p '<task>'" &
ssh host-b "<agent2> -p '<task>'" &
# wait for both, then:
ssh host-c "<agent3> -p 'synthesize: <output1> <output2>'"
```

Before starting a multi-phase workflow, confirm with the user:

1. Which agent for each phase? (`codex` / `claude`)
2. Which host for each phase? (can be same or different machines)

## Tips

- Set up SSH key authentication in advance (`ssh-copy-id <host>`)
- Use full paths in `cd` — the remote shell starts at the user's home
- Quote the prompt carefully: single quotes avoid local shell expansion
- For long prompts, use a heredoc or write the prompt to a temp file
- Run `codex exec` (not `codex`) when you need the result piped back without TUI
- The remote machine must have `codex` or `claude` on PATH
