/** Workflow graph model: validation, step reordering and drafting. */

import { WEEKDAY_OPTIONS } from '../constants'
import { formatDateTime } from './format'
import type {
  WorkflowDraft,
  WorkflowGraphCheck,
  WorkflowInfo,
  WorkflowStepDraft,
  WorkflowStepInfo,
  WorkflowStepMovePlan,
  WorkflowTriggerDraft,
} from '../types'
import dayjs from 'dayjs'


/** Where a step's text lives inside its payload, by kind. */
export function stepContentKey(kind: string): string {
  return kind === 'message' ? 'message_text' : 'prompt'
}


export function stepContent(kind: string, payload?: Record<string, any>): string {
  if (!payload) return ''
  const key = stepContentKey(kind)
  if (typeof payload[key] === 'string') return payload[key]
  return ''
}


export function nextStepKey(steps: { key: string }[]): string {
  const taken = new Set(steps.map(item => item.key.trim()))
  for (let index = 1; index < 100; index += 1) {
    const candidate = `step${index}`
    if (!taken.has(candidate)) return candidate
  }
  return `step${Date.now()}`
}


export function defaultWorkflowTrigger(): WorkflowTriggerDraft {
  const at = dayjs().add(1, 'hour').startOf('minute')
  return {
    trigger_type: 'daily',
    at: at.toISOString(),
    every: 1,
    unit: 'hours',
    anchor_at: at.toISOString(),
    time_of_day: '09:00',
    day_of_week: 'monday',
    day_of_month: at.date(),
    signal_name: '',
  }
}


export function defaultWorkflowStep(workspaceRoot = ''): WorkflowStepDraft {
  return {
    key: 'step1',
    name: '',
    kind: 'agent_prompt',
    content: '',
    depends_on: [],
    workspace_root: workspaceRoot,
    payload: {},
    trigger: defaultWorkflowTrigger(),
    triggerFrom: '',
  }
}


export function defaultWorkflowDraft(workspaceRoot = ''): WorkflowDraft {
  return {
    name: '',
    description: '',
    steps: [defaultWorkflowStep(workspaceRoot)],
  }
}


/**
 * A stored workflow, as the editor holds it.
 *
 * The entry step's trigger comes back as `null` on purpose: the editor shows
 * what it is but does not own it, and a body that omits it leaves it where the
 * user put it. Only a step that has no upstreams and no stored trigger yet --
 * a brand new one -- is given a trigger to fill in.
 */
export function workflowDraftFromInfo(info: WorkflowInfo): WorkflowDraft {
  return {
    name: info.name,
    description: info.description || '',
    // Listed in the order they run, not the order the array happens to be in.
    // The stored order is an artefact of how the graph was written and means
    // nothing; the editor is where people read the chain, so it reads it right.
    steps: stepsInRunningOrder(info.steps).map(step => ({
      key: step.key,
      name: step.name || step.key,
      kind: step.kind === 'message' ? 'message' : 'agent_prompt',
      content: stepContent(step.kind, step.payload),
      depends_on: [...(step.depends_on || [])],
      workspace_root: step.workspace_root || '',
      payload: { ...(step.payload || {}) },
      trigger: null,
      triggerFrom: '',
    })),
  }
}


export function workflowDraftStepPayload(step: WorkflowStepDraft): Record<string, any> {
  const key = stepContentKey(step.kind)
  const payload: Record<string, any> = { ...step.payload }
  // The other key is dropped rather than carried: a step that used to be a
  // reminder and is now an Agent task should not still hold its old text.
  delete payload[stepContentKey(step.kind === 'message' ? 'agent_prompt' : 'message')]
  payload[key] = step.content
  return payload
}


/**
 * The same checks the backend makes, so the editor can answer before saving.
 *
 * Kept deliberately identical in what it refuses and how it says it: the
 * message about a cycle names the ring, because "there is a cycle somewhere"
 * leaves the reader to find it. This is a convenience, not the rule -- the
 * server checks again, and it is the server's answer that counts.
 *
 * Takes anything with a key and upstreams rather than a draft, because the
 * running order it computes is also what the stored workflow's step list is
 * sorted by -- the same edges, from the same function, so the two readings of a
 * chain cannot disagree.
 */
