/** The board as a panel you pull out, not a block you scroll past.
 *
 * Why a panel
 * -----------
 * The board used to sit at the top of 主页, above the box: every visit paid for it
 * in vertical space even when nothing needed a person, and a two-line summary of
 * "everything is fine" is the least useful thing a landing page can say. It also
 * fought the one thing the page is for -- talking -- by pushing the box down.
 *
 * A panel answers the same question on demand ("what is waiting") without charging
 * the page for it when the answer is "nothing". It stays open while you work,
 * because it is a *state* view rather than a task you finish and close.
 *
 * Why the columns stack here
 * --------------------------
 * Three side-by-side columns need ~700px. A right rail is ~380px, so the same
 * three groups are stacked vertically instead: 等你处理 first (it needs a person),
 * then 正在跑 (alive, needs nothing), then 已排期 (future, needs nothing yet). The
 * order is the same argument as the wide version -- act, watch, ignore -- just
 * turned ninety degrees.
 */

import type { AppCtx } from '../app/AppCtx'
import type { BoardCard } from '../types'
import { relativeTime } from '../lib/format'
import { Button, Popconfirm, Tag } from 'antd'

/** How a trigger repeats, in one word: for a scheduled card, "每天" is the fact
 *  that matters -- it says it will come back, which a countdown alone does not. */
export const TRIGGER_LABEL: Record<string, string> = {
  daily: '每天',
  weekly: '每周',
  interval: '间隔',
  once: '一次',
  cron: '定时',
  signal: '等信号',
}

export const RUN_STATUS_LABEL: Record<string, string> = {
  succeeded: '成功',
  failed: '失败',
  interrupted: '被打断',
  cancelled: '已取消',
  skipped: '被跳过',
  unverified: '未判定',
  running: '正在跑',
  queued: '排队中',
}

const REVERSIBILITY_LABEL: Record<string, string> = {
  reversible: '只读',
  workspace: '改工作区',
  irreversible: '不可逆',
}

/** One group of cards, with a heading and a line for when it is empty.
 *
 * The empty line is not decoration: a group that vanishes reads as "this part is
 * broken", while "没有卡住的" beside a full group reads as "checked, nothing wrong"
 * -- which is the whole reason for looking.
 */
function BoardGroup({
  cards,
  empty,
  more,
  onDecide,
  onMore,
  onOpen,
  title,
  tone,
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
    <section className={`panel-group panel-group-${tone}${cards.length === 0 ? ' is-empty' : ''}`}>
      <div className="panel-group-head">
        <span className="panel-group-title">{title}</span>
        {cards.length > 0 ? (
          <span className="panel-group-count">{cards.length}</span>
        ) : (
          /* The empty sentence belongs on the heading's line, not under it.
             Stacked, two empty groups spent ~120px of a 380px panel saying
             nothing, and pushed the one group that had cards to the bottom
             half of the screen -- so the panel opened on the least useful part
             of itself. The information (checked, nothing there) is the same. */
          <span className="panel-group-empty">{empty}</span>
        )}
      </div>
      {cards.length > 0 && (
        cards.map(card =>
          card.kind === 'proposal' ? (
            /* A proposal is a *candidate* task blocked on a person, so its card
               carries the answer: making somebody close the panel and find 自主 to
               answer the thing this panel just showed them would be the panel
               knowing what to do and refusing to do it. */
            <div key={`proposal:${card.id}`} className="panel-card panel-card-proposal">
              <strong title={card.name}>{card.name}</strong>
              {card.detail && <span className="panel-card-detail">{card.detail}</span>}
              <div className="panel-card-actions">
                <Tag className="panel-card-tag">
                  {REVERSIBILITY_LABEL[card.reversibility || ''] || '未知'}
                </Tag>
                {/* Agreement first: it is the answer that keeps the agent moving,
                    and it needs no second step. A decline is the one that is worth
                    confirming -- it is what the agent is told never to raise again. */}
                <Button
                  size="small"
                  type="primary"
                  onClick={() => onDecide?.(card.id || '', 'accepted')}
                >
                  同意
                </Button>
                <Popconfirm
                  title="不要这件事？"
                  description="它不会再提第二次。"
                  okText="不要"
                  cancelText="取消"
                  onConfirm={() => onDecide?.(card.id || '', 'declined')}
                >
                  <Button size="small">不要</Button>
                </Popconfirm>
              </div>
            </div>
          ) : (
            /* A task card is a way into the task, not a picture of it: the group it
               sits in is made of things that need acting on. */
            <div
              key={card.task_id}
              className="panel-card panel-card-open"
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
              {/* For a scheduled card the trigger comes first: "每天" is what the
                  card is *about* -- it says it will come back -- and the countdown
                  is the detail. Read the other way round it said "21 小时后 ·
                  每天", which answers "when" before saying what it is. */}
              <span className="panel-card-meta">
                {card.at_kind === 'next' ? (
                  <>
                    {TRIGGER_LABEL[card.trigger_type] || '排期中'}
                    {card.at ? ` · ${relativeTime(card.at)}` : ''}
                  </>
                ) : (
                  <>
                    {relativeTime(card.at)}
                    {card.at ? ' · ' : ''}
                    {card.unread > 1
                      ? `${RUN_STATUS_LABEL[card.status] || card.status} · ${card.unread} 次未查看`
                      : RUN_STATUS_LABEL[card.status] || card.status}
                  </>
                )}
              </span>
              {card.detail && <span className="panel-card-detail">{card.detail}</span>}
            </div>
          ),
        )
      )}
      {more ? (
        <Button type="link" size="small" className="panel-group-more" onClick={onMore}>
          {more}
        </Button>
      ) : null}
    </section>
  )
}

