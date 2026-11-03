import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { KNOWN_PERMISSION_PROFILES, SCHEDULE_OVERDUE_GRACE_MS, SCHEDULE_WATCH_WINDOW_MS } from '../constants'
import { msUntilNextRun } from '../lib/schedule'
import { stepContent } from '../lib/workflow'
import type { AttentionRun, AutomationRow, AutonomyFunnel, HomeActivity, HomeOverview, ProposalRow, PermissionProfileOption, ScheduleArtifact, ScheduleInfo, ScheduleRun, ScheduleRunOutput, SchedulerHealth, WorkflowInfo } from '../types'
import { useUi } from './useUi'
import { useApiClient } from './useApiClient'
import { useConfirm } from './useConfirm'
import { useConversations } from './useConversations'
import { useSettings } from './useSettings'

type Deps = ReturnType<typeof useUi> & ReturnType<typeof useApiClient> & ReturnType<typeof useConfirm> & ReturnType<typeof useConversations> & ReturnType<typeof useSettings>

export function useAutomation(deps: Deps) {
  const { api, confirmResourceDeletion, messageApi, refreshJson, setLoadingView, view } = deps

  const [schedules, setSchedules] = useState<ScheduleInfo[]>([])
  // Workflows whose "run now" request is in flight. The ref is the guard (a
  // double click lands before the state re-renders); the state drives the
  // button's spinner.
  const startingWorkflowRef = useRef<Set<string>>(new Set())
  const [startingWorkflowIds, setStartingWorkflowIds] = useState<string[]>([])



  const [unseenFailures, setUnseenFailures] = useState(0)

  // The runs behind `unseenFailures`, from the same response. Kept as a pair
  // because the badge and its list used to come from different places, which
  // is how a number ended up on the navigation with nothing on the page that
  // added up to it.
  const [attentionRuns, setAttentionRuns] = useState<AttentionRun[]>([])

  // The run to open per task, from the same payload. The list is capped, so
  // deriving this from its rows would leave a card whose count is showing
  // with no run to click through to -- and falling back to the newest run is
  // landing on the one that is usually fine, which is what the count was
  // complaining about.
  const [attentionLatestByTask, setAttentionLatestByTask] = useState<Record<string, string>>({})

  const [permissionProfiles, setPermissionProfiles] = useState<PermissionProfileOption[]>([])

  const [scheduleQuery, setScheduleQuery] = useState('')

  const [scheduleStatusFilter, setScheduleStatusFilter] = useState('all')

  const [selectedScheduleIds, setSelectedScheduleIds] = useState<string[]>([])







  const [schedulerHealth, setSchedulerHealth] = useState<SchedulerHealth>({ status: 'offline' })

  //: Whether the last background refresh reached the server, and when one last
  //: did. Kept as state rather than inferred from the data, because data that
  //: stopped arriving and data that stopped changing are indistinguishable.
  const [schedulerStale, setSchedulerStale] = useState(false)

  const [schedulerRefreshedAt, setSchedulerRefreshedAt] = useState<number | null>(null)

  const [scheduleDetailOpen, setScheduleDetailOpen] = useState(false)

  const [selectedSchedule, setSelectedSchedule] = useState<ScheduleInfo | null>(null)

  const [scheduleRuns, setScheduleRuns] = useState<ScheduleRun[]>([])

  const [selectedScheduleRunId, setSelectedScheduleRunId] = useState<string | null>(null)

  const [scheduleRunsLoading, setScheduleRunsLoading] = useState(false)

  const [scheduleRunOutput, setScheduleRunOutput] = useState<ScheduleRunOutput | null>(null)

  const [scheduleArtifacts, setScheduleArtifacts] = useState<ScheduleArtifact[]>([])

  const [scheduleOutputLoading, setScheduleOutputLoading] = useState(false)

  const [workflows, setWorkflows] = useState<WorkflowInfo[]>([])

  /**
   * Whether the workflow list on screen is the server's answer rather than its
   * initial value.  A task that names a workflow it cannot find in the list is
   * either a step of a deleted workflow or a step of one that has not arrived
   * yet, and those two want opposite things: the first is an ordinary task now
   * and can be deleted, the second must not be offered a deletion the server
   * will refuse.  An empty list is only evidence once it is loaded evidence.
   */
  const [workflowsLoaded, setWorkflowsLoaded] = useState(false)

  // Two views, not three: what needs a person, and everything they manage. The
  // "task or workflow" split was a choice offered on arrival; it is a filter now.
  const [automationTab, setAutomationTab] = useState<'automation' | 'attention'>('automation')
  // The filter inside the list. `all` is the landing view: one row per thing a
  // person manages, a step never listed beside its chain (see
  // agent/scheduler/automations.py for the invariant and why it matters).
  const [automationKind, setAutomationKind] = useState<'all' | 'task' | 'workflow'>('all')
  const [automations, setAutomations] = useState<AutomationRow[]>([])
  //: Whether this page has ever read the unified list. Without it, an empty
  //: list and an unread list look the same -- which is what the page showed for
  //: the first ten seconds after opening it: "还没有自动化" over data that existed.
  const [automationsLoaded, setAutomationsLoaded] = useState(false)
  const [automationQuery, setAutomationQuery] = useState('')




  const [editingWorkflowId, setEditingWorkflowId] = useState<string | null>(null)


  const [workflowQuery, setWorkflowQuery] = useState('')

  /**
   * The one writer for the badge and the list behind it.
   *
   * Both come off the same response, which is the whole point: the count and
   * the rows are read from one payload, so they cannot disagree. Every other
   * place that used to set the count from its own response now refreshes
   * through here instead -- two writers to one number is how it drifted.
   */
  const applyAttention = useCallback((data: any) => {
    setUnseenFailures(Number(data?.unseen_attention || 0))
    setAttentionRuns(
      Array.isArray(data?.attention_runs)
        ? (data.attention_runs as AttentionRun[])
        : [],
    )
    setAttentionLatestByTask(
      data && typeof data.latest_run_by_task === 'object' && data.latest_run_by_task
        ? data.latest_run_by_task as Record<string, string>
        : {},
    )
  }, [])

  const applySchedules = useCallback((data: any) => {
    setSchedules(data.tasks || [])
    setPermissionProfiles(
      Array.isArray(data.permission_profiles) ? data.permission_profiles : [],
    )
    applyAttention(data)
    setSelectedSchedule(current => {
      if (!current) return current
      const refreshed = (data.tasks || []).find(
        (item: ScheduleInfo) => item.id === current.id,
      )
      return refreshed || current
    })
  }, [applyAttention])

  // `silent` means "this refresh was not asked for": the caller is the timer,
  // so there is no spinner to show and no toast worth raising. It is also the
  // path that has to report failure through state instead, because a silent
  // failure would otherwise be invisible -- the page would go on showing the
  // last numbers it managed to fetch and look exactly like a quiet afternoon.
  const loadSchedules = useCallback(async (silent = false) => {
    if (!silent) setLoadingView(true)
    try {
      const data = silent
        ? await refreshJson('/api/schedules')
        : await (await api('/api/schedules')).json()
      if (data === null) {
        setSchedulerStale(true)
        return
      }
      applySchedules(data)
      setSchedulerStale(false)
      setSchedulerRefreshedAt(Date.now())
    } catch {
      // The shared helper has already said what went wrong. What matters here
      // is that what is on screen is now older than it looks.
      setSchedulerStale(true)
    } finally {
      if (!silent) setLoadingView(false)
    }
  }, [api, applySchedules, refreshJson])

  /**
   * The list a person lands on: every automation in one shape.
   *
   * Its own endpoint (`GET /api/automations`) rather than two client-side merges,
   * because the merge is a *rule* -- a step is never its own row, a chain's
   * attention is its steps' attention -- and a rule kept in two places is the
   * kind that drifts. The response also carries the badge, so the count and the
   * rows are one read (the same contract `/api/schedules` makes).
   */
  const loadAutomations = useCallback(async (silent = false) => {
    if (!silent) setLoadingView(true)
    try {
      const data = silent
        ? await refreshJson('/api/automations')
        : await (await api('/api/automations')).json()
      if (data === null) {
        setSchedulerStale(true)
        return
      }
      setAutomations(Array.isArray(data.automations) ? data.automations : [])
      setAutomationsLoaded(true)
      if (Array.isArray(data.permission_profiles) && data.permission_profiles.length) {
        setPermissionProfiles(data.permission_profiles)
      }
      applyAttention(data)
      setSchedulerStale(false)
      setSchedulerRefreshedAt(Date.now())
    } catch {
      // Same rule as the other lists: an unreachable list must not read as "you
      // have nothing", so the last known one stands and is marked stale.
      setSchedulerStale(true)
    } finally {
      if (!silent) setLoadingView(false)
    }
  }, [api, applyAttention, refreshJson])

  const loadWorkflows = useCallback(async (silent = false) => {
    if (!silent) setLoadingView(true)
    try {
      const data = silent
        ? await refreshJson('/api/workflows')
        : await (await api('/api/workflows')).json()
      if (data === null) {
        setSchedulerStale(true)
        return
      }
      setWorkflows(Array.isArray(data.workflows) ? data.workflows : [])
      setWorkflowsLoaded(true)
      if (Array.isArray(data.permission_profiles) && data.permission_profiles.length) {
        setPermissionProfiles(data.permission_profiles)
      }
      setSchedulerStale(false)
      setSchedulerRefreshedAt(Date.now())
    } catch {
      // An unreachable list must not read as "you have no workflows", so the
      // last known one is left standing -- and marked as no longer trustworthy.
      setSchedulerStale(true)
    } finally {
      if (!silent) setLoadingView(false)
    }
  }, [api, refreshJson])

  // ── the home page ──────────────────────────────────────────────────────
  //
  // One request, because the page's question ("is anything waiting for me") must
  // have one answer. Computing it here from five lists would be a second
  // implementation of what the server already decides -- and the two would drift,
  // starting with the failure count that the sidebar badge also shows.
  const [home, setHome] = useState<HomeOverview | null>(null)
  const [homeLoaded, setHomeLoaded] = useState(false)

  // Older pages of the timeline, kept beside `home` rather than inside it: the
  // first page comes from `/api/home` and the rest are appended here, so a refresh
  // of the landing view starts from the top again instead of re-playing every
  // "加载更早" the reader ever clicked.
  const [olderActivity, setOlderActivity] = useState<HomeActivity[]>([])
  const [activityCursor, setActivityCursor] = useState('')
  const [activityLoading, setActivityLoading] = useState(false)
  //: Set once a page came back short. Without it the button stays forever: the
  //: first page always carries a cursor (it is full), so "there is a cursor" and
  //: "there is more" stop being the same statement after the first click.
  const [activityExhausted, setActivityExhausted] = useState(false)

  const loadHome = useCallback(async () => {
    try {
      const data = await (await api('/api/home')).json()
      if (data && typeof data === 'object') setHome(data as HomeOverview)
      setOlderActivity([])
      setActivityCursor('')
      setActivityExhausted(false)
      setHomeLoaded(true)
    } catch {
      // A failed read leaves the last known state on screen: an empty home page
      // would read as "nothing is happening", which is a claim we cannot make.
    }
  }, [api])

  const loadMoreActivity = useCallback(async () => {
    if (activityLoading) return
    const before = activityCursor || home?.activity_next || ''
    if (!before) return
    setActivityLoading(true)
    try {
      const data = await (
        await api(`/api/activity?before=${encodeURIComponent(before)}&limit=20`)
      ).json()
      const page: HomeActivity[] = Array.isArray(data.activity) ? data.activity : []
      if (!page.length) {
        setActivityCursor('')
        setActivityExhausted(true)
      } else {
        setOlderActivity(prev => [...prev, ...page])
        setActivityCursor(String(data.next_before || ''))
        // A short page is the last page; a full one may have more behind it.
        if (!data.next_before) setActivityExhausted(true)
      }
    } catch {
      // Keep the button: the reader can try again.
    } finally {
      setActivityLoading(false)
    }
  }, [activityCursor, activityLoading, api, home])

  // ── the autonomy page ──────────────────────────────────────────────────
  //
  // Separate state from the automation page's: proposals are a review queue, not
  // a list of things that run, and the two pages never show the same row. The
  // loader lives here because both pages are part of the same view family and the
  // shell already threads this hook's functions.
  const [proposals, setProposals] = useState<ProposalRow[]>([])
  const [proposalCounts, setProposalCounts] = useState<Record<string, number>>({})
  const [proposalsLoaded, setProposalsLoaded] = useState(false)
  const [funnel, setFunnel] = useState<AutonomyFunnel | null>(null)
  const [answeredProposals, setAnsweredProposals] = useState<ProposalRow[]>([])

  const loadAutonomy = useCallback(async () => {
    try {
      const data = await (await api('/api/autonomy')).json()
      setProposals(Array.isArray(data.live) ? data.live : [])
      setProposalCounts(
        data && typeof data.counts === 'object' && data.counts ? data.counts : {},
      )
      setFunnel(data && typeof data.funnel === 'object' && data.funnel ? data.funnel : null)
      setAnsweredProposals(Array.isArray(data.answered) ? data.answered : [])
      setProposalsLoaded(true)
    } catch {
      // An unreachable list must not read as "the agent proposed nothing": the
      // last known one stands, and the page says it is stale.
    }
  }, [api])

  const decideProposal = async (id: string, state: 'accepted' | 'declined') => {
    try {
      const resp = await api('/api/autonomy/decide', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, state }),
      })
      const data = await resp.json().catch(() => ({}))
      if (!data.ok) {
        messageApi.warning(String(data.reason || data.error || '没有记录这次答复'))
      } else {
        messageApi.success(state === 'accepted' ? '已记下：你会同意这件事' : '已记下：不做这件事')
      }
      // Both surfaces show this row: 自主 lists proposals, and the home board's
      // "等你处理" column holds the same ones. Refreshing only the page the
      // request came from is how the other one keeps a card the server has
      // already removed.
      await Promise.all([loadAutonomy(), loadHome()])
    } catch {
      // surfaced by the api helper
    }
  }

  const loadSchedulerHealth = useCallback(async () => {
    try {
      const resp = await api('/api/scheduler/health')
      setSchedulerHealth(await resp.json())
    } catch {
      setSchedulerHealth({ status: 'offline' })
    }
  }, [api])

  const loadUnseenFailures = useCallback(async () => {
    try {
      const resp = await api('/api/schedules/attention')
      const data = await resp.json()
      applyAttention(data)
    } catch {
      // A transport failure is not "no failures"; leave the last known count
      // alone rather than clearing a badge the user has not acted on.
    }
  }, [api, applyAttention])

  const loadScheduleRuns = useCallback(
    // `focusRunId` is for opening a task on the run somebody was actually
    // pointed at. Picking the newest instead would land them on a later,
    // healthy run and the thing they came to see would be one click away in a
    // list they now have to search.
    async (taskId: string, selectLatest = false, silent = false, focusRunId?: string) => {
      const path = `/api/schedules/${encodeURIComponent(taskId)}/runs?limit=50`
      try {
        if (!silent) setScheduleRunsLoading(true)
        // Same split as the list: the timer's refresh reports failure through
        // state, and the one a click caused reports it to the user. Without
        // this the open drawer re-toasted every two seconds for as long as the
        // gateway stayed down, which is the failure the flag exists to replace.
        const data = silent
          ? await refreshJson(path)
          : await (await api(path)).json()
        if (data === null) {
          setSchedulerStale(true)
          return
        }
        const runs = Array.isArray(data.runs) ? data.runs as ScheduleRun[] : []
        setScheduleRuns(runs)
        setSelectedSchedule(current => current && current.id === taskId
          ? { ...current, ...(data.task || {}) }
          : current)
        setSelectedScheduleRunId(current => {
          if (focusRunId && runs.some(run => run.id === focusRunId)) return focusRunId
          if (selectLatest) return runs[0]?.id || null
          return current && runs.some(run => run.id === current)
            ? current
            : runs[0]?.id || null
        })
      } catch {
        setSchedulerStale(true)
      } finally {
        if (!silent) setScheduleRunsLoading(false)
      }
    },
    [api, refreshJson],
  )

  const openScheduleDetails = useCallback((task: ScheduleInfo, runId?: string) => {
    setSelectedSchedule(task)
    setScheduleDetailOpen(true)
    setScheduleRuns([])
    setSelectedScheduleRunId(runId || null)
    setScheduleRunOutput(null)
    void loadScheduleRuns(task.id, true, false, runId)
  }, [loadScheduleRuns])

  const selectedScheduleRun = useMemo(
    () => scheduleRuns.find(run => run.id === selectedScheduleRunId) || null,
    [scheduleRuns, selectedScheduleRunId],
  )

  // The task a run belongs to, for the criterion it was judged by. Taken from
  // the list the page already holds rather than fetched with the run: the
  // criterion is a property of the definition, and every run row was opened
  // from a task that already carries it.
  const selectedScheduleRunTask = useMemo(
    () => schedules.find(task => task.id === selectedScheduleRun?.task_id) || null,
    [schedules, selectedScheduleRun],
  )

  /**
   * The tasks tab's list.
   *
   * Tasks, not "rows in the task table". A step of a workflow is a task in the
   * table and is not one on this page: its trigger comes from its upstreams,
   * its switch is rewritten from the workflow's on the next save, and it can
   * be neither deleted nor retried on its own. Listed beside the tasks that
   * really are independent, it reads as one of them -- two different things
   * behind identical cards, which is what "步骤拆在任务里，显示混乱" was. A chain
   * is read on the workflow tab, where every step already has its own entry
   * and a graph to see it in, so nothing here becomes unreachable.
   *
   * The one exception is a step whose workflow was deleted. Nothing owns it
   * any more, no save will rewrite it, and this list is the last place it can
   * be seen and removed -- which is why the exception exists rather than a
   * flat "hide everything with a workflow_id". That answer needs the graph, so
   * a step stays hidden until the graphs have arrived: before that every step
   * would look orphaned and the list would flicker.
   */
  const filteredSchedules = useMemo(() => {
    const query = scheduleQuery.trim().toLowerCase()
    const liveWorkflows = workflowsLoaded
      ? new Set(workflows.map(flow => flow.id))
      : null
    return schedules.filter(task => {
      if (task.workflow_id && (!liveWorkflows || liveWorkflows.has(task.workflow_id))) {
        return false
      }
      const latestStatus = task.latest_run?.status || ''
      const statusMatches = scheduleStatusFilter === 'all'
        // `in_flight` rather than a status test, so that a run sitting queued
        // behind its upstream counts as running -- which is what it is doing.
        || (scheduleStatusFilter === 'running' && task.in_flight === true)
        || (scheduleStatusFilter === 'failed' && latestStatus === 'failed')
        || (scheduleStatusFilter === 'paused' && task.enabled === false)
      if (!statusMatches) return false
      if (!query) return true
      // The trigger is part of how a task is identified now that it is not
      // only a time: "which task waits for report.ready" is a question people
      // will ask this filter.
      const trigger = task.trigger || {}
      const triggerText = task.trigger_type === 'signal' ? String(trigger.name || '') : ''
      const content = `${task.name} ${triggerText} ${task.workspace_root || ''} ${task.payload?.prompt || task.payload?.message_text || ''}`.toLowerCase()
      return content.includes(query)
    })
  }, [scheduleQuery, scheduleStatusFilter, schedules, workflows, workflowsLoaded])

  const filteredWorkflows = useMemo(() => {
    const query = workflowQuery.trim().toLowerCase()
    if (!query) return workflows
    return workflows.filter(item => {
      const text = [
        item.name,
        item.description || '',
        ...item.steps.map(step => `${step.key} ${step.name} ${stepContent(step.kind, step.payload)}`),
      ].join(' ').toLowerCase()
      return text.includes(query)
    })
  }, [workflows, workflowQuery])

  const attentionByTask = useMemo(
    () => new Map(Object.entries(attentionLatestByTask)),
    [attentionLatestByTask],
  )

  const permissionProfileOptions = useMemo(
    () => (permissionProfiles.length > 0 ? permissionProfiles : KNOWN_PERMISSION_PROFILES),
    [permissionProfiles],
  )

  const permissionProfileLabel = useCallback((key?: string) => (
    permissionProfileOptions.find(item => item.key === key)?.label || key || '继承全局权限'
  ), [permissionProfileOptions])

  /**
   * The run the drawer is showing, reduced to the four values that decide
   * whether its output has to be read again.
   *
   * Ids and statuses rather than the task and run objects they come from. Both
   * writers of `selectedSchedule` -- the list refresh and the silent run
   * refresh -- store a freshly parsed copy on every poll, and the run list
   * hands back a new array of new runs just as often; so an object in this
   * effect's dependency list announces a change once a poll about a run whose
   * output was written once and cannot change afterwards. Keyed that way, the
   * drawer re-read the file and blanked the whole block to a spinner on every
   * tick, which is what "完整输出一直在刷新" was: two requests a poll, one per
   * state write, and four visible flickers a minute for a file nobody touched.
   */
  const openTaskId = selectedSchedule?.id || ''
  const openRunId = selectedScheduleRun?.id || ''
  const openRunStatus = selectedScheduleRun?.status || ''
  const openRunHasOutput = selectedScheduleRun?.output_available === true

  useEffect(() => {
    if (!scheduleDetailOpen || !openTaskId || !openRunId) {
      setScheduleRunOutput(null)
      setScheduleOutputLoading(false)
      return
    }
    if (openRunStatus === 'running') {
      setScheduleRunOutput(null)
      setScheduleOutputLoading(false)
      return
    }
    if (!openRunHasOutput) {
      setScheduleRunOutput({
        run_id: openRunId,
        available: false,
        content: '',
      })
      setScheduleOutputLoading(false)
      return
    }
    let cancelled = false
    setScheduleOutputLoading(true)
    api(
      `/api/schedules/${encodeURIComponent(openTaskId)}` +
      `/runs/${encodeURIComponent(openRunId)}/output`,
    )
      .then(resp => resp.json())
      .then(data => {
        if (!cancelled) setScheduleRunOutput(data as ScheduleRunOutput)
      })
      .catch(() => {
        if (!cancelled) setScheduleRunOutput(null)
      })
      .finally(() => {
        if (!cancelled) setScheduleOutputLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [
    api,
    scheduleDetailOpen,
    openTaskId,
    openRunId,
    openRunStatus,
    openRunHasOutput,
  ])

  useEffect(() => {
    if (!scheduleDetailOpen || !selectedSchedule || !selectedScheduleRun) {
      setScheduleArtifacts([])
      return
    }
    let cancelled = false
    api(
      `/api/schedules/${encodeURIComponent(selectedSchedule.id)}` +
      `/runs/${encodeURIComponent(selectedScheduleRun.id)}/artifacts`,
    )
      .then(resp => resp.json())
      .then(data => {
        if (!cancelled) setScheduleArtifacts(Array.isArray(data.artifacts) ? data.artifacts : [])
      })
      .catch(() => {
        if (!cancelled) setScheduleArtifacts([])
      })
    return () => { cancelled = true }
  }, [api, scheduleDetailOpen, selectedSchedule?.id, selectedScheduleRun?.id, selectedScheduleRun?.status])

  /** Whether anything on this page can change in the next few seconds.
   *
   * Two ways for that to be true: a run is in flight, or one is about to start.
   * Both are read off data already in hand, so neither needs a request to stay
   * true -- which is the point, because the old cadence was decided by the very
   * thing it was deciding.
   *
   * The scheduler being up is part of the question, not a safety net. A run in
   * flight is worth watching because it will finish, and it is the scheduler
   * that finishes it: with the service not running, a lease left behind by a
   * previous process never expires, `in_flight` stays true, and the page would
   * ask every two seconds forever about something that cannot move.
   */
  const schedulerWatchful = useMemo(() => {
    if (view !== 'schedules' || schedulerHealth.status !== 'online') return false
    const inFlight =
      schedules.some(task => task.in_flight === true)
      || scheduleRuns.some(run => run.status === 'running' || run.status === 'queued')
    if (inFlight) return true
    const untilNext = msUntilNextRun(schedules)
    return (
      untilNext !== null
      && untilNext <= SCHEDULE_WATCH_WINDOW_MS
      && untilNext > -SCHEDULE_OVERDUE_GRACE_MS
    )
  }, [scheduleRuns, schedulerHealth.status, schedules, view])

  // Polled from every view, not just the schedules page: the badge exists so
  // that a failure is noticed by someone who is not looking at the page.
  useEffect(() => {
    void loadUnseenFailures()
    const timer = window.setInterval(loadUnseenFailures, 30000)
    return () => window.clearInterval(timer)
  }, [loadUnseenFailures])

  const deleteSchedule = (task: ScheduleInfo) => {
    confirmResourceDeletion('任务', task.name, async () => {
      await api(`/api/schedules/${encodeURIComponent(task.id)}`, { method: 'DELETE' })
      setSchedules(prev => prev.filter(item => item.id !== task.id))
      if (selectedSchedule?.id === task.id) {
        setScheduleDetailOpen(false)
        setSelectedSchedule(null)
        setScheduleRuns([])
        setSelectedScheduleRunId(null)
      }
      void loadAutomations(true)
      messageApi.success('任务已删除')
    })
  }

  const toggleSchedule = async (task: ScheduleInfo, enabled: boolean) => {
    try {
      await api(`/api/schedules/${encodeURIComponent(task.id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      })
      setSchedules(prev => prev.map(item => item.id === task.id ? { ...item, enabled } : item))
      setSelectedSchedule(current => current?.id === task.id
        ? { ...current, enabled }
        : current)
      void loadAutomations(true)
    } catch { /* surfaced */ }
  }

  const bulkScheduleAction = async (action: 'enable' | 'disable' | 'delete') => {
    if (!selectedScheduleIds.length) return
    const execute = async () => {
      const resp = await api('/api/schedules', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, task_ids: selectedScheduleIds }),
      })
      const data = await resp.json()
      const completed = Array.isArray(data.completed) ? data.completed as string[] : []
      setSelectedScheduleIds(prev => prev.filter(id => !completed.includes(id)))
      await loadSchedules(true)
      await loadAutomations(true)
      if (data.skipped?.length) {
        messageApi.warning(`${completed.length} 个任务已处理，${data.skipped.length} 个任务已跳过（运行中、属于流程或不存在）`)
      } else {
        messageApi.success(`${completed.length} 个任务已${action === 'delete' ? '删除' : action === 'enable' ? '启用' : '暂停'}`)
      }
    }
    if (action === 'delete') {
      confirmResourceDeletion('任务', `${selectedScheduleIds.length} 个所选任务`, execute)
    } else {
      await execute()
    }
  }

  const deleteWorkflow = (info: WorkflowInfo) => {
    confirmResourceDeletion('流程', info.name, async () => {
      const resp = await api(`/api/workflows/${encodeURIComponent(info.id)}`, { method: 'DELETE' })
      const data = await resp.json().catch(() => ({}))
      if (data.error) {
        messageApi.error(String(data.error))
        return
      }
      await Promise.all([loadWorkflows(true), loadSchedules(true), loadAutomations(true)])
      messageApi.success('流程已删除，它的步骤已停止运行，可在任务列表里单独删除')
    })
  }

  const toggleWorkflow = async (info: WorkflowInfo, enabled: boolean) => {
    try {
      // Sent as a whole-workflow switch rather than one per step: saving a
      // workflow writes every step's enabled flag from its own, so a switch
      // thrown on a single step would be undone by the next save.
      const resp = await api(`/api/workflows/${encodeURIComponent(info.id)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      })
      const data = await resp.json().catch(() => ({}))
      if (data.error) {
        messageApi.error(String(data.error))
        return
      }
      await Promise.all([loadWorkflows(true), loadSchedules(true), loadAutomations(true)])
    } catch { /* surfaced */ }
  }

  const runWorkflowNow = async (info: WorkflowInfo) => {
    if (startingWorkflowRef.current.has(info.id)) return
    const entries = info.steps.filter(step => step.depends_on.length === 0 && step.task_id)
    if (!entries.length) {
      messageApi.warning('这个流程还没有可运行的入口步骤')
      return
    }
    // An entry already in flight would only come back as a 409 -- and it used
    // to abort the loop there, so the entries after it never started and the
    // ones before it had started without a word: the success toast and the
    // refresh both sat after the loop.
    const idle = entries.filter(step => !step.in_flight)
    if (!idle.length) {
      messageApi.info('入口步骤都在运行中')
      return
    }
    startingWorkflowRef.current.add(info.id)
    setStartingWorkflowIds(Array.from(startingWorkflowRef.current))
    try {
      // Started at the entries and only at the entries: the steps below them
      // are waiting for those to succeed, and running them directly would be
      // asking for a result that does not exist yet. Each entry is its own
      // request, so one refusal does not stop the others; `api` has already
      // toasted each refusal's reason.
      const results = await Promise.allSettled(
        idle.map(step => api(`/api/schedules/${encodeURIComponent(step.task_id)}/run`, { method: 'POST' })),
      )
      const started = results.filter(result => result.status === 'fulfilled').length
      const skipped = entries.length - idle.length
      const failed = idle.length - started
      const tail = [
        skipped ? `${skipped} 个已在运行` : '',
        failed ? `${failed} 个未能启动` : '',
      ].filter(Boolean).join('，')
      if (!started) {
        messageApi.error(`入口步骤都没有启动${tail ? `（${tail}）` : ''}`)
      } else if (tail) {
        messageApi.warning(`已启动 ${started} 个入口步骤，${tail}`)
      } else {
        messageApi.success(started > 1 ? `已启动 ${started} 个入口步骤` : '入口步骤已开始运行')
      }
    } finally {
      // Whatever did start is now running, so the picture is stale either way.
      await Promise.all([loadWorkflows(true), loadSchedules(true)])
      startingWorkflowRef.current.delete(info.id)
      setStartingWorkflowIds(Array.from(startingWorkflowRef.current))
    }
  }
  const runScheduleNow = async (task: ScheduleInfo) => {
    try {
      const resp = await api(`/api/schedules/${encodeURIComponent(task.id)}/run`, {
        method: 'POST',
      })
      const data = await resp.json()
      const runId: string | undefined = data.run?.id || undefined
      messageApi.success('任务已开始运行')
      setSelectedScheduleRunId(runId || null)
      await loadSchedules(true)
      if (scheduleDetailOpen && selectedSchedule?.id === task.id) {
        await loadScheduleRuns(task.id, true, true)
      } else {
        // The run id has to go through: opening the drawer resets the
        // selection, so the one set above was wiped and the drawer came up
        // on no run instead of the run just started.
        openScheduleDetails(task, runId)
      }
    } catch { /* surfaced */ }
  }

  const cancelScheduleRun = async (task: ScheduleInfo, run: ScheduleRun) => {
    try {
      await api(
        `/api/schedules/${encodeURIComponent(task.id)}/runs/${encodeURIComponent(run.id)}/cancel`,
        { method: 'POST' },
      )
      setScheduleRuns(prev => prev.map(item => item.id === run.id
        ? { ...item, cancel_requested_at: new Date().toISOString() }
        : item))
      messageApi.info('正在取消任务')
    } catch { /* surfaced */ }
  }

  // Marking a failure as seen is what removes the badge, so it is applied to
  // local state right away: waiting for a refetch would leave the dot sitting
  // there after the click and the control would read as broken.
  // Keyed by ids rather than by the objects, so a run can be acknowledged
  // from a list that only knows where it came from -- the attention list holds
  // ids, and asking it to first find the whole task definition would put a
  // dependency between "clear this" and "the tasks are loaded".
  const acknowledgeScheduleRun = async (taskId: string, runId: string) => {
    try {
      const resp = await api(
        `/api/schedules/${encodeURIComponent(taskId)}/runs/${encodeURIComponent(runId)}/acknowledge`,
        { method: 'POST' },
      )
      const data = await resp.json()
      const consumed = data.acknowledged ? 1 : 0
      // The failure was in the home board's "等你处理" column; acknowledging it
      // is what makes the column drop it.
      void loadHome()
      setScheduleRuns(prev => prev.map(item => item.id === runId
        ? { ...item, needs_attention: false, acknowledged_at: new Date().toISOString() }
        : item))
      const dropOne = (count?: number) => Math.max(0, (count || 0) - consumed)
      setSchedules(prev => prev.map(item => item.id === taskId
        ? { ...item, unseen_attention: dropOne(item.unseen_attention) }
        : item))
      setSelectedSchedule(current => current && current.id === taskId
        ? { ...current, unseen_attention: dropOne(current.unseen_attention) }
        : current)
      applyAttention(data)
    } catch { /* surfaced */ }
  }

  const clearScheduleAttention = async (taskId?: string) => {
    try {
      const resp = await api('/api/schedules/attention', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(taskId ? { task_id: taskId } : {}),
      })
      const data = await resp.json()
      const affectsSelected = !taskId || taskId === selectedSchedule?.id
      if (affectsSelected) {
        setScheduleRuns(prev => prev.map(item => item.needs_attention
          ? { ...item, needs_attention: false, acknowledged_at: new Date().toISOString() }
          : item))
      }
      const zeroed = (item: ScheduleInfo) => !taskId || item.id === taskId
      setSchedules(prev => prev.map(item => zeroed(item)
        ? { ...item, unseen_attention: 0 }
        : item))
      setSelectedSchedule(current => current && zeroed(current)
        ? { ...current, unseen_attention: 0 }
        : current)
      applyAttention(data)
      if (data.cleared) messageApi.success(`已将 ${data.cleared} 次失败标记为已读`)
    } catch { /* surfaced */ }
  }

  const retryScheduleRun = async (task: ScheduleInfo, run: ScheduleRun, useLatest: boolean) => {
    try {
      await api(
        `/api/schedules/${encodeURIComponent(task.id)}/runs/${encodeURIComponent(run.id)}/retry`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ use_latest: useLatest }),
        },
      )
      messageApi.success(useLatest ? '已使用当前配置重试' : '已使用原运行配置重试')
      await loadSchedules(true)
      await loadScheduleRuns(task.id, true, true)
    } catch { /* surfaced */ }
  }
  const editingWorkflow = useMemo(
    () => workflows.find(item => item.id === editingWorkflowId) || null,
    [workflows, editingWorkflowId],
  )
  return { acknowledgeScheduleRun, activityCursor, activityExhausted, activityLoading, decideProposal, answeredProposals, funnel, home, homeLoaded, loadHome, loadMoreActivity, olderActivity, loadAutonomy, proposalCounts, proposals, proposalsLoaded, applySchedules, attentionByTask, attentionLatestByTask, attentionRuns, automationKind, automationQuery, automations, automationsLoaded, automationTab, bulkScheduleAction, cancelScheduleRun, clearScheduleAttention, deleteSchedule, deleteWorkflow, editingWorkflow, editingWorkflowId, filteredSchedules, filteredWorkflows, loadAutomations, loadScheduleRuns, loadSchedulerHealth, loadSchedules, loadUnseenFailures, loadWorkflows, openScheduleDetails, permissionProfileLabel, permissionProfiles, retryScheduleRun, runScheduleNow, runWorkflowNow, scheduleArtifacts, scheduleDetailOpen, scheduleOutputLoading, scheduleQuery, scheduleRunOutput, scheduleRuns, scheduleRunsLoading, scheduleStatusFilter, schedulerHealth, schedulerRefreshedAt, schedulerStale, schedulerWatchful, schedules, selectedSchedule, selectedScheduleIds, selectedScheduleRun, selectedScheduleRunId, selectedScheduleRunTask, setAutomationKind, setAutomationQuery, setAutomationTab, setEditingWorkflowId, setScheduleArtifacts, setScheduleDetailOpen, setScheduleOutputLoading, setScheduleQuery, setScheduleRunOutput, setScheduleRuns, setScheduleRunsLoading, setScheduleStatusFilter, setSchedulerHealth, setSchedulerRefreshedAt, setSchedulerStale, setSelectedSchedule, setSelectedScheduleIds, setSelectedScheduleRunId, setUnseenFailures, setWorkflowQuery, startingWorkflowIds, toggleSchedule, toggleWorkflow, unseenFailures, workflowQuery, workflows } as const
}
