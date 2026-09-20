/** Scheduled-task drafting, plus human-readable trigger and run descriptions. */

import { WEEKDAY_OPTIONS } from '../constants'
import { formatDateTime } from './format'
import type {
  AcceptanceInfo,
  PermissionProfileKey,
  ScheduleDraft,
  ScheduleInfo,
  ScheduleRun,
} from '../types'
import {
  CheckCircleFilled,
  ClockCircleOutlined,
  ExclamationCircleFilled,
  LoadingOutlined,
  QuestionCircleFilled,
} from '@ant-design/icons'
import dayjs from 'dayjs'


export function defaultScheduleDraft(workspaceRoot = ''): ScheduleDraft {
  const start = dayjs().add(1, 'hour').startOf('minute')
  const weekday = WEEKDAY_OPTIONS[(start.day() + 6) % 7].value
  return {
    name: '',
    action_type: 'agent_task',
    trigger_type: 'once',
    at: start.toISOString(),
    every: 1,
    unit: 'hours',
    anchor_at: start.toISOString(),
    time_of_day: start.format('HH:mm'),
    day_of_week: weekday,
    day_of_month: start.date(),
    signal_name: '',
    prompt: '',
    message_text: '',
    workspace_root: workspaceRoot,
    context_policy: 'stateless',
    model_override: '',
    timeout_seconds: 1800,
    max_attempts: 1,
    backoff_seconds: 30,
    selected_skills: [],
    permission_profile: 'inherit',
    delivery_mode: 'standalone',
    delivery_chat_id: '',
    delivery_chat_name: '',
  }
}


export function scheduleDraftFromTask(task: ScheduleInfo): ScheduleDraft {
  const fallback = defaultScheduleDraft(task.workspace_root || '')
  const trigger = task.trigger || {}
  return {
    ...fallback,
    name: task.name,
    action_type: task.kind === 'message' ? 'message' : 'agent_task',
    trigger_type: (task.trigger_type || 'once') as ScheduleDraft['trigger_type'],
    at: String(trigger.at || fallback.at),
    every: Number(trigger.every || 1),
    unit: (trigger.unit || 'hours') as ScheduleDraft['unit'],
    anchor_at: String(trigger.anchor_at || fallback.anchor_at),
    time_of_day: String(trigger.time_of_day || fallback.time_of_day),
    day_of_week: String(trigger.day_of_week || fallback.day_of_week),
    day_of_month: Number(trigger.day_of_month || fallback.day_of_month),
    signal_name: String(trigger.name || ''),
    prompt: String(task.payload?.prompt || ''),
    message_text: String(task.payload?.message_text || ''),
    workspace_root: task.workspace_root || '',
    context_policy: task.context_policy || 'stateless',
    model_override: task.model_override || '',
    timeout_seconds: Number(task.timeout_seconds || 1800),
    max_attempts: Number(task.retry_policy?.max_attempts || 1),
    backoff_seconds: Number(task.retry_policy?.backoff_seconds ?? 30),
    selected_skills: task.selected_skills || [],
    permission_profile: (task.permission_profile || 'inherit') as PermissionProfileKey,
    delivery_mode: task.delivery_mode === 'channel' ? 'channel' : 'standalone',
    delivery_chat_id: task.delivery_mode === 'channel'
      ? String(task.delivery_target?.payload?.chat_id || '')
      : '',
    delivery_chat_name: '',
  }
}


export function scheduleRequestBody(draft: ScheduleDraft) {
  const {
    max_attempts,
    backoff_seconds,
    delivery_chat_id: chatId,
    delivery_chat_name: _chatName,
    ...rest
  } = draft
  return {
    ...rest,
    // A standalone delivery sends no target at all: the server answers
    // "standalone" with a standalone target, and sending one anyway would
    // only be a second opinion about nothing.
    ...(draft.delivery_mode === 'channel'
      ? {
          delivery_target: {
            target_type: 'feishu_chat',
            payload: { chat_id: chatId, chat_type: 'group' },
          },
        }
      : {}),
    retry_policy: { max_attempts, backoff_seconds },
    timezone_name: Intl.DateTimeFormat().resolvedOptions().timeZone,
  }
}


export function scheduleTimeValue(value: string) {
  const [hour, minute] = String(value || '').split(':').map(Number)
  if (!Number.isInteger(hour) || !Number.isInteger(minute)) return null
  return dayjs().hour(hour).minute(minute).second(0).millisecond(0)
}


