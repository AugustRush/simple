import {
  AppstoreOutlined,
  ClockCircleOutlined,
  CloseOutlined,
  CodeOutlined,
  DeleteOutlined,
  EditOutlined,
  FolderOpenOutlined,
  MenuOutlined,
  MessageOutlined,
  MoonOutlined,
  MoreOutlined,
  PlusOutlined,
  RobotOutlined,
  SearchOutlined,
  SettingOutlined,
  SunOutlined,
} from '@ant-design/icons'
import {
  Button,
  ConfigProvider,
  Dropdown,
  Empty,
  Form,
  Input,
  Layout,
  Menu,
  message,
  Modal,
  Skeleton,
  theme,
  Tooltip,
} from 'antd'
import zhCN from 'antd/locale/zh_CN'
import dayjs from 'dayjs'
import React from 'react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import 'dayjs/locale/zh-cn'
import './index.css'

import type { AppCtx } from './app/AppCtx'
import { createChatView } from './views/chat'
import { createSessionsView } from './views/sessions'
import { createExtensionsView } from './views/extensions'
import { createAutomationView } from './views/automation'
import { createSettingsView } from './views/settings'
import {
  CONVERSATION_SUMMARY_HALF_HEIGHT,
  INTERRUPT_RETRY_MS,
  INTERRUPT_STUCK_MS,
  KNOWN_PERMISSION_PROFILES,
  MODEL_PICKER_CHROME,
  SCHEDULE_FAST_POLL_MS,
  SCHEDULE_IDLE_POLL_MS,
  SCHEDULE_OVERDUE_GRACE_MS,
  SCHEDULE_WATCH_WINDOW_MS,
  TASK_INTERRUPTED_STATUSES,
} from './constants'
import {
  effortOptionsFrom,
  estimateLabelWidth,
  measureLabelWidth,
  relativeTime,
  thinkingEffortOf,
  truncate,
} from './lib/format'
import { fileHref, withFileSession } from './lib/media'
import {
  defaultScheduleDraft,
  msUntilNextRun,
  scheduleDraftFromTask,
  scheduleRequestBody,
} from './lib/schedule'
import {
  findOpenSubAgentNote,
  foldSubAgentEvent,
  newSubAgentNote,
  sealSubAgentNotes,
} from './lib/subagent'
import { sessionStatusOf } from './lib/tools'
import {
  checkWorkflowGraph,
  cleanStepKeys,
  defaultWorkflowDraft,
  defaultWorkflowStep,
  defaultWorkflowTrigger,
  describeChainTrigger,
  nextStepKey,
  planWorkflowStepMove,
  spliceWorkflowStep,
  stepContent,
  storedEntryStep,
  swapWorkflowSteps,
  workflowDownstreamKeys,
  workflowDraftFromInfo,
  workflowRequestBody,
} from './lib/workflow'
import type {
  AttachmentInfo,
  AttentionRun,
  CommandInfo,
  ConfirmDecision,
  ConfirmRequest,
  FeishuChatInfo,
  Message,
  MessageRole,
  PermissionProfileOption,
  PluginInfo,
  QueuedMessage,
  ScheduleArtifact,
  ScheduleDraft,
  ScheduleInfo,
  SchedulerHealth,
  ScheduleRun,
  ScheduleRunOutput,
  SessionInfo,
  SessionState,
  SignalInfo,
  SkillInfo,
  ToolState,
  WorkflowDraft,
  WorkflowInfo,
  WorkflowStepDraft,
  WorkflowStepInfo,
} from './types'

const { Content, Header, Sider } = Layout

