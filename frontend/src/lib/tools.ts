/** Presentation helpers for tool calls and approval prompts. */

import {
  CONFIRM_TOOL_LABELS,
  MAX_TRACE_DOTS,
  SESSION_STATUS_LABELS,
  TRACE_SEVERITY,
} from '../constants'
import type { ConfirmRisk, Message, SessionInfo, ToolDotSummary, ToolState } from '../types'


export function toolStateLabel(state?: ToolState): string {
  if (state === 'running') return '执行中'
  if (state === 'blocked') return '已阻止'
  if (state === 'interrupted') return '已中断'
  return '已完成'
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