/**
 * Turn a signal name into something a person recognizes.
 *
 * A task's own signal is `task:<id>:<status>` — exact and rename-proof, but
 * not something to show anyone. The id is resolved against the tasks already
 * on the page, so this stays a pure function of data the caller has rather
 * than reaching for a second lookup.
 */
export function describeSignalName(name: string, tasks: ScheduleInfo[] = []): string {
  const match = /^task:([^:]+):(.+)$/.exec(name || '')
  // A free-form name is quoted too, so it reads as a name rather than as a
  // word that happens to sit between two Chinese characters.
  if (!match) return name ? `「${name}」` : name
  const [, taskId, status] = match
  const taskName = tasks.find(item => item.id === taskId)?.name || taskId
  return `「${taskName}」${scheduleRunStatusLabel(status)}`
}


export function scheduleTriggerLabel(task: ScheduleInfo, tasks: ScheduleInfo[] = []): string {
  const trigger = task.trigger || {}
  if (task.trigger_type === 'once') {
    return trigger.at ? `一次 · ${formatDateTime(trigger.at)}` : '一次性执行'
  }
  if (task.trigger_type === 'interval') {
    const units: Record<string, string> = { minutes: '分钟', hours: '小时', days: '天', weeks: '周' }
    return `每 ${trigger.every || 1} ${units[trigger.unit] || trigger.unit || ''}`
  }
  if (task.trigger_type === 'daily') return `每天 ${trigger.time_of_day || ''}`
  if (task.trigger_type === 'weekdays') return `工作日 ${trigger.time_of_day || ''}`
  if (task.trigger_type === 'monthly') return `每月 ${trigger.day_of_month || 1} 日 ${trigger.time_of_day || ''}`
  if (task.trigger_type === 'weekly') {
    const weekday = WEEKDAY_OPTIONS.find(item => item.value === trigger.day_of_week)?.label || trigger.day_of_week || ''
    return `每${weekday} ${trigger.time_of_day || ''}`
  }
  if (task.trigger_type === 'signal') {
    const name = String(trigger.name || '')
    return name ? `当${describeSignalName(name, tasks)}发生时` : '等待信号'
  }
  return '未设置计划'
}


/** What a run's status reads as.
 *
 * `queued` needs the run as well as the status, because it means two different
 * things: a run woken by a signal is queued until a later scheduler tick claims
 * it, and a retry is queued until its backoff has elapsed. Both are "not
 * running yet"; only one of them is waiting to try again, and calling the first
 * one a retry is a lie about what the run is doing.
 */
export function scheduleRunStatusLabel(status?: string, run?: ScheduleRun | null): string {
  if (status === 'running') return '执行中'
  if (status === 'queued') return run?.retry_of_run_id ? '等待重试' : '排队等待'
  if (status === 'succeeded') return '执行成功'
  if (status === 'failed') return '执行失败'
  if (status === 'interrupted') return '已中断'
  if (status === 'cancelled') return '已取消'
  // Not 「执行失败」: the run happened, and what it produced was delivered --
  // what nobody can say is whether that was the right thing, because the
  // acceptance check could not be evaluated. Calling it a failure would
  // assert something nobody observed, and calling it a success would be
  // worse.
  if (status === 'unverified') return '结果未能判定'
  // Not "已取消": nobody decided this. A step above it failed, and saying so
  // is the difference between a chain that stopped working and a chain that
  // somebody turned off.
  if (status === 'skipped') return '已跳过'
  return '等待首次执行'
}


/** Why this run started, in the terms that will make sense to the reader.
 *
 * "按计划运行" is the safe default and the wrong answer for a run that a
 * signal woke, because the whole point of a signal is that it is not the
 * clock. Saying which one fired is what lets a chain of runs be read as a
 * chain instead of as unrelated activity.
 */
export function describeRunTrigger(run: ScheduleRun, tasks: ScheduleInfo[] = []): string {
  const source = run.trigger_source || 'schedule'
  if (source === 'manual') return '手动运行'
  if (source.startsWith('retry') || source === 'automatic_retry') {
    return `重试 · 第 ${run.attempt || 1} 次`
  }
  if (source.startsWith('signal:')) {
    const name = source.slice('signal:'.length)
    return `由信号触发 · ${describeSignalName(name, tasks)}`
  }
  // A skipped step carries `workflow:<step>` rather than the signal that
  // blocked it, because the thing worth naming there is the step.
  if (source.startsWith('workflow:')) {
    return `上游步骤「${source.slice('workflow:'.length)}」未成功`
  }
  return '按计划运行'
}


