/** The landing view: the agent itself, and what it has been doing.
 *
 * Why this page exists
 * --------------------
 * The app used to open on a chat view whose identity was "whichever session was
 * last active". That was honest when the product *was* a session manager. It is
 * not any more: the agent has one identity, threads are where its work happened,
 * and the question a person has on opening the app is not "which conversation was
 * I in" but **"is anything waiting for me, and what has it been doing"**.
 *
 * Two blocks, in that order:
 *
 * 1. **A box** -- the agent. Typing here starts a thread; the first message *is*
 *    the thread. Tasks are created the same way, by asking: `schedule_create` is
 *    a tool, and a form on this page would be a second path to the same act. The
 *    one line allowed above it is how much is waiting, because that is the only
 *    fact that changes what the person should do next.
 * 2. **最近做了什么** -- one ledger of runs and conversations, newest first, with
 *    day headings, because "what has it done lately" is a question about the
 *    agent rather than about a table.
 *
 * The board is a panel (see `board.tsx`), not a block on this page: it answers
 * the same question on demand without charging every visit for it.
 *
 * What it deliberately is not
 * ---------------------------
 * Not a dashboard, not a duplicate of the chat page, and not a duplicate of the
 * sidebar. It used to open on an "S" mark over "有什么可以帮你？" -- byte for byte
 * the chat page's empty state -- so the two pages were indistinguishable on the
 * screen a person sees first. Threads live in the sidebar; the conversation
 * lives on 对话; this page is the box plus the record.
 */

import type { AppCtx } from '../app/AppCtx'
import type { HomeActivity } from '../types'
import { RUN_STATUS_LABEL } from './board'
import { dayBucket, formatDateTime, relativeTime } from '../lib/format'
import { Button, Empty, Input, Skeleton } from 'antd'
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  LoadingOutlined,
  MessageOutlined,
  SendOutlined,
} from '@ant-design/icons'