/** The panel's contents: three groups, most urgent first. */
export function renderBoardPanel(ctx: AppCtx) {
  const { decideProposal, home, navigateTo, openScheduleDetails, schedules, setBoardOpen } = ctx

  const board = home?.board
  const waiting = board?.waiting ?? []
  const inFlight = board?.running ?? []
  const scheduled = board?.scheduled ?? []
  const scheduledTotal = Number(board?.scheduled_total || 0)

  /** Open a task's detail: the drawer lives on 自动化, so arriving there is part of
   *  opening it. The board knows the task by id; the drawer wants the record. */
  const openTask = (taskId: string) => {
    const task = schedules.find(item => item.id === taskId)
    if (!task) {
      // The list has not loaded (or the task was deleted elsewhere). Landing on the
      // page that owns it is still better than doing nothing.
      navigateTo('schedules')
      return
    }
    // Close the panel first: the task's own drawer is about to open on the same
    // edge, and two of them stacked is a stack of close buttons with no way to tell
    // which one goes back to what.
    setBoardOpen(false)
    openScheduleDetails(task)
    navigateTo('schedules')
  }

  const empty = waiting.length + inFlight.length + scheduledTotal === 0

  if (empty) {
    return (
      <div className="panel-empty">
        <p>现在没有要处理的事。</p>
        <p className="panel-empty-hint">
          它在后台每十分钟看一遍；能自己做的会直接做，需要你点头的会出现在这里。
        </p>
      </div>
    )
  }

  return (
    <div className="panel-body">
      <BoardGroup
        title="等你处理"
        tone="waiting"
        cards={waiting}
        empty="没有卡住的"
        onDecide={(id, state) => void decideProposal(id, state)}
        onOpen={openTask}
      />
      <BoardGroup
        title="正在跑"
        tone="running"
        cards={inFlight}
        empty="没有在跑的"
        onOpen={openTask}
      />
      <BoardGroup
        title="已排期"
        tone="scheduled"
        cards={scheduled}
        empty="没有排期的"
        more={
          scheduledTotal > scheduled.length
            ? `还有 ${scheduledTotal - scheduled.length} 个 · 全部`
            : ''
        }
        onMore={() => navigateTo('schedules')}
        onOpen={openTask}
      />
    </div>
  )
}
