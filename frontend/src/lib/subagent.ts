/** Folding live sub-agent telemetry into one rolling status note. */

import { SUBAGENT_KIND_LABELS, SUBAGENT_TERMINAL_KINDS } from '../constants'
import type { Message, SubAgentNote } from '../types'


/**
 * One log line for the strip, built from the event's own fields.
 *
 * ``evt.message`` is deliberately not used. The server renders it for a console
 * (``Parallel batch running: 1/3 completed, 2 still running``) and for a
 * sub-agent start it embeds the raw task text, so printing it put English
 * sentences and literal ``**`` emphasis inside a Chinese panel.
 * ``SUBAGENT_KIND_LABELS`` already names the same states in the UI's language.
 */
export function subagentEventLine(evt: Record<string, unknown>): string {
  const kind = String(evt.kind || '').trim()
  const role = String(evt.role || '').trim()
  const label = SUBAGENT_KIND_LABELS[kind] || kind || '状态更新'
  return role ? `${role} · ${label}` : label
}


export function newSubAgentNote(now: number): SubAgentNote {
  return {
    logs: [],
    roles: [],
    doneRoles: [],
    completed: 0,
    total: 0,
    failed: 0,
    running: true,
    finished: false,
    startedAt: now,
    endedAt: now,
  }
}


/** Fold one socket event into a note, preserving the user's expanded state. */
export function foldSubAgentEvent(
  note: SubAgentNote,
  evt: Record<string, unknown>,
  now: number,
): SubAgentNote {
  const kind = String(evt.kind || '').trim()
  const role = String(evt.role || '').trim()
  const line = subagentEventLine(evt)
  const failed = note.failed + (kind === 'agent_failed' ? 1 : 0)
  const finished = note.finished || SUBAGENT_TERMINAL_KINDS.has(kind)
  const terminal = kind === 'agent_finished' || kind === 'agent_failed'
  const lastLog = note.logs[note.logs.length - 1]
  // Progress heartbeats are not events. Their state is already the strip's
  // counter, they repeat verbatim, and logging them would push the one line
  // worth reading out of the strip.
  const logs =
    kind === 'batch_progress' || lastLog === line
      ? note.logs
      : [...note.logs, line]
  return {
    ...note,
    logs,
    roles: role && !note.roles.includes(role) ? [...note.roles, role] : note.roles,
    doneRoles:
      terminal && role && !note.doneRoles.includes(role)
        ? [...note.doneRoles, role]
        : note.doneRoles,
    completed: Math.max(note.completed, Number(evt.completed) || 0),
    total: Math.max(note.total, Number(evt.total) || 0),
    failed,
    running: !finished,
    finished,
    endedAt: now,
  }
}


/**
 * Agent counts, preferring the server's own completed/total counters and
 * falling back to the roles observed on the socket. A batch that never emits
 * ``batch_progress`` would otherwise read as "0/0" the whole way through.
 */
export function subagentCounts(note: SubAgentNote): { done: number; total: number } {
  const observed = Math.max(note.roles.length, note.doneRoles.length)
  return {
    done: note.completed > 0 ? note.completed : note.doneRoles.length,
    total: note.total > 0 ? note.total : observed,
  }
}


/**
 * The still-open sub-agent note for the turn in progress, or null.
 *
 * Deliberately not "the last row in the list". Every tool call appends a row --
 * including the sub-agents' own, because they inherit the turn's output sink --
 * so while a batch runs the last row is a tool row about as often as it is the
 * note. Keying the fold on that made one batch open a fresh strip after every
 * sub-agent tool call. A note never outlives its turn, so the search stops at
 * the user message that opened it.
 */
export function findOpenSubAgentNote(list: Message[]): Message | null {
  for (let index = list.length - 1; index >= 0; index -= 1) {
    const item = list[index]
    if (item.role === 'user') return null
    if (item.role === 'subagent' && item.subagent && !item.subagent.finished) {
      return item
    }
  }
  return null
}


export function sealSubAgentNotes(list: Message[]): Message[] {
  return list.map(item =>
    item.role === 'subagent' && item.subagent && !item.subagent.finished
      ? { ...item, subagent: { ...item.subagent, running: false, finished: true } }
      : item,
  )
}
