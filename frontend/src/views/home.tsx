/** The landing view: one agent, one box, and whatever needs a person.
 *
 * Why this page exists
 * --------------------
 * The app used to open on a chat view whose identity was "whichever session was
 * last active", above a sidebar of twenty equal conversations. That shape was
 * honest when the product *was* a session manager. It is not any more: the agent
 * has one identity, the sessions are threads of work, and the first question a
 * person has on opening the app is not "which conversation was I in" but **"is
 * anything waiting for me".
 *
 * So this page answers that question and nothing else:
 *
 * 1. **What needs you** -- proposals the patrol raised (they will not be executed
 *    without an answer) and runs that ended badly. Absent when there is nothing,
 *    because a healthy state must not take space.
 * 2. **A box** -- the agent itself. Typing here starts a thread, without a
 *    "new conversation" click first: the first message *is* the thread.
 * 3. **Recent threads** -- five, by title. The rest is one click away, because
 *    browsing history is not a thing you do on the way to asking something.
 *
 * What it deliberately is not
 * ---------------------------
 * Not a dashboard. There are no counts of things that are fine, no charts, no
 * "welcome back" -- every element here either needs a person or is the input.
 */

import type { AppCtx } from '../app/AppCtx'
import { formatDateTime, relativeTime } from '../lib/format'
import { Button, Card, Empty, Input, Tag } from 'antd'
import {
  ArrowRightOutlined,
  CompassOutlined,
  MessageOutlined,
  SendOutlined,
  WarningOutlined,
} from '@ant-design/icons'

/** The five most recent threads, live first. */
const RECENT_LIMIT = 5

export function renderHome(ctx: AppCtx) {
  const {
    activeSession, composerRef, creatingSession, filteredSessions, input,
    navigateTo, proposals, selectSession, sendMessage, setInput, unseenFailures,
    attentionRuns,
  } = ctx

  const waiting = proposals.length
  const failed = Number(unseenFailures || 0)
  const needsPerson = waiting + failed > 0

  const recent = filteredSessions.slice(0, RECENT_LIMIT)
  const more = Math.max(0, filteredSessions.length - RECENT_LIMIT)

  /** Send from here: no session needed. The first message creates the thread. */
  const submit = () => {
    if (!input.trim() || creatingSession) return
    // `sendMessage` already creates a session when none is active, and switches
    // to the chat view -- so this page does not need its own create-then-send
    // dance, which is what would have made "type here" a two-step flow.
    void sendMessage()
  }

  return (
    <div className="page-view home-view">
      {/* ── what needs a person ─────────────────────────────────────────── */}
      {needsPerson && (
        <section className="home-needs">
          <div className="home-section-title">需要你</div>
          {waiting > 0 && (
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
                  <strong>{waiting} 条自主提议等你答复</strong>
                  <span>它不会自己动手——同意或拒绝都是一句话的事</span>
                </div>
                <ArrowRightOutlined className="home-card-arrow" />
              </div>
            </Card>
          )}
          {failed > 0 && (
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
                  <strong>{failed} 次运行结束得不好</strong>
                  <span>
                    {attentionRuns[0]?.task_name
                      ? `最近一次：${attentionRuns[0].task_name}`
                      : '还没有人看过'}
                  </span>
                </div>
                <ArrowRightOutlined className="home-card-arrow" />
              </div>
            </Card>
          )}
        </section>
      )}

      {/* ── the agent ───────────────────────────────────────────────────── */}
      <section className="home-agent">
        <div className="home-agent-mark">S</div>
        <h1 className="home-agent-title">
          {needsPerson ? '还有几件事等你' : '有什么可以帮你？'}
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
            placeholder="说一句话就开始——不用先新建会话"
            autoSize={{ minRows: 2, maxRows: 6 }}
            className="home-composer-input"
          />
          <div className="home-composer-foot">
            <span className="home-composer-hint">
              第一条消息就是一条线程；它会自己命名，也会被自主巡逻看见
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

      {/* ── threads ─────────────────────────────────────────────────────── */}
      <section className="home-threads">
        <div className="home-section-title">
          最近的线程
          {more > 0 && (
            <Button type="link" size="small" onClick={() => navigateTo('sessions')}>
              全部 {filteredSessions.length} 条
            </Button>
          )}
        </div>
        {recent.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            // An empty state says how the list fills: the box above is the way.
            description="还没有线程。在上面那个框里说一句话，就有了第一条。"
            className="page-empty"
          />
        ) : (
          <div className="home-thread-list">
            {recent.map(item => (
              <div
                key={item.session_id}
                className={`home-thread ${item.session_id === activeSession ? 'active' : ''}`}
                role="button"
                tabIndex={0}
                aria-label={`打开线程 ${item.title || '未命名线程'}`}
                onClick={() => selectSession(item.session_id)}
                onKeyDown={event => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    selectSession(item.session_id)
                  }
                }}
              >
                <span className="home-thread-icon"><MessageOutlined /></span>
                <div className="home-thread-body">
                  {/* The title is the thread. No turn count, no id: those are
                      properties of a session record, and this page is about
                      conversations somebody had. */}
                  <strong>{item.title || '未命名线程'}</strong>
                  <span title={formatDateTime(item.last_activity, true)}>
                    {relativeTime(item.last_activity)}
                    {item.live ? ' · 已加载' : ''}
                  </span>
                </div>
                {item.status === 'queued' && <Tag color="gold">排队中</Tag>}
              </div>
            ))}
          </div>
        )}
      </section>

      {/* A page that opens the app should say what the agent is doing when it is
          not being asked -- that is the whole point of the autonomy work. */}
      <p className="home-footnote">
        它在后台每十分钟看一遍线程，有发现会出现在「自主」里；<strong>不会自己动手</strong>，
        除非你同意。
      </p>
    </div>
  )
}
