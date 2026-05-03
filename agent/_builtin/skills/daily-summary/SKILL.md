---
name: Daily Summary
description: Generate structured daily or weekly summaries from context memory, session history, and scheduled tasks.
user-invocable: true
disable-model-invocation: false
---

# Daily Summary

Use this skill when the user asks for a daily summary, weekly recap, or "what happened today/this week".

## What to Do

1. **Check recent sessions** — use `context_retrieve` with queries like "today's conversation" or "recent tasks" to find relevant context from the current and recent sessions.

2. **List scheduled tasks** — use `schedule_list` to show active reminders and recurring tasks.

3. **Review memory** — use `memory_index` and `memory_search` to surface recent memory writes or changes.

4. **Compose the summary** — structure the output as:

```
## Daily Summary — {date}

### Today's Conversations
- Brief bullet points of key topics discussed

### Active Reminders & Tasks
- Scheduled tasks and their status

### Key Decisions & Findings
- Important conclusions or decisions made today

### Memory Updates
- New facts or preferences stored today
```

## Tips

- Use `current_time` to get the accurate local date before composing the summary.
- Keep each bullet concise — one sentence max.
- If there is no activity for a section, write "No activity today" instead of omitting it.
- For weekly summaries, aggregate daily summaries or scan a broader date range in context memory.
