import { useCallback, useEffect, useMemo, useState } from 'react'
import { KNOWN_PERMISSION_PROFILES, SCHEDULE_OVERDUE_GRACE_MS, SCHEDULE_WATCH_WINDOW_MS } from '../constants'
import { defaultScheduleDraft, msUntilNextRun, scheduleDraftFromTask, scheduleRequestBody } from '../lib/schedule'
import { checkWorkflowGraph, cleanStepKeys, defaultWorkflowDraft, defaultWorkflowStep, defaultWorkflowTrigger, describeChainTrigger, nextStepKey, planWorkflowStepMove, spliceWorkflowStep, stepContent, storedEntryStep, swapWorkflowSteps, workflowDownstreamKeys, workflowDraftFromInfo, workflowRequestBody } from '../lib/workflow'
import type { AttentionRun, PermissionProfileOption, ScheduleArtifact, ScheduleDraft, ScheduleInfo, ScheduleRun, ScheduleRunOutput, SchedulerHealth, SignalInfo, WorkflowDraft, WorkflowInfo, WorkflowStepDraft, WorkflowStepInfo } from '../types'
import { Modal } from 'antd'
import dayjs from 'dayjs'
import { useUi } from './useUi'
import { useApiClient } from './useApiClient'
import { useConfirm } from './useConfirm'
import { useConversations } from './useConversations'
import { useSettings } from './useSettings'

type Deps = ReturnType<typeof useUi> & ReturnType<typeof useApiClient> & ReturnType<typeof useConfirm> & ReturnType<typeof useConversations> & ReturnType<typeof useSettings>

