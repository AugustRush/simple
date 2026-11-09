/** The board as a panel you pull out, not a block you scroll past.
 *
 * Why a panel
 * -----------
 * The board used to sit at the top of 主页, above the box: every visit paid for
 * it in vertical space even when nothing needed a person, and a two-line summary
 * of "everything is fine" is the least useful thing a landing page can say. A
 * panel answers the same question on demand without charging the page for it.
 *
 * Why it opens on one sentence
 * ----------------------------
 * The panel's job is to answer "does anything need me" before you have read
 * anything. It used to answer with three groups, each with a heading and a
 * divider -- two of them reporting their own emptiness, which is the most
 * expensive possible way to say "nothing". Now the first line says it once
 * ("没有要你处理的事 · 3 个已排期"), and the groups below exist only when they
 * have something in them. An empty group is not evidence of work; the sentence
 * that says the panel checked is.
 *
 * Why the record is under the queue
 * ---------------------------------
 * The answered proposals and the patrol's own numbers used to be a page of their
 * own (自主), which drew the same proposal rows this panel draws plus a log of
 * what had been decided. A second door into one room is how an app starts
 * feeling bigger than it is: the queue is answered here, so the history of that
 * queue belongs here too -- below it, quieter, and always visible, because "what
 * did I agree to" is asked when nothing is pending.
 *
 * Why two shapes of card
 * ----------------------
 * A proposal is a decision and carries its own buttons; a running or scheduled
 * task is a *pointer* to something the agent owns. Drawing both as identical
 * boxed cards made the panel a list of boxes to scan rather than a list of
 * things to act on and things to ignore. So: a card for what needs an answer,
 * a row for what needs nothing -- the same distinction the three groups make,
 * expressed in shape rather than in a heading the reader has to hold in mind.
 */

import type { AppCtx } from '../app/AppCtx'
import type { BoardCard, WorkingRow } from '../types'
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

const ANSWER_LABEL: Record<string, string> = {
  acted: '已执行',
  accepted: '同意',
  declined: '不要',
}

export const REVERSIBILITY_LABEL: Record<string, string> = {
  reversible: '只读',
  workspace: '改工作区',
  irreversible: '不可逆',
}

/** A proposal: the one card shape that carries a decision. */
function ProposalCard({
  card,
  onDecide,
  onOpenTask,
}: {
  card: BoardCard
  onDecide?: (id: string, state: 'accepted' | 'declined') => void
  onOpenTask?: (taskId: string) => void
}) {
  return (
    <div className="panel-card panel-card-proposal">
      <strong title={card.name}>{card.name}</strong>
      {card.detail && <span className="panel-card-detail">{card.detail}</span>}
      <div className="panel-card-actions">
        <Tag className="panel-card-tag">
          {REVERSIBILITY_LABEL[card.reversibility || ''] || '未知'}
        </Tag>
        {/* Agreement first: it is the answer that keeps the agent moving, and it
            needs no second step. A decline is the one worth confirming -- it is
            what the agent is told never to raise again. */}
        <Button size="small" type="primary" onClick={() => onDecide?.(card.id || '', 'accepted')}>
          同意
        </Button>
        {/* The way to the thing being asked about. Some proposals are a
            question whose answer is an edit on another screen ("decide what to
            do with 汇报"); a card that names the task and cannot open it makes
            the reader hunt for it by name. */}
        {card.target_task_id && (
          <Button size="small" type="link" onClick={() => onOpenTask?.(card.target_task_id || '')}>
            打开任务
          </Button>
        )}
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
  )
}

/** A run that ended badly and is waiting to be read: a card, but not a decision.
 *
 * It shares the "等你处理" group with proposals because both need a person, yet
 * they ask for different things: a proposal asks yes/no, a failed run asks to be
 * looked at. Drawing the second with the first's buttons would have offered a
 * "同意" that answers nothing.
 */
function AttentionCard({ card, onOpen }: { card: BoardCard; onOpen?: (taskId: string) => void }) {
  const status = RUN_STATUS_LABEL[card.status || ''] || card.status || ''
  return (
    <button
      type="button"
      className="panel-card panel-card-attention"
      aria-label={`打开任务 ${card.name}`}
      onClick={() => onOpen?.(card.task_id)}
    >
      <strong title={card.name}>{card.name}</strong>
      <span className="panel-card-sub">
        {status}
        {card.unread > 1 ? ` · ${card.unread} 次未查看` : ''}
        {card.at ? ` · ${relativeTime(card.at)}` : ''}
      </span>
      {card.detail && <span className="panel-card-detail">{card.detail}</span>}
    </button>
  )
}

