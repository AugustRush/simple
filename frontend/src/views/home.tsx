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
import type { HomeActivity } from '../types'
import { formatDateTime, relativeTime } from '../lib/format'
import { Button, Card, Empty, Input, Skeleton } from 'antd'
import {
  ArrowRightOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  CompassOutlined,
  LoadingOutlined,
  MessageOutlined,
  SendOutlined,
  WarningOutlined,
} from '@ant-design/icons'

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

export function renderHome(ctx: AppCtx) {
  const {
    attentionRuns, composerRef, creatingSession, home, homeLoaded, input,
    navigateTo, selectSession, sendMessage, setInput,
  } = ctx

  const needs = home?.needs
  const running = home?.running
  const activity = home?.activity ?? []

  const proposals = Number(needs?.proposals || 0)
  const failures = Number(needs?.failures || 0)
  const inFlight = running?.in_flight ?? []
  const nextUp = running?.next_up ?? []
  const needsPerson = proposals + failures > 0
  const busy = inFlight.length > 0

  const submit = () => {
    if (!input.trim() || creatingSession) return
    // `sendMessage` creates a session when none is active and switches to the
    // chat view, so "type here" is one step. A create-then-send dance here would
    // make this page's box a two-step flow for no reason.
    void sendMessage()
  }

  /** The newest failure's task name, for the one line the card shows. */
  const newestFailureName = attentionRuns[0]?.task_name || ''

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
      {/* ── what needs a person ─────────────────────────────────────────── */}
      {needsPerson && (
        <section className="home-needs">
          <div className="home-section-title">需要你</div>
          {proposals > 0 && (
            <Card
              className="home-card home-card-action"
              hoverable
              role="button"
              tabIndex={0}
              onClick={() => navigateTo('autonomy')}
              onKeyDown={event => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  navigateTo('autonomy')
                }
              }}
            >
              <div className="home-card-head">
                <span className="home-card-icon"><CompassOutlined /></span>
                <div>
                  <strong>{proposals} 条自主提议等你答复</strong>
                  <span>它不会自己动手——同意或拒绝都是一句话的事</span>
                </div>
                <ArrowRightOutlined className="home-card-arrow" />
              </div>
            </Card>
          )}
          {failures > 0 && (
            <Card
              className="home-card home-card-action"
              hoverable
              role="button"
              tabIndex={0}
              onClick={() => navigateTo('schedules')}
              onKeyDown={event => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  navigateTo('schedules')
                }
              }}
            >
              <div className="home-card-head">
                <span className="home-card-icon home-card-icon-warn"><WarningOutlined /></span>
                <div>
                  <strong>{failures} 次运行结束得不好</strong>
                  <span>{newestFailureName ? `最近一次：${newestFailureName}` : '还没有人看过'}</span>
                </div>
                <ArrowRightOutlined className="home-card-arrow" />
              </div>
            </Card>
          )}
        </section>
      )}

      {/* ── what is happening now ───────────────────────────────────────── */}
      {(busy || nextUp.length > 0) && (
        <section className="home-running">
          <div className="home-section-title">正在做</div>
          <div className="home-running-list">
            {inFlight.map(item => (
              <div key={`fly:${item.task_id}`} className="home-running-row is-now">
                <LoadingOutlined />
                <strong>{item.name}</strong>
                <span>
                  {RUN_STATUS_LABEL[item.status] || item.status}
                  {item.started_at ? ` · 开始于 ${relativeTime(item.started_at)}` : ''}
                </span>
              </div>
            ))}
            {nextUp.map(item => (
              <div key={`next:${item.task_id}`} className="home-running-row">
                <ClockCircleOutlined />
                <strong>{item.name}</strong>
                {/* `relativeTime` already says the direction ("3 小时后"), so no
                    suffix here: appending one produced "刚刚后" before the
                    formatter knew about future times. */}
                <span title={formatDateTime(item.next_run_at, true)}>
                  {relativeTime(item.next_run_at)}
                </span>
              </div>
            ))}
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
          {activity.length > 0 && (
            <Button type="link" size="small" onClick={() => navigateTo('schedules')}>
              全部任务
            </Button>
          )}
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
          <div className="home-timeline">{activity.map(renderActivity)}</div>
        )}
      </section>
    </div>
  )
}