export function checkWorkflowGraph(
  steps: { key: string; depends_on: string[] }[],
): WorkflowGraphCheck {
  const problems: string[] = []
  const keys: string[] = []
  const seen = new Map<string, number>()
  steps.forEach((step, index) => {
    const key = step.key.trim()
    if (!key) {
      problems.push(`第 ${index + 1} 个步骤缺少 key`)
      keys.push('')
      return
    }
    if (/\s/.test(key)) {
      problems.push(`步骤 key「${key}」不能包含空格`)
    }
    if (seen.has(key)) {
      problems.push(`步骤 key「${key}」重复了（第 ${seen.get(key)! + 1} 个和第 ${index + 1} 个）`)
    } else {
      seen.set(key, index)
    }
    keys.push(key)
  })

  const known = new Set(keys.filter(Boolean))
  steps.forEach((step, index) => {
    const key = keys[index] || `第 ${index + 1} 个`
    const upstreams = step.depends_on.map(item => item.trim()).filter(Boolean)
    const unique = new Set<string>()
    upstreams.forEach(upstream => {
      if (upstream === step.key.trim()) {
        problems.push(`步骤「${key}」不能依赖自己`)
      } else if (!known.has(upstream)) {
        problems.push(`步骤「${key}」的上游「${upstream}」不存在`)
      }
      if (unique.has(upstream)) {
        problems.push(`步骤「${key}」重复指定了上游「${upstream}」`)
      }
      unique.add(upstream)
    })
  })

  // Longest-path layering, computed from the edges that are valid so far. It
  // doubles as the order: a step whose upstreams have all been placed can be
  // placed, and anything left over is part of a cycle.
  const order: string[] = []
  const pending = new Map(keys.filter(Boolean).map(key => [key, new Set<string>()]))
  steps.forEach((step, index) => {
    const key = keys[index]
    if (!key || !pending.has(key)) return
    step.depends_on.forEach(raw => {
      const upstream = raw.trim()
      if (upstream && upstream !== key && pending.has(upstream)) {
        pending.get(key)!.add(upstream)
      }
    })
  })
  for (;;) {
    const ready = [...pending.entries()]
      .filter(([, waiting]) => waiting.size === 0)
      .map(([key]) => key)
    if (ready.length === 0) break
    ready.forEach(key => {
      order.push(key)
      pending.delete(key)
    })
    pending.forEach(waiting => ready.forEach(key => waiting.delete(key)))
  }
  if (pending.size > 0) {
    problems.push(`循环依赖：${describeWorkflowRing(pending)}`)
  }
  return { order, problems }
}


/** The ring itself, like `a → b → a`, so the reader does not have to hunt. */
export function describeWorkflowRing(pending: Map<string, Set<string>>): string {
  const ring: string[] = []
  let current = [...pending.keys()][0]
  for (let hops = 0; hops <= pending.size + 1; hops += 1) {
    ring.push(current)
    const waiting = pending.get(current)
    const next = waiting ? [...waiting][0] : undefined
    if (!next) break
    const seenAt = ring.indexOf(next)
    if (seenAt >= 0) {
      return [...ring.slice(seenAt), next].join(' → ')
    }
    current = next
  }
  return [...ring, ring[0]].join(' → ')
}


/** A step's key and upstreams, with the blank entries taken out. */
export function cleanStepKeys(keys: string[]): string[] {
  return keys.map(item => item.trim()).filter(Boolean)
}


/**
 * Rows in the order they will run.
 *
 * The array a workflow is stored as makes no promise about order -- the edges
 * do -- so anything listed in array order can show the step that runs last as
 * if it ran second. A graph that does not sort (a cycle, written before the
 * checks existed) keeps the order it came in rather than dropping rows: this is
 * a reading of the chain, not another gate in front of it.
 */
export function stepsInRunningOrder<T extends { key: string; depends_on: string[] }>(
  rows: T[],
): T[] {
  const order = checkWorkflowGraph(rows).order
  if (order.length !== rows.length) return rows
  const byKey = new Map(rows.map(step => [step.key.trim(), step]))
  return order.map(key => byKey.get(key)).filter(Boolean) as T[]
}


