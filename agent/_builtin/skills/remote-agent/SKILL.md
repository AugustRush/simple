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

## Examples

```bash
# Review a PR on the CI server
ssh ci-server "cd /app && codex -p 'review the latest PR changes'"

# Fix a build failure on the build box
ssh build-box "cd /project && claude -p 'diagnose and fix the build error'"

# Parallel: security review on dev, style check on staging
ssh dev-box "cd /src && codex -p 'find security vulnerabilities'"
ssh staging "cd /src && claude -p 'check code style and best practices'"
```

## Tips

- Set up SSH key authentication in advance (`ssh-copy-id <host>`)
- Use full paths in `cd` — the remote shell starts at the user's home
- Quote the prompt carefully: single quotes avoid local shell expansion
- For long prompts, use a heredoc or write the prompt to a temp file
- Run `codex exec` (not `codex`) when you need the result piped back without TUI
- The remote machine must have `codex` or `claude` on PATH
