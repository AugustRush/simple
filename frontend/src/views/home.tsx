/** The agent's screen. One page: talk to it, and see what it did on its own.
 *
 * Why there is no chat page any more
 * ----------------------------------
 * A conversation is not a *place* you navigate to; it is what this screen shows
 * the moment you say something. Keeping 主页 as a landing and 对话 as a second
 * page meant the app had two entrances, one of them a welcome screen you had
 * already walked through -- and it made "which page am I on" and "which thread am
 * I in" two questions with different answers.
 *
 * So the app has one screen, in two states:
 *
 * * **A thread is open** -- the conversation, with a one-line strip above it
 *   saying what the agent did while you were away. Collapsed by default: it is
 *   context, and the conversation is the point.
 * * **No thread** -- the box, and the record of what it has been doing. That is
 *   the whole landing page: no greeting, no illustration, no second column.
 *   Typing here starts a thread; the first message *is* the thread.
 *
 * Why the record is a strip and not a section
 * -------------------------------------------
 * The agent works while nobody is watching. That is the product's reason to
 * exist, and it used to be a section that charged every visit for it -- eight
 * rows, a heading, day dividers -- even when you only came to ask one question.
 * Now it is one line that opens into the same list: "has it done anything" costs
 * a line, "what exactly" costs a click.
 *
 * What the record deliberately is not
 * -----------------------------------
 * Not a dashboard. Runs and conversations carry "when" and "what", so they sort
 * together; nothing is padded into anything else's shape. Threads are *rows*
 * here rather than a second navigation column -- the sidebar is gone, and an old
 * thread is reopened by clicking the row that says the thread exists.
 */

import type { AppCtx } from '../app/AppCtx'
import type { HomeActivity } from '../types'
import { renderChat } from './chat'
import { RUN_STATUS_LABEL } from './board'
import { dayBucket, formatDateTime, relativeTime } from '../lib/format'
import { Button, Empty, Input, Skeleton } from 'antd'
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  DownOutlined,
  LoadingOutlined,
  MessageOutlined,
  SendOutlined,
  UpOutlined,
} from '@ant-design/icons'
import { useState } from 'react'

/** One row of the record: a run, or a thread somebody can reopen. */
function activityRow(item: HomeActivity, selectSession: (id: string) => void) {
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
  // A success does not need explaining on a feed -- the icon says it, and a row
  // whose second line repeats "成功" takes two lines to say one thing. Only a
  // failure earns the second line, because that line is why it is worth showing.
  const detail = failed ? (item.error || item.summary || '') : ''

  return (
    <div key={`run:${item.run_id}`} className={`home-timeline-row ${failed ? 'is-failed' : ''}`}>
      <span className="home-timeline-icon">{icon}</span>
      <div className="home-timeline-body">
        <strong>
          {/* An errand the agent started for itself says so: the title is the
              sentence it wrote, and without the marker a reader would take it
              for a task they had created. */}
          {item.origin === 'autonomy' && (
            <span className="home-timeline-own">自主</span>
          )}
          {item.title}
        </strong>
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

/** What the agent has been doing, as one line that opens into the list.
 *
 * A component (not part of the render function) because it owns the open/closed
 * state: these view modules are plain functions, and a hook inside one is the
 * React #300 blank page this app has already paid for once.
 */
function SelfActivity({ ctx, defaultOpen = false }: { ctx: AppCtx; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  const activity = [...(ctx.home?.activity ?? []), ...ctx.olderActivity]
  const loading = !ctx.homeLoaded
  const latest = activity[0]

  // Day headings, derived here rather than as a column: eight rows sharing a
  // heading is noise, and a date column pushes the titles away from the edge.
  const groups: { label: string; items: HomeActivity[] }[] = []
  for (const item of activity) {
    const label = dayBucket(item.at) || '更早'
    const last = groups[groups.length - 1]
    if (last && last.label === label) last.items.push(item)
    else groups.push({ label, items: [item] })
  }

  const summary = loading
    ? '正在读…'
    : activity.length === 0
      ? '还没有记录'
      : `${activity.length} 条 · 最近 ${relativeTime(latest?.at)}`

  return (
    <section className={`self-activity${open ? ' is-open' : ''}`}>
      <button
        type="button"
        className="self-activity-head"
        aria-expanded={open}
        onClick={() => setOpen(value => !value)}
      >
        <span className="self-activity-title">它自己做的事</span>
        <span className="self-activity-summary">{summary}</span>
        {open ? <UpOutlined /> : <DownOutlined />}
      </button>
      {open && (
        <div className="self-activity-body">
          {loading ? (
            <Skeleton active paragraph={{ rows: 4 }} title={false} />
          ) : activity.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="还没有记录。在上面那个框里说一句话，或者让它定时做点什么，这里就会长出来。"
              className="page-empty self-activity-empty"
            />
          ) : (
            <>
              <div className="home-timeline">
                {groups.map(group => (
                  <div className="home-day" key={group.label}>
                    <div className="home-day-label">{group.label}</div>
                    {group.items.map(item => activityRow(item, ctx.selectSession))}
                  </div>
                ))}
              </div>
              {!ctx.activityExhausted && (ctx.activityCursor || ctx.home?.activity_next) && (
                <Button
                  type="link"
                  size="small"
                  className="home-timeline-more"
                  loading={ctx.activityLoading}
                  onClick={() => void ctx.loadMoreActivity()}
                >
                  加载更早
                </Button>
              )}
            </>
          )}
        </div>
      )}
    </section>
  )
}

export function renderHome(ctx: AppCtx) {
  // A thread is open: this screen *is* the conversation. The strip above it stays
  // collapsed -- while you are talking, what it did while you were away is
  // background, not the subject.
  if (ctx.activeSession) {
    return (
      <div className="home-room">
        <SelfActivity ctx={ctx} />
        <div className="home-room-thread">{renderChat(ctx)}</div>
      </div>
    )
  }

  const { composerRef, creatingSession, input, sendMessage, setInput } = ctx
  const submit = () => {
    if (!input.trim() || creatingSession) return
    // `sendMessage` creates a thread when none is active, and this screen becomes
    // the conversation. A create-then-send dance would make the box two steps.
    void sendMessage()
  }

  return (
    <div className="page-view home-view">
      <div className="home-box">
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
          autoSize={{ minRows: 1, maxRows: 8 }}
          className="home-composer-input"
        />
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
      <SelfActivity ctx={ctx} defaultOpen />
    </div>
  )
}