/** Whether a stored step is the one the chain starts from. */
export function storedEntryStep(stored: WorkflowStepInfo | undefined): boolean {
  return !!stored && cleanStepKeys(stored.depends_on || []).length === 0
}


/** The steps that wait on *key*, by key. */
export function workflowDownstreamKeys(
  steps: { key: string; depends_on: string[] }[],
  key: string,
): string[] {
  return steps
    .map(step => step.key.trim())
    .filter(item => item && item !== key)
    .filter(item =>
      steps
        .find(step => step.key.trim() === item)!
        .depends_on.some(dep => dep.trim() === key),
    )
}


/**
 * Whether a step can trade places with the one next to it, and with which.
 *
 * Order is what the edges say, so "move it up" is not a list operation: it
 * means exchanging this step with the nearest step it is actually linked to.
 * That has a unique answer only when the step being overtaken waits on nothing
 * else -- with a second upstream, moving past it would drop one of the two
 * edges and leave a step with nothing waiting on it, which is a change nobody
 * asked for. Refusing is the honest answer there, and the upstream field is
 * still there for the case this cannot express.
 */
export function planWorkflowStepMove(
  steps: WorkflowStepDraft[],
  key: string,
  direction: 'earlier' | 'later',
): WorkflowStepMovePlan {
  const step = steps.find(item => item.key.trim() === key)
  if (!step) return { pair: null, problem: '这一步不在流程里' }
  const upstreams = cleanStepKeys(step.depends_on)
  if (direction === 'earlier') {
    if (!upstreams.length) return { pair: null, problem: '它没有上游，已经在最前面' }
    if (upstreams.length > 1) {
      return {
        pair: null,
        problem: `它同时在等「${upstreams.join('」「')}」，换位没有唯一答案；请直接改上游`,
      }
    }
    return { pair: [upstreams[0], key], problem: '' }
  }
  const followers = workflowDownstreamKeys(steps, key)
  if (!followers.length) return { pair: null, problem: '没有步骤在等它，已经在最后面' }
  if (followers.length > 1) {
    return {
      pair: null,
      problem: `有「${followers.join('」「')}」在等它，换位没有唯一答案；请直接改上游`,
    }
  }
  const follower = followers[0]
  const followerUpstreams = cleanStepKeys(
    steps.find(item => item.key.trim() === follower)?.depends_on || [],
  )
  if (followerUpstreams.length > 1) {
    return {
      pair: null,
      problem: `「${follower}」除了它还在等别的步骤，换位没有唯一答案；请直接改上游`,
    }
  }
  return { pair: [key, follower], problem: '' }
}


/**
 * The step that is actually carrying the chain's schedule.
 *
 * A handover is a reference to another step, and more than one can be made in a
 * single edit, so the answer is at the end of the chain of references. Bounded
 * by the number of steps, because a reference left over from a graph that has
 * since changed must not be able to loop.
 */
export function triggerBearingKey(steps: WorkflowStepDraft[], key: string): string {
  const byKey = new Map(steps.map(step => [step.key.trim(), step]))
  let current = key
  for (let hops = 0; hops <= steps.length; hops += 1) {
    const step = byKey.get(current)
    if (!step || step.trigger || !step.triggerFrom) return current
    current = step.triggerFrom
  }
  return current
}


/**
 * Two linked steps exchanged, as edge rewrites.
 *
 * The later step takes the earlier one's place -- it inherits the upstreams the
 * earlier one waited on -- and the earlier one follows it. Everything that
 * waited on the later step now waits on the earlier one, so it stays where it
 * was in the chain. Keys never change, and a step's key is what its task is
 * found by, so a reorder rebuilds nothing and no run history moves.
 *
 * The one thing that has to travel is the schedule: it belongs to whoever runs
 * first, so a swap that changes which step is the entry hands it over instead
 * of leaving a clock ticking on a step in the middle of the chain.
 */