/** How a run's cascade context reads, or null when there is none. */
export function describeCascade(run: ScheduleRun): string | null {
  const signal = run.config_snapshot?.signal
  if (!signal || typeof signal !== 'object') return null
  const depth = Number(signal.depth || 0)
  // Depth zero means the signal was raised by a person, a clock or the agent
  // itself rather than by another run, so there is no chain to describe.
  if (depth <= 0) return null
  return `信号链第 ${depth + 1} 层`
}


/** How far a moment is from now, in the units a person would say it in.
 *
 * Returns null for a value that will not parse, so each caller can fall back
 * to the absolute timestamp it already has rather than inventing a distance.
 */
export function describeSpan(iso: string, now: number): { overdue: boolean; text: string } | null {
  const at = Date.parse(iso)
  if (Number.isNaN(at)) return null
  const delta = at - now
  const total = Math.round(Math.abs(delta) / 1000)
  const overdue = delta < 0
  if (total < 60) return { overdue, text: `${total} 秒` }
  // Round once, to minutes, and decompose from that. Rounding the remainder
  // separately is how 6h59m59s becomes "6 小时 60 分" -- a reading that is both
  // wrong and impossible, and one that shows up for a full minute out of every
  // hour on a seven-hour wait.
  const minutes = Math.round(total / 60)
  if (minutes < 60) return { overdue, text: `${minutes} 分钟` }
  if (total < 86400) {
    const hours = Math.floor(minutes / 60)
    const rest = minutes % 60
    return { overdue, text: rest > 0 ? `${hours} 小时 ${rest} 分` : `${hours} 小时` }
  }
  return { overdue, text: `${Math.floor(total / 86400)} 天` }
}


/** How long until a moment.
 *
 * The absolute time is not thrown away -- it is the tooltip and the detail
 * drawer. It is just not the primary reading, because "2026-09-16 15:25" makes
 * the reader do arithmetic against a clock they are not looking at, and the
 * question actually being asked of a scheduler is "when, from now".
 *
 * Past due is its own wording, not a negative countdown: it means the
 * scheduler is behind, which is a different thing to report than "nearly".
 */
export function describeCountdown(iso: string, now: number): string {
  const span = describeSpan(iso, now)
  if (!span) return formatDateTime(iso)
  return span.overdue ? `已逾期 ${span.text}` : `${span.text}后`
}


/** How long ago a moment was.
 *
 * Separate from :func:`describeCountdown` because a run that happened an hour
 * ago is not an hour overdue. Sharing the wording would turn every task's
 * normal history into something that reads like a warning.
 */
export function describeElapsed(iso: string, now: number): string {
  const span = describeSpan(iso, now)
  if (!span) return formatDateTime(iso)
  return span.overdue ? `${span.text}前` : `${span.text}后`
}


/** How a task's next run reads, or the honest alternative when there is none.
 *
 * "暂无后续执行" is right for a task whose schedule has run out and wrong for
 * one that is waiting on a signal: the second will run, and saying otherwise
 * invites deleting a task that is working exactly as asked.
 */
export function describeNextRun(task: ScheduleInfo, now: number = Date.now()): string {
  if (task.next_run_at) return describeCountdown(task.next_run_at, now)
  if (task.trigger_type === 'signal') return '等待信号触发'
  return '暂无后续执行'
}


/** How current the page's data is, in the words a person would use.
 *
 * The point of saying it at all is to tell "nothing has changed" apart from "I
 * can no longer reach the server". On a page whose numbers only move when
 * something happens, those two look exactly alike.
 */
export function describeFreshness(
  stale: boolean,
  refreshedAt: number | null,
  now: number,
): string {
  if (stale) return '状态未更新 · 重试中'
  if (!refreshedAt) return '正在获取状态'
  const seconds = Math.max(0, Math.round((now - refreshedAt) / 1000))
  if (seconds < 3) return '状态已同步'
  if (seconds < 60) return `${seconds} 秒前同步`
  return `${Math.round(seconds / 60)} 分钟前同步`
}


