/** Presentation helpers for tool calls and approval prompts. */

import {
  CONFIRM_TOOL_LABELS,
  MAX_TRACE_DOTS,
  SESSION_STATUS_LABELS,
  TRACE_SEVERITY,
} from '../constants'
import type {
  ConfirmRisk,
  Message,
  SessionInfo,
  SessionState,
  ToolDotSummary,
  ToolState,
} from '../types'


export function toolStateLabel(state?: ToolState): string {
  if (state === 'running') return '执行中'
  if (state === 'failed') return '失败'
  if (state === 'blocked') return '已阻止'
  if (state === 'interrupted') return '已中断'
  return '已完成'
}


/** How many steps of a turn ended in each state, keyed by the state itself. */
export function countToolStates(tools: Message[]): Record<ToolState, number> {
  const counts: Record<ToolState, number> = {
    done: 0,
    running: 0,
    failed: 0,
    blocked: 0,
    interrupted: 0,
  }
  for (const tool of tools) counts[tool.toolState || 'done'] += 1
  return counts
}


/**
 * The one thing a collapsed trace should say about its steps, or ''.
 *
 * Only the states a person would act on: a done step is not news, and a running
 * one is already pulsing in the marks. When several kinds went wrong the worst
 * is named -- the same worst-case rule the marks use -- because a summary that
 * lists four categories is not a summary.
 */
export function toolAttentionLabel(counts: Record<ToolState, number>): string {
  if (counts.failed) return `${counts.failed} 步失败`
  if (counts.blocked) return `${counts.blocked} 步被阻止`
  if (counts.interrupted) return `${counts.interrupted} 步中断`
  return ''
}


/** The full breakdown, for a hover title or a detail header. */
export function describeToolStates(counts: Record<ToolState, number>): string {
  const parts: string[] = []
  if (counts.done) parts.push(`${counts.done} 步完成`)
  if (counts.running) parts.push(`${counts.running} 步执行中`)
  if (counts.failed) parts.push(`${counts.failed} 步失败`)
  if (counts.blocked) parts.push(`${counts.blocked} 步被阻止`)
  if (counts.interrupted) parts.push(`${counts.interrupted} 步中断`)
  return parts.join(' · ')
}


export function summariseToolDots(tools: Message[]): ToolDotSummary[] {
  if (tools.length <= MAX_TRACE_DOTS) {
    return tools.map(tool => ({ state: tool.toolState || 'done', count: 1 }))
  }
  const summaries: ToolDotSummary[] = []
  for (let index = 0; index < MAX_TRACE_DOTS; index += 1) {
    // Contiguous, non-overlapping spans that together cover every step, so
    // compressing cannot silently drop one from the count.
    const start = Math.floor((index * tools.length) / MAX_TRACE_DOTS)
    const end = Math.floor(((index + 1) * tools.length) / MAX_TRACE_DOTS)
    let state: ToolState = 'done'
    for (let cursor = start; cursor < end; cursor += 1) {
      const candidate = tools[cursor].toolState || 'done'
      if (TRACE_SEVERITY[candidate] > TRACE_SEVERITY[state]) state = candidate
    }
    summaries.push({ state, count: end - start })
  }
  return summaries
}


/**
 * A user message that belongs to the turn already open, not to a new one.
 *
 * Two flags, one meaning.  ``queued`` is the local one: this message was sent
 * while the agent was mid-reply, and the server has not answered yet about
 * which queue it went into.  ``interjection`` is the server's: the message was
 * folded into the turn that was running, so it is part of that turn's record.
 * Both cases have to keep the turn open -- flushing on either one is what
 * split a running turn in two and pushed its tool trace to the bottom.
 *
 * A message queued for a *later* turn (the restart queue) carries ``queued``
 * too, and is also kept inside the open group while it waits: it has not
 * started a turn yet, and the server's transcript will place it correctly once
 * it does.
 */
export function isInTurnUserMessage(item: Message): boolean {
  return item.role === 'user' && Boolean(item.queued || item.interjection)
}


export function confirmToolLabel(name?: string): string {
  const key = String(name || '').trim()
  if (!key) return '未知操作'
  return CONFIRM_TOOL_LABELS[key] || key
}


export function confirmRisk(level?: string): ConfirmRisk {
  const value = String(level || '').trim().toLowerCase()
  // An unrecognised tier is treated as the most dangerous one: a mislabelled
  // prompt should look alarming, not reassuring.
  if (value === 'medium' || value === 'low') return value
  return 'high'
}


/** A session's busy-state word, or '' when it is idle (or its server is old). */
export const sessionStatusOf = (item: SessionInfo): string =>
  SESSION_STATUS_LABELS[String(item.status || 'idle')] || ''


/** Running, stopping or holding queued work -- the states that get a badge. */
export const isSessionBusy = (item: SessionInfo): boolean => Boolean(sessionStatusOf(item))


/**
 * Whether a ``/state`` snapshot says the session still has work in it.
 *
 * The server's own word, from the same snapshot the task guidance is read
 * from, so it is the only thing that can say whether the agent is still
 * working on that task.  ``isStreaming`` cannot say it: that flag tracks a live
 * *text* stream, and a turn that is running tool calls -- or that reported an
 * error it is still recovering from -- has none, so the flag reads "stopped"
 * for the whole stretch.  A queue count counts too: the coordinator runs
 * queued work itself, so nothing is waiting for the reader while it is
 * non-empty.
 */
export const sessionStateBusy = (
  state?: Pick<SessionState, 'operation_state' | 'queue'> | null,
): boolean =>
  String(state?.operation_state || 'idle') !== 'idle' ||
  Number(state?.queue?.pending || 0) > 0