export function swapWorkflowSteps(
  steps: WorkflowStepDraft[],
  earlierKey: string,
  laterKey: string,
): WorkflowStepDraft[] {
  const earlier = steps.find(step => step.key.trim() === earlierKey)
  const later = steps.find(step => step.key.trim() === laterKey)
  if (!earlier || !later) return steps
  // Read before anything is written: both steps are being rewritten.
  const earlierUpstreams = cleanStepKeys(earlier.depends_on)
  const laterFollowers = workflowDownstreamKeys(steps, laterKey)
  const becomesEntry = earlierUpstreams.length === 0
  // A trigger the editor is holding moves across as it is; one that is stored is
  // handed over by name. When the schedule is already sitting on the step that
  // is about to need it -- a swap and its undo -- there is nothing to say, and
  // the body says nothing so the server keeps what that step already has.
  const bearing = triggerBearingKey(steps, earlier.key.trim())
  const handover = !becomesEntry
    ? { trigger: null as WorkflowTriggerDraft | null, triggerFrom: '' }
    : bearing === laterKey
      ? { trigger: null as WorkflowTriggerDraft | null, triggerFrom: '' }
      : earlier.trigger
        ? { trigger: earlier.trigger, triggerFrom: '' }
        : { trigger: null as WorkflowTriggerDraft | null, triggerFrom: bearing }
  const rewired = steps.map(step => {
    const key = step.key.trim()
    if (key === laterKey) {
      return { ...step, depends_on: earlierUpstreams, ...handover }
    }
    if (key === earlierKey) {
      return { ...step, depends_on: [laterKey], trigger: null, triggerFrom: '' }
    }
    if (laterFollowers.includes(key)) {
      return {
        ...step,
        depends_on: step.depends_on.map(item =>
          item.trim() === laterKey ? earlierKey : item,
        ),
      }
    }
    return step
  })
  // The list is reordered to match, so the row the reader just moved moves.
  // Only the array is touched: what runs is decided by the edges above, and the
  // array was never one of the things that decides it.
  const at = (key: string) => rewired.findIndex(step => step.key.trim() === key)
  const [from, to] = [at(earlierKey), at(laterKey)]
  const ordered = [...rewired]
  ;[ordered[from], ordered[to]] = [ordered[to], ordered[from]]
  return ordered
}


/**
 * A chain with a new step spliced into it, right after *afterIndex*.
 *
 * Whatever waited on the step being inserted behind waits on the new one
 * instead. That second half is what makes this an insert rather than a branch
 * off the side: leaving it out is how "in between" quietly becomes "in
 * parallel", which is a different chain and not the one anybody asked for.
 */
export function spliceWorkflowStep(
  steps: WorkflowStepDraft[],
  afterIndex: number,
  newStep: WorkflowStepDraft,
  adoptFollowers: boolean,
): WorkflowStepDraft[] {
  const anchorKey = steps[afterIndex]?.key.trim() || ''
  const followers = anchorKey ? workflowDownstreamKeys(steps, anchorKey) : []
  const repointed = steps.map(step =>
    adoptFollowers && followers.includes(step.key.trim())
      ? {
          ...step,
          depends_on: step.depends_on.map(item =>
            item.trim() === anchorKey ? newStep.key.trim() : item,
          ),
        }
      : step,
  )
  return [
    ...repointed.slice(0, afterIndex + 1),
    newStep,
    ...repointed.slice(afterIndex + 1),
  ]
}