/** Milliseconds until the soonest scheduled run, or null if none is set.
 *
 * Only tasks that are switched on and carry a time count. A signal-triggered
 * step has no clock to predict, which is the reason the idle cadence has to
 * exist rather than the page going quiet when nothing is imminent.
 */
export function msUntilNextRun(tasks: ScheduleInfo[], now: number = Date.now()): number | null {
  let soonest: number | null = null
  for (const task of tasks) {
    if (!task.enabled || !task.next_run_at) continue
    const at = Date.parse(task.next_run_at)
    if (Number.isNaN(at)) continue
    const delta = at - now
    if (soonest === null || delta < soonest) soonest = delta
  }
  return soonest
}


export function scheduleRunStatusIcon(status?: string) {
  if (status === 'running') return <LoadingOutlined spin />
  if (status === 'queued') return <ClockCircleOutlined />
  if (status === 'succeeded') return <CheckCircleFilled />
  if (status === 'unverified') return <QuestionCircleFilled />
  if (status === 'failed' || status === 'interrupted' || status === 'cancelled') {
    return <ExclamationCircleFilled />
  }
  return <ClockCircleOutlined />
}


/** Why this run is asking for attention, in the run's own words.
 *
 * There are two reasons and they are not the same thing: the run ended badly,
 * or it is the first run after a stretch in which nothing was running and
 * some occurrences never fired.  The second case succeeds, so saying "失败"
 * about it would be false -- and the point of the marker is that a person can
 * tell what happened without opening anything.
 */
export function scheduleRunAttentionReason(run: { status?: string; missed_count?: number }): string {
  const reasons: string[] = []
  const missed = run.missed_count || 0
  if (missed > 0) reasons.push(`本次运行前有 ${missed} 次计划未能执行`)
  if (run.status === 'failed' || run.status === 'interrupted') {
    reasons.push(`本次运行${scheduleRunStatusLabel(run.status)}`)
  } else if (run.status === 'unverified') {
    // Its own sentence rather than a status label, because what needs looking
    // at is not the run -- that finished fine -- but the criterion that could
    // not be evaluated.
    reasons.push('本次运行的结果未能判定是否达标')
  }
  return reasons.length ? `${reasons.join('；')}，你还没有查看` : '你还没有查看'
}


/** What the run's result was worth, as opposed to whether it ran.
 *
 * `status` answers "did this happen"; this answers "did it do the job". They
 * are different questions and a run can get either one wrong on its own, so
 * the two are shown side by side rather than folded into one word. Returns
 * null when there is nothing to say -- a task with no criterion and no
 * self-report has no verdict, and inventing one would be the collapse this
 * exists to undo.
 */
export function scheduleRunVerdictLabel(run: ScheduleRun): string | null {
  if (run.verdict === 'failed') return '未达验收标准'
  if (run.verdict === 'passed') return '已达验收标准'
  if (run.verdict === 'unknown') return '验收无法判定'
  return null
}


/** Why the verdict came out that way, in the check's own words, or null. */
export function scheduleRunVerificationDetail(run: ScheduleRun): string | null {
  const verification = run.verification
  if (!verification) return null
  if (verification.status === 'passed') return null
  if (verification.status === 'failed') {
    const detail = String(verification.stderr_tail || verification.stdout_tail || '').trim()
    const head = `退出码 ${verification.exit_code}`
    return detail ? `${head}：${detail}` : head
  }
  // The remaining statuses are statements about the *check*, not the work:
  // it was refused, timed out, was cancelled, or could not be set up. Saying
  // so is what keeps "we could not tell" from reading as "it was wrong".
  return String(verification.error || '').trim() || `验收状态：${verification.status}`
}


/** The criterion a task is judged by, as one line, or null when there is none. */
export function describeAcceptance(acceptance?: AcceptanceInfo): string | null {
  if (!acceptance) return null
  const criteria = (acceptance.criteria || []).filter((item) => String(item).trim())
  const command = String(acceptance.verify_command || '').trim()
  if (!criteria.length && !command) return null
  const parts = [...criteria.map((item) => String(item))]
  if (command) parts.push(`验收命令：${command}`)
  return parts.join('；')
}


export function scheduleDeliveryStatusLabel(status?: string): string {
  if (status === 'stored') return '结果已保存'
  if (status === 'delivered') return '结果已发送'
  if (status === 'skipped') return '无文本输出'
  if (status === 'failed') return '结果交付失败'
  return status || '—'
}