export function renderHome(ctx: AppCtx) {
  const {
    activityCursor, activityExhausted, activityLoading, composerRef, creatingSession,
    home, homeLoaded, input, loadMoreActivity, olderActivity,
    selectSession, sendMessage, setBoardOpen, setInput,
  } = ctx

  // The first page plus whatever "加载更早" has pulled in. Kept separate from
  // `home` so refreshing the landing view starts from the top again.
  const activity = [...(home?.activity ?? []), ...olderActivity]

  const waitingCount = Number(home?.board?.waiting?.length || 0)
  const needsPerson = waitingCount > 0

  // Day headings, derived here rather than rendered as a column: the same
  // heading repeated on eight rows is noise, and a date column would push the
  // titles (the thing being scanned) away from the edge.
  const groups: { label: string; items: HomeActivity[] }[] = []
  for (const item of activity) {
    const label = dayBucket(item.at) || '更早'
    const last = groups[groups.length - 1]
    if (last && last.label === label) last.items.push(item)
    else groups.push({ label, items: [item] })
  }

  const submit = () => {
    if (!input.trim() || creatingSession) return
    // `sendMessage` creates a session when none is active and switches to the
    // chat view, so "type here" is one step. A create-then-send dance here would
    // make this page's box a two-step flow for no reason.
    void sendMessage()
  }

  const renderActivity = (item: HomeActivity) => {
    if (item.kind === 'thread') {
      return (
        <div
          key={`thread:${item.session_id}`}
          className="home-timeline-row home-timeline-thread"
          role="button"
          tabIndex={0}
          aria-label={`打开线程 ${item.title}`}
          onClick={() => selectSession(item.session_id || '')}
          onKeyDown={event => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault()
              selectSession(item.session_id || '')
            }
          }}
        >
          <span className="home-timeline-icon"><MessageOutlined /></span>
          <div className="home-timeline-body">
            <strong>{item.title}</strong>
            <span>{item.turn_count ? `${item.turn_count} 轮对话` : '对话'}</span>
          </div>
          <span className="home-timeline-at" title={formatDateTime(item.at, true)}>
            {relativeTime(item.at)}
          </span>
        </div>
      )
    }

    const failed = item.status === 'failed' || item.status === 'interrupted'
    const live = item.status === 'running' || item.status === 'queued'
    const icon = live ? <LoadingOutlined /> : failed ? <CloseCircleOutlined /> : <CheckCircleOutlined />
    // A run's second line says how it ended, in its own words: the summary when
    // there is one, the error when it failed, nothing when neither exists.
    // A success does not need explaining on a feed -- "✓ 每天早上看日志 · 成功" is
    // the whole story. Only a failure earns a second line, because that line is the
    // reason it is worth surfacing at all. Showing every run's full output summary
    // here turned the feed into a wall of log-file lists.
    const detail = failed ? (item.error || item.summary || '') : ''

    return (
      <div key={`run:${item.run_id}`} className={`home-timeline-row ${failed ? 'is-failed' : ''}`}>
        <span className="home-timeline-icon">{icon}</span>
        <div className="home-timeline-body">
          <strong>{item.title}</strong>
          {/* No second line for a success: the icon already says it, and a row
              whose second line repeats "成功" is a row taking two lines to say one
              thing. Only a failure earns the extra line, because the reason is the
              whole point of surfacing it. */}
          {failed && (
            <span>
              {RUN_STATUS_LABEL[item.status || ''] || item.status}
              {detail ? ` · ${detail}` : ''}
            </span>
          )}
        </div>
        <span className="home-timeline-at" title={formatDateTime(item.at, true)}>
          {relativeTime(item.at)}
        </span>
      </div>
    )
  }

  return (
    <div className="page-view home-view">
      {/* ── the box ─────────────────────────────────────────────────────────
          No greeting above it. "S / 有什么可以帮你？" is the *conversation's*
          empty state, and this page had it too -- two pages whose first
          impression was the same avatar asking the same question, which is the
          duplication that made the old 会话 page removable. A landing page for
          the agent is the box and the ledger; the box needs no introduction. */}
      <section className="home-agent">
        {needsPerson && (
          <button
            type="button"
            className="home-needs-line"
            onClick={() => setBoardOpen(true)}
          >
            <span className="home-needs-dot" aria-hidden="true" />
            {waitingCount} 件等你处理
            <span className="home-needs-open">打开看板 →</span>
          </button>
        )}
        <div className="home-composer">
          <Input.TextArea
            ref={composerRef}
            value={input}
            onChange={event => setInput(event.target.value)}
            onPressEnter={event => {
              if (event.shiftKey) return
              event.preventDefault()
              submit()
            }}
            placeholder="说一句话就开始——也可以让它「每天早上提醒我看日志」"
            autoSize={{ minRows: 1, maxRows: 6 }}
            className="home-composer-input"
          />
          <div className="home-composer-foot">
            <span className="home-composer-hint">
              第一条消息就是一条线程；任务也一样，说出来就行，不用填表
            </span>
            <Button
              className="home-send"
              type="primary"
              shape="circle"
              icon={<SendOutlined />}
              aria-label="发送"
              loading={creatingSession}
              disabled={!input.trim()}
              onClick={submit}
            />
          </div>
        </div>
      </section>

      {/* ── what happened ───────────────────────────────────────────────── */}
      <section className="home-activity">
        <div className="home-section-title">
          最近做了什么
          {/* No "全部任务" link any more: that page is where tasks are *managed*,
              and sending somebody who asked "what happened" into a management
              screen answers a different question. The history is below. */}
        </div>
        {!homeLoaded ? (
          <Skeleton active paragraph={{ rows: 4 }} title={false} />
        ) : activity.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            // Names the way in: the box above, or asking for a task in words.
            description="还没有记录。在上面那个框里说一句话，或者让它定时做点什么，这里就会长出来。"
            className="page-empty"
          />
        ) : (
          <>
            <div className="home-timeline">
              {groups.map(group => (
                <div className="home-day" key={group.label}>
                  <div className="home-day-label">{group.label}</div>
                  {group.items.map(renderActivity)}
                </div>
              ))}
            </div>
            {!activityExhausted && (activityCursor || home?.activity_next) && (
              <Button
                type="link"
                size="small"
                className="home-timeline-more"
                loading={activityLoading}
                onClick={() => void loadMoreActivity()}
              >
                加载更早
              </Button>
            )}
          </>
        )}
      </section>
    </div>
  )
}