/** A running or scheduled task: a row, because there is nothing to decide.
 *
 * It still opens the task -- the panel is a way in as well as a status -- but a
 * row says "this exists" without asking to be read like a card does.
 */
function TaskRow({
  card,
  tone,
  onOpen,
}: {
  card: BoardCard
  tone: 'running' | 'scheduled'
  onOpen?: (taskId: string) => void
}) {
  const status = RUN_STATUS_LABEL[card.status || ''] || card.status || ''
  const meta =
    tone === 'scheduled'
      ? TRIGGER_LABEL[card.trigger_type] || '排期中'
      : card.unread > 1
        ? `${status} · ${card.unread} 次未查看`
        : status

  return (
    <button
      type="button"
      className={`panel-row panel-row-${tone}`}
      aria-label={`打开任务 ${card.name}`}
      onClick={() => onOpen?.(card.task_id)}
    >
      <span className="panel-row-dot" aria-hidden="true" />
      <span className="panel-row-main">
        <strong title={card.name}>{card.name}</strong>
        <span>{meta}</span>
      </span>
      <span className="panel-row-when" title={card.at}>
        {relativeTime(card.at)}
      </span>
    </button>
  )
}

/** A goal it is working on across turns: a row with a progress count.
 *
 * Not clickable. A Ralph task has no page; the honest thing a row can offer is
 * the goal and how far it has got. The count is `第 2/8 轮`, not a percentage:
 * iterations are attempts, and an attempt can fail, so a percentage would promise
 * a linearity the loop does not have.
 */
function WorkRow({ row }: { row: WorkingRow }) {
  const progress = row.max_iterations ? `第 ${row.iteration}/${row.max_iterations} 轮` : ''
  return (
    <div className="panel-row panel-row-working">
      <span className="panel-row-dot panel-row-dot-working" aria-hidden="true" />
      <span className="panel-row-main">
        <strong title={row.goal}>{row.goal}</strong>
        <span>{progress || '进行中'}</span>
      </span>
      <span className="panel-row-when">{relativeTime(row.at)}</span>
    </div>
  )
}

/** The panel's contents: one summary line, then only the groups that have cards. */
/** The record under the queue: what was answered, and how much it looked at.
 *
 * Deliberately not cards. Nothing here can be acted on -- one is a history, the
 * other is a number -- and giving history a card's shape is how a panel grows a
 * second list of things that look like they want something.
 */
function PanelRecord({ ctx }: { ctx: AppCtx }) {
  const { answeredProposals, funnel } = ctx
  // Every answered row, not a sample: the count beside the heading says how many
  // there are, and a heading that says 6 above four rows is the two-numbers-
  // disagree bug -- the one this panel already paid for once. History sits below
  // the queue, so length here costs the reader nothing they came for.
  const answered = answeredProposals
  const passes = Number(funnel?.passes || 0)
  const examined = Number(funnel?.examined || 0)
  const executed = Number(funnel?.executed || 0)

  return (
    <section className="panel-group panel-group-record">
      {answered.length > 0 && (
        <>
          <div className="panel-group-head">
            已答复<span className="panel-group-count">{answered.length}</span>
          </div>
          {answered.map(row => {
            // An answer is only as good as what came of it. The row says 已执行
            // when the run started; the one case worth a second word is the run
            // that started and ended badly -- otherwise "approve and never hear
            // about it again" is the same leap of faith the page was built to end.
            const outcome = row.outcome?.status || ''
            const outcomeBad = outcome === 'failed' || outcome === 'interrupted'
            return (
              <div className="panel-log-row" key={row.id}>
                <span className={`panel-log-state is-${row.state}`}>
                  {ANSWER_LABEL[row.state] || row.state}
                </span>
                <span className="panel-log-action" title={row.action}>{row.action}</span>
                {outcomeBad && <span className="panel-log-outcome">失败</span>}
                <span className="panel-log-at">{relativeTime(row.decided_at || row.created_at)}</span>
              </div>
            )
          })}
        </>
      )}
      {/* The evidence, not a boast: "nothing waiting" is only worth trusting if
          the panel can show that the thing which produces proposals has been
          running. It says 趟 because that is what a patrol is -- the count is of
          passes over the conversations, not of things it thought of. */}
      {passes > 0 && (
        <p className="panel-foot">
          近 7 天巡逻 <strong>{passes}</strong> 趟 · 看过 <strong>{examined}</strong> 次会话 · 自己做了{' '}
          <strong>{executed}</strong> 件
        </p>
      )}
      <p className="panel-rule">只读的动作它自己做；会改动工作区之外的动作，永远不会自己做。</p>
    </section>
  )
}

