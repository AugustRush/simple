# Truncated Response Auto-Continue Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically continue truncated OpenAI-compatible model responses instead of ending with a partial answer.

**Architecture:** Detect `finish_reason=length` on final text turns, issue bounded continuation requests against a temporary context without tools, merge returned text back into one assistant reply, and preserve an explicit error if the continuation budget is exhausted.

**Tech Stack:** Python, pytest.

---

## File Structure

- Modify `agent/core/agent.py`: add bounded continuation flow and overlap merge helper.
- Modify `tests/test_agent_integration.py`: cover successful auto-continue, overlap trimming, and exhausted continuation budget.
- Keep `tests/test_feishu_channel.py`: preserve visible partial-text behavior for terminal truncation errors.

## Task 1: Regression Tests

**Files:**
- Modify: `tests/test_agent_integration.py`

- [ ] **Step 1: Write failing tests**
- [ ] **Step 2: Run focused tests to verify they fail**

Run: `uv run pytest -q tests/test_agent_integration.py::test_send_message_auto_continues_openai_length_finish tests/test_agent_integration.py::test_send_message_auto_continue_trims_overlap tests/test_agent_integration.py::test_send_message_reports_error_after_auto_continue_budget`

Expected: fail because truncated responses are only surfaced as errors today.

## Task 2: Minimal Implementation

**Files:**
- Modify: `agent/core/agent.py`

- [ ] **Step 1: Add bounded continuation helper**
- [ ] **Step 2: Add overlap merge helper**
- [ ] **Step 3: Re-run focused tests**

## Task 3: Full Verification

- [ ] **Step 1: Run targeted suites**

Run: `uv run pytest -q tests/test_agent_integration.py tests/test_feishu_channel.py`

- [ ] **Step 2: Run full suite**

Run: `uv run pytest -q`

- [ ] **Step 3: Commit**

```bash
git add agent/core/agent.py tests/test_agent_integration.py tests/test_feishu_channel.py docs/superpowers/specs/2026-04-21-truncated-response-auto-continue-design.md docs/superpowers/plans/2026-04-21-truncated-response-auto-continue.md
git commit -m "feat: auto-continue truncated model responses"
```
