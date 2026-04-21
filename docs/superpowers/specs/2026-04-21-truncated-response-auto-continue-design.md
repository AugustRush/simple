# Truncated Response Auto-Continue Design

## Goal

When an OpenAI-compatible model ends with `finish_reason=length`, the agent should try to continue the same answer automatically instead of stopping after a partial response.

## Design

- Scope is limited to OpenAI-compatible final text turns.
- When a final response is truncated, the agent issues a short continuation request using temporary in-memory messages:
  - prior assistant partial text
  - a synthetic continuation user prompt
- Continuation requests do not expose tools.
- The agent merges continuation text back into one final answer with overlap trimming to reduce duplicate prefixes.
- Automatic continuation is bounded to 2 attempts after the original truncated response.
- If the answer is still truncated after the limit, the agent returns the merged partial text plus an explicit truncation error.

## Non-Goals

- No automatic continuation for tool-use turns.
- No Anthropic-specific continuation behavior in this phase.
- No unbounded retries or generic retry framework.

## Acceptance Criteria

- A single truncated response can be completed automatically when the next call returns `stop`.
- Repeated truncated responses stop after the configured bound and surface a clear error.
- The merged answer avoids simple repeated overlap at continuation boundaries.