export function workflowRequestBody(draft: WorkflowDraft) {
  return {
    name: draft.name.trim(),
    description: draft.description.trim(),
    steps: draft.steps.map(step => {
      const body: Record<string, any> = {
        key: step.key.trim(),
        name: step.name.trim() || step.key.trim(),
        kind: step.kind,
        payload: workflowDraftStepPayload(step),
        depends_on: step.depends_on.map(item => item.trim()).filter(Boolean),
        workspace_root: step.workspace_root.trim(),
      }
      // Only a step with no upstreams has a schedule, and only a schedule the
      // user just chose is sent field by field. A schedule moving onto this step
      // from another is sent as the name of that step -- a weekly clock the
      // editor can only render as a sentence would not survive being written
      // back out of a form. Everything else is left out so the server keeps
      // whatever is there.
      if (body.depends_on.length === 0 && step.trigger) {
        const trigger = step.trigger
        body.trigger_type = trigger.trigger_type
        body.timezone_name = Intl.DateTimeFormat().resolvedOptions().timeZone
        if (trigger.trigger_type === 'once') body.at = trigger.at
        if (trigger.trigger_type === 'interval') {
          body.every = trigger.every
          body.unit = trigger.unit
          body.anchor_at = trigger.anchor_at
        }
        if (trigger.trigger_type === 'daily' || trigger.trigger_type === 'weekdays') {
          body.time_of_day = trigger.time_of_day
        }
        if (trigger.trigger_type === 'weekly') {
          body.day_of_week = trigger.day_of_week
          body.time_of_day = trigger.time_of_day
        }
        if (trigger.trigger_type === 'monthly') {
          body.day_of_month = trigger.day_of_month
          body.time_of_day = trigger.time_of_day
        }
        if (trigger.trigger_type === 'signal') body.signal_name = trigger.signal_name
      } else if (body.depends_on.length === 0 && step.triggerFrom.trim()) {
        body.trigger_from = step.triggerFrom.trim()
      }
      return body
    }),
  }
}


/**
 * A step's schedule in words, for the one place the editor shows one.
 *
 * Reads the stored trigger rather than reformatting the form: an entry step
 * whose trigger was set elsewhere (a weekly clock, say) is shown as it is
 * instead of being flattened into something the editor could have produced.
 */
export function describeStepTrigger(step: WorkflowStepInfo): string {
  const trigger = step.trigger || {}
  const type = step.trigger_type || 'signal'
  const time = String(trigger.time_of_day || '')
  if (type === 'daily') return time ? `每天 ${time}` : '每天'
  if (type === 'weekdays') return time ? `工作日 ${time}` : '工作日'
  if (type === 'weekly') {
    const day = WEEKDAY_OPTIONS.find(item => item.value === trigger.day_of_week)
    return `${day ? day.label : String(trigger.day_of_week || '每周')} ${time}`.trim()
  }
  if (type === 'monthly') return `每月 ${trigger.day_of_month || ''} 日 ${time}`.trim()
  if (type === 'interval') return `每 ${trigger.every || 1} ${trigger.unit === 'days' ? '天' : trigger.unit === 'weeks' ? '周' : trigger.unit === 'minutes' ? '分钟' : '小时'}`
  if (type === 'once') {
    return trigger.at ? `仅一次：${formatDateTime(String(trigger.at))}` : '仅一次'
  }
  // A step with upstreams has no schedule of its own, and its stored trigger
  // is the subscription that waits for them.
  if (step.depends_on?.length) return '上游步骤完成后'
  return trigger.name ? `信号「${trigger.name}」` : '等待信号'
}


/**
 * A trigger the editor is holding, in the words `describeStepTrigger` uses for
 * a stored one.
 *
 * The two shapes differ in one place -- a signal's name is `signal_name` before
 * it is saved and `name` after -- so the draft is handed over in the stored
 * shape rather than described a second time. Two descriptions of "每天 09:00"
 * would be free to disagree about what the user actually chose.
 */
export function describeTriggerDraft(trigger: WorkflowTriggerDraft): string {
  return describeStepTrigger({
    key: '',
    name: '',
    kind: 'agent_prompt',
    depends_on: [],
    task_id: '',
    trigger_type: trigger.trigger_type,
    trigger: { ...trigger, name: trigger.signal_name },
  })
}


/**
 * The chain's schedule in words, wherever it currently sits.
 *
 * A schedule can be mid-handover -- moved from one step to another earlier in
 * the same edit -- so the step being asked about is not always the step holding
 * it. Following the handovers to the end is what keeps a preview from quoting a
 * schedule the step it names no longer has.
 */
export function describeChainTrigger(
  steps: WorkflowStepDraft[],
  stored: WorkflowStepInfo[] | undefined,
  key: string,
): string {
  const bearing = triggerBearingKey(steps, key)
  const held = steps.find(step => step.key.trim() === bearing)?.trigger
  if (held) return describeTriggerDraft(held)
  const storedStep = stored?.find(item => item.key === bearing)
  return storedStep ? describeStepTrigger(storedStep) : ''
}
