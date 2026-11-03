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
 * Three blocks, in that order:
 *
 * 1. **需要你** -- proposals nobody has answered (they will not be executed without
 *    one) and runs that ended badly. Absent when empty: a healthy agent must not
 *    take up screen with a report that it is healthy.
 * 2. **正在做** -- what is running, and what is next. Also absent when empty.
 * 3. **A box** -- the agent. Typing here starts a thread; the first message *is*
 *    the thread. Tasks are created the same way, by asking: `schedule_create` is
 *    a tool, and a form on this page would be a second path to the same act.
 *
 * Then the **board**: three columns -- 等你处理 / 正在跑 / 已排期 -- ordered by how
 * close each is to needing a person, so reading left to right is reading from
 * "act" to "ignore". The urgent column is leftmost on purpose: a column that
 * needs a person must not sit behind two that do not. There is no "done" column:
 * finished work that needs nobody is the timeline's job, and a board with a done
 * column is a log with worse scanning.
 *
 * Then **最近做了什么**: one timeline of runs and conversations, newest first,
 * because "what has it done lately" is a question about the agent, not about a
 * table. Splitting them would answer a question about the schema.
 *
 * What it deliberately is not
 * ---------------------------
 * Not a dashboard, and not a duplicate of the sidebar. The old version listed five
 * threads -- the same `filteredSessions` the sidebar already shows, taking a
 * quarter of the page to say nothing new. Threads live in the sidebar; this page
 * is about what the agent did *with* them.
 */

import type { AppCtx } from '../app/AppCtx'
import type { BoardCard, HomeActivity } from '../types'
import { formatDateTime, relativeTime } from '../lib/format'
import { Button, Empty, Input, Skeleton, Space, Tag } from 'antd'
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  LoadingOutlined,
  MessageOutlined,
  SendOutlined,
} from '@ant-design/icons'

/** How a trigger repeats, in one word. The card shows this instead of a status
 *  for scheduled items, because "daily" is the fact that matters there: it says
 *  it will come back, which "next run in 11 hours" alone does not. */
const TRIGGER_LABEL: Record<string, string> = {
  daily: '每天',
  weekly: '每周',
  interval: '间隔',
  once: '一次',
  cron: '定时',
  signal: '等信号',
}

const REVERSIBILITY_LABEL: Record<string, string> = {
  reversible: '只读',
  workspace: '改工作区',
  irreversible: '不可逆',
}

const RUN_STATUS_LABEL: Record<string, string> = {
  succeeded: '成功',
  failed: '失败',
  interrupted: '被打断',
  cancelled: '已取消',
  skipped: '被跳过',
  unverified: '未判定',
  running: '正在跑',
  queued: '排队中',
}

/** One board column: a heading, its cards, and what to say when it is empty.
 *
 * The empty line is not decoration. A column that vanishes reads as "this part of
 * the page is broken"; a column that says "没有卡住的" while the one beside it is
 * full reads as "checked, nothing wrong", which is the whole point of looking.
 */
function BoardColumn({
  cards, empty, more, onDecide, onMore, onOpen, title, tone,
}: {
  cards: BoardCard[]
  empty: string
  more?: string
  onDecide?: (id: string, state: 'accepted' | 'declined') => void
  onMore?: () => void
  onOpen?: (taskId: string) => void
  title: string
  tone: 'running' | 'waiting' | 'scheduled'
}) {
  return (
    <div className={`home-column home-column-${tone}`}>
      <div className="home-column-head">
        <span className="home-column-title">{title}</span>
        <span className="home-column-count">{cards.length || ''}</span>
      </div>
      <div className="home-column-body">
        {cards.length === 0 ? (
          <span className="home-column-empty">{empty}</span>
        ) : (
          cards.map(card =>
            /* A proposal is a *candidate* task blocked on a person, so its card
               carries the answer instead of a status line: making somebody
               navigate to 自主 to answer the thing this page just showed them
               would be the page knowing what to do and refusing to do it. */
            card.kind === 'proposal' ? (
              <div key={`proposal:${card.id}`} className="home-card-tile home-card-proposal">
                <strong title={card.name}>{card.name}</strong>
                {card.detail && <span className="home-tile-detail">{card.detail}</span>}
                <div className="home-tile-actions">
                  <Tag className="home-tile-tag">
                    {REVERSIBILITY_LABEL[card.reversibility || ''] || '未知'}
                  </Tag>
                  <Space size={4}>
                    <Button
                      size="small"
                      onClick={() => onDecide?.(card.id || '', 'accepted')}
                    >
                      同意
                    </Button>
                    <Button
                      size="small"
                      onClick={() => onDecide?.(card.id || '', 'declined')}
                    >
                      不要
                    </Button>
                  </Space>
                </div>
              </div>
            ) : (
              /* A card on the board is a way into the task, not a picture of it.
                 The first column is entirely things that need a person, and
                 showing them without a way to act on them makes the column a
                 notice board bolted shut. */
              <div
                key={card.task_id}
                className={`home-card-tile home-card-${tone} home-card-open`}
                role="button"
                tabIndex={0}
                aria-label={`打开任务 ${card.name}`}
                onClick={() => onOpen?.(card.task_id)}
                onKeyDown={event => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    onOpen?.(card.task_id)
                  }
                }}
              >
                <strong title={card.name}>{card.name}</strong>
                <span className="home-tile-meta">
                  {/* One formatter for all three columns: `relativeTime` already
                      handles both directions ("3 小时后" / "2 分钟前"), and the
                      column already says which tense the card is in. */}
                  {relativeTime(card.at)}
                  {card.at ? ' · ' : ''}
                  {card.at_kind === 'next'
                    ? TRIGGER_LABEL[card.trigger_type] || '排期中'
                    : card.unread > 1
                      /* Status *and* count: the badge counts runs while a card
                         counts a task, so the number is what makes the two agree
                         -- and the status is what says what went wrong. Showing
                         only one of them throws away half the card. */
                      ? `${RUN_STATUS_LABEL[card.status] || card.status} · ${card.unread} 次未查看`
                      : RUN_STATUS_LABEL[card.status] || card.status}
                </span>
                {card.detail && <span className="home-tile-detail">{card.detail}</span>}
              </div>
            ),
          )
        )}
      </div>
      {more ? (
        <Button type="link" size="small" className="home-column-more" onClick={onMore}>
          {more}
        </Button>
      ) : null}
    </div>
  )
}