export function useAutomation(deps: Deps) {
  const { api, apiHeaders, config, confirmResourceDeletion, feishuChatsLoaded, feishuChatsLoading, loadFeishuChats, messageApi, refreshJson, sessionState, setLoadingView, view } = deps

  const [schedules, setSchedules] = useState<ScheduleInfo[]>([])

  const [signals, setSignals] = useState<SignalInfo[]>([])

  const [signalsWaiting, setSignalsWaiting] = useState<{ name: string; subscriber_count: number }[]>([])

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

  const [scheduleModalOpen, setScheduleModalOpen] = useState(false)

  const [scheduleSaving, setScheduleSaving] = useState(false)

  const [scheduleDraft, setScheduleDraft] = useState<ScheduleDraft>(defaultScheduleDraft)

  const [editingScheduleId, setEditingScheduleId] = useState<string | null>(null)

  const [schedulePreview, setSchedulePreview] = useState<string[]>([])

  const [schedulePreviewError, setSchedulePreviewError] = useState('')

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

  const [automationTab, setAutomationTab] = useState<'tasks' | 'workflows' | 'attention'>('tasks')

  const [workflowModalOpen, setWorkflowModalOpen] = useState(false)

  const [workflowSaving, setWorkflowSaving] = useState(false)

  const [workflowDraft, setWorkflowDraft] = useState<WorkflowDraft>(defaultWorkflowDraft)

  const [editingWorkflowId, setEditingWorkflowId] = useState<string | null>(null)

  //: The one step whose key is open for rewriting, and the key it started with.
  //:
  //: A step's key is its identity: the task behind it is found by that key, so
  //: changing it does not rename anything -- it points the graph at a task that
  //: does not exist yet and leaves the old one, with its run history, behind.
  //: Renaming is therefore an explicit act rather than a text field to nudge,
  //: and this holds the original key so the rest of the editor can keep
  //: treating the step as the one that is stored.
  const [workflowKeyRewrite, setWorkflowKeyRewrite] = useState<
    { at: number; key: string } | null
  >(null)

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

  const loadSchedulerHealth = useCallback(async () => {
    try {
      const resp = await api('/api/scheduler/health')
      setSchedulerHealth(await resp.json())
    } catch {
      setSchedulerHealth({ status: 'offline' })
    }
  }, [api])

  const loadSignals = useCallback(async () => {
    try {
      const resp = await api('/api/signals')
      const data = await resp.json()
      setSignals(Array.isArray(data.signals) ? data.signals : [])
      setSignalsWaiting(Array.isArray(data.waiting) ? data.waiting : [])
    } catch {
      // An unreachable list is not "no signals": leave the picker empty
      // rather than telling the user that nothing has ever been emitted.
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

  /**
   * The step being edited, when the task editor was opened on one.
   *
   * A step that has upstreams does not own its timing, so the form must stop
   * offering one, must not send one -- the server refuses a contradicting
   * trigger, and would refuse the form's own default of "wait for a signal"
   * since a fan-in has no single name to show -- and must not ask for a
   * preview of times it does not have.
   */
  const editingStep = useMemo(() => {
    if (!editingScheduleId) return null
    const task = schedules.find(item => item.id === editingScheduleId)
    if (!task?.workflow_id) return null
    const flow = workflows.find(item => item.id === task.workflow_id)
    const step = flow?.steps.find(item => item.key === task.step_key)
    if (!step) return null
    return { flow, step, followsUpstreams: step.depends_on.length > 0 }
  }, [editingScheduleId, schedules, workflows])

  // Folders a task has already run in are the folders most likely to be
  // wanted again, so the form offers them as one-click answers instead of
  // asking the user to type (or re-pick) a path they already trusted.
  const recentWorkspaceRoots = useMemo(() => {
    const roots: string[] = []
    for (const task of schedules) {
      if (task.workspace_root) roots.push(task.workspace_root)
    }
    for (const flow of workflows) {
      for (const step of flow.steps) {
        if (step.workspace_root) roots.push(step.workspace_root)
      }
    }
    if (sessionState?.workspace_root) roots.push(sessionState.workspace_root)
    if (config?.workspace_root) roots.push(config.workspace_root)
    return Array.from(new Set(roots)).slice(0, 6)
  }, [schedules, workflows, sessionState?.workspace_root, config?.workspace_root])

  // The chat list is a network call against the user's Feishu app, so it is
  // fetched only when the form can actually show it -- opening the editor on
  // a channel task, or choosing 发到飞书 -- and not on every modal open.
  //
  // The guard has to test `feishuChatsLoaded`, not `feishuChats.length`. An
  // empty list is a perfectly good answer, and `0` is falsy, so a length test
  // reads "we have no chats" as "we have not asked": the effect re-fires the
  // moment `feishuChatsLoading` drops back to false, and the picker spins
  // forever on a successful response. It never settles because it never stops
  // asking.
  useEffect(() => {
    if (!scheduleModalOpen) return
    if (scheduleDraft.delivery_mode !== 'channel') return
    if (feishuChatsLoaded || feishuChatsLoading) return
    void loadFeishuChats()
  }, [
    scheduleModalOpen,
    scheduleDraft.delivery_mode,
    feishuChatsLoaded,
    feishuChatsLoading,
    loadFeishuChats,
  ])

  useEffect(() => {
    if (!scheduleModalOpen) {
      setSchedulePreview([])
      setSchedulePreviewError('')
      return
    }
    const taskContent = scheduleDraft.action_type === 'agent_task'
      ? scheduleDraft.prompt.trim()
      : scheduleDraft.message_text.trim()
    if (!scheduleDraft.name.trim() || !taskContent || (
      scheduleDraft.action_type === 'agent_task' && !scheduleDraft.workspace_root.trim()
    )) {
      setSchedulePreview([])
      return
    }
    // A step that follows upstreams has no times to preview, and asking for
    // them would come back as "pick a signal to wait for" -- an error about a
    // question this form is not asking.
    if (editingStep?.followsUpstreams) {
      setSchedulePreview([])
      setSchedulePreviewError('')
      return
    }
    let cancelled = false
    const timer = window.setTimeout(async () => {
      try {
        const resp = await fetch('/api/schedules/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...apiHeaders() },
          body: JSON.stringify(scheduleRequestBody(scheduleDraft)),
        })
        const data = await resp.json()
        if (cancelled) return
        if (!resp.ok) {
          setSchedulePreview([])
          setSchedulePreviewError(data.error || '无法预览执行时间')
          return
        }
        setSchedulePreview(Array.isArray(data.occurrences) ? data.occurrences : [])
        setSchedulePreviewError('')
      } catch {
        if (!cancelled) setSchedulePreviewError('无法连接调度服务')
      }
    }, 350)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [apiHeaders, scheduleDraft, scheduleModalOpen, editingStep?.followsUpstreams])

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

  const filteredSchedules = useMemo(() => {
    const query = scheduleQuery.trim().toLowerCase()
    return schedules.filter(task => {
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
  }, [scheduleQuery, scheduleStatusFilter, schedules])

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

  const workflowAttention = useMemo(
    () => workflows.reduce((sum, item) => sum + (item.unseen_attention || 0), 0),
    [workflows],
  )

  /**
   * Which run to open for a task that has something waiting.
   *
   * The count shown on a card is the task's own `unseen_attention` -- that is
   * the number the task carries, and it stays right even when the list behind
   * the badge is long enough to be capped. This map only answers "and which
   * one", which the count cannot -- and it comes from the server's snapshot
   * rather than the capped list, so a task whose rows fell out of the list
   * still opens on the run the count is about.
   */
  const attentionByTask = useMemo(
    () => new Map(Object.entries(attentionLatestByTask)),
    [attentionLatestByTask],
  )

  const permissionProfileOptions = useMemo(
    () => (permissionProfiles.length > 0 ? permissionProfiles : KNOWN_PERMISSION_PROFILES),
    [permissionProfiles],
  )

  const activePermissionProfile = useMemo(
    () => permissionProfileOptions.find(
      item => item.key === scheduleDraft.permission_profile,
    ),
    [permissionProfileOptions, scheduleDraft.permission_profile],
  )

  const permissionProfileLabel = useCallback((key?: string) => (
    permissionProfileOptions.find(item => item.key === key)?.label || key || '继承全局权限'
  ), [permissionProfileOptions])

  useEffect(() => {
    if (!scheduleDetailOpen || !selectedSchedule || !selectedScheduleRun) {
      setScheduleRunOutput(null)
      setScheduleOutputLoading(false)
      return
    }
    if (selectedScheduleRun.status === 'running') {
      setScheduleRunOutput(null)
      setScheduleOutputLoading(false)
      return
    }
    if (!selectedScheduleRun.output_available) {
      setScheduleRunOutput({
        run_id: selectedScheduleRun.id,
        available: false,
        content: '',
      })
      setScheduleOutputLoading(false)
      return
    }
    let cancelled = false
    setScheduleOutputLoading(true)
    api(
      `/api/schedules/${encodeURIComponent(selectedSchedule.id)}` +
      `/runs/${encodeURIComponent(selectedScheduleRun.id)}/output`,
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
    selectedSchedule,
    selectedScheduleRun?.id,
    selectedScheduleRun?.output_available,
    selectedScheduleRun?.status,
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

  const openCreateSchedule = () => {
    setEditingScheduleId(null)
    setScheduleDraft(defaultScheduleDraft(sessionState?.workspace_root || config?.workspace_root || ''))
    // Refreshed on open, not only when the page loaded: the signals worth
    // waiting for are the ones emitted since, and a picker showing yesterday's
    // list is how someone ends up typing a name by hand again.
    void loadSignals()
    setScheduleModalOpen(true)
  }

  const workflowGraph = useMemo(() => checkWorkflowGraph(workflowDraft.steps), [workflowDraft])

  // Whether the cards and the running order have come apart. They agree as long
  // as steps are only ever added at the end, so a difference means somebody
  // moved something -- and then the numbers stop reading 1, 2, 3, which needs
  // saying rather than leaving the reader to work out why.
  const workflowOrderDiffersFromArray =
    workflowGraph.order.length === workflowDraft.steps.length
    && workflowGraph.order.some(
      (key, at) => key !== (workflowDraft.steps[at]?.key.trim() || ''),
    )

  const openCreateWorkflow = () => {
    setEditingWorkflowId(null)
    setWorkflowDraft(defaultWorkflowDraft(sessionState?.workspace_root || config?.workspace_root || ''))
    setWorkflowKeyRewrite(null)
    setWorkflowModalOpen(true)
  }

  const openEditWorkflow = (info: WorkflowInfo) => {
    setEditingWorkflowId(info.id)
    setWorkflowDraft(workflowDraftFromInfo(info))
    setWorkflowKeyRewrite(null)
    setWorkflowModalOpen(true)
  }

  const patchWorkflowStep = (index: number, patch: Partial<WorkflowStepDraft>) => {
    setWorkflowDraft(current => ({
      ...current,
      steps: current.steps.map((step, at) => (at === index ? { ...step, ...patch } : step)),
    }))
  }

  /**
   * Re-point a step's upstreams, and keep its trigger section telling the truth.
   *
   * A step that gains upstreams has no schedule of its own -- the rules say so
   * and the server drops one on sight -- so nothing about a trigger may be sent
   * for it, and a handover left over from an earlier edit has to go with it or
   * the next save would name a step that no longer has a schedule to hand over.
   *
   * Clearing them makes this the step the chain starts from, and it needs a way
   * to say when. A step that was already the entry keeps the stored schedule it
   * has; one that never was has nothing to keep, so the form gets a trigger to
   * fill in rather than a line claiming to hold one.
   */
  const changeWorkflowStepUpstreams = (index: number, upstreams: string[]) => {
    setWorkflowDraft(current => ({
      ...current,
      steps: current.steps.map((step, at) => {
        if (at !== index) return step
        if (upstreams.length) {
          return { ...step, depends_on: upstreams, trigger: null, triggerFrom: '' }
        }
        const stored = editingWorkflow?.steps.find(item => item.key === step.key.trim())
        return {
          ...step,
          depends_on: upstreams,
          trigger: step.trigger || (storedEntryStep(stored) ? null : defaultWorkflowTrigger()),
          triggerFrom: step.triggerFrom,
        }
      }),
    }))
  }

  /** A blank step for the chain, chained onto whatever it is put behind. */
  const blankWorkflowStep = (
    steps: WorkflowStepDraft[],
    afterKey: string,
    workspaceRoot: string,
  ): WorkflowStepDraft => ({
    ...defaultWorkflowStep(workspaceRoot),
    key: nextStepKey(steps),
    depends_on: afterKey ? [afterKey] : [],
    // A step with an upstream is driven by it and carries no schedule; a step
    // that ended up with no upstream at all -- because the step it was put
    // behind has no key yet -- does need one.
    trigger: afterKey ? null : defaultWorkflowTrigger(),
    triggerFrom: '',
  })

  const addWorkflowStep = () => {
    setWorkflowDraft(current => {
      // The end of the *running* order, not the end of the array. The two agree
      // until an edit makes them disagree, and the array is the one that means
      // nothing -- chaining onto a step picked by array position is how a new
      // step ends up waiting on something it has nothing to do with.
      const order = checkWorkflowGraph(current.steps).order
      const lastKey = order[order.length - 1] || ''
      const at = current.steps.findIndex(step => step.key.trim() === lastKey)
      const anchor = current.steps[at < 0 ? current.steps.length - 1 : at]
      return {
        ...current,
        steps: spliceWorkflowStep(
          current.steps,
          at < 0 ? current.steps.length - 1 : at,
          blankWorkflowStep(current.steps, anchor?.key?.trim() || '', anchor?.workspace_root || ''),
          false,
        ),
      }
    })
  }

  /**
   * Put a new step behind the one at *index*, between it and whatever follows.
   *
   * Everything that waited on that step waits on the new one instead. That
   * second half is the whole point: skipping it is how "in between" silently
   * becomes "off to the side", and the two are indistinguishable afterwards
   * because both are a valid graph. It re-points other steps though, so it is
   * asked about first and the question names them.
   */
  const insertWorkflowStepAfter = (index: number) => {
    const anchor = workflowDraft.steps[index]
    const anchorKey = anchor?.key?.trim() || ''
    const followers = anchorKey
      ? workflowDownstreamKeys(workflowDraft.steps, anchorKey)
      : []
    const apply = (adoptFollowers: boolean) => {
      setWorkflowDraft(current => {
        const step = current.steps[index]
        const key = step?.key?.trim() || ''
        return {
          ...current,
          steps: spliceWorkflowStep(
            current.steps,
            index,
            blankWorkflowStep(current.steps, key, step?.workspace_root || ''),
            adoptFollowers,
          ),
        }
      })
    }
    // Nothing waited on it: this is an ordinary append and there is nothing to
    // ask about.
    if (!followers.length) {
      apply(false)
      return
    }
    Modal.confirm({
      title: `在「${anchor.name.trim() || anchorKey}」后面插入一步？`,
      content: `现在「${followers.join('」「')}」接在它后面，插入后改接新步骤，仍然排在它后面。`,
      okText: '插入',
      cancelText: '取消',
      centered: true,
      onOk: () => apply(true),
    })
  }

  /**
   * Trade a step's place with the step it is linked to.
   *
   * Two steps and the edges around them get rewritten, and one of them may have
   * to hand the chain's schedule over -- there is no undo, so the question says
   * which way round they end up, who follows whom, and where the clock goes.
   */
  const moveWorkflowStep = (index: number, direction: 'earlier' | 'later') => {
    const step = workflowDraft.steps[index]
    const plan = planWorkflowStepMove(
      workflowDraft.steps,
      step?.key?.trim() || '',
      direction,
    )
    if (!plan.pair) {
      messageApi.warning(plan.problem || '这一步现在换不了位置')
      return
    }
    const [earlierKey, laterKey] = plan.pair
    const earlier = workflowDraft.steps.find(item => item.key.trim() === earlierKey)
    const later = workflowDraft.steps.find(item => item.key.trim() === laterKey)
    const label = (item?: WorkflowStepDraft) => item?.name.trim() || item?.key.trim() || ''
    const followers = workflowDownstreamKeys(workflowDraft.steps, laterKey)
    const becomesEntry = cleanStepKeys(earlier?.depends_on || []).length === 0
    const clock = becomesEntry
      ? describeChainTrigger(workflowDraft.steps, editingWorkflow?.steps, earlierKey)
      : ''
    Modal.confirm({
      title: `把「${label(later)}」${direction === 'earlier' ? '上移' : '下移'}一位？`,
      content: (
        <div className="workflow-move-confirm">
          <p>
            {`「${label(earlier)}」和「${label(later)}」调换位置：`
              + `${earlierKey} → ${laterKey} 变成 ${laterKey} → ${earlierKey}。`}
          </p>
          {followers.length > 0 && (
            <p>{`「${followers.join('」「')}」改接在「${label(earlier)}」后面，仍然排在最后。`}</p>
          )}
          {clock && <p>{`入口触发方式（${clock}）会跟着入口移到「${label(later)}」上。`}</p>}
        </div>
      ),
      okText: '换位',
      cancelText: '取消',
      centered: true,
      onOk: () => {
        setWorkflowDraft(current => ({
          ...current,
          steps: swapWorkflowSteps(current.steps, earlierKey, laterKey),
        }))
      },
    })
  }

  const removeWorkflowStep = (index: number) => {
    setWorkflowDraft(current => {
      const removed = current.steps[index]?.key?.trim()
      const steps = current.steps
        .filter((_, at) => at !== index)
        .map(step => ({
          ...step,
          depends_on: step.depends_on.filter(key => key.trim() !== removed),
        }))
      return { ...current, steps }
    })
  }

  const saveWorkflow = async () => {
    const check = checkWorkflowGraph(workflowDraft.steps)
    if (!workflowDraft.name.trim()) {
      messageApi.warning('请填写流程名称')
      return
    }
    if (check.problems.length) {
      messageApi.warning(check.problems[0])
      return
    }
    // A step with no upstreams has to say when the chain starts. Checked here
    // for an edit as well as for a new chain, because reordering is exactly the
    // edit that changes which step is first: without this the save goes out,
    // comes back refused, and names a step the user just moved. A step that was
    // already the entry keeps the stored schedule it is not sending, and one
    // taking over a schedule says so by name.
    const missingTrigger = workflowDraft.steps.find(step => {
      if (cleanStepKeys(step.depends_on).length) return false
      if (step.trigger || step.triggerFrom) return false
      const stored = editingWorkflow?.steps.find(item => item.key === step.key.trim())
      return !storedEntryStep(stored)
    })
    if (missingTrigger) {
      messageApi.warning(`步骤「${missingTrigger.key}」没有上游，需要指定触发方式`)
      return
    }
    if (workflowDraft.steps.some(step => !step.content.trim())) {
      messageApi.warning('每个步骤都要有执行内容')
      return
    }
    setWorkflowSaving(true)
    try {
      const body = workflowRequestBody(workflowDraft)
      const resp = await api(
        editingWorkflowId
          ? `/api/workflows/${encodeURIComponent(editingWorkflowId)}`
          : '/api/workflows',
        {
          method: editingWorkflowId ? 'PUT' : 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        },
      )
      const data = await resp.json()
      if (data.error) {
        messageApi.error(String(data.error))
        return
      }
      setWorkflowModalOpen(false)
      setEditingWorkflowId(null)
      messageApi.success(editingWorkflowId ? '流程已保存' : '流程已创建')
      // Steps are tasks, so both lists change: the graph gained or moved
      // tasks, and the task list is where they are shown.
      await Promise.all([loadWorkflows(true), loadSchedules(true)])
    } catch {
      // Surfaced by the api helper.
    } finally {
      setWorkflowSaving(false)
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
      await Promise.all([loadWorkflows(true), loadSchedules(true)])
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
      await Promise.all([loadWorkflows(true), loadSchedules(true)])
    } catch { /* surfaced */ }
  }

  const runWorkflowNow = async (info: WorkflowInfo) => {
    const entries = info.steps.filter(step => step.depends_on.length === 0 && step.task_id)
    if (!entries.length) {
      messageApi.warning('这个流程还没有可运行的入口步骤')
      return
    }
    try {
      // Started at the entries and only at the entries: the steps below them
      // are waiting for those to succeed, and running them directly would be
      // asking for a result that does not exist yet.
      for (const step of entries) {
        await api(`/api/schedules/${encodeURIComponent(step.task_id)}/run`, { method: 'POST' })
      }
      messageApi.success(
        entries.length > 1
          ? `已启动 ${entries.length} 个入口步骤`
          : '入口步骤已开始运行',
      )
      await Promise.all([loadWorkflows(true), loadSchedules(true)])
    } catch { /* surfaced */ }
  }

  const openEditSchedule = (task: ScheduleInfo) => {
    setEditingScheduleId(task.id)
    const draft = scheduleDraftFromTask(task)
    setScheduleDraft({
      ...draft,
      workspace_root: draft.workspace_root || sessionState?.workspace_root || config?.workspace_root || '',
    })
    void loadSignals()
    setScheduleModalOpen(true)
  }

  const duplicateSchedule = (task: ScheduleInfo) => {
    setEditingScheduleId(null)
    const draft = scheduleDraftFromTask(task)
    setScheduleDraft({
      ...draft,
      name: `${task.name} 副本`,
      workspace_root: draft.workspace_root || sessionState?.workspace_root || config?.workspace_root || '',
    })
    void loadSignals()
    setScheduleModalOpen(true)
  }

  const saveSchedule = async () => {
    const taskContent = scheduleDraft.action_type === 'agent_task'
      ? scheduleDraft.prompt.trim()
      : scheduleDraft.message_text.trim()
    if (!scheduleDraft.name.trim()) {
      messageApi.warning('请填写任务名称')
      return
    }
    if (!taskContent) {
      messageApi.warning(scheduleDraft.action_type === 'agent_task' ? '请填写任务执行要求' : '请填写提醒内容')
      return
    }
    const followsUpstreams = !!editingStep?.followsUpstreams
    if (!followsUpstreams && scheduleDraft.trigger_type === 'once') {
      if (!scheduleDraft.at || !dayjs(scheduleDraft.at).isValid()) {
        messageApi.warning('请选择执行时间')
        return
      }
      if (dayjs(scheduleDraft.at).valueOf() <= Date.now()) {
        messageApi.warning('执行时间必须晚于当前时间')
        return
      }
    }
    if (!followsUpstreams && scheduleDraft.trigger_type === 'interval' && !scheduleDraft.anchor_at) {
      messageApi.warning('请选择首次执行时间')
      return
    }
    if (!followsUpstreams && ['daily', 'weekly', 'weekdays', 'monthly'].includes(scheduleDraft.trigger_type) && !scheduleDraft.time_of_day) {
      messageApi.warning('请选择每天的执行时间')
      return
    }
    if (!followsUpstreams && scheduleDraft.trigger_type === 'signal' && !scheduleDraft.signal_name.trim()) {
      messageApi.warning('请选择或填写要等待的信号')
      return
    }
    if (scheduleDraft.delivery_mode === 'channel' && !scheduleDraft.delivery_chat_id.trim()) {
      messageApi.warning('发到飞书需要选择一个会话')
      return
    }
    try {
      setScheduleSaving(true)
      const target = editingScheduleId
        ? `/api/schedules/${encodeURIComponent(editingScheduleId)}`
        : '/api/schedules'
      const body = scheduleRequestBody(scheduleDraft)
      if (followsUpstreams) {
        // Left out entirely rather than replaced: an omission keeps whatever
        // the step already waits for, which is the only right answer here.
        delete (body as Record<string, any>).trigger_type
        delete (body as Record<string, any>).time_of_day
        delete (body as Record<string, any>).day_of_week
        delete (body as Record<string, any>).day_of_month
        delete (body as Record<string, any>).at
        delete (body as Record<string, any>).every
        delete (body as Record<string, any>).unit
        delete (body as Record<string, any>).anchor_at
        delete (body as Record<string, any>).signal_name
      }
      const resp = await api(target, {
        method: editingScheduleId ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await resp.json()
      if (data.error) {
        messageApi.error(String(data.error))
        return
      }
      setScheduleModalOpen(false)
      setEditingScheduleId(null)
      setScheduleDraft(defaultScheduleDraft(sessionState?.workspace_root || config?.workspace_root || ''))
      await loadSchedules()
      // A step's edit was copied back into its graph, so the picture of that
      // graph is stale until it is read again.
      if (editingStep) await loadWorkflows(true)
      if (selectedSchedule?.id === data.task.id) {
        setSelectedSchedule(data.task)
      }
      messageApi.success(`任务“${data.task.name}”已${editingScheduleId ? '更新' : '创建'}`)
    } catch { /* surfaced */ } finally {
      setScheduleSaving(false)
    }
  }

  const runScheduleNow = async (task: ScheduleInfo) => {
    try {
      const resp = await api(`/api/schedules/${encodeURIComponent(task.id)}/run`, {
        method: 'POST',
      })
      const data = await resp.json()
      messageApi.success('任务已开始运行')
      setSelectedScheduleRunId(data.run?.id || null)
      await loadSchedules(true)
      if (scheduleDetailOpen && selectedSchedule?.id === task.id) {
        await loadScheduleRuns(task.id, true, true)
      } else {
        openScheduleDetails(task)
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

  const openStepDetails = (step: WorkflowStepInfo) => {
    const task = schedules.find(item => item.id === step.task_id)
    if (task) openScheduleDetails(task)
    else messageApi.info('这一步的任务暂时读不到，先刷新一下')
  }

  return { acknowledgeScheduleRun, activePermissionProfile, addWorkflowStep, applyAttention, applySchedules, attentionByTask, attentionLatestByTask, attentionRuns, automationTab, blankWorkflowStep, bulkScheduleAction, cancelScheduleRun, changeWorkflowStepUpstreams, clearScheduleAttention, deleteSchedule, deleteWorkflow, duplicateSchedule, editingScheduleId, editingStep, editingWorkflow, editingWorkflowId, filteredSchedules, filteredWorkflows, insertWorkflowStepAfter, loadScheduleRuns, loadSchedulerHealth, loadSchedules, loadSignals, loadUnseenFailures, loadWorkflows, moveWorkflowStep, openCreateSchedule, openCreateWorkflow, openEditSchedule, openEditWorkflow, openScheduleDetails, openStepDetails, patchWorkflowStep, permissionProfileLabel, permissionProfileOptions, permissionProfiles, recentWorkspaceRoots, removeWorkflowStep, retryScheduleRun, runScheduleNow, runWorkflowNow, saveSchedule, saveWorkflow, scheduleArtifacts, scheduleDetailOpen, scheduleDraft, scheduleModalOpen, scheduleOutputLoading, schedulePreview, schedulePreviewError, scheduleQuery, scheduleRunOutput, scheduleRuns, scheduleRunsLoading, scheduleSaving, scheduleStatusFilter, schedulerHealth, schedulerRefreshedAt, schedulerStale, schedulerWatchful, schedules, selectedSchedule, selectedScheduleIds, selectedScheduleRun, selectedScheduleRunId, selectedScheduleRunTask, setAttentionLatestByTask, setAttentionRuns, setAutomationTab, setEditingScheduleId, setEditingWorkflowId, setPermissionProfiles, setScheduleArtifacts, setScheduleDetailOpen, setScheduleDraft, setScheduleModalOpen, setScheduleOutputLoading, setSchedulePreview, setSchedulePreviewError, setScheduleQuery, setScheduleRunOutput, setScheduleRuns, setScheduleRunsLoading, setScheduleSaving, setScheduleStatusFilter, setSchedulerHealth, setSchedulerRefreshedAt, setSchedulerStale, setSchedules, setSelectedSchedule, setSelectedScheduleIds, setSelectedScheduleRunId, setSignals, setSignalsWaiting, setUnseenFailures, setWorkflowDraft, setWorkflowKeyRewrite, setWorkflowModalOpen, setWorkflowQuery, setWorkflowSaving, setWorkflows, setWorkflowsLoaded, signals, signalsWaiting, toggleSchedule, toggleWorkflow, unseenFailures, workflowAttention, workflowDraft, workflowGraph, workflowKeyRewrite, workflowModalOpen, workflowOrderDiffersFromArray, workflowQuery, workflowSaving, workflows, workflowsLoaded } as const
}