/** The panel's contents: one summary line, then only the groups that have cards. */
export function renderBoardPanel(ctx: AppCtx) {
  const { decideProposal, home, navigateTo, openScheduleDetails, schedules, setBoardOpen } = ctx

  const board = home?.board
  const waiting = board?.waiting ?? []
  const inFlight = board?.running ?? []
  const scheduled = board?.scheduled ?? []
  const working = board?.working ?? []
  const promised = board?.commitments ?? []
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
    // edge, and two of them stacked is a stack of close buttons with no way to
    // tell which one goes back to what.
    setBoardOpen(false)
    openScheduleDetails(task)
    navigateTo('schedules')
  }

  const nothingToDo =
    waiting.length + inFlight.length + working.length + promised.length + scheduledTotal === 0

  // The panel's first line, and the only sentence most visits need.
  const rest = [
    // Promises first among the counts that are not "action needed": an owing
    // promise is a claim on the person's plan, and it is the one number here
    // that is about *them* rather than about the machinery.
    promised.length ? `${promised.length} 件答应过你` : '',
    working.length ? `${working.length} 件在做` : '',
    inFlight.length ? `${inFlight.length} 个在跑` : '',
    scheduledTotal ? `${scheduledTotal} 个已排期` : '',
  ].filter(Boolean)

  return (
    <div className="panel-body">
      {nothingToDo ? (
        <div className="panel-empty">
          <p>现在没有要处理的事。</p>
          <p className="panel-empty-hint">
            它在后台每十分钟看一遍；能自己做的会直接做，需要你点头的会出现在这里。
          </p>
        </div>
      ) : (
        <>
          <div className="panel-summary">
            <span className={`panel-summary-state${waiting.length ? ' is-waiting' : ''}`}>
              {waiting.length ? `${waiting.length} 件等你处理` : '没有要你处理的事'}
            </span>
            {rest.length > 0 && <span className="panel-summary-rest">{rest.join(' · ')}</span>}
          </div>

          {waiting.length > 0 && (
            <section className="panel-group panel-group-waiting">
              <div className="panel-group-head">
                等你处理<span className="panel-group-count">{waiting.length}</span>
              </div>
              {waiting.map(card =>
                card.kind === 'proposal' ? (
                  <ProposalCard
                    key={`proposal:${card.id}`}
                    card={card}
                    onDecide={(id, state) => void decideProposal(id, state)}
                    onOpenTask={openTask}
                  />
                ) : (
                  <AttentionCard key={card.task_id} card={card} onOpen={openTask} />
                ),
              )}
            </section>
          )}

          {promised.length > 0 && (
            <section className="panel-group panel-group-promised">
              <div className="panel-group-head">
                它答应你的事<span className="panel-group-count">{promised.length}</span>
              </div>
              {promised.map(row => (
                <div
                  className={`panel-row panel-row-promised${row.overdue ? ' is-overdue' : ''}`}
                  key={row.task_id}
                  // No click: a promise has no page. The row is the whole
                  // message -- what it said it would do, and when.
                  title={row.said || undefined}
                >
                  <span className="panel-row-dot panel-row-dot-promised" aria-hidden="true" />
                  <span className="panel-row-main">
                    <strong>{row.goal}</strong>
                    {/* `due_text` already reads as a phrase ("40 分钟后" / "已经晚了 5 分钟");
                        prefixing it with 还有 produced "还有 40 分钟后". */}
                    <span>{row.due_text}</span>
                  </span>
                </div>
              ))}
            </section>
          )}

          {working.length > 0 && (
            <section className="panel-group panel-group-working">
              <div className="panel-group-head">
                正在做<span className="panel-group-count">{working.length}</span>
              </div>
              {working.map(row => (
                <WorkRow key={row.id} row={row} />
              ))}
            </section>
          )}

          {inFlight.length > 0 && (
            <section className="panel-group panel-group-running">
              <div className="panel-group-head">
                正在跑<span className="panel-group-count">{inFlight.length}</span>
              </div>
              {inFlight.map(card => (
                <TaskRow key={card.task_id} card={card} tone="running" onOpen={openTask} />
              ))}
            </section>
          )}

          {scheduled.length > 0 && (
            <section className="panel-group panel-group-scheduled">
              <div className="panel-group-head">
                已排期<span className="panel-group-count">{scheduledTotal}</span>
              </div>
              {scheduled.map(card => (
                <TaskRow key={card.task_id} card={card} tone="scheduled" onOpen={openTask} />
              ))}
              {scheduledTotal > scheduled.length && (
                <Button type="link" size="small" className="panel-group-more" onClick={() => navigateTo('schedules')}>
                  还有 {scheduledTotal - scheduled.length} 个 · 全部
                </Button>
              )}
            </section>
          )}
        </>
      )}

      <PanelRecord ctx={ctx} />
    </div>
  )
}