export function renderHome(ctx: AppCtx) {
  const {
    activityCursor, activityExhausted, activityLoading, composerRef, creatingSession, decideProposal,
    home, homeLoaded, input, loadMoreActivity, navigateTo, olderActivity,
    openScheduleDetails, schedules, selectSession, sendMessage, setInput,
  } = ctx

  const board = home?.board
  // The first page plus whatever "加载更早" has pulled in. Kept separate from
  // `home` so refreshing the landing view starts from the top again.
  const activity = [...(home?.activity ?? []), ...olderActivity]

  const inFlight = board?.running ?? []
  const waiting = board?.waiting ?? []
  const scheduled = board?.scheduled ?? []
  const scheduledTotal = Number(board?.scheduled_total || 0)
  const waitingOnYou = waiting.length
  const busy = inFlight.length > 0
  const boardEmpty = inFlight.length + waiting.length + scheduledTotal === 0
  const needsPerson = waitingOnYou > 0

  /** Open a task's detail: the drawer lives on 自动化, so arriving there is part
   *  of opening it. The board knows the task by id; the drawer wants the record. */
  const openTask = (taskId: string) => {
    const task = schedules.find(item => item.id === taskId)
    if (!task) {
      // The list has not loaded yet (or the task was deleted elsewhere). Landing
      // on the page that owns it is still better than doing nothing.
      navigateTo('schedules')
      return
    }
    openScheduleDetails(task)
    navigateTo('schedules')
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
    const detail = item.summary || item.error || ''

    return (
      <div key={`run:${item.run_id}`} className={`home-timeline-row ${failed ? 'is-failed' : ''}`}>
        <span className="home-timeline-icon">{icon}</span>
        <div className="home-timeline-body">
          <strong>{item.title}</strong>
          <span>
            {RUN_STATUS_LABEL[item.status || ''] || item.status}
            {detail ? ` · ${detail}` : ''}
          </span>
        </div>
        <span className="home-timeline-at" title={formatDateTime(item.at, true)}>
          {relativeTime(item.at)}
        </span>
      </div>
    )
  }

  return (
    <div className="page-view home-view">
      {/* ── the board ───────────────────────────────────────────────────── */}
      {!boardEmpty && (
        <section className="home-board">
          <div className="home-section-title">任务看板</div>
          <div className="home-board-columns">
            {/* Ordered act -> watch -> ignore. The urgent column is leftmost
                because reading order is attention order, and a column that needs
                a person must not sit behind two that do not. */}
            <BoardColumn
              title="等你处理"
              tone="waiting"
              cards={waiting}
              empty="没有卡住的"
              onDecide={(id, state) => void decideProposal(id, state)}
              onOpen={openTask}
            />
            <BoardColumn
              title="正在跑"
              tone="running"
              cards={inFlight}
              empty="没有在跑的"
              onOpen={openTask}
            />
            <BoardColumn
              title="已排期"
              tone="scheduled"
              cards={scheduled}
              empty="没有排期的"
              onOpen={openTask}
              more={
                scheduledTotal > scheduled.length
                  ? /* The rest is one click away: a board is glanced at, and
                       thirty cards in this column is a list wearing a board's
                       clothes. */
                    `还有 ${scheduledTotal - scheduled.length} 个 · 全部`
                  : ''
              }
              onMore={() => navigateTo('schedules')}
            />
          </div>
        </section>
      )}

      {/* ── the agent ───────────────────────────────────────────────────── */}
      <section className="home-agent">
        <div className="home-agent-mark">S</div>
        <h1 className="home-agent-title">
          {needsPerson ? '还有几件事等你' : busy ? '正在替你做事' : '有什么可以帮你？'}
        </h1>
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
            autoSize={{ minRows: 2, maxRows: 6 }}
            className="home-composer-input"
          />
          <div className="home-composer-foot">
            <span className="home-composer-hint">
              第一条消息就是一条线程；任务也一样，说出来就行，不用填表
            </span>
            <Button
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
            <div className="home-timeline">{activity.map(renderActivity)}</div>
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