dayjs.locale('zh-cn')
function App() {

  const [messageApi, contextHolder] = message.useMessage()
  const [themeMode, setThemeMode] = useState<string>(
    () => localStorage.getItem('agent_theme') || 'dark',
  )
  const [view, setView] = useState<string>('chat')
  const [sessions, setSessions] = useState<SessionInfo[]>([])
  const [selectedSessionIds, setSelectedSessionIds] = useState<string[]>([])
  const [pendingDeleteSessionId, setPendingDeleteSessionId] = useState<string | null>(null)
  const [activeSession, setActiveSession] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [commands, setCommands] = useState<CommandInfo[]>([])
  const [plugins, setPlugins] = useState<PluginInfo[]>([])
  const [skills, setSkills] = useState<SkillInfo[]>([])
  const [schedules, setSchedules] = useState<ScheduleInfo[]>([])
  const [signals, setSignals] = useState<SignalInfo[]>([])
  const [signalsWaiting, setSignalsWaiting] = useState<{ name: string; subscriber_count: number }[]>([])
  const [feishuChats, setFeishuChats] = useState<FeishuChatInfo[]>([])
  const [feishuChatsLoading, setFeishuChatsLoading] = useState(false)
  // "Have we asked yet" is not the same question as "did we get anything".
  // Inferring the first from `feishuChats.length` treats a *successful* empty
  // list -- a bot that is in no groups yet, which is the normal first-run state
  // -- as "never fetched", and the effect below re-fires every time loading
  // falls back to false. That loop keeps the spinner up forever.
  const [feishuChatsLoaded, setFeishuChatsLoaded] = useState(false)
  const [feishuChatsError, setFeishuChatsError] = useState('')
  const [feishuTesting, setFeishuTesting] = useState(false)
  const [pickingDirectory, setPickingDirectory] = useState(false)
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
  //: A clock for the two things on this page that are about the present moment
  //: rather than about the data: how long until the next run, and how long ago
  //: this was last refreshed. Neither can be read off the payload.
  const [clock, setClock] = useState(() => Date.now())
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
  // The merged page keeps two lists behind one navigation entry.  The tab is
  // remembered across visits for the same reason the search strings are:
  // someone who toggles a plugin off and comes back later is coming back for
  // the list they left, not for a default.
  const [extensionsTab, setExtensionsTab] = useState<'plugins' | 'skills'>('plugins')
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
  const [pendingAttachments, setPendingAttachments] = useState<AttachmentInfo[]>([])
  const [config, setConfig] = useState<any>(null)
  const [configText, setConfigText] = useState<string>('')
  const [confirmReq, setConfirmReq] = useState<ConfirmRequest | null>(null)
  const [confirmRemaining, setConfirmRemaining] = useState(0)
  const [confirmDetailOpen, setConfirmDetailOpen] = useState(false)
  const [confirmOverflowing, setConfirmOverflowing] = useState(false)
  const approvalCommandRef = useRef<HTMLDivElement | null>(null)
  // Absolute deadline rather than a decremented counter: background tabs get
  // their timers throttled, and a counter would drift behind the server.
  const confirmDeadlineRef = useRef(0)
  const [drafts, setDrafts] = useState<Record<string, string>>(() => {
    try {
      const raw = localStorage.getItem('chat_drafts')
      return raw ? (JSON.parse(raw) as Record<string, string>) : {}
    } catch {
      return {}
    }
  })
  const draftKey = activeSession ?? '__new__'
  const input = drafts[draftKey] ?? ''
  const setInput = useCallback((value: string) => {
    setDrafts(prev => ({ ...prev, [draftKey]: value }))
  }, [draftKey])
  useEffect(() => {
    try {
      localStorage.setItem('chat_drafts', JSON.stringify(drafts))
    } catch {
      // ignore storage errors
    }
  }, [drafts])

  // Cache a bounded set of the most recent sessions locally. In-progress turns
  // are rare and completed history lives server-side, so only a few recent
  // sessions need to be cached (LRU, capped) to restore instantly on switch and
  // to avoid blowing the localStorage quota. Writes are debounced so streaming
  // chunks don't hit localStorage on every frame.
  const MAX_CACHED_SESSIONS = 6
  useEffect(() => {
    if (!activeSession) return
    const t = setTimeout(() => {
      try {
        // Sub-agent notes are live telemetry with no server-side record, so
        // caching them would only produce a flash of stale strips on reload.
        const durable = messages.filter(item => item.role !== 'subagent')
        localStorage.setItem(`chat_messages:${activeSession}`, JSON.stringify(durable))
        const raw = localStorage.getItem('chat_messages:index')
        const index = (raw ? (JSON.parse(raw) as string[]) : []).filter(
          id => id !== activeSession,
        )
        index.unshift(activeSession)
        while (index.length > MAX_CACHED_SESSIONS) {
          const evicted = index.pop()
          if (evicted) localStorage.removeItem(`chat_messages:${evicted}`)
        }
        localStorage.setItem('chat_messages:index', JSON.stringify(index))
      } catch {
        // ignore storage errors
      }
    }, 300)
    return () => clearTimeout(t)
  }, [messages, activeSession])
  const [sessionSearch, setSessionSearch] = useState('')
  const [pluginSearch, setPluginSearch] = useState('')
  const [skillSearch, setSkillSearch] = useState('')
  const [skillFilter, setSkillFilter] = useState<'all' | 'callable' | 'internal'>('all')
  // '' means "provider default": the backend resolves no override to the
  // active provider's default model. Never a display string — the backend
  // must not have to know the UI's wording.
  const [currentModel, setCurrentModel] = useState('')
  const [currentProvider, setCurrentProvider] = useState('')
  const [permissionLevel, setPermissionLevel] = useState('ask')
  const [sandboxMode, setSandboxMode] = useState('read_all')
  const [connected, setConnected] = useState(false)
  const [isStreaming, setIsStreaming] = useState(false)
  // True between the user asking to stop and the turn actually unwinding.
  // The abort is cooperative, so this cannot be derived from `isStreaming`.
  const [interrupting, setInterrupting] = useState(false)
  const [activity, setActivity] = useState('')
  const [loadingSessions, setLoadingSessions] = useState(false)
  const [loadingView, setLoadingView] = useState(false)
  const [creatingSession, setCreatingSession] = useState(false)
  const [collapsed, setCollapsed] = useState(() => window.innerWidth <= 768)
  const [searchOpen, setSearchOpen] = useState(false)
  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false)
  const [paletteQuery, setPaletteQuery] = useState('')
  const [commandIndex, setCommandIndex] = useState(0)
  // Esc only hides the inline command popover for the current input; typing
  // again re-opens it. Without this state, closing the popover would require
  // destroying the user's draft.
  const [commandDismissed, setCommandDismissed] = useState(false)
  // Whether the highlight was moved by the user (arrow keys / hover) rather
  // than merely defaulting to the first suggestion. A bare "/" must not send
  // that default, but an explicitly chosen entry should still win.
  const [commandIndexPinned, setCommandIndexPinned] = useState(false)
  const [expandedTraces, setExpandedTraces] = useState<Record<string, boolean>>({})
  const [hoveredTurn, setHoveredTurn] = useState<{ id: string; top: number } | null>(null)
  const [hoveredTurnIndex, setHoveredTurnIndex] = useState<number | null>(null)
  const [settingsDirty, setSettingsDirty] = useState(false)
  // What the settings page offers, as /api/config delivered it. State rather
  // than a constant because it is the backend's list, not ours.
  const [thinkingEffortOptions, setThinkingEffortOptions] = useState(
    () => effortOptionsFrom(null),
  )
  const [sendShortcut, setSendShortcut] = useState<'enter' | 'ctrl-enter'>(
    () => (localStorage.getItem('send_shortcut') === 'ctrl-enter' ? 'ctrl-enter' : 'enter'),
  )
  const sendShortcutLabel = sendShortcut === 'ctrl-enter' ? 'Ctrl/Cmd + Enter' : 'Enter'
  const [sessionState, setSessionState] = useState<SessionState | null>(null)
  const [resumingTaskId, setResumingTaskId] = useState<string | null>(null)
  // Guards the task-guidance buttons against a double click landing two
  // decisions (and two queued interjections) before React re-renders.
  const taskActionRef = useRef(false)
  // Interrupt is cooperative, so the turn can outlive the request by seconds.
  // These drive an app-side retry instead of making the user re-click, and are
  // torn down the moment the turn ends.
  const interruptTimersRef = useRef<number[]>([])
  const interruptAttemptsRef = useRef(0)
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([])
  const [form] = Form.useForm()
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const activeProviderName = Form.useWatch('active_provider', form)
  const wsRef = useRef<WebSocket | null>(null)
  // Keep the selected model available to WebSocket callbacks without making
  // the socket reconnect every time the dropdown changes.
  const currentModelRef = useRef(currentModel)
  const activeSessionRef = useRef<string | null>(null)
  const pendingSendRef = useRef<string | null>(null)
  const pendingMessageIdRef = useRef<string | null>(null)
  const pendingModelRef = useRef<string | null>(null)
  const queuedMessagesRef = useRef<QueuedMessage[]>([])
  // How many messages this client has put on the wire. A REST snapshot that
  // started before the latest send can be answered before the server applied
  // that message, so its "idle" describes a moment that predates the turn.
  const messageSendSeqRef = useRef(0)
  // Whether the previous state snapshot already reported idle. See
  // applyStreamingSnapshot for why one idle reading is not enough.
  const idleSnapshotSeenRef = useRef(false)
  const streamIdRef = useRef<string | null>(null)
  const messagesRef = useRef<Message[]>([])
  const loadMessagesRequestRef = useRef(0)
  const chatScrollRef = useRef<HTMLDivElement | null>(null)
  const followChatRef = useRef(true)
  const commandItemRefs = useRef<Record<string, HTMLButtonElement | null>>({})
  const turnRefs = useRef<Record<string, HTMLDivElement | null>>({})
  const conversationRailRef = useRef<HTMLDivElement | null>(null)
  const conversationMarkerRefs = useRef<Record<string, HTMLButtonElement | null>>({})
  const hoverClearTimerRef = useRef<number | null>(null)
  const idRef = useRef(0)
  // The token lives in state, not in a `localStorage.getItem` read on every
  // render, because everything derived from it -- the auth headers, the file
  // and stream links -- has to be rebuilt when it changes.  Reading storage
  // during render cannot announce a change, so the old code reloaded the whole
  // page to make the new token visible, which threw away the conversation.
  const [token, setToken] = useState(() => localStorage.getItem('agent_token') || '')
  // What the box currently shows.  Separate from ``token`` so that half-typed
  // credentials are not sent as headers before the user has finished: the
  // applied token only moves when they press save.
  const [tokenDraft, setTokenDraft] = useState(token)
  const tokenDirty = tokenDraft.trim() !== token

  const scrollChatToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    const container = chatScrollRef.current
    if (!container) return
    followChatRef.current = true
    container.scrollTo({ top: container.scrollHeight, behavior })
  }, [])

  const handleChatScroll = useCallback(() => {
    const container = chatScrollRef.current
    if (!container) return
    // Keep following only while the reader is already close to the bottom.
    // A generous threshold accounts for the composer and touchpad inertia.
    const distanceToBottom = container.scrollHeight - container.scrollTop - container.clientHeight
    const nearBottom = distanceToBottom <= 96
    followChatRef.current = nearBottom
  }, [])

  useEffect(() => {
    currentModelRef.current = currentModel
  }, [currentModel])

  const makeId = useCallback(() => {
    idRef.current += 1
    return `msg-${Date.now()}-${idRef.current}`
  }, [])

  useEffect(() => {
    document.documentElement.dataset.theme = themeMode
    document.body.dataset.theme = themeMode
    document.body.style.background = ''
  }, [themeMode])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setCommandPaletteOpen(true)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  useEffect(() => {
    const collapseOnMobile = () => {
      if (window.innerWidth <= 768) setCollapsed(true)
    }
    window.addEventListener('resize', collapseOnMobile)
    return () => window.removeEventListener('resize', collapseOnMobile)
  }, [])

  useEffect(() => {
    // Streaming updates can arrive many times per second. Never enqueue a
    // smooth animation for each chunk; follow instantly only when the user is
    // already at the bottom. Once they scroll up, preserve their reading
    // position until they explicitly return to the bottom.
    if (!followChatRef.current) {
      return
    }
    const frame = requestAnimationFrame(() => scrollChatToBottom('auto'))
    return () => cancelAnimationFrame(frame)
  }, [messages, scrollChatToBottom])

  const apiHeaders = useCallback((): Record<string, string> => {
    const t = token
    return t ? { Authorization: `Bearer ${t}`, 'X-Auth-Token': t } : {}
  }, [token])

  const api = useCallback(
    async (path: string, options: RequestInit = {}) => {
      const resp = await fetch(path, {
        ...options,
        headers: {
          ...(options.headers as Record<string, string> || {}),
          ...apiHeaders(),
        },
      })
      if (resp.status === 401) {
        messageApi.error('鉴权失败：请检查 auth_token')
        throw new Error('unauthorized')
      }
      if (!resp.ok) {
        let msg = '请求失败'
        try {
          const body = await resp.json()
          msg = body.error || msg
        } catch {
          // Ignore non-JSON error responses.
        }
        messageApi.error(msg)
        throw new Error(msg)
      }
      return resp
    },
    [apiHeaders, messageApi],
  )

  /** A refresh nobody asked for: same request, but a failure goes to the
   *  caller instead of to the user.
   *
   * `api` reports every failure with a toast, which is right when a person has
   * just clicked something and wrong when a timer did it. A gateway that went
   * away would otherwise produce one toast per tick, and -- worse -- the caller
   * would never find out that the numbers on screen had stopped being current.
   */
  const refreshJson = useCallback(async (path: string) => {
    try {
      const resp = await fetch(path, { headers: apiHeaders() })
      if (!resp.ok) return null
      return await resp.json()
    } catch {
      return null
    }
  }, [apiHeaders])

  const loadSessions = useCallback(async () => {
    try {
      setLoadingSessions(true)
      const resp = await api('/api/sessions')
      const data = await resp.json()
      const nextSessions = data.sessions || []
      setSessions(nextSessions)
      setSelectedSessionIds(current =>
        current.filter(id => nextSessions.some((item: SessionInfo) => item.session_id === id)),
      )
      return nextSessions as SessionInfo[]
    } catch {
      // API errors are surfaced by the shared request helper.
      return [] as SessionInfo[]
    } finally {
      setLoadingSessions(false)
    }
  }, [api])

  const loadSessionPermissions = useCallback(async (sid: string) => {
    try {
      const resp = await api(`/api/sessions/${encodeURIComponent(sid)}/permissions`)
      const data = await resp.json()
      setPermissionLevel(data.level || 'ask')
      setSandboxMode(data.sandbox || 'read_all')
    } catch {
      setPermissionLevel('ask')
      setSandboxMode('read_all')
    }
  }, [api])

  // Both REST reads (``loadMessages`` and ``loadSessionState``) report whether
  // a turn is running, and both used to write that straight onto isStreaming.
  // The write is only safe when the answer is "a turn is running": that is
  // what brings the stop button back when a session that is already working is
  // reopened. The other direction can be stale -- a fetch started before the
  // latest send can be answered before the server applied that message, and is
  // then read as "nothing is running". Taking that at face value is how the
  // stop button vanished right after the first message of a brand new session:
  // the socket had only just opened, so the snapshot the send itself triggered
  // was answered while the server still had nothing to report.
  //
  // So an idle snapshot retires the turn only when it cannot be stale -- no
  // send since it started -- and only after a second consecutive idle reading.
  // Two readings cannot both predate the same send, whereas one can.
  const applyStreamingSnapshot = useCallback(
    (turnRunning: boolean, sendSeq: number) => {
      if (turnRunning) {
        idleSnapshotSeenRef.current = false
        setIsStreaming(true)
        return
      }
      if (sendSeq !== messageSendSeqRef.current) return
      if (idleSnapshotSeenRef.current) {
        setIsStreaming(false)
        return
      }
      idleSnapshotSeenRef.current = true
    },
    [],
  )

  const loadMessages = useCallback(
    async (sid: string) => {
      const requestId = ++loadMessagesRequestRef.current
      const sendSeq = messageSendSeqRef.current
      try {
        const stateRequest = api(`/api/sessions/${encodeURIComponent(sid)}/state`)
          .then(resp => resp.json() as Promise<SessionState>)
          .catch(() => null)
        const [resp, state] = await Promise.all([
          api(`/api/sessions/${encodeURIComponent(sid)}/messages`),
          stateRequest,
        ])
        const data = await resp.json()
        if (
          requestId !== loadMessagesRequestRef.current ||
          activeSessionRef.current !== sid
        ) {
          return
        }
        const loaded = (data.messages || []).map(
          (item: any): Message => {
            // The backend only knows the path; the owner (this session) is
            // attached here so the gateway can authorise the read.
            const link = withFileSession(item.link || '', sid, token)
            return {
              id: makeId(),
              role: (item.role as MessageRole) || 'assistant',
              content: item.content || '',
              link,
              tool: item.tool,
              toolState: item.toolState as ToolState | undefined,
              attachments: Array.isArray(item.attachments) ? item.attachments : undefined,
            }
          },
        )
        const current = messagesRef.current
        const hasLiveTransient = current.some(item =>
          item.streaming ||
          (item.role === 'tool' && item.toolState === 'running') ||
          (item.role === 'subagent' && !!item.subagent?.running),
        )
        // A socket event may be newer than the state snapshot when a message
        // is submitted immediately after selecting the session.
        const operationActive =
          String(state?.operation_state || 'idle') !== 'idle' || hasLiveTransient
        let merged = [...loaded]
        if (operationActive) {
          // Only socket events received after cache sanitisation are eligible
          // here. Keep the latest reply snapshot and reconcile running tools
          // with their durable event projection.
          const latestStream = [...current]
            .reverse()
            .find(item => item.role === 'assistant' && item.streaming)
          const runningTools = current.filter(
            item => item.role === 'tool' && item.toolState === 'running',
          )
          // Sub-agent notes are socket-only; the server never persists them,
          // so a state refresh mid-batch would erase a running note.
          const runningSubagents = current.filter(
            item => item.role === 'subagent' && !!item.subagent?.running,
          )
          for (const tool of runningTools) {
            const durableIndex = merged.findIndex(item =>
              item.role === 'tool' &&
              item.toolState === 'running' &&
              item.tool === tool.tool,
            )
            if (durableIndex >= 0) merged[durableIndex] = tool
            else merged.push(tool)
          }
          merged.push(...runningSubagents)
          if (latestStream) merged.push(latestStream)
        }
        messagesRef.current = merged
        setMessages(merged)
        if (state) setSessionState(state)
        else setSessionState(null)
        applyStreamingSnapshot(operationActive, sendSeq)
      } catch {
        if (
          requestId !== loadMessagesRequestRef.current ||
          activeSessionRef.current !== sid
        ) {
          return
        }
        messagesRef.current = []
        setMessages([])
      }
    },
    [api, applyStreamingSnapshot, makeId, token],
  )

  const loadSessionState = useCallback(async (sid: string) => {
    const sendSeq = messageSendSeqRef.current
    try {
      const resp = await api(`/api/sessions/${encodeURIComponent(sid)}/state`)
      const data = (await resp.json()) as SessionState
      if (activeSessionRef.current === sid) {
        setSessionState(data)
        setResumingTaskId(previous => {
          if (!previous) return null
          const task = data.task
          // Defaulting to "show the card again" is what made the prompt come
          // back after the user answered it: a refresh that briefly reports no
          // task, or a task whose id has not been assigned yet, is not evidence
          // that a new interruption happened. Only a genuinely different task
          // in a non-interrupted state retires the suppression.
          if (!task) return previous
          const taskKey = task.task_id || task.active_goal || ''
          if (!taskKey) return previous
          const status = String(task.status || '').toLowerCase()
          return taskKey === previous && TASK_INTERRUPTED_STATUSES.has(status)
            ? previous
            : null
        })
        // Restoring a session while its agent is still working should bring
        // back the generating state (and stop button) even before the first
        // snapshot/chunk arrives on the newly opened socket.
        applyStreamingSnapshot(
          String(data.operation_state || 'idle') !== 'idle',
          sendSeq,
        )
      }
    } catch {
      if (activeSessionRef.current === sid) {
        setSessionState(null)
        setResumingTaskId(null)
        idleSnapshotSeenRef.current = false
        setIsStreaming(false)
      }
    }
  }, [api, applyStreamingSnapshot])

  const appendMessage = useCallback((next: Message) => {
    messagesRef.current = [...messagesRef.current, next]
    setMessages([...messagesRef.current])
  }, [])

  const updateMessage = useCallback((id: string, patch: Partial<Message>) => {
    messagesRef.current = messagesRef.current.map(item =>
      item.id === id ? { ...item, ...patch } : item,
    )
    setMessages([...messagesRef.current])
  }, [])

  const connectWs = useCallback(
    (sid: string) => {
      if (wsRef.current) {
        try {
          wsRef.current.close()
        } catch {
          // Ignore close errors on an already closed socket.
        }
      }
      setConnected(false)
      setActivity('正在连接…')
      setIsStreaming(false)
      streamIdRef.current = null
      const t = token
      const qs = t ? `?token=${encodeURIComponent(t)}` : ''
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      const ws = new WebSocket(
        `${proto}://${location.host}/api/sessions/${sid}/stream${qs}`,
      )
      wsRef.current = ws

      ws.onopen = () => {
        if (wsRef.current !== ws || activeSessionRef.current !== sid) return
        setConnected(true)
        setActivity('')
        const pending = pendingSendRef.current
        if (pending) {
          pendingSendRef.current = null
          setIsStreaming(true)
          // This send starts a turn the socket is about to narrate. Any state
          // fetch already in flight predates it and must not retire it later.
          messageSendSeqRef.current += 1
          idleSnapshotSeenRef.current = false
          ws.send(JSON.stringify({
            type: 'message',
            message_id: pendingMessageIdRef.current || undefined,
            text: pending,
            model: pendingModelRef.current,
          }))
          pendingMessageIdRef.current = null
          pendingModelRef.current = null
        }
      }

      ws.onclose = () => {
        if (wsRef.current !== ws || activeSessionRef.current !== sid) return
        setConnected(false)
        setResumingTaskId(null)
        setIsStreaming(false)
        setActivity('连接已断开')
      }

      ws.onerror = () => {
        if (wsRef.current !== ws || activeSessionRef.current !== sid) return
        setConnected(false)
        setResumingTaskId(null)
        setIsStreaming(false)
        setActivity('连接异常')
      }

      ws.onmessage = event => {
        // A closed socket can still deliver buffered events after a session
        // switch. Ignore anything that does not belong to the active socket.
        if (wsRef.current !== ws || activeSessionRef.current !== sid) return
        let evt: any
        try {
          evt = JSON.parse(event.data)
        } catch {
          return
        }

        // Thinking arrives before the first answer token, so this is often what
        // creates the assistant row — the same row `stream_chunk` will then
        // fill. It does not touch `content`: the note is not the reply.
        if (evt.type === 'reasoning_chunk') {
          const chunk = String(evt.chunk || '')
          if (!chunk) return
          const existingId = streamIdRef.current
          if (!existingId) {
            const id = makeId()
            streamIdRef.current = id
            appendMessage({ id, role: 'assistant', content: '', streaming: true })
          }
          const current = messagesRef.current.find(
            item => item.id === streamIdRef.current,
          )
          if (current) {
            updateMessage(current.id, {
              reasoning: (current.reasoning || '') + chunk,
            })
          }
          return
        }

        if (evt.type === 'stream_chunk') {
          const existingId = streamIdRef.current
          if (!existingId) {
            const id = makeId()
            streamIdRef.current = id
            appendMessage({ id, role: 'assistant', content: '', streaming: true })
          }
          const current = messagesRef.current.find(
            item => item.id === streamIdRef.current,
          )
          if (current) {
            updateMessage(current.id, {
              content: current.content + (evt.chunk || ''),
            })
          }
          return
        }

        if (evt.type === 'stream_snapshot') {
          const text = String(evt.text || '')
          // A tab that comes back mid-turn gets the thinking too, so the note
          // does not empty itself under the reader's cursor.
          const reasoning = typeof evt.reasoning === 'string' ? evt.reasoning : undefined
          const existingId = streamIdRef.current
          if (existingId) {
            updateMessage(existingId, {
              content: text,
              streaming: true,
              ...(reasoning !== undefined ? { reasoning } : {}),
            })
          } else {
            const id = makeId()
            streamIdRef.current = id
            appendMessage({
              id,
              role: 'assistant',
              content: text,
              streaming: true,
              ...(reasoning !== undefined ? { reasoning } : {}),
            })
          }
          setIsStreaming(true)
          setActivity('正在生成…')
          return
        }

        if (evt.type === 'turn_complete') {
          if (streamIdRef.current) {
            updateMessage(streamIdRef.current, {
              content: evt.text || messagesRef.current.find(i => i.id === streamIdRef.current)?.content || '',
              streaming: false,
            })
            streamIdRef.current = null
          }
          setIsStreaming(false)
          setActivity('')
          // The turn is over, so any prompt still on screen was answered or
          // timed out server-side; leaving it up would offer dead buttons.
          setConfirmReq(null)
          // A batch that never emitted ``batch_finished`` (interrupted, or an
          // older server) must not keep spinning forever.
          messagesRef.current = sealSubAgentNotes(messagesRef.current)
          setMessages([...messagesRef.current])
          loadSessions()
          loadSessionState(sid)
          const nextQueued = queuedMessagesRef.current.shift()
          if (nextQueued) {
            setQueuedMessages([...queuedMessagesRef.current])
            updateMessage(nextQueued.id, { queued: false })
          }
          return
        }

        if (evt.type === 'tool_start') {
          setActivity(evt.name ? `正在执行 ${evt.name}` : '正在执行工具')
          appendMessage({
            id: makeId(),
            role: 'tool',
            content: '',
            tool: evt.name || 'tool',
            toolState: 'running',
          })
          return
        }

        if (evt.type === 'tool_end') {
          const target = [...messagesRef.current]
            .reverse()
            .find(item => item.role === 'tool' && item.tool === evt.name && item.toolState === 'running')
          if (target) {
            updateMessage(target.id, {
              toolState: 'done',
              content: evt.result ? truncate(String(evt.result), 220) : '',
            })
          }
          return
        }

        if (evt.type === 'tool_progress') {
          const target = [...messagesRef.current]
            .reverse()
            .find(item => item.role === 'tool' && item.tool === evt.name && item.toolState === 'running')
          if (target) {
            const progress =
              typeof evt.progress === 'string'
                ? evt.progress
                : JSON.stringify(evt.progress)
            updateMessage(target.id, { content: truncate(progress, 180) })
            setActivity(`${evt.name || '工具'} · ${truncate(progress, 80)}`)
          }
          return
        }

        if (evt.type === 'tool_blocked') {
          const target = [...messagesRef.current]
            .reverse()
            .find(item => item.role === 'tool' && item.tool === evt.name && item.toolState === 'running')
          if (target) {
            updateMessage(target.id, {
              toolState: 'blocked',
              content: evt.reason || '操作被阻止',
            })
          }
          return
        }

        if (evt.type === 'status' || evt.type === 'info') {
          const text =
            evt.text ||
            (typeof evt.content === 'string'
              ? evt.content
              : JSON.stringify(evt.content))
          // The coordinator acknowledges messages submitted while a turn is
          // active with a queue status.  Remove the optimistic local queue
          // marker as soon as that acknowledgement arrives; otherwise an
          // interjection (which is consumed by the current turn) can remain
          // stuck as "排队中" forever after a single turn_complete event.
          if (/queued|排队/i.test(text) && queuedMessagesRef.current.length > 0) {
            const queued = queuedMessagesRef.current.shift()
            setQueuedMessages([...queuedMessagesRef.current])
            if (queued) updateMessage(queued.id, { queued: false })
          }
          setActivity(truncate(text, 90))
          appendMessage({ id: makeId(), role: 'command', content: text })
          return
        }

        if (evt.type === 'notification') {
          appendMessage({
            id: makeId(),
            role: 'command',
            content: `**${evt.title || '通知'}**${evt.body ? `\n${evt.body}` : ''}`,
          })
          return
        }

        if (evt.type === 'attachment') {
          const t = token
          const link = fileHref(evt.path, sid, t)
          let currentTurnStart = -1
          for (let index = messagesRef.current.length - 1; index >= 0; index -= 1) {
            if (messagesRef.current[index].role === 'user') {
              currentTurnStart = index
              break
            }
          }
          const alreadyShown = messagesRef.current
            .slice(currentTurnStart + 1)
            .some(item => item.role === 'tool' && item.link === link)
          if (alreadyShown) return
          appendMessage({
            id: makeId(),
            role: 'tool',
            content: `${evt.name || evt.path}`,
            tool: 'attachment',
            link,
          })
          return
        }

        if (evt.type === 'subagent_event') {
          // Update the turn's open note in place; only start a new one once the
          // previous batch has reported itself finished.
          const now = Date.now()
          const open = findOpenSubAgentNote(messagesRef.current)
          if (open && open.subagent) {
            updateMessage(open.id, {
              subagent: foldSubAgentEvent(open.subagent, evt, now),
            })
          } else {
            appendMessage({
              id: makeId(),
              role: 'subagent',
              content: '',
              subagent: foldSubAgentEvent(newSubAgentNote(now), evt, now),
            })
          }
          return
        }

        if (evt.type === 'heartbeat') {
          if (evt.current_op) {
            setActivity(evt.op_detail || evt.current_op)
          }
          return
        }

        if (evt.type === 'workspace_changed') {
          setSessionState(prev => prev ? {
            ...prev,
            workspace_root: evt.workspace_root || prev.workspace_root,
            workspace_status: evt.status || 'ready',
            workspace_exists: true,
            workspace_read: !!evt.workspace_read,
            workspace_write: !!evt.workspace_write,
          } : prev)
          setActivity('项目文件夹已更新')
          return
        }

        if (evt.type === 'error') {
          appendMessage({
            id: makeId(),
            role: 'error',
            content: evt.error || '发生错误',
          })
          setResumingTaskId(null)
          setIsStreaming(false)
          // The turn is over, so the status line must not keep narrating work
          // that stopped. The row it was streaming into has to be told the same
          // thing: a turn that failed while thinking would otherwise leave its
          // typing dots and its thinking note pulsing for work nobody is doing.
          if (streamIdRef.current) {
            updateMessage(streamIdRef.current, { streaming: false })
          }
          setActivity('')
          setConfirmReq(null)
          messagesRef.current = sealSubAgentNotes(messagesRef.current)
          setMessages([...messagesRef.current])
          return
        }

        if (evt.type === 'confirm_request') {
          const timeout = Number(evt.timeout_seconds) > 0 ? Number(evt.timeout_seconds) : 120
          confirmDeadlineRef.current = Date.now() + timeout * 1000
          setConfirmRemaining(timeout)
          setConfirmDetailOpen(false)
          setConfirmReq(evt as ConfirmRequest)
        }
      }
    },
    [appendMessage, loadSessionState, loadSessions, makeId, token, updateMessage],
  )

  const selectSession = useCallback(
    (sid: string) => {
      setPendingDeleteSessionId(null)
      followChatRef.current = true
      queuedMessagesRef.current = []
      setQueuedMessages([])
      setSessionState(null)
      activeSessionRef.current = sid
      setActiveSession(sid)
      setView('chat')
      streamIdRef.current = null
      setIsStreaming(false)
      setActivity('')
      // Restore the previous conversation instantly from the local cache, then
      // let the server reconcile in the background so the view never blanks out.
      const cached = localStorage.getItem(`chat_messages:${sid}`)
      if (cached) {
        try {
          const arr = JSON.parse(cached) as Message[]
          if (Array.isArray(arr)) {
            // Streaming and running-tool rows are snapshots, not history.
            // A reconnecting socket will restore them if this session is
            // genuinely still active.
            const durableCache = arr
              .filter(item =>
                !item.streaming &&
                item.role !== 'subagent' &&
                !(item.role === 'tool' && item.toolState === 'running'),
              )
              // Cache entries can predate session-scoped file links.
              .map(item => (item.link ? { ...item, link: withFileSession(item.link, sid, token) } : item))
            messagesRef.current = durableCache
            setMessages(durableCache)
          } else {
            messagesRef.current = []
            setMessages([])
          }
        } catch {
          messagesRef.current = []
          setMessages([])
        }
      } else {
        messagesRef.current = []
        setMessages([])
      }
      loadMessages(sid)
      loadSessionPermissions(sid)
    },
    [loadMessages, loadSessionPermissions, token],
  )

  useEffect(() => {
    let cancelled = false
    // Read the remembered session before awaiting the fetch: the persist
    // effect below clears the key while activeSession is still null.
    const stored = localStorage.getItem('agent_active_session')
    const restore = async () => {
      // A browser refresh used to dump the user on the welcome screen even
      // though their conversation was still on the server. Re-open the last
      // session when it still exists; otherwise start clean.
      const list = await loadSessions()
      if (cancelled) return
      if (stored && list.some(item => item.session_id === stored)) {
        selectSession(stored)
      }
    }
    restore()
    api('/api/commands')
      .then(r => r.json())
      .then(data => setCommands(data.commands || []))
      .catch(() => {})
    return () => { cancelled = true }
  }, [api, loadSessions, selectSession])

  // Remember the open conversation so a refresh can return to it. Deleting the
  // active session clears the key and the next load starts fresh.
  useEffect(() => {
    if (activeSession) localStorage.setItem('agent_active_session', activeSession)
    else localStorage.removeItem('agent_active_session')
  }, [activeSession])

  useEffect(() => {
    if (activeSession) {
      connectWs(activeSession)
    }
    // Only reconnect when the active session changes. connectWs is stable enough
    // for the lifecycle we need here.
  }, [activeSession, connectWs])

  // Working-state projections and coordinator mailboxes are updated while a
  // turn is running. Polling this lightweight endpoint keeps task guidance and
  // queue counts useful during long tool runs, including messages submitted
  // from another browser tab.
  useEffect(() => {
    if (!activeSession) return
    const refresh = () => loadSessionState(activeSession)
    refresh()
    if (!isStreaming) return
    const timer = window.setInterval(refresh, 1200)
    return () => window.clearInterval(timer)
  }, [activeSession, isStreaming, loadSessionState])

  const uploadPendingAttachments = async (): Promise<AttachmentInfo[]> => {
    if (!activeSession || !pendingAttachments.length) return pendingAttachments
    return pendingAttachments
  }

  const sendMessage = async (overrideText?: string) => {
    const text = (overrideText ?? input).trim()
    if ((!text && pendingAttachments.length === 0) || creatingSession) return

    // A newly submitted turn is an explicit request to see the response.
    // Re-enable bottom following even if the reader had previously scrolled
    // up to inspect older messages.
    followChatRef.current = true
    const queueWhileBusy =
      isStreaming && !/^\/(?:cancel|now)(?:\s|$)/i.test(text)
    const messageId = makeId()
    const attachments = await uploadPendingAttachments()
    appendMessage({ id: messageId, role: 'user', content: text, queued: queueWhileBusy, attachments })
    setInput('')
    setPendingAttachments([])
    setActivity('等待模型响应')

    if (queueWhileBusy) {
      const queued = {
        id: messageId,
        text,
        model: currentModelRef.current,
      }
      queuedMessagesRef.current = [...queuedMessagesRef.current, queued]
      setQueuedMessages([...queuedMessagesRef.current])
      setActivity(`已排队 ${queuedMessagesRef.current.length} 条消息`)
      // The server's restart queue grew, which is what the session list's
      // 排队中 badge reads -- without this the badge appears only after some
      // later list refresh happens to run.
      void loadSessions()
    }

    if (!activeSession) {
      try {
        setCreatingSession(true)
        pendingSendRef.current = text
        pendingMessageIdRef.current = messageId
        pendingModelRef.current = currentModelRef.current
        const resp = await api('/api/sessions', { method: 'POST' })
        const data = await resp.json()
        const sid = data.session_id as string
        activeSessionRef.current = sid
        await loadSessions()
        setView('chat')
        setActiveSession(sid)
        loadSessionState(sid)
      } catch {
        pendingSendRef.current = null
        pendingMessageIdRef.current = null
        pendingModelRef.current = null
        setActivity('')
      } finally {
        setCreatingSession(false)
      }
      return
    }

    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      setIsStreaming(true)
      // The session list shows what each session is doing; a turn just began,
      // so the badge beside this session is now stale.  Not awaited: the list
      // is a side detail of sending, and waiting for it would delay the turn.
      void loadSessions()
      // Bumped before the send, so a state fetch already in flight predates
      // this turn and is barred from retiring it when its answer arrives.
      messageSendSeqRef.current += 1
      idleSnapshotSeenRef.current = false
      ws.send(JSON.stringify({
        type: 'message',
        // The id travels with the message so the queue entry the server keeps
        // behind the running turn can still be named later — that is what
        // makes taking one message back possible.
        message_id: messageId,
        text,
        model: currentModelRef.current,
        attachments,
      }))
      return
    }

    messageApi.warning('连接已断开，正在重新连接…')
    connectWs(activeSession)
  }

  // What the queue really holds: the server's own list, plus anything just
  // submitted that the server has not echoed back yet. Server entries win on
  // id, so a message is never shown twice, and an entry that only exists
  // locally is not offered for withdrawal — it may not have reached the queue
  // yet, and reporting it as taken back would be a promise we cannot keep.
  const queueView = (() => {
    const items = sessionState?.queue?.items ?? []
    const seen = new Set(items.map(item => item.id))
    return [
      ...items.map(item => ({
        id: item.id,
        text: item.text,
        withdrawable: Boolean(item.id),
      })),
      ...queuedMessages
        .filter(item => !seen.has(item.id))
        .map(item => ({ id: item.id, text: item.text, withdrawable: false })),
    ]
  })()

  const dropQueuedLocally = (messageId: string) => {
    queuedMessagesRef.current = queuedMessagesRef.current.filter(
      item => item.id !== messageId,
    )
    setQueuedMessages([...queuedMessagesRef.current])
    messagesRef.current = messagesRef.current.filter(item => item.id !== messageId)
    setMessages([...messagesRef.current])
  }

  // Returns the withdrawn text, or '' when the server no longer had it.
  const withdrawOneQueued = async (messageId: string): Promise<string> => {
    const sid = activeSessionRef.current
    if (!sid) return ''
    try {
      const response = await api(
        `/api/sessions/${encodeURIComponent(sid)}/queue/${encodeURIComponent(messageId)}`,
        { method: 'DELETE' },
      )
      const data = await response.json().catch(() => ({}))
      if (data?.withdrawn === false) return ''
      dropQueuedLocally(messageId)
      return String(data?.text || '')
    } catch {
      messageApi.warning('撤回失败，请重试')
      return ''
    }
  }

  const withdrawQueuedMessages = async (messageIds: string[]) => {
    const sid = activeSessionRef.current
    if (!sid) return
    const restored: string[] = []
    // Sequential on purpose: each withdrawal has to settle before the next,
    // and the composer is only written once at the end. Restoring per call
    // would let the last write win and silently drop the other messages.
    for (const messageId of messageIds) {
      const text = await withdrawOneQueued(messageId)
      if (text) restored.push(text)
    }
    if (restored.length < messageIds.length) {
      messageApi.warning('有消息已经开始处理，撤不回来了')
    }
    if (restored.length) {
      // Hand the text back to the composer instead of editing it in place:
      // once a message is on its way the honest undo is to take it out of the
      // queue, because anything the model already read cannot be unread.
      // Whatever is already typed in the composer is kept.
      const taken = restored.join('\n')
      setInput(input.trim() ? `${taken}\n${input}` : taken)
    }
    loadSessionState(sid)
  }

  const clearInterruptTimers = useCallback(() => {
    interruptTimersRef.current.forEach(clearTimeout)
    interruptTimersRef.current = []
  }, [])

  // Every way a turn can end -- turn_complete, an error, a session switch, a
  // dropped socket -- lands on `isStreaming === false`, so this is the one
  // place that retires the interrupt affordance. It can never outlive the turn
  // it was aimed at, and a stale disabled button cannot appear on the next one.
  useEffect(() => {
    if (isStreaming) return
    clearInterruptTimers()
    interruptAttemptsRef.current = 0
    setInterrupting(false)
  }, [isStreaming, clearInterruptTimers])

  const requestInterrupt = async (sid: string) => {
    interruptAttemptsRef.current += 1
    let accepted = false
    try {
      const response = await api(
        `/api/sessions/${encodeURIComponent(sid)}/cancel`,
        { method: 'POST' },
      )
      const payload = await response.json().catch(() => null)
      accepted = !!(payload && payload.cancelled)
    } catch {
      // A transport error proves nothing either way; the retry timer and the
      // eventual turn_complete still decide what the user is told.
    }
    if (sid !== activeSessionRef.current) return
    if (!accepted) {
      // The server reports no running turn for this session, so the local
      // "streaming" flag is stale. Re-derive it instead of leaving a stop
      // button that can never do anything.
      loadSessionState(sid)
    }
  }

  const stopStreaming = () => {
    // Ask the backend to cancel the running turn. The stream flow emits
    // `turn_complete` (with whatever partial text was generated), which resets
    // `isStreaming` and flips the send button back to "发送".
    if (!activeSession) return
    const sid = activeSession
    // Acknowledge the click immediately. Force-cancel aborts the in-flight
    // model request or child process, but the turn still has to unwind, and
    // during that window nothing used to change on screen -- which is what made
    // the button feel dead and get clicked over and over.
    clearInterruptTimers()
    interruptAttemptsRef.current = 0
    setInterrupting(true)
    setActivity('正在中断…')
    void requestInterrupt(sid)
    interruptTimersRef.current = [
      window.setTimeout(() => {
        if (activeSessionRef.current !== sid) return
        // Still running. Ask again rather than making the user do it.
        void requestInterrupt(sid)
        setActivity('中断中，正在等待当前步骤结束…')
      }, INTERRUPT_RETRY_MS),
      window.setTimeout(() => {
        if (activeSessionRef.current !== sid) return
        // Say what is actually happening and hand the button back, so the user
        // can decide whether to keep waiting or restart the gateway.
        setActivity('中断请求已发送，但当前步骤仍未结束')
        setInterrupting(false)
      }, INTERRUPT_STUCK_MS),
    ]
  }

  const dismissTaskGuidance = async () => {
    if (!activeSession || !sessionState?.task || taskActionRef.current) return
    const taskId = sessionState.task.task_id || ''
    taskActionRef.current = true
    try {
      await api(`/api/sessions/${encodeURIComponent(activeSession)}/task-guidance/dismiss`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(taskId ? { task_id: taskId } : {}),
      })
      setSessionState(previous => previous
        ? { ...previous, task: previous.task ? { ...previous.task, status: 'dismissed', next_action: '' } : previous.task }
        : previous)
    } catch {
      // The shared request helper reports the server error to the user.
    } finally {
      taskActionRef.current = false
    }
  }

  const continueTask = async () => {
    const task = sessionState?.task
    // A second click must not queue a second continuation for the same prompt.
    if (!task || taskActionRef.current) return
    taskActionRef.current = true
    setResumingTaskId(task.task_id || task.active_goal || '')
    try {
      await sendMessage('继续当前任务')
    } finally {
      taskActionRef.current = false
    }
  }

  const handleFilesSelected = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || [])
    event.target.value = ''
    if (!files.length) return
    let sessionId = activeSession
    if (!sessionId) {
      try {
        const created = await api('/api/sessions', { method: 'POST' })
        const data = await created.json()
        sessionId = data.session_id as string
        activeSessionRef.current = sessionId
        setActiveSession(sessionId)
        await loadSessions()
      } catch { return }
    }
    if (files.length + pendingAttachments.length > 12) {
      messageApi.warning('最多同时发送 12 个文件')
      return
    }
    const body = new FormData()
    files.forEach(file => body.append('files', file, file.name))
    try {
      const resp = await api(`/api/sessions/${encodeURIComponent(sessionId)}/attachments`, { method: 'POST', body })
      const data = await resp.json()
      setPendingAttachments(prev => [...prev, ...(data.attachments || [])])
    } catch {
      // api helper surfaces the error
    }
  }

  const pickWorkspace = async () => {
    if (!activeSession) {
      messageApi.info('请先选择或新建会话')
      return
    }
    try {
      const resp = await api(`/api/sessions/${encodeURIComponent(activeSession)}/workspace/pick`, { method: 'POST' })
      const data = await resp.json()
      if (data.cancelled) return
      await loadSessionState(activeSession)
      if (data.ok === false) {
        messageApi.warning(data.error || '项目文件夹切换失败')
      } else {
        messageApi.success(`项目文件夹已切换：${data.workspace_root}`)
      }
    } catch {
      // api helper already reports the error
    }
  }

  // Clear the prompt before sending: a double click on "允许" must not emit two
  // replies for one token, and the bar should not linger while the turn resumes.
  const sendConfirm = useCallback(
    (decision: ConfirmDecision) => {
      const request = confirmReq
      setConfirmReq(null)
      setConfirmDetailOpen(false)
      if (!request) return
      const socket = wsRef.current
      if (!socket || socket.readyState !== WebSocket.OPEN) return
      socket.send(
        JSON.stringify({
          type: 'confirm_response',
          decision,
          confirmation_token: request.confirmation_token || '',
        }),
      )
    },
    [confirmReq],
  )

  // Countdown to the server-side deadline, so "it just silently expired" can't
  // happen while the user is deciding.
  useEffect(() => {
    if (!confirmReq) {
      setConfirmRemaining(0)
      return
    }
    const tick = () => {
      setConfirmRemaining(
        Math.max(0, Math.ceil((confirmDeadlineRef.current - Date.now()) / 1000)),
      )
    }
    tick()
    const timer = window.setInterval(tick, 1000)
    return () => window.clearInterval(timer)
  }, [confirmReq])

  // Whether the command is actually clipped, measured from the rendered box.
  // A character-count guess gets this wrong the moment the text is mostly
  // CJK (one character is a full column wide, not a half) and would leave the
  // user staring at a command they cannot expand.
  useEffect(() => {
    if (!confirmReq) {
      setConfirmOverflowing(false)
      return
    }
    // Keep the toggle available while expanded, otherwise collapsing becomes
    // impossible as soon as the tall box stops overflowing.
    if (confirmDetailOpen) return
    const element = approvalCommandRef.current
    if (!element) {
      setConfirmOverflowing(false)
      return
    }
    const measure = () => setConfirmOverflowing(element.scrollHeight > element.clientHeight + 1)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [confirmReq, confirmDetailOpen])

  useEffect(() => {
    if (!confirmReq) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        sendConfirm('deny')
        return
      }
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        event.preventDefault()
        sendConfirm(event.shiftKey && confirmReq.allow_session ? 'allow_session' : 'allow_once')
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [confirmReq, sendConfirm])

  const loadPlugins = useCallback(async () => {
    try {
      setLoadingView(true)
      const resp = await api('/api/plugins')
      const data = await resp.json()
      setPlugins(data.plugins || [])
    } catch {
      // Handled by api helper.
    } finally {
      setLoadingView(false)
    }
  }, [api])

  const loadSkills = useCallback(async (silent = false) => {
    try {
      if (!silent) setLoadingView(true)
      const resp = await api('/api/skills')
      const data = await resp.json()
      setSkills(data.skills || [])
    } catch {
      // Handled by api helper.
    } finally {
      if (!silent) setLoadingView(false)
    }
  }, [api])

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

  const loadFeishuChats = useCallback(async () => {
    setFeishuChatsLoading(true)
    setFeishuChatsError('')
    // This is a network round-trip out to Feishu on the user's own app
    // credentials. Unbounded, a hung connection leaves the picker spinning with
    // nothing to click and no way to tell it apart from a slow success -- which
    // is exactly how it was reported. 15s is far past a healthy list call.
    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), 15_000)
    try {
      const resp = await api('/api/feishu/chats', { signal: controller.signal })
      const data = await resp.json()
      setFeishuChats(Array.isArray(data.chats) ? data.chats : [])
    } catch (error) {
      // Keep the reason on the form itself: "no permission" and "no config"
      // read identically as an empty dropdown, and the user cannot fix what
      // they cannot see.
      if (error instanceof Error && error.name === 'AbortError') {
        setFeishuChatsError('获取会话列表超时，请检查网络或飞书配置后点「重新获取」')
      } else {
        setFeishuChatsError(error instanceof Error ? error.message : '会话列表获取失败')
      }
    } finally {
      window.clearTimeout(timer)
      setFeishuChatsLoading(false)
      // Set last: this is what stops the effect from asking again. A failure
      // counts as "asked" too, or a broken config would loop instead of
      // settling on the error message with its 重新获取 button.
      setFeishuChatsLoaded(true)
    }
  }, [api])

  const sendFeishuTest = useCallback(async (chatId: string) => {
    setFeishuTesting(true)
    try {
      await api('/api/feishu/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: chatId }),
      })
      messageApi.success('测试消息已发送，请检查飞书会话')
    } catch {
      // The server message (missing config, wrong scope, chat not found)
      // already surfaced as a toast; there is nothing more specific to say.
    } finally {
      setFeishuTesting(false)
    }
  }, [api, messageApi])

  const pickDirectory = useCallback(async (apply: (path: string) => void) => {
    setPickingDirectory(true)
    try {
      // The native dialog lives as long as the user needs, so this request
      // simply waits; cancelling the dialog is a normal outcome, not an error.
      const resp = await api('/api/fs/pick-directory', { method: 'POST' })
      const data = await resp.json()
      if (!data.cancelled && typeof data.workspace_root === 'string' && data.workspace_root) {
        apply(data.workspace_root)
      }
    } catch {
      // Surfaced by the api helper.
    } finally {
      setPickingDirectory(false)
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

  // How often to ask the server again -- and, more to the point, that it is
  // asked at all.
  //
  // This used to be "poll every two seconds if the last response said a run was
  // in flight". The only thing that could ever change that answer was the poll
  // itself, so a page opened while nothing was running never started asking,
  // and a task that fired on its own schedule while the page sat open was never
  // seen. The page could only observe the runs it had started itself.
  //
  // The cadence now comes from `schedulerWatchful`, which is also why the timer
  // survives a poll: it used to depend on `schedules`, so every response tore
  // the interval down and rebuilt it, and the real period was the cadence plus
  // a render plus a round trip. A signal-triggered step has no clock, so no
  // predicate covers it -- which is why the idle cadence exists at all rather
  // than the timer stopping when nothing is imminent.
  useEffect(() => {
    if (view !== 'schedules') return
    const cadence = schedulerWatchful ? SCHEDULE_FAST_POLL_MS : SCHEDULE_IDLE_POLL_MS
    const refresh = () => {
      void loadSchedules(true)
      // The graph draws each step's liveness from the workflow payload, so a
      // running workflow watched from this tab has to refresh that too --
      // otherwise the steps sit still while the run behind them moves.
      if (automationTab === 'workflows') void loadWorkflows(true)
      if (scheduleDetailOpen && selectedSchedule) {
        void loadScheduleRuns(selectedSchedule.id, false, true)
      }
    }
    const timer = window.setInterval(refresh, cadence)
    return () => window.clearInterval(timer)
  }, [
    automationTab,
    loadScheduleRuns,
    loadSchedules,
    loadWorkflows,
    scheduleDetailOpen,
    schedulerWatchful,
    selectedSchedule,
    view,
  ])

  // A countdown that does not tick is a timestamp with extra words. One second
  // is worth it only while something is about to happen; the rest of the time
  // the label is in coarser units and ten is plenty. Both readers -- the next
  // run and the freshness line -- want the same clock, so there is one, and it
  // asks the same question the poll does.
  useEffect(() => {
    if (view !== 'schedules') return
    const tick = () => setClock(Date.now())
    tick()
    const timer = window.setInterval(tick, schedulerWatchful ? 1000 : 10_000)
    return () => window.clearInterval(timer)
  }, [schedulerWatchful, view])

  useEffect(() => {
    if (view !== 'schedules') return
    void loadSchedulerHealth()
    const timer = window.setInterval(loadSchedulerHealth, 10000)
    return () => window.clearInterval(timer)
  }, [loadSchedulerHealth, view])

  // Polled from every view, not just the schedules page: the badge exists so
  // that a failure is noticed by someone who is not looking at the page.
  useEffect(() => {
    void loadUnseenFailures()
    const timer = window.setInterval(loadUnseenFailures, 30000)
    return () => window.clearInterval(timer)
  }, [loadUnseenFailures])

  const loadSettings = useCallback(async () => {
    try {
      setLoadingView(true)
      const resp = await api('/api/config')
      const data = await resp.json()
      const cfg = data.config || {}
      setConfig(cfg)
      setConfigText(JSON.stringify(cfg, null, 2))
      setSettingsDirty(false)
      // The levels on offer come from the same response as the config, so
      // the page offers what this backend will validate -- a level added on
      // the server shows up here without a second copy of the list here.
      setThinkingEffortOptions(effortOptionsFrom(data.thinking_efforts))

      const providers = cfg.providers || {}
      const active = providers[cfg.active_provider] || {}
      setCurrentProvider(cfg.active_provider || '')
      // Only seed the composer's model when the user has not picked one:
      // reloading settings (opening the settings view, saving) must not
      // silently revert a per-turn model selection.
      setCurrentModel(prev => prev || active.default_model || '')
      form.setFieldsValue({
        active_provider: cfg.active_provider,
        model: active.default_model,
        max_tokens: active.max_tokens,
        thinking_effort: thinkingEffortOf(active),
        web_enabled: !!(cfg.channels?.web?.enabled),
        feishu_enabled: !!(cfg.channels?.feishu?.enabled),
      })
    } catch {
      // Handled by api helper.
    } finally {
      setLoadingView(false)
    }
  }, [api, form])

  useEffect(() => {
    loadSettings()
  }, [loadSettings])

  useEffect(() => {
    // Both lists load on entering the merged page: the counts on the tabs and
    // the page subtitle are about both, and switching tabs is not a data
    // event -- it is the same visit continuing.
    if (view === 'extensions') {
      loadPlugins()
      loadSkills()
    }
    if (view === 'schedules') {
      loadSchedules()
      loadWorkflows(true)
      loadSkills(true)
      loadSchedulerHealth()
      loadSignals()
    }
    if (view === 'settings') loadSettings()
  }, [view, loadPlugins, loadSkills, loadSchedules, loadWorkflows, loadSchedulerHealth, loadSignals, loadSettings])

  const createSession = async () => {
    try {
      setCreatingSession(true)
      const resp = await api('/api/sessions', { method: 'POST' })
      const data = await resp.json()
      activeSessionRef.current = data.session_id
      await loadSessions()
      selectSession(data.session_id)
    } catch {
      // Handled by api helper.
    } finally {
      setCreatingSession(false)
    }
  }

  const renameSession = (item: SessionInfo) => {
    let value = item.title || ''
    Modal.confirm({
      title: '重命名会话',
      content: (
        <Input
          defaultValue={value}
          autoFocus
          placeholder="输入会话标题"
          onChange={event => {
            value = event.target.value
          }}
        />
      ),
      okText: '保存',
      cancelText: '取消',
      onOk: async () => {
        await api(
          `/api/sessions/${encodeURIComponent(item.session_id)}`,
          {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: value.trim() }),
          },
        )
        messageApi.success('会话已重命名')
        loadSessions()
      },
    })
  }

  const revealSession = async (item: SessionInfo) => {
    try {
      const resp = await api(`/api/sessions/${encodeURIComponent(item.session_id)}/reveal`, {
        method: 'POST',
      })
      const data = await resp.json()
      messageApi.success(data.path ? '已在 Finder 中显示会话数据' : '已打开会话数据')
    } catch {
      // api() already surfaces the server error through the global message API.
    }
  }

  const deleteSession = async (item: SessionInfo) => {
    try {
      await api(`/api/sessions/${encodeURIComponent(item.session_id)}`, {
        method: 'DELETE',
      })
      messageApi.success('会话已删除')
      try {
        localStorage.removeItem(`chat_messages:${item.session_id}`)
        const raw = localStorage.getItem('chat_messages:index')
        const index = raw
          ? (JSON.parse(raw) as string[]).filter(id => id !== item.session_id)
          : []
        localStorage.setItem('chat_messages:index', JSON.stringify(index))
      } catch {
        // ignore storage errors
      }
      if (activeSession === item.session_id) {
        activeSessionRef.current = null
        setActiveSession(null)
        setMessages([])
        messagesRef.current = []
        streamIdRef.current = null
        queuedMessagesRef.current = []
        setQueuedMessages([])
        setSessionState(null)
      }
      setPendingDeleteSessionId(current => current === item.session_id ? null : current)
      await loadSessions()
    } catch {
      // Errors are surfaced by the shared request helper.
    }
  }

  // Dropdown menus are rendered through a React portal. Guard the session
  // container click as well as the menu itself so selecting an action cannot
  // bubble into selectSession() and immediately reset the pending state.
  // Takes a keyboard event as well as a mouse one: a row is activated with
  // Enter or Space as well as a click, and the guard below (do not act when the
  // event landed on the row's own dropdown or delete buttons) is the same
  // question either way, so it stays in one place rather than being restated
  // per call site.
  const handleSessionContainerClick = (
    event: React.MouseEvent<HTMLElement> | React.KeyboardEvent<HTMLElement>,
    sid: string,
  ) => {
    const target = event.target as HTMLElement | null
    if (
      target?.closest('.ant-dropdown') ||
      target?.closest('.ant-dropdown-trigger') ||
      target?.closest('.session-item-delete-actions')
    ) {
      return
    }
    selectSession(sid)
  }

  const deleteSelectedSessions = () => {
    const ids = selectedSessionIds.filter(id => sessions.some(item => item.session_id === id))
    if (!ids.length) return
    Modal.confirm({
      title: `删除选中的 ${ids.length} 个会话？`,
      content: '历史记录将被永久删除，此操作无法撤销。',
      okText: '批量删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          const resp = await api('/api/sessions', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_ids: ids }),
          })
          const data = await resp.json()
          for (const id of data.deleted || []) {
            try { localStorage.removeItem(`chat_messages:${id}`) } catch { /* ignore */ }
          }
          try {
            const raw = localStorage.getItem('chat_messages:index')
            const index = raw ? (JSON.parse(raw) as string[]).filter(id => !(data.deleted || []).includes(id)) : []
            localStorage.setItem('chat_messages:index', JSON.stringify(index))
          } catch { /* ignore */ }
          if (activeSession && (data.deleted || []).includes(activeSession)) {
            activeSessionRef.current = null
            setActiveSession(null)
            setMessages([])
            messagesRef.current = []
            streamIdRef.current = null
            queuedMessagesRef.current = []
            setQueuedMessages([])
            setSessionState(null)
          }
          setSelectedSessionIds([])
          await loadSessions()
          const failed = (data.failed || []).length
          messageApi[failed ? 'warning' : 'success'](
            failed ? `已删除 ${data.deleted?.length || 0} 个，${failed} 个失败` : `已删除 ${data.deleted?.length || 0} 个会话`,
          )
        } catch {
          // api() surfaces the server error.
        }
      },
    })
  }

  const updateSessionPermissions = async (patch: { level?: string; sandbox?: string }) => {
    if (!activeSession) {
      messageApi.info('请先选择或新建会话')
      return
    }
    try {
      const resp = await api(`/api/sessions/${encodeURIComponent(activeSession)}/permissions`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      })
      const data = await resp.json()
      setPermissionLevel(data.level || 'ask')
      setSandboxMode(data.sandbox || 'read_all')
      messageApi.success('当前会话权限已更新')
    } catch {
      // api() surfaces the server error.
    }
  }

  const permissionLabel = permissionLevel === 'full'
    ? '完全访问'
    : permissionLevel === 'high'
      ? '高权限'
      : permissionLevel === 'medium'
        ? '中权限'
        : '需确认'

  const togglePlugin = async (plugin: PluginInfo, enabled: boolean) => {
    setPlugins(prev =>
      prev.map(item => (item.name === plugin.name ? { ...item, enabled } : item)),
    )
    try {
      await api(`/api/plugins/${encodeURIComponent(plugin.name)}/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      })
      messageApi.success(enabled ? '插件已启用' : '插件已停用')
    } catch {
      setPlugins(prev =>
        prev.map(item =>
          item.name === plugin.name
            ? { ...item, enabled: !enabled }
            : item,
        ),
      )
    }
  }

  const confirmResourceDeletion = (
    resourceLabel: string,
    resourceName: string,
    onConfirm: () => Promise<void>,
  ) => {
    Modal.confirm({
      title: `删除${resourceLabel}“${resourceName}”？`,
      content: `此操作会永久删除该${resourceLabel}，且无法恢复。`,
      okText: '确认删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      centered: true,
      className: 'resource-delete-confirm',
      onOk: onConfirm,
    })
  }

  const deletePlugin = (plugin: PluginInfo) => {
    if (plugin.source !== 'user') return messageApi.info('内置插件不能删除')
    confirmResourceDeletion('插件', plugin.name, async () => {
      await api(`/api/plugins/${encodeURIComponent(plugin.name)}`, { method: 'DELETE' })
      setPlugins(prev => prev.filter(item => item.name !== plugin.name))
      messageApi.success('插件已删除')
    })
  }

  const deleteSkill = (skill: SkillInfo) => {
    if (skill.source !== 'user') return messageApi.info('内置技能不能删除')
    confirmResourceDeletion('技能', skill.name || skill.id, async () => {
      await api(`/api/skills/${encodeURIComponent(skill.id)}`, { method: 'DELETE' })
      setSkills(prev => prev.filter(item => item.id !== skill.id))
      messageApi.success('技能已删除')
    })
  }

  const toggleSkill = async (skill: SkillInfo, enabled: boolean) => {
    setSkills(prev =>
      prev.map(item => (item.id === skill.id ? { ...item, enabled } : item)),
    )
    try {
      await api(`/api/skills/${encodeURIComponent(skill.id)}/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      })
      messageApi.success(enabled ? '技能已启用' : '技能已停用')
    } catch {
      setSkills(prev =>
        prev.map(item => (item.id === skill.id ? { ...item, enabled: !enabled } : item)),
      )
    }
  }

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

  /** Fold the form's fields into a config object.
   *
   *  One definition, because two callers need it: saving, and the write-through
   *  that keeps the JSON box showing what saving would send.  When the two were
   *  computed separately the box disagreed with the form, and the form won at
   *  save time -- so an edit made in the box could be discarded without a word.
   */
  const mergeSettingsForm = (base: any, values: any) => {
    const cfg = { ...(base || {}) }
    const provider = values.active_provider
    cfg.active_provider = provider
    if (provider) {
      cfg.providers = { ...(cfg.providers || {}) }
      const providerCfg = { ...(cfg.providers[provider] || {}) }
      providerCfg.default_model = values.model
      providerCfg.max_tokens = values.max_tokens
      // Only ever one provider's thinking block, and only when a level was
      // actually chosen. Clearing the field means "no opinion" -- which is a
      // different request from any level, including 关闭 -- so the key is
      // removed rather than written empty.
      const effort = values.thinking_effort
      if (effort) {
        providerCfg.thinking = { ...(providerCfg.thinking || {}), effort }
      } else if (providerCfg.thinking) {
        const { effort: _cleared, ...rest } = providerCfg.thinking
        if (Object.keys(rest).length) providerCfg.thinking = rest
        else delete providerCfg.thinking
      }
      cfg.providers[provider] = providerCfg
    }
    cfg.channels = { ...(cfg.channels || {}) }
    cfg.channels.web = { ...(cfg.channels.web || {}), enabled: values.web_enabled }
    cfg.channels.feishu = {
      ...(cfg.channels.feishu || {}),
      enabled: values.feishu_enabled,
    }
    return cfg
  }

  const handleSettingsFormChange = (changed: any, all: any) => {
    let values = all
    // A model and a max_tokens belong to one provider, so choosing a different
    // provider has to change them.  Leaving the previous provider's model in
    // the box meant save wrote it onto the newly chosen provider -- a config
    // edit nobody made, in a provider nobody was looking at.
    if (changed && 'active_provider' in changed) {
      const provider = config?.providers?.[changed.active_provider] || {}
      values = {
        ...all,
        model: provider.default_model ?? '',
        max_tokens: provider.max_tokens ?? null,
        thinking_effort: thinkingEffortOf(provider),
      }
      form.setFieldsValue({
        model: values.model,
        max_tokens: values.max_tokens,
        thinking_effort: values.thinking_effort,
      })
    }
    setSettingsDirty(true)
    setConfigText(current => {
      try {
        return JSON.stringify(
          mergeSettingsForm(JSON.parse(current || '{}'), values),
          null,
          2,
        )
      } catch {
        // The box holds something unparseable, so the user is typing in it.
        // Rewriting would destroy that; saving is blocked while it stays
        // invalid, so nothing is lost behind their back either way.
        return current
      }
    })
  }

  const applyToken = () => {
    const next = tokenDraft.trim()
    localStorage.setItem('agent_token', next)
    // State, not a page reload: the headers and every link built from the
    // token are rebuilt from this, and the conversation survives.
    setToken(next)
    setTokenDraft(next)
    messageApi.success(next ? '令牌已保存' : '令牌已清除')
  }

  const saveSettings = async () => {
    try {
      const values = await form.validateFields()
      const cfg = mergeSettingsForm(JSON.parse(configText || '{}'), values)

      await api('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ config: cfg }),
      })
      messageApi.success('设置已保存')
      setSettingsDirty(false)
      loadSettings()
    } catch (error) {
      if (error instanceof SyntaxError) {
        messageApi.error('保存失败：JSON 格式有误')
      } else if (error instanceof Error && error.message) {
        messageApi.error(error.message)
      } else {
        messageApi.error('保存失败，请检查 JSON 格式')
      }
    }
  }

  const resetSettings = () => {
    const next = JSON.stringify(config || {}, null, 2)
    setConfigText(next)
    setSettingsDirty(false)
    // Everything unsaved on this page, including the token box: "discard" that
    // left one field behind would be the same surprise this page already had.
    setTokenDraft(token)
    const providers = config?.providers || {}
    const active = providers[config?.active_provider] || {}
    form.setFieldsValue({
      active_provider: config?.active_provider,
      model: active.default_model,
      max_tokens: active.max_tokens,
      thinking_effort: thinkingEffortOf(active),
      web_enabled: !!config?.channels?.web?.enabled,
      feishu_enabled: !!config?.channels?.feishu?.enabled,
    })
  }

  const jsonStatus = useMemo(() => {
    try {
      JSON.parse(configText || '{}')
      return { valid: true, label: 'JSON 格式有效' }
    } catch {
      return { valid: false, label: 'JSON 格式有误' }
    }
  }, [configText])

  /** Move to another view, asking first if the settings page has unsaved edits.
   *
   * Entering the settings view refetches the config and clears the dirty flag,
   * so without this a click on any other nav item quietly threw the edits away
   * -- and coming back showed the saved values, as though nothing had happened.
   */
  const navigateTo = (next: string) => {
    if (next === view) return
    if (view !== 'settings' || !(settingsDirty || tokenDirty)) {
      setView(next)
      return
    }
    Modal.confirm({
      title: '放弃未保存的设置？',
      content: '离开设置页会丢掉尚未保存的修改。',
      okText: '放弃修改',
      cancelText: '留在本页',
      okButtonProps: { danger: true },
      onOk: () => {
        resetSettings()
        setView(next)
      },
    })
  }

  /**
   * Go straight to the runs that are waiting to be looked at.
   *
   * `navigateTo` alone is not enough: it returns early when the view is
   * already the schedules page, which is exactly when the badge is most
   * likely to be pressed -- someone is on the page and still cannot find what
   * the number is talking about.
   */
  const openAttention = () => {
    navigateTo('schedules')
    setAutomationTab('attention')
  }

  const filteredSessions = useMemo(() => {
    const query = sessionSearch.trim().toLowerCase()
    const filtered = sessions.filter(
      item =>
        !query ||
        (item.title || '').toLowerCase().includes(query) ||
        item.session_id.toLowerCase().includes(query),
    )
    return [
      ...filtered.filter(item => !item.live),
      ...filtered.filter(item => item.live),
    ]
  }, [sessions, sessionSearch])

  const allFilteredSessionsSelected = filteredSessions.length > 0 &&
    filteredSessions.every(item => selectedSessionIds.includes(item.session_id))

  const filteredPlugins = useMemo(() => {
    const query = pluginSearch.trim().toLowerCase()
    if (!query) return plugins
    return plugins.filter(item =>
      [item.name, item.description, item.source].join(' ').toLowerCase().includes(query),
    )
  }, [plugins, pluginSearch])

  const filteredSkills = useMemo(() => {
    const query = skillSearch.trim().toLowerCase()
    return skills.filter(item => {
      const matchesFilter =
        skillFilter === 'all' ||
        (skillFilter === 'callable' && item.user_invocable) ||
        (skillFilter === 'internal' && !item.user_invocable)
      return matchesFilter && (!query ||
        [item.id, item.name, item.description, item.source].join(' ').toLowerCase().includes(query))
    })
  }, [skills, skillSearch, skillFilter])

  const modelOptions = useMemo(() => {
    // Every configured provider's models are selectable: the backend routes a
    // model id to the provider that owns it, so the list is not limited to the
    // active provider's group. The active provider's group comes first.
    const providers = config?.providers || {}
    const activeName = config?.active_provider
    const groups: { label: string; options: { value: string; label: string }[] }[] = []
    const seen = new Set<string>()
    const push = (providerName: string) => {
      const provider = providers[providerName]
      if (!provider) return
      const models = provider.models?.length
        ? provider.models
        : [provider.default_model].filter(Boolean)
      const options: { value: string; label: string }[] = []
      for (const model of models || []) {
        if (!model || seen.has(model)) continue
        seen.add(model)
        options.push({ value: model, label: model })
      }
      if (options.length) groups.push({ label: providerName, options })
    }
    if (activeName) push(activeName)
    for (const name of Object.keys(providers)) {
      if (name !== activeName) push(name)
    }
    return groups
  }, [config])

  // Placeholder while the config has not loaded yet; once loaded,
  // currentModel holds the active provider's default model id.
  const modelSelectPlaceholder = currentModel ? undefined : '默认模型'

  // Size the model picker to the label it currently shows, not to the longest
  // id in the list: a short model should not reserve a long one's width. The
  // popup is width-independent (popupMatchSelectWidth={false}), so long entries
  // still read in full while open; the closed control ellipsizes past the clamp.
  const modelSelectWidth = useMemo(() => {
    const label = currentModel || modelSelectPlaceholder || ''
    const measured = measureLabelWidth(label) || estimateLabelWidth(label)
    return `${Math.max(88, Math.min(240, Math.ceil(measured) + MODEL_PICKER_CHROME))}px`
  }, [currentModel, modelSelectPlaceholder])

  // Settings page: models of the currently selected provider. The default
  // model is chosen from a dropdown instead of free-text input, so the value
  // always matches a real model id of the active provider.
  const settingsModelOptions = useMemo(() => {
    const provider = config?.providers?.[activeProviderName]
    const models = provider?.models?.length
      ? provider.models
      : [provider?.default_model].filter(Boolean)
    const seen = new Set<string>()
    const list: string[] = []
    for (const model of models || []) {
      if (model && !seen.has(model)) {
        seen.add(model)
        list.push(model)
      }
    }
    return list.map(model => ({ value: model, label: model }))
  }, [config, activeProviderName])

  const handleModelChange = (model: string) => {
    currentModelRef.current = model
    setCurrentModel(model)
  }

  const filteredCommands = useMemo(() => {
    const query = input.startsWith('/')
      ? input.slice(1).trim().toLowerCase()
      : ''
    if (!query) return commands
    return commands.filter(command =>
      (command.name || '').toLowerCase().includes(query) ||
      (command.aliases || []).some(alias => alias.toLowerCase().includes(query)),
    )
  }, [commands, input])

  // The popover shows while the input is a bare command prefix (`/pref` with
  // no arguments) and has not been dismissed with Esc. Once arguments start,
  // the command is decided and completion no longer applies.
  const commandPrefixActive = input.startsWith('/') &&
    !/\s/.test(input) &&
    !commandDismissed
  const inlineCommandOpen = commandPrefixActive && filteredCommands.length > 0
  const inlineCommandEmpty = commandPrefixActive &&
    commands.length > 0 &&
    filteredCommands.length === 0

  useEffect(() => {
    setCommandIndex(0)
    setCommandIndexPinned(false)
  }, [filteredCommands])

  // Keep the highlighted command visible while navigating with ↑/↓. The
  // popover is its own scroll container, so relying on browser focus would
  // not scroll the active item (and would also move focus away from the
  // composer). ``nearest`` avoids jumping the surrounding page while only
  // adjusting the command list when necessary.
  useEffect(() => {
    if (!inlineCommandOpen) return
    const command = filteredCommands[commandIndex] || filteredCommands[0]
    commandItemRefs.current[command.name]?.scrollIntoView({ block: 'nearest' })
  }, [commandIndex, filteredCommands, inlineCommandOpen])

  const paletteCommands = useMemo(() => {
    const query = paletteQuery.trim().toLowerCase()
    if (!query) return commands
    return commands.filter(command =>
      [command.name, ...(command.aliases || [])]
        .join(' ')
        .toLowerCase()
        .includes(query),
    )
  }, [commands, paletteQuery])

  const navItems = [
    { key: 'chat', icon: <MessageOutlined />, label: '对话' },
    { key: 'sessions', icon: <FolderOpenOutlined />, label: '会话管理' },
    // One entry for both lists: plugins and skills answer the same question
    // ("what can the agent do beyond its own tools?") and each list on its
    // own is too short to justify a navigation slot of its own.
    { key: 'extensions', icon: <AppstoreOutlined />, label: '扩展' },
    {
      key: 'schedules',
      icon: <ClockCircleOutlined />,
      // The badge lives on the navigation entry, not on the schedules page,
      // because the entire problem is a failure that finished while the page
      // was closed -- and it is a button rather than a chip, because a number
      // that says "two things need you" and then drops you on an unfiltered
      // list of forty tasks has told you nothing you can act on. Pressing it
      // opens the runs themselves.
      label: (
        <span className="nav-label">
          自动化
          {unseenFailures > 0 && (
            <button
              type="button"
              className="nav-badge"
              aria-label={`${unseenFailures} 次运行需要查看，打开待处理列表`}
              title={`${unseenFailures} 次运行需要查看`}
              onClick={event => {
                // The menu entry behind it would otherwise also fire and
                // settle the tab back to whatever it was.
                event.stopPropagation()
                openAttention()
              }}
            >
              {unseenFailures > 99 ? '99+' : unseenFailures}
            </button>
          )}
        </span>
      ),
    },
    // Settings is deliberately absent from the main navigation: it is visited
    // rarely and briefly, so it lives as a small entry in the sidebar footer
    // next to the theme switch rather than taking a slot beside the pages
    // someone visits every day.
  ]

  const pageMeta: Record<string, { title: string; subtitle: string }> = {
    chat: {
      title: activeSession ? '当前对话' : '开始新的对话',
      subtitle: activeSession
        ? `${activeSession.slice(0, 12)} · ${connected ? '实时连接中' : '连接已断开'}`
        : '与你的 AI Agent 开始一段对话',
    },
    sessions: {
      title: '会话管理',
      subtitle: `${sessions.length} 个会话，${sessions.filter(item => item.live).length} 个动态会话`,
    },
    // One meta for the merged page: the head the visitor sees is the page's,
    // while each tab keeps its own counts where they already were -- the
    // skills summary strip and the plugins search both belong to their lists.
    extensions: {
      title: '扩展',
      subtitle: `${plugins.length} 个插件 · ${skills.length} 个技能`,
    },
    // Not "定时任务": the page now holds tasks that wait for a signal, and a
    // name that promises a time would be wrong for them. "自动化" is what the
    // page actually is -- work that runs without being asked each time.
    schedules: { title: '自动化', subtitle: '管理定时执行与等待信号的任务。' },
    settings: {
      title: '设置',
      subtitle: '管理访问令牌、模型与频道',
    },
  }

  const copyMessage = (content: string) => {
    navigator.clipboard?.writeText(content).then(() => {
      messageApi.success('已复制')
    }).catch(() => {
      messageApi.error('复制失败')
    })
  }

  const applyCommand = (command: CommandInfo) => {
    setInput(`/${command.name} `)
    setView('chat')
    setCommandPaletteOpen(false)
    setPaletteQuery('')
  }

  const toggleTraceExpanded = (id: string) => {
    setExpandedTraces(prev => ({ ...prev, [id]: !prev[id] }))
  }

  // Sending must resolve a `/command` identically no matter which affordance
  // triggered it (Enter, the send button). An exact command name — or a
  // command that already carries arguments — goes through as typed; only a
  // bare partial prefix completes to the highlighted suggestion.
  const resolveComposerText = (raw: string) => {
    const trimmed = raw.trim()
    if (!trimmed.startsWith('/')) return trimmed
    const parts = trimmed.slice(1).split(/\s+/)
    if (parts.length > 1) return trimmed
    const name = (parts[0] || '').toLowerCase()
    // A lone "/" only opens the menu: nothing has been typed and no entry was
    // highlighted, so there is no chosen command to complete to. Returning ''
    // makes send a no-op instead of running the first suggestion by accident.
    if (!name && !commandIndexPinned) return ''
    const isExact = commands.some(command =>
      (command.name || '').toLowerCase() === name ||
      (command.aliases || []).some(alias => alias.toLowerCase() === name),
    )
    if (isExact || !inlineCommandOpen) return trimmed
    const command = filteredCommands[commandIndex] || filteredCommands[0]
    return `/${command.name}`
  }

  // The single source of truth for both send affordances (Enter and the
  // button), so an incomplete command cannot look sendable on one but not
  // the other.
  const resolvedComposerText = resolveComposerText(input)
  const composerSendable =
    Boolean(resolvedComposerText) || pendingAttachments.length > 0

  const handleComposerKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // 输入法（IME）组合输入时，Enter 用于选中候选字/上屏，不应触发送出。
    if (event.nativeEvent?.isComposing || event.keyCode === 229) {
      return
    }

    const ctrlOrCmd = event.ctrlKey || event.metaKey

    const send = () => {
      sendMessage(resolveComposerText(input))
    }

    if (sendShortcut === 'ctrl-enter') {
      // 单独 Enter = 换行；Ctrl/Cmd + Enter = 发送
      if (event.key === 'Enter') {
        if (ctrlOrCmd) {
          event.preventDefault()
          send()
        }
        return
      }
    } else {
      if (event.key === 'Enter') {
        if (event.shiftKey) return
        event.preventDefault()
        send()
        return
      }
    }

    if (event.key === 'Escape') {
      // First Esc hides the popover but keeps the draft; once the popover is
      // gone (or was never open), Esc clears a command draft as before.
      if (commandPrefixActive && (inlineCommandOpen || inlineCommandEmpty)) {
        event.preventDefault()
        setCommandDismissed(true)
      } else if (input.startsWith('/')) {
        event.preventDefault()
        setInput('')
      }
      return
    }

    if (!commandPrefixActive) return

    if (event.key === 'Tab' && inlineCommandOpen) {
      // Complete the highlighted command without sending, so arguments can
      // be filled in next.
      event.preventDefault()
      const command = filteredCommands[commandIndex] || filteredCommands[0]
      setInput(`/${command.name} `)
    } else if (event.key === 'ArrowDown' && inlineCommandOpen) {
      event.preventDefault()
      setCommandIndexPinned(true)
      setCommandIndex(prev => (prev + 1) % filteredCommands.length)
    } else if (event.key === 'ArrowUp' && inlineCommandOpen) {
      event.preventDefault()
      setCommandIndexPinned(true)
      setCommandIndex(
        prev => (prev - 1 + filteredCommands.length) % filteredCommands.length,
      )
    }
  }

  const conversationTurns = messages.filter(item => item.role === 'user')
  // Keep a calm rhythm for short conversations, then compress the rail as
  // history grows so the indicator remains a compact page-edge affordance.
  const conversationGap = Math.max(
    3,
    Math.min(10, 11 - Math.max(0, conversationTurns.length - 2) * 0.45),
  )
  const activateTurnIndex = (index: number) => {
    const item = conversationTurns[index]
    if (!item) return
    if (hoverClearTimerRef.current) window.clearTimeout(hoverClearTimerRef.current)
    setHoveredTurnIndex(index)
    const marker = conversationMarkerRefs.current[item.id]
    const indicator = conversationRailRef.current
    const target = marker?.getBoundingClientRect()
    const bounds = indicator?.getBoundingClientRect()
    if (!target || !bounds) return
    // The summary card is centered on the active marker via CSS. Keep a
    // small safe margin so the compact card never crosses the chat bounds.
    const rawTop = target.top + target.height / 2 - bounds.top
    const cardHalfHeight = CONVERSATION_SUMMARY_HALF_HEIGHT
    const maxTop = Math.max(cardHalfHeight, bounds.height - cardHalfHeight)
    setHoveredTurn({ id: item.id, top: Math.max(cardHalfHeight, Math.min(rawTop, maxTop)) })
  }

  // Marker geometry is stable while the pointer is over the rail (the rail
  // lives outside the scroll container), so measure once per hover session
  // instead of on every mousemove. The cache key covers what can move
  // markers mid-hover: appended turns (last id), a session switch (first id)
  // and a window resize (innerHeight; reading it does not force layout).
  const railGeometryRef = useRef<{ key: string; centers: number[] } | null>(null)

  const handleRailMouseMove = (event: React.MouseEvent<HTMLDivElement>) => {
    if (conversationTurns.length < 2) return
    const key = `${conversationTurns[0]?.id}:${conversationTurns[conversationTurns.length - 1]?.id}:${window.innerHeight}`
    let geometry = railGeometryRef.current
    if (!geometry || geometry.key !== key) {
      geometry = {
        key,
        centers: conversationTurns.map(item => {
          const marker = conversationMarkerRefs.current[item.id]
          if (!marker) return Number.NaN
          const bounds = marker.getBoundingClientRect()
          return bounds.top + bounds.height / 2
        }),
      }
      railGeometryRef.current = geometry
    }
    let nearestIndex = 0
    let nearestDistance = Number.POSITIVE_INFINITY
    geometry.centers.forEach((center, index) => {
      if (Number.isNaN(center)) return
      const distance = Math.abs(event.clientY - center)
      if (distance < nearestDistance) {
        nearestDistance = distance
        nearestIndex = index
      }
    })
    activateTurnIndex(nearestIndex)
  }

  const scheduleHideTurnSummary = () => {
    if (hoverClearTimerRef.current) window.clearTimeout(hoverClearTimerRef.current)
    railGeometryRef.current = null
    hoverClearTimerRef.current = window.setTimeout(() => {
      setHoveredTurn(null)
      setHoveredTurnIndex(null)
    }, 140)
  }

  const keepTurnSummary = () => {
    if (hoverClearTimerRef.current) window.clearTimeout(hoverClearTimerRef.current)
  }

  const editingWorkflow = useMemo(
    () => workflows.find(item => item.id === editingWorkflowId) || null,
    [workflows, editingWorkflowId],
  )

  const openStepDetails = (step: WorkflowStepInfo) => {
    const task = schedules.find(item => item.id === step.task_id)
    if (task) openScheduleDetails(task)
    else messageApi.info('这一步的任务暂时读不到，先刷新一下')
  }  // Views are pure renderers built from this one context object.
  const ctx: AppCtx = {
    updateMessage,
    activeSession,
    token,
    turnRefs,
    copyMessage,
    expandedTraces,
    toggleTraceExpanded,
    messages,
    conversationTurns,
    conversationRailRef,
    keepTurnSummary,
    scheduleHideTurnSummary,
    conversationGap,
    handleRailMouseMove,
    conversationMarkerRefs,
    hoveredTurn,
    hoveredTurnIndex,
    activateTurnIndex,
    chatScrollRef,
    handleChatScroll,
    setInput,
    isStreaming,
    confirmReq,
    confirmRemaining,
    confirmOverflowing,
    confirmDetailOpen,
    approvalCommandRef,
    setConfirmDetailOpen,
    sendConfirm,
    queueView,
    withdrawQueuedMessages,
    sessionState,
    resumingTaskId,
    continueTask,
    dismissTaskGuidance,
    activity,
    interrupting,
    pendingAttachments,
    setPendingAttachments,
    inlineCommandOpen,
    inlineCommandEmpty,
    sendShortcutLabel,
    filteredCommands,
    commandItemRefs,
    commandIndex,
    setCommandIndex,
    setCommandIndexPinned,
    input,
    setCommandDismissed,
    handleComposerKeyDown,
    fileInputRef,
    handleFilesSelected,
    pickWorkspace,
    permissionLevel,
    updateSessionPermissions,
    sandboxMode,
    permissionLabel,
    currentModel,
    modelSelectPlaceholder,
    handleModelChange,
    modelOptions,
    modelSelectWidth,
    stopStreaming,
    composerSendable,
    creatingSession,
    sendMessage,
    resolvedComposerText,
    pageMeta,
    filteredSessions,
    allFilteredSessionsSelected,
    selectedSessionIds,
    setSelectedSessionIds,
    deleteSelectedSessions,
    sessionSearch,
    setSessionSearch,
    loadingSessions,
    handleSessionContainerClick,
    pendingDeleteSessionId,
    deleteSession,
    setPendingDeleteSessionId,
    revealSession,
    renameSession,
    pluginSearch,
    setPluginSearch,
    loadingView,
    filteredPlugins,
    togglePlugin,
    deletePlugin,
    skills,
    skillFilter,
    setSkillFilter,
    skillSearch,
    setSkillSearch,
    filteredSkills,
    toggleSkill,
    deleteSkill,
    extensionsTab,
    setExtensionsTab,
    schedules,
    attentionRuns,
    clock,
    openScheduleDetails,
    acknowledgeScheduleRun,
    patchWorkflowStep,
    signals,
    workflowModalOpen,
    editingWorkflowId,
    workflowSaving,
    workflowGraph,
    setWorkflowModalOpen,
    setEditingWorkflowId,
    saveWorkflow,
    workflowDraft,
    setWorkflowDraft,
    workflowOrderDiffersFromArray,
    workflowKeyRewrite,
    editingWorkflow,
    setWorkflowKeyRewrite,
    insertWorkflowStepAfter,
    moveWorkflowStep,
    removeWorkflowStep,
    changeWorkflowStepUpstreams,
    pickingDirectory,
    pickDirectory,
    openEditSchedule,
    addWorkflowStep,
    workflows,
    filteredWorkflows,
    toggleWorkflow,
    schedulerHealth,
    runWorkflowNow,
    openEditWorkflow,
    deleteWorkflow,
    openStepDetails,
    schedulerRefreshedAt,
    schedulerStale,
    automationTab,
    loadSchedules,
    loadWorkflows,
    openCreateSchedule,
    openCreateWorkflow,
    unseenFailures,
    clearScheduleAttention,
    setAutomationTab,
    workflowAttention,
    scheduleQuery,
    setScheduleQuery,
    workflowQuery,
    setWorkflowQuery,
    scheduleStatusFilter,
    setScheduleStatusFilter,
    selectedScheduleIds,
    bulkScheduleAction,
    scheduleModalOpen,
    editingScheduleId,
    scheduleSaving,
    setScheduleModalOpen,
    setEditingScheduleId,
    saveSchedule,
    scheduleDraft,
    setScheduleDraft,
    recentWorkspaceRoots,
    permissionProfileOptions,
    activePermissionProfile,
    editingStep,
    signalsWaiting,
    feishuChatsLoading,
    feishuChats,
    feishuTesting,
    sendFeishuTest,
    feishuChatsError,
    loadFeishuChats,
    feishuChatsLoaded,
    schedulePreview,
    schedulePreviewError,
    scheduleDetailOpen,
    selectedSchedule,
    setScheduleDetailOpen,
    runScheduleNow,
    duplicateSchedule,
    deleteSchedule,
    scheduleRunsLoading,
    loadScheduleRuns,
    permissionProfileLabel,
    scheduleRuns,
    selectedScheduleRunId,
    setSelectedScheduleRunId,
    selectedScheduleRun,
    cancelScheduleRun,
    retryScheduleRun,
    scheduleRunOutput,
    selectedScheduleRunTask,
    scheduleOutputLoading,
    scheduleArtifacts,
    filteredSchedules,
    workflowsLoaded,
    attentionByTask,
    setSelectedScheduleIds,
    toggleSchedule,
    settingsDirty,
    tokenDirty,
    jsonStatus,
    resetSettings,
    saveSettings,
    tokenDraft,
    setTokenDraft,
    applyToken,
    sendShortcut,
    setSendShortcut,
    messageApi,
    form,
    handleSettingsFormChange,
    config,
    settingsModelOptions,
    thinkingEffortOptions,
    configText,
    setConfigText,
    setSettingsDirty,
  }
  const { renderChat } = createChatView(ctx)
  const { renderSessions } = createSessionsView(ctx)
  const { renderExtensions } = createExtensionsView(ctx)
  const { renderSchedules } = createAutomationView(ctx)
  const { renderSettings } = createSettingsView(ctx)

  const renderCurrentView = () => {
    if (view === 'chat') return renderChat()
    if (view === 'sessions') return renderSessions()
    if (view === 'extensions') return renderExtensions()
    if (view === 'schedules') return renderSchedules()
    if (view === 'settings') return renderSettings()
    return renderChat()
  }

  const currentMeta = pageMeta[view] || pageMeta.chat
  const activeSessionInfo = sessions.find(item => item.session_id === activeSession)

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: themeMode === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: {
          colorPrimary: themeMode === 'dark' ? '#e4e4e7' : '#27272a',
          colorInfo: themeMode === 'dark' ? '#d4d4d8' : '#52525b',
          colorSuccess: themeMode === 'dark' ? '#d4d4d8' : '#52525b',
          colorWarning: themeMode === 'dark' ? '#d6c7a3' : '#806f4b',
          colorError: themeMode === 'dark' ? '#f0a3a3' : '#a16262',
          borderRadius: 8,
          // One control height for every control. Raising only `Button` to 38
          // left Input/Select/InputNumber/DatePicker at antd's 32, so any row
          // holding two of them disagreed about its own height -- and inside a
          // `Space.Compact` the taller half overhung the shorter one it was
          // supposed to join. Pages patched the rows they noticed.
          controlHeight: 38,
          fontFamily:
            "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif",
        },
        components: {
          Layout: {
            bodyBg: 'transparent',
            headerBg: 'transparent',
            siderBg: 'transparent',
          },
          Card: {
            colorBorderSecondary: 'rgba(128, 128, 150, 0.14)',
          },
        },
      }}
    >
      {contextHolder}
      <Layout className="app-shell">
        <Sider
          className="app-sider"
          width={260}
          collapsedWidth={0}
          collapsed={collapsed}
          trigger={null}
        >
          <div className="brand">
            <div className="brand-logo">
              <RobotOutlined />
            </div>
            <div className="brand-copy">
              <strong>Simple Agent</strong>
              <span>Personal AI</span>
            </div>
            <Tooltip title="搜索会话">
              <Button
                type="text"
                size="small"
                className="brand-action"
                icon={<SearchOutlined />}
                onClick={() => setSearchOpen(true)}
              />
            </Tooltip>
            <Button
              type="text"
              size="small"
              icon={<CloseOutlined />}
              onClick={() => setCollapsed(true)}
            />
          </div>

          <div className="sider-nav">
            <Menu
              mode="inline"
              selectedKeys={[view]}
              items={navItems}
              onClick={event => {
                navigateTo(event.key)
                if (window.innerWidth <= 768) setCollapsed(true)
              }}
            />
          </div>

          <div className="sider-actions">
            <Button
              type="primary"
              block
              icon={<PlusOutlined />}
              loading={creatingSession}
              onClick={createSession}
            >
              新建会话
            </Button>
          </div>

          <div className="session-panel">
            <div className="session-panel-label">最近会话</div>
            {loadingSessions ? (
              <Skeleton active paragraph={{ rows: 5 }} title={false} />
            ) : filteredSessions.length === 0 ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="暂无会话"
              />
            ) : (
              filteredSessions.map((item, index) => {
                const showLiveHeader =
                  item.live &&
                  (index === 0 || !filteredSessions[index - 1].live)
                return (
                  <React.Fragment key={item.session_id}>
                    {showLiveHeader && (
                      <div className="session-panel-label sub">动态会话</div>
                    )}
                    <div
                      className={`session-item ${
                        item.session_id === activeSession ? 'active' : ''
                      }`}
                      role="button"
                      tabIndex={0}
                      aria-label={`打开会话 ${item.title || '未命名会话'}${sessionStatusOf(item) ? `（${sessionStatusOf(item)}）` : ''}`}
                      aria-current={item.session_id === activeSession ? 'true' : undefined}
                      onClick={event => handleSessionContainerClick(event, item.session_id)}
                      onKeyDown={event => {
                        if (event.target !== event.currentTarget) return
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault()
                          handleSessionContainerClick(event, item.session_id)
                        }
                      }}
                    >
                      <div className="session-item-status">
                        <span className={item.live ? 'live' : 'durable'} />
                      </div>
                      <div className="session-item-main">
                        <div className="session-item-title">
                          {item.title || '未命名会话'}
                        </div>
                        <div className="session-item-meta">
                          {/* The busy word rides with the counts because that
                              row is the session's "what is it doing" line; a
                              badge in the title row would fight the rename
                              affordance for the same pixels.  Only busy states
                              show one — "空闲" on every row would drown out
                              the one row it matters on. */}
                          {sessionStatusOf(item) && (
                            <span
                              className={`session-status-badge ${
                                item.status === 'queued' ? 'is-queued' : 'is-running'
                              }`}
                            >
                              {sessionStatusOf(item)}
                            </span>
                          )}
                          {item.turn_count || 0} 轮 · {relativeTime(item.last_activity)}
                        </div>
                      </div>
                      {pendingDeleteSessionId === item.session_id ? (
                        <div
                          className="session-item-delete-actions"
                          onClick={event => event.stopPropagation()}
                        >
                          <Button
                            type="text"
                            danger
                            size="small"
                            onClick={() => deleteSession(item)}
                          >
                            删除
                          </Button>
                          <Button
                            type="text"
                            size="small"
                            onClick={() => setPendingDeleteSessionId(null)}
                          >
                            取消
                          </Button>
                        </div>
                      ) : (
                        <Dropdown
                          trigger={['click']}
                          menu={{
                            onClick: ({ domEvent }) => domEvent.stopPropagation(),
                            items: [
                              {
                                key: 'reveal',
                                label: '在 Finder 中显示',
                                icon: <FolderOpenOutlined />,
                                onClick: () => revealSession(item),
                              },
                              {
                                key: 'rename',
                                label: '重命名',
                                icon: <EditOutlined />,
                                onClick: () => renameSession(item),
                              },
                              {
                                key: 'delete',
                                label: '删除',
                                icon: <DeleteOutlined />,
                                danger: true,
                                onClick: () => setPendingDeleteSessionId(item.session_id),
                              },
                            ],
                          }}
                        >
                          <Button
                            type="text"
                            size="small"
                            icon={<MoreOutlined />}
                            onClick={event => event.stopPropagation()}
                          />
                        </Dropdown>
                      )}
                    </div>
                  </React.Fragment>
                )
              })
            )}
          </div>

          <div className="sider-footer">
            <div className="sider-footer-row">
              <Button
                block
                icon={themeMode === 'dark' ? <SunOutlined /> : <MoonOutlined />}
                onClick={() => {
                  const next = themeMode === 'dark' ? 'light' : 'dark'
                  setThemeMode(next)
                  localStorage.setItem('agent_theme', next)
                }}
              >
                {themeMode === 'dark' ? '浅色模式' : '深色模式'}
              </Button>
              {/* The settings entry, out of the main navigation by design: it
                  sits beside the theme switch -- the two things a person
                  touches once and then rarely -- rather than beside the pages
                  they visit every day.  navigateTo keeps the unsaved-edits
                  guard, so this small entry refuses to lose work exactly as
                  the menu item it replaces did. */}
              <Button
                block
                icon={<SettingOutlined />}
                aria-current={view === 'settings' ? 'page' : undefined}
                onClick={() => navigateTo('settings')}
              >
                设置
              </Button>
            </div>
          </div>
        </Sider>

        <div
          className={`mobile-sider-backdrop ${collapsed ? '' : 'visible'}`}
          onClick={() => setCollapsed(true)}
          aria-hidden="true"
        />

        <Layout className="app-main">
          <Header className="app-header">
            <div className="header-left">
              <Button
                type="text"
                className="header-menu"
                icon={<MenuOutlined />}
                aria-label={collapsed ? '展开侧栏' : '收起侧栏'}
                onClick={() => setCollapsed(value => !value)}
              />
              <div>
                <div className="page-title">{currentMeta.title}</div>
                <div className="page-subtitle">{currentMeta.subtitle}</div>
              </div>
            </div>
          </Header>
          <Content className="app-content">{renderCurrentView()}</Content>
        </Layout>
      </Layout>

      <Modal
        open={searchOpen}
        className="global-search-modal"
        title={
          <div className="global-search-title">
            <SearchOutlined />
            <span>搜索会话</span>
            <kbd>ESC</kbd>
          </div>
        }
        footer={null}
        onCancel={() => {
          setSearchOpen(false)
          setSessionSearch('')
        }}
      >
        <div className="global-search-body">
          <Input
            autoFocus
            size="large"
            prefix={<SearchOutlined />}
            placeholder="搜索标题或会话 ID"
            value={sessionSearch}
            onChange={event => setSessionSearch(event.target.value)}
            allowClear
          />

          <div className="current-session-panel">
            <div className="current-session-label">当前会话</div>
            {activeSessionInfo ? (
              <div className="current-session-content">
                <div className="current-session-main">
                  <strong>{activeSessionInfo.title || '未命名会话'}</strong>
                  <code>{activeSessionInfo.session_id}</code>
                </div>
                <div className="current-session-stats">
                  <span className={`session-status ${connected ? 'connected' : ''}`}>
                    <i /> {connected ? '实时连接' : '连接中断'}
                  </span>
                  <span>{activeSessionInfo.turn_count || 0} 轮</span>
                  <span>{relativeTime(activeSessionInfo.last_activity)}</span>
                </div>
              </div>
            ) : (
              <div className="current-session-empty">尚未选择会话</div>
            )}
          </div>

          <div className="global-search-section-title">会话</div>
          <div className="global-search-results">
            {filteredSessions.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的会话" />
            ) : (
              filteredSessions.slice(0, 12).map(item => (
                <button
                  type="button"
                  className={`global-search-item ${item.session_id === activeSession ? 'active' : ''}`}
                  key={item.session_id}
                  onClick={() => {
                    selectSession(item.session_id)
                    setSearchOpen(false)
                    setSessionSearch('')
                  }}
                >
                  <span className={`session-status-dot ${item.live ? 'live' : ''}`} />
                  <span className="global-search-item-main">
                    <strong>{item.title || '未命名会话'}</strong>
                    <small>{item.turn_count || 0} 轮 · {relativeTime(item.last_activity)} · {item.session_id.slice(0, 12)}</small>
                  </span>
                  {item.session_id === activeSession && <span className="current-mark">当前</span>}
                </button>
              ))
            )}
          </div>
        </div>
      </Modal>

      <Modal
        open={commandPaletteOpen}
        className="command-palette-modal"
        title={null}
        footer={null}
        closable={false}
        onCancel={() => {
          setCommandPaletteOpen(false)
          setPaletteQuery('')
        }}
      >
        <div className="command-palette">
          <div className="command-palette-search">
            <SearchOutlined />
            <input
              autoFocus
              value={paletteQuery}
              onChange={event => setPaletteQuery(event.target.value)}
              placeholder="搜索命令、导航或输入关键词…"
            />
            <kbd>ESC</kbd>
          </div>
          <div className="command-palette-section">命令</div>
          {paletteCommands.length === 0 ? (
            <div className="command-empty">没有匹配命令</div>
          ) : (
            paletteCommands.slice(0, 8).map(command => (
              <button
                type="button"
                className="command-item"
                key={command.name}
                onClick={() => applyCommand(command)}
              >
                <CodeOutlined />
                <span className="command-item-main">
                  <strong>{command.usage || `/${command.name}`}</strong>
                  <small>{command.description || '无描述'}</small>
                </span>
                <span>Enter</span>
              </button>
            ))
          )}
          <div className="command-palette-section">导航</div>
          <div className="palette-nav">
            {navItems.map(item => (
              <button
                type="button"
                key={item.key}
                onClick={() => {
                  navigateTo(item.key)
                  setCommandPaletteOpen(false)
                  setPaletteQuery('')
                }}
              >
                {item.icon}
                {item.label}
              </button>
            ))}
            {/* Settings lives in the sidebar footer, but the palette is the
                keyboard's map of the app: leaving it out would make the one
                navigation surface that cannot reach it. */}
            <button
              type="button"
              key="settings"
              onClick={() => {
                navigateTo('settings')
                setCommandPaletteOpen(false)
                setPaletteQuery('')
              }}
            >
              <SettingOutlined />
              设置
            </button>
          </div>
        </div>
      </Modal>
    </ConfigProvider>
  )}

export default App
