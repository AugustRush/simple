/** The corner that answers "is anything happening" without leaving the page.
 *
 * The board used to be a 400px drawer, which meant the only way to know whether
 * the agent was busy was to open something that covered the conversation. But
 * most of the time the question is a glance, not a visit: "is it still working",
 * "is anything waiting for me". Those are two numbers and a colour.
 *
 * So this is the glance: a compact strip in the top-right that says how much is
 * running, how much is waiting on the person, and how much is queued behind the
 * current work. Clicking it opens the full board -- which is now a page, because
 * a panel that covers the composer is not where you read a list.
 *
 * What it deliberately does not show
 * ----------------------------------
 * The record (runs, conversations, what it did on its own). That is the board's
 * footer and it is a *reading*, not a status: putting a number like "近 7 天
 * 巡逻 279 趟" in the corner would make the corner something you have to parse.
 */

import type { AppCtx } from '../app/AppCtx'

export function renderStatusPanel(ctx: AppCtx) {
  const { home, navigateTo } = ctx
  const board = home?.board
  if (!board) return null

  const running = (board.running?.length ?? 0) + (board.working?.length ?? 0)
  const waiting = board.waiting?.length ?? 0
  const parked = board.parked?.length ?? 0
  const scheduled = Number(board.scheduled_total || 0)

  // Idle is a state worth showing, not an absence: a strip that vanishes when
  // nothing is happening leaves the reader unable to tell "it is idle" from
  // "this panel is broken". One quiet dot says which.
  if (!running && !waiting && !parked && !scheduled) {
    return (
      <button
        type="button"
        className="status-panel is-idle"
        onClick={() => navigateTo('board')}
        aria-label="状态：空闲。打开看板"
        title="打开看板"
      >
        <span className="status-panel-item">
          <i aria-hidden="true" />
          <span className="status-panel-label">空闲</span>
        </span>
      </button>
    )
  }

  // What the strip leads with: the thing that needs a person, then what is
  // moving, then what is queued. One dot, one number, in that order of urgency.
  const items: { key: string; label: string; count: number; tone: string }[] = []
  if (waiting) items.push({ key: 'waiting', label: '等你', count: waiting, tone: 'is-waiting' })
  if (running) items.push({ key: 'running', label: '在跑', count: running, tone: 'is-running' })
  if (parked) items.push({ key: 'parked', label: '排着', count: parked, tone: 'is-parked' })
  if (scheduled) items.push({ key: 'scheduled', label: '已排期', count: scheduled, tone: 'is-scheduled' })

  return (
    <button
      type="button"
      className="status-panel"
      onClick={() => navigateTo('board')}
      // The accessible name says what the numbers are; the visible text is
      // deliberately terse because it sits in the bar.
      aria-label={`状态：${items.map(item => `${item.label} ${item.count}`).join('，')}。打开看板`}
      title="打开看板"
    >
      {items.map(item => (
        <span className={`status-panel-item ${item.tone}`} key={item.key}>
          <i aria-hidden="true" />
          <span className="status-panel-count">{item.count}</span>
          <span className="status-panel-label">{item.label}</span>
        </span>
      ))}
    </button>
  )
}
