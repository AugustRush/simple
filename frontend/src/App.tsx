import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { createPortal } from 'react-dom'
import {
  AutoComplete,
  Avatar,
  Badge,
  Button,
  Card,
  Checkbox,
  Col,
  ConfigProvider,
  DatePicker,
  Drawer,
  Dropdown,
  Empty,
  Form,
  Input,
  InputNumber,
  Layout,
  Menu,
  Modal,
  Row,
  Select,
  Skeleton,
  Space,
  Spin,
  Switch,
  Tag,
  theme,
  TimePicker,
  Tooltip,
  Typography,
  message,
} from 'antd'
import zhCN from 'antd/locale/zh_CN'
import {
  ApiOutlined,
  AppstoreOutlined,
  ArrowUpOutlined,
  CheckCircleFilled,
  CheckOutlined,
  ClockCircleOutlined,
  CloseOutlined,
  CodeOutlined,
  CopyOutlined,
  DeleteOutlined,
  DownOutlined,
  EditOutlined,
  ExclamationCircleFilled,
  FileTextOutlined,
  FolderOpenOutlined,
  LoadingOutlined,
  MenuOutlined,
  MessageOutlined,
  MoonOutlined,
  MoreOutlined,
  PaperClipOutlined,
  PlusOutlined,
  ReloadOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SearchOutlined,
  SettingOutlined,
  StopOutlined,
  SunOutlined,
  TagsOutlined,
  ThunderboltOutlined,
  UserOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import './index.css'

dayjs.locale('zh-cn')

const { Sider, Header, Content } = Layout
const { TextArea } = Input
const { Paragraph, Text } = Typography

type MessageRole = 'user' | 'assistant' | 'tool' | 'command' | 'error' | 'subagent'
type ToolState = 'running' | 'done' | 'blocked' | 'interrupted'
type ConfirmDecision = 'allow_once' | 'allow_session' | 'deny'
type ConfirmRisk = 'high' | 'medium' | 'low'

/**
 * Rolling state for one batch of sub-agent activity.
 *
 * Sub-agent progress is live telemetry, not conversation: a single batch emits
 * a start/progress/finish event per agent, so appending a chat row per event
 * buried the transcript under dozens of near-identical lines. The note is
 * updated in place instead and rendered as one thin status strip.
 */
interface SubAgentNote {
  /** Ordered event log, kept whole for the expanded view. */
  logs: string[]
  /** Distinct agent roles seen so far. */
  roles: string[]
  /** Roles that reached a terminal state (finished or failed). */
  doneRoles: string[]
  completed: number
  total: number
  failed: number
  running: boolean
  finished: boolean
  open?: boolean
  /** Local epoch millis of the first and last event, for the duration readout. */
  startedAt: number
  endedAt: number
}

/**
 * A pending tool-approval prompt pushed by the server. `allow_session` is only
 * true when the server holds a redeemable pending record for the command, so
 * the "总是允许" option is never offered where it could not be honoured.
 */
interface ConfirmRequest {
  name?: string
  command?: string
  risk_level?: string
  reason?: string
  confirmation_token?: string
  allow_session?: boolean
  timeout_seconds?: number
}

interface SessionInfo {
  session_id: string
  title?: string
  live?: boolean
  turn_count?: number
  last_activity?: string
}

interface Message {
  id: string
  role: MessageRole
  content: string
  streaming?: boolean
  queued?: boolean
  link?: string
  tool?: string
  toolState?: ToolState
  attachments?: AttachmentInfo[]
  subagent?: SubAgentNote
}

interface AttachmentInfo {
  id: string
  filename: string
  mime_type: string
  kind: string
  path: string
  size_bytes?: number
}

interface PluginInfo {
  name: string
  version?: string
  description?: string
  source?: string
  enabled?: boolean
}

interface SkillInfo {
  id: string
  name?: string
  description?: string
  source?: string
  user_invocable?: boolean
}

interface CommandInfo {
  name: string
  aliases?: string[]
  usage?: string
  description?: string
  kind?: 'command' | 'skill'
}

interface PermissionProfileOption {
  key: string
  label: string
  summary: string
  detail: string
}

type PermissionProfileKey = 'inherit' | 'read_only' | 'workspace_write'

// Display-only fallback, used only until the first /api/schedules response
// arrives. The backend list is authoritative -- it is what decides which keys
// are accepted, so duplicating the set here permanently would let the two
// drift apart.
const KNOWN_PERMISSION_PROFILES: PermissionProfileOption[] = [
  { key: 'inherit', label: '继承全局权限', summary: '不改动任何权限配置。', detail: '' },
  { key: 'read_only', label: '强制只读', summary: '不写工作区文件。', detail: '' },
  { key: 'workspace_write', label: '可在项目内写入', summary: '可改文件、跑测试、出报告。', detail: '' },
]

interface ScheduleInfo {
  id: string
  name: string
  kind: string
  enabled?: boolean
  trigger_type?: string
  trigger?: Record<string, any>
  payload?: Record<string, any>
  next_run_at?: string
  last_run_at?: string
  last_success_at?: string
  delivery_mode?: string
  active_run_id?: string | null
  latest_run?: ScheduleRun | null
  model_override?: string | null
  workspace_root?: string
  context_policy?: 'stateless' | 'task_history' | 'shared_memory'
  timeout_seconds?: number
  retry_policy?: { max_attempts?: number; backoff_seconds?: number }
  selected_skills?: string[]
  permission_profile?: PermissionProfileKey
  unseen_attention?: number
}

interface SignalInfo {
  name: string
  source: 'task' | 'custom'
  task_id: string
  task_name: string
  status: string
  last_emitted_at?: string | null
  emission_count: number
  subscriber_count: number
}

interface ScheduleRun {
  id: string
  task_id: string
  status: string
  scheduled_for?: string
  started_at?: string
  finished_at?: string
  duration_ms?: number | null
  summary?: string
  error?: string
  delivery_status?: string
  output_available?: boolean
  output_url?: string
  trigger_source?: string
  attempt?: number
  cancel_requested_at?: string | null
  retry_of_run_id?: string
  missed_count?: number
  acknowledged_at?: string | null
  needs_attention?: boolean
  config_snapshot?: Record<string, any>
}

interface SchedulerHealth {
  status: 'online' | 'offline' | string
  last_heartbeat?: string
  active_runs?: number
  max_concurrent_runs?: number
}

interface ScheduleRunOutput {
  run_id: string
  available: boolean
  content: string
  truncated?: boolean
  output_url?: string
}

interface ScheduleArtifact {
  path: string
  name: string
  mime_type: string
  size_bytes: number
  url: string
}

interface ScheduleDraft {
  name: string
  action_type: 'agent_task' | 'message'
  trigger_type: 'once' | 'interval' | 'daily' | 'weekly' | 'weekdays' | 'monthly' | 'signal'
  at: string
  every: number
  unit: 'minutes' | 'hours' | 'days' | 'weeks'
  anchor_at: string
  time_of_day: string
  day_of_week: string
  day_of_month: number
  signal_name: string
  prompt: string
  message_text: string
  workspace_root: string
  context_policy: 'stateless' | 'task_history' | 'shared_memory'
  model_override: string
  timeout_seconds: number
  max_attempts: number
  backoff_seconds: number
  selected_skills: string[]
  permission_profile: PermissionProfileKey
}

interface SessionTaskGuidance {
  task_id?: string
  active_goal?: string
  status?: string
  progress?: string
  next_action?: string
  last_error?: string
  artifacts?: string[]
}

interface SessionState {
  session_id: string
  live?: boolean
  operation_state?: string
  queue?: {
    pending?: number
    interjections?: number
    restarts?: number
  }
  task?: SessionTaskGuidance | null
  workspace_root?: string
  workspace_status?: 'ready' | 'missing' | 'unset' | string
  workspace_exists?: boolean
  workspace_read?: boolean
  workspace_write?: boolean
}

interface QueuedMessage {
  id: string
  text: string
  model: string
}

function escapeHtml(s: string): string {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[c]!))
}

const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'avif', 'ico'])
const AUDIO_EXT = new Set(['mp3', 'wav', 'm4a', 'ogg', 'flac', 'aac', 'opus'])
const VIDEO_EXT = new Set(['mp4', 'webm', 'mov', 'm4v', 'avi', 'mkv', 'ogv'])

type MediaKind = 'image' | 'audio' | 'video' | 'file'

function mediaKindForUrl(url: string): MediaKind {
  if (!url) return 'file'
  let s = String(url)
  try {
    // /api/files?path=... — read the underlying path so the extension is true.
    const u = new URL(s, location.origin)
    const p = u.searchParams.get('path')
    if (p && !/^https?:/i.test(p)) return mediaKindForUrl(p)
  } catch {
    // not a parseable URL — fall through to extension sniffing
  }
  const clean = s.split('?')[0].split('#')[0].toLowerCase()
  const ext = (clean.split('.').pop() || '').trim()
  if (IMAGE_EXT.has(ext)) return 'image'
  if (AUDIO_EXT.has(ext)) return 'audio'
  if (VIDEO_EXT.has(ext)) return 'video'
  return 'file'
}

/** Build a /api/files link that names the session owning the file.
 *
 * The gateway treats a file URL as a read capability and only serves it to
 * the session that owns the file, so the session id is part of the link. */
function fileHref(path: string, sessionId?: string | null, token?: string | null): string {
  const params = new URLSearchParams()
  params.set('path', path)
  if (sessionId) params.set('session_id', sessionId)
  if (token) params.set('token', token)
  return `/api/files?${params.toString()}`
}

/** Attach the owning session (and token) to a backend-supplied /api/files link. */
function withFileSession(link: string, sessionId?: string | null, token?: string | null): string {
  if (!link) return ''
  let url: URL
  try {
    url = new URL(link, location.origin)
  } catch {
    return link
  }
  if (url.pathname !== '/api/files') return link
  if (sessionId && !url.searchParams.get('session_id')) url.searchParams.set('session_id', sessionId)
  if (token && !url.searchParams.get('token')) url.searchParams.set('token', token)
  return `${url.pathname}?${url.searchParams.toString()}`
}

/** Resolve an image target emitted by the model into something the browser can load. */
function markdownMediaHref(
  rawTarget: string,
  sessionId?: string | null,
  token?: string | null,
): string {
  let target = rawTarget.trim()
  if (target.startsWith('<') && target.endsWith('>')) {
    target = target.slice(1, -1).trim()
  }
  // Markdown commonly escapes parentheses in filenames.
  target = target.replace(/\\([\\() ])/g, '$1')
  if (!target) return ''

  if (/^(?:https?:|data:|blob:)/i.test(target) || target.startsWith('//')) return target
  if (target.startsWith('/api/files?')) return withFileSession(target, sessionId, token)
  // Scheduled-run Markdown has a different task_id/run_id ownership model.
  // Without a session owner, preserve the target instead of manufacturing a
  // file URL that the gateway must correctly reject.
  if (!sessionId) return target

  if (/^file:/i.test(target)) {
    try {
      const url = new URL(target)
      target = decodeURIComponent(url.pathname)
      // file:///C:/path becomes /C:/path in URL.pathname.
      if (/^\/[A-Za-z]:\//.test(target)) target = target.slice(1)
    } catch {
      return target
    }
  }

  const isAbsoluteLocalPath = target.startsWith('/') || /^[A-Za-z]:[\\/]/.test(target)
  return isAbsoluteLocalPath ? fileHref(target, sessionId, token) : target
}

/**
 * Replace Markdown images while balancing parentheses in the destination.
 * A regex ending at the first `)` corrupts common names such as `result (1).png`.
 */
function replaceMarkdownImages(
  text: string,
  render: (alt: string, target: string) => string,
): string {
  let output = ''
  let cursor = 0

  while (cursor < text.length) {
    const start = text.indexOf('![', cursor)
    if (start < 0) {
      output += text.slice(cursor)
      break
    }
    output += text.slice(cursor, start)

    let altEnd = start + 2
    while (altEnd < text.length) {
      if (text[altEnd] === ']' && text[altEnd - 1] !== '\\') break
      altEnd += 1
    }
    if (altEnd >= text.length || text[altEnd + 1] !== '(') {
      output += text[start]
      cursor = start + 1
      continue
    }

    let depth = 1
    let targetEnd = altEnd + 2
    while (targetEnd < text.length && depth > 0) {
      const char = text[targetEnd]
      const escaped = text[targetEnd - 1] === '\\'
      if (!escaped && char === '(') depth += 1
      if (!escaped && char === ')') depth -= 1
      targetEnd += 1
    }
    if (depth !== 0) {
      output += text[start]
      cursor = start + 1
      continue
    }

    const alt = text.slice(start + 2, altEnd).replace(/\\([\\\]])/g, '$1')
    const target = text.slice(altEnd + 2, targetEnd - 1)
    output += render(alt, target)
    cursor = targetEnd
  }

  return output
}

// The model picker's label renders at 11px (see .composer-tools .model-select
// .ant-select-selection-item). Measuring with that same font keeps the control's
// inline width honest — counting characters under-read the small label font and
// left ~40px of dead space to the right of the model name.
const MODEL_LABEL_FONT =
  "11px -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif"

// What the control spends on chrome around the label: 8px of left padding plus
// 28px on the right for the 10px chevron, its 11px inset and a 7px gap. The
// last 2px absorb the subpixel difference between canvas metrics and DOM text
// layout, which otherwise ellipsizes a label that exactly fits.
const MODEL_PICKER_CHROME = 38

// Stopping a turn is cooperative: the cancel request aborts the in-flight
// model request or child process, but whatever step is already running has to
// unwind before `turn_complete` arrives. These bound how long the UI stays
// quiet about it — first by asking again on the user's behalf, then by saying
// plainly that it has not taken effect.
const INTERRUPT_RETRY_MS = 5000
const INTERRUPT_STUCK_MS = 20000

let labelMeasureContext: CanvasRenderingContext2D | null | undefined

function measureLabelWidth(text: string): number {
  if (!text || typeof document === 'undefined') return 0
  try {
    if (labelMeasureContext === undefined) {
      labelMeasureContext = document.createElement('canvas').getContext('2d')
    }
    if (!labelMeasureContext) return 0
    labelMeasureContext.font = MODEL_LABEL_FONT
    return labelMeasureContext.measureText(text).width
  } catch {
    return 0
  }
}

// Fallback for when canvas measurement is unavailable: at this size CJK is
// roughly twice as wide as Latin. It only ever over-estimates, so the control
// cannot end up too narrow to read its own label.
function estimateLabelWidth(text: string): number {
  return [...text].reduce(
    (sum, ch) => sum + (ch.charCodeAt(0) > 0x2e7f ? 13.4 : 5.9),
    0,
  )
}

function markdownToHtml(
  text: string,
  sessionId?: string | null,
  token?: string | null,
): string {
  let t = String(text || '')
  const codeBlocks: string[] = []
  const images: string[] = []

  // Replace fenced code with placeholders so line-level parsing can't corrupt it.
  t = t.replace(
    /```([\w-]*)[ \t]*\n?([\s\S]*?)```/g,
    (_m, lang: string, code: string) => {
      const label = lang ? escapeHtml(lang) : 'code'
      const body = code.replace(/\n$/, '')
      const index = codeBlocks.length
      codeBlocks.push(
        `<div class="code-block"><div class="code-block-head"><span>${label}</span></div>` +
        `<pre><code>${escapeHtml(body)}</code></pre></div>`,
      )
      return `\u0000CODE${index}\u0000`
    },
  )

  t = replaceMarkdownImages(t, (alt, target) => {
    const index = images.length
    const href = markdownMediaHref(target, sessionId, token)
    images.push(
      `<img class="md-img" src="${escapeHtml(href)}" alt="${escapeHtml(alt)}" loading="lazy" />`,
    )
    return `\u0000IMAGE${index}\u0000`
  })

  t = escapeHtml(t)

  t = t.replace(/`([^`]+)`/g, '<code>$1</code>')
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  t = t.replace(/\*([^*]+)\*/g, '<em>$1</em>')
  t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')

  const lines = t.split('\n')
  const output: string[] = []
  let listOpen = false

  for (const line of lines) {
    const listItem = line.match(/^\s*[-*]\s+(.*)$/)
    if (listItem) {
      if (!listOpen) {
        output.push('<ul>')
        listOpen = true
      }
      output.push(`<li>${listItem[1]}</li>`)
      continue
    }

    if (listOpen) {
      output.push('</ul>')
      listOpen = false
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/)
    if (heading) {
      const level = heading[1].length
      output.push(`<h${level}>${heading[2]}</h${level}>`)
    } else {
      output.push(line)
    }
  }

  if (listOpen) output.push('</ul>')
  t = output.join('\n')
  t = t.replace(/\u0000CODE(\d+)\u0000/g, (_m, index: string) => codeBlocks[Number(index)] || '')
  t = t.replace(/\u0000IMAGE(\d+)\u0000/g, (_m, index: string) => images[Number(index)] || '')
  t = t.replace(/\n{2,}/g, '<br /><br />')
  t = t.replace(/\n/g, '<br />')

  return t
}

function truncate(value: string, length = 64): string {
  return value.length > length ? `${value.slice(0, length)}…` : value
}

// Half of .conversation-summary's max-height (124px, index.css). The hover
// summary clamps its top position so the card never crosses the chat bounds;
// keep this in sync when the card's max-height changes.
const CONVERSATION_SUMMARY_HALF_HEIGHT = 62

function compactWorkspacePath(value: string, maxLength = 36): string {
  const path = String(value || '').trim()
  if (!path) return path
  const parts = path.split(/[\\/]+/).filter(Boolean)
  if (parts.length < 3) return truncate(path, maxLength)
  const tail = parts.slice(-2).join('/')
  const compact = `…/${tail}`
  return compact.length <= maxLength ? compact : truncate(compact, maxLength)
}

function relativeTime(value?: string): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const diff = Date.now() - date.getTime()
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  return `${Math.floor(diff / 86_400_000)} 天前`
}

function toolStateLabel(state?: ToolState): string {
  if (state === 'running') return '执行中'
  if (state === 'blocked') return '已阻止'
  if (state === 'interrupted') return '已中断'
  return '已完成'
}

// One mark per call does not scale: the width grows linearly while the
// information per mark saturates -- nobody reads 90 dots differently from
// 100 -- so past this budget the strip describes the shape of the turn
// instead of indexing it. The exact step count stays in the label next to it,
// which is why the marks are free to become summaries.
const MAX_TRACE_DOTS = 20

// A summarised mark reports its most severe member, never a sample of them.
// That is the whole point of aggregating by worst case rather than picking
// every n-th step: a single blocked call must not be able to disappear just
// because the turn around it was long. ``done`` is the floor, matching the
// `toolState || 'done'` the marks have always used.
const TRACE_SEVERITY: Record<ToolState, number> = {
  done: 0,
  running: 1,
  interrupted: 2,
  blocked: 3,
}

interface ToolDotSummary {
  state: ToolState
  count: number
}

function summariseToolDots(tools: Message[]): ToolDotSummary[] {
  if (tools.length <= MAX_TRACE_DOTS) {
    return tools.map(tool => ({ state: tool.toolState || 'done', count: 1 }))
  }
  const summaries: ToolDotSummary[] = []
  for (let index = 0; index < MAX_TRACE_DOTS; index += 1) {
    // Contiguous, non-overlapping spans that together cover every step, so
    // compressing cannot silently drop one from the count.
    const start = Math.floor((index * tools.length) / MAX_TRACE_DOTS)
    const end = Math.floor(((index + 1) * tools.length) / MAX_TRACE_DOTS)
    let state: ToolState = 'done'
    for (let cursor = start; cursor < end; cursor += 1) {
      const candidate = tools[cursor].toolState || 'done'
      if (TRACE_SEVERITY[candidate] > TRACE_SEVERITY[state]) state = candidate
    }
    summaries.push({ state, count: end - start })
  }
  return summaries
}

function toolStateColor(state?: ToolState): string {
  if (state === 'running') return 'processing'
  if (state === 'blocked') return 'error'
  return 'success'
}

function toolStateIcon(state?: ToolState) {
  if (state === 'running') return <LoadingOutlined spin />
  if (state === 'blocked') return <ExclamationCircleFilled />
  return <CheckCircleFilled />
}

// Approval prompts arrive with the tool's internal name; the bar says what the
// user is actually being asked about rather than echoing a code identifier.
const CONFIRM_TOOL_LABELS: Record<string, string> = {
  shell: '终端命令',
  install_plugin: '安装插件',
  create_tool: '激活用户工具',
  memory_clear: '清空记忆',
}

const CONFIRM_RISK_LABELS: Record<ConfirmRisk, string> = {
  high: '高风险',
  medium: '中风险',
  low: '低风险',
}

function confirmToolLabel(name?: string): string {
  const key = String(name || '').trim()
  if (!key) return '未知操作'
  return CONFIRM_TOOL_LABELS[key] || key
}

function confirmRisk(level?: string): ConfirmRisk {
  const value = String(level || '').trim().toLowerCase()
  // An unrecognised tier is treated as the most dangerous one: a mislabelled
  // prompt should look alarming, not reassuring.
  if (value === 'medium' || value === 'low') return value
  return 'high'
}

/**
 * Task statuses where "continue or abandon?" is a real question.
 *
 * The working state is written on every turn end with one of
 * `cancelled` / `failed` / `completed` / `in_progress`, so a blocklist of
 * terminal states is not enough: `in_progress` (a turn that produced neither
 * text nor an error) was also advertised as 「任务已中断」. After the user
 * answered the prompt once, the continuation turn wrote exactly such a state,
 * the card came back, and every further click queued another interjection.
 */
const TASK_INTERRUPTED_STATUSES = new Set(['cancelled', 'interrupted', 'failed'])

const TASK_STATUS_LABELS: Record<string, string> = {
  cancelled: '已取消',
  interrupted: '已中断',
  failed: '失败',
}

// Sub-agent progress is the noisiest thing the socket carries: one batch emits
// a start/progress/finish event per agent. Stamping each event into the
// transcript cost roughly 60px apiece — a 36px note row plus the 22–32px
// conversation gap — for lines that mostly repeated each other, so a single
// multi-agent turn could push 600–1800px of telemetry through the scroll area.
// Folding the events into one note keeps that cost constant.
const SUBAGENT_KIND_LABELS: Record<string, string> = {
  batch_started: '批量启动',
  batch_progress: '批量进度',
  batch_finished: '批量结束',
  agent_started: '开始执行',
  agent_finished: '执行完成',
  agent_failed: '执行失败',
  agent_retry: '重试',
}

/** Kinds that end a batch; everything after them starts a fresh note. */
const SUBAGENT_TERMINAL_KINDS = new Set(['batch_finished'])

function subagentEventLine(evt: Record<string, unknown>): string {
  const message = String(evt.message || '').trim()
  if (message) return message
  const role = String(evt.role || '').trim()
  const kind = String(evt.kind || '').trim()
  const label = SUBAGENT_KIND_LABELS[kind] || kind || '状态更新'
  return role ? `${role} · ${label}` : label
}

function newSubAgentNote(now: number): SubAgentNote {
  return {
    logs: [],
    roles: [],
    doneRoles: [],
    completed: 0,
    total: 0,
    failed: 0,
    running: true,
    finished: false,
    startedAt: now,
    endedAt: now,
  }
}

/** Fold one socket event into a note, preserving the user's expanded state. */
function foldSubAgentEvent(
  note: SubAgentNote,
  evt: Record<string, unknown>,
  now: number,
): SubAgentNote {
  const kind = String(evt.kind || '').trim()
  const role = String(evt.role || '').trim()
  const line = subagentEventLine(evt)
  const failed = note.failed + (kind === 'agent_failed' ? 1 : 0)
  const finished = note.finished || SUBAGENT_TERMINAL_KINDS.has(kind)
  const terminal = kind === 'agent_finished' || kind === 'agent_failed'
  const lastLog = note.logs[note.logs.length - 1]
  return {
    ...note,
    logs: lastLog === line ? note.logs : [...note.logs, line],
    roles: role && !note.roles.includes(role) ? [...note.roles, role] : note.roles,
    doneRoles:
      terminal && role && !note.doneRoles.includes(role)
        ? [...note.doneRoles, role]
        : note.doneRoles,
    completed: Math.max(note.completed, Number(evt.completed) || 0),
    total: Math.max(note.total, Number(evt.total) || 0),
    failed,
    running: !finished,
    finished,
    endedAt: now,
  }
}

/**
 * Agent counts, preferring the server's own completed/total counters and
 * falling back to the roles observed on the socket. A batch that never emits
 * ``batch_progress`` would otherwise read as "0/0" the whole way through.
 */
function subagentCounts(note: SubAgentNote): { done: number; total: number } {
  const observed = Math.max(note.roles.length, note.doneRoles.length)
  return {
    done: note.completed > 0 ? note.completed : note.doneRoles.length,
    total: note.total > 0 ? note.total : observed,
  }
}

function sealSubAgentNotes(list: Message[]): Message[] {
  return list.map(item =>
    item.role === 'subagent' && item.subagent && !item.subagent.finished
      ? { ...item, subagent: { ...item.subagent, running: false, finished: true } }
      : item,
  )
}

const WEEKDAY_OPTIONS = [
  { value: 'monday', label: '周一' },
  { value: 'tuesday', label: '周二' },
  { value: 'wednesday', label: '周三' },
  { value: 'thursday', label: '周四' },
  { value: 'friday', label: '周五' },
  { value: 'saturday', label: '周六' },
  { value: 'sunday', label: '周日' },
]

function defaultScheduleDraft(workspaceRoot = ''): ScheduleDraft {
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
  }
}

function scheduleDraftFromTask(task: ScheduleInfo): ScheduleDraft {
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
  }
}

function scheduleRequestBody(draft: ScheduleDraft) {
  const { max_attempts, backoff_seconds, ...rest } = draft
  return {
    ...rest,
    retry_policy: { max_attempts, backoff_seconds },
    timezone_name: Intl.DateTimeFormat().resolvedOptions().timeZone,
  }
}

function scheduleTimeValue(value: string) {
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
function describeSignalName(name: string, tasks: ScheduleInfo[] = []): string {
  const match = /^task:([^:]+):(.+)$/.exec(name || '')
  // A free-form name is quoted too, so it reads as a name rather than as a
  // word that happens to sit between two Chinese characters.
  if (!match) return name ? `「${name}」` : name
  const [, taskId, status] = match
  const taskName = tasks.find(item => item.id === taskId)?.name || taskId
  return `「${taskName}」${scheduleRunStatusLabel(status)}`
}

function scheduleTriggerLabel(task: ScheduleInfo, tasks: ScheduleInfo[] = []): string {
  const trigger = task.trigger || {}
  if (task.trigger_type === 'once') {
    return trigger.at ? `一次 · ${new Date(trigger.at).toLocaleString()}` : '一次性执行'
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

function scheduleRunStatusLabel(status?: string): string {
  if (status === 'running') return '执行中'
  if (status === 'queued') return '等待重试'
  if (status === 'succeeded') return '执行成功'
  if (status === 'failed') return '执行失败'
  if (status === 'interrupted') return '已中断'
  if (status === 'cancelled') return '已取消'
  return '等待首次执行'
}

/** Why this run started, in the terms that will make sense to the reader.
 *
 * "按计划运行" is the safe default and the wrong answer for a run that a
 * signal woke, because the whole point of a signal is that it is not the
 * clock. Saying which one fired is what lets a chain of runs be read as a
 * chain instead of as unrelated activity.
 */
function describeRunTrigger(run: ScheduleRun, tasks: ScheduleInfo[] = []): string {
  const source = run.trigger_source || 'schedule'
  if (source === 'manual') return '手动运行'
  if (source.startsWith('retry') || source === 'automatic_retry') {
    return `重试 · 第 ${run.attempt || 1} 次`
  }
  if (source.startsWith('signal:')) {
    const name = source.slice('signal:'.length)
    return `由信号触发 · ${describeSignalName(name, tasks)}`
  }
  return '按计划运行'
}

/** How a run's cascade context reads, or null when there is none. */
function describeCascade(run: ScheduleRun): string | null {
  const signal = run.config_snapshot?.signal
  if (!signal || typeof signal !== 'object') return null
  const depth = Number(signal.depth || 0)
  // Depth zero means the signal was raised by a person, a clock or the agent
  // itself rather than by another run, so there is no chain to describe.
  if (depth <= 0) return null
  return `信号链第 ${depth + 1} 层`
}

/** How a task's next run reads, or the honest alternative when there is none.
 *
 * "暂无后续执行" is right for a task whose schedule has run out and wrong for
 * one that is waiting on a signal: the second will run, and saying otherwise
 * invites deleting a task that is working exactly as asked.
 */
function describeNextRun(task: ScheduleInfo): string {
  if (task.next_run_at) return new Date(task.next_run_at).toLocaleString()
  if (task.trigger_type === 'signal') return '等待信号触发'
  return '暂无后续执行'
}

function scheduleRunStatusIcon(status?: string) {
  if (status === 'running') return <LoadingOutlined spin />
  if (status === 'queued') return <ClockCircleOutlined />
  if (status === 'succeeded') return <CheckCircleFilled />
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
function scheduleRunAttentionReason(run: ScheduleRun): string {
  const reasons: string[] = []
  const missed = run.missed_count || 0
  if (missed > 0) reasons.push(`本次运行前有 ${missed} 次计划未能执行`)
  if (run.status === 'failed' || run.status === 'interrupted') {
    reasons.push(`本次运行${scheduleRunStatusLabel(run.status)}`)
  }
  return reasons.length ? `${reasons.join('；')}，你还没有查看` : '你还没有查看'
}

function formatScheduleDuration(durationMs?: number | null): string {
  if (durationMs === null || durationMs === undefined) return '—'
  if (durationMs < 1000) return `${durationMs} 毫秒`
  const seconds = Math.round(durationMs / 1000)
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`
}

function formatFileSize(sizeBytes: number): string {
  if (sizeBytes < 1024) return `${sizeBytes} B`
  if (sizeBytes < 1024 * 1024) return `${Math.max(1, Math.round(sizeBytes / 1024))} KB`
  return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`
}

function scheduleDeliveryStatusLabel(status?: string): string {
  if (status === 'stored') return '结果已保存'
  if (status === 'delivered') return '结果已发送'
  if (status === 'skipped') return '无文本输出'
  if (status === 'failed') return '结果交付失败'
  return status || '—'
}

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
  const [unseenFailures, setUnseenFailures] = useState(0)
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
  const [scheduleDetailOpen, setScheduleDetailOpen] = useState(false)
  const [selectedSchedule, setSelectedSchedule] = useState<ScheduleInfo | null>(null)
  const [scheduleRuns, setScheduleRuns] = useState<ScheduleRun[]>([])
  const [selectedScheduleRunId, setSelectedScheduleRunId] = useState<string | null>(null)
  const [scheduleRunsLoading, setScheduleRunsLoading] = useState(false)
  const [scheduleRunOutput, setScheduleRunOutput] = useState<ScheduleRunOutput | null>(null)
  const [scheduleArtifacts, setScheduleArtifacts] = useState<ScheduleArtifact[]>([])
  const [scheduleOutputLoading, setScheduleOutputLoading] = useState(false)
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
  const pendingModelRef = useRef<string | null>(null)
  const queuedMessagesRef = useRef<QueuedMessage[]>([])
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
  const token = localStorage.getItem('agent_token') || ''

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

  const loadMessages = useCallback(
    async (sid: string) => {
      const requestId = ++loadMessagesRequestRef.current
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
        setIsStreaming(operationActive)
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
    [api, makeId, token],
  )

  const loadSessionState = useCallback(async (sid: string) => {
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
        setIsStreaming(String(data.operation_state || 'idle') !== 'idle')
      }
    } catch {
      if (activeSessionRef.current === sid) {
        setSessionState(null)
        setResumingTaskId(null)
        setIsStreaming(false)
      }
    }
  }, [api])

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
          ws.send(JSON.stringify({
            type: 'message',
            text: pending,
            model: pendingModelRef.current,
          }))
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
          const existingId = streamIdRef.current
          if (existingId) {
            updateMessage(existingId, { content: text, streaming: true })
          } else {
            const id = makeId()
            streamIdRef.current = id
            appendMessage({ id, role: 'assistant', content: text, streaming: true })
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
          // Update the open note in place; only start a new one once the
          // previous batch has reported itself finished.
          const now = Date.now()
          const list = messagesRef.current
          const last = list[list.length - 1]
          if (last && last.role === 'subagent' && last.subagent && !last.subagent.finished) {
            updateMessage(last.id, {
              subagent: foldSubAgentEvent(last.subagent, evt, now),
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
          // that stopped.
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
    }

    if (!activeSession) {
      try {
        setCreatingSession(true)
        pendingSendRef.current = text
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
      ws.send(JSON.stringify({
        type: 'message',
        text,
        model: currentModelRef.current,
        attachments,
      }))
      return
    }

    messageApi.warning('连接已断开，正在重新连接…')
    connectWs(activeSession)
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

  const loadSchedules = useCallback(async (silent = false) => {
    try {
      if (!silent) setLoadingView(true)
      const resp = await api('/api/schedules')
      const data = await resp.json()
      setSchedules(data.tasks || [])
      setPermissionProfiles(
        Array.isArray(data.permission_profiles) ? data.permission_profiles : [],
      )
      setUnseenFailures(Number(data.unseen_attention || 0))
      setSelectedSchedule(current => {
        if (!current) return current
        const refreshed = (data.tasks || []).find(
          (item: ScheduleInfo) => item.id === current.id,
        )
        return refreshed || current
      })
    } finally {
      if (!silent) setLoadingView(false)
    }
  }, [api])

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
      setUnseenFailures(Number(data.unseen_attention || 0))
    } catch {
      // A transport failure is not "no failures"; leave the last known count
      // alone rather than clearing a badge the user has not acted on.
    }
  }, [api])

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
  }, [apiHeaders, scheduleDraft, scheduleModalOpen])

  const loadScheduleRuns = useCallback(
    async (taskId: string, selectLatest = false, silent = false) => {
      try {
        if (!silent) setScheduleRunsLoading(true)
        const resp = await api(
          `/api/schedules/${encodeURIComponent(taskId)}/runs?limit=50`,
        )
        const data = await resp.json()
        const runs = Array.isArray(data.runs) ? data.runs as ScheduleRun[] : []
        setScheduleRuns(runs)
        setSelectedSchedule(current => current && current.id === taskId
          ? { ...current, ...(data.task || {}) }
          : current)
        setSelectedScheduleRunId(current => {
          if (selectLatest) return runs[0]?.id || null
          return current && runs.some(run => run.id === current)
            ? current
            : runs[0]?.id || null
        })
      } finally {
        if (!silent) setScheduleRunsLoading(false)
      }
    },
    [api],
  )

  const openScheduleDetails = useCallback((task: ScheduleInfo) => {
    setSelectedSchedule(task)
    setScheduleDetailOpen(true)
    setScheduleRuns([])
    setSelectedScheduleRunId(null)
    setScheduleRunOutput(null)
    void loadScheduleRuns(task.id, true)
  }, [loadScheduleRuns])

  const selectedScheduleRun = useMemo(
    () => scheduleRuns.find(run => run.id === selectedScheduleRunId) || null,
    [scheduleRuns, selectedScheduleRunId],
  )

  const filteredSchedules = useMemo(() => {
    const query = scheduleQuery.trim().toLowerCase()
    return schedules.filter(task => {
      const latestStatus = task.latest_run?.status || ''
      const statusMatches = scheduleStatusFilter === 'all'
        || (scheduleStatusFilter === 'running' && (!!task.active_run_id || latestStatus === 'running'))
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

  useEffect(() => {
    if (view !== 'schedules') return
    const hasRunningTask = schedules.some(task =>
      !!task.active_run_id || task.latest_run?.status === 'running',
    ) || scheduleRuns.some(run => run.status === 'running')
    if (!hasRunningTask) return
    const refresh = () => {
      void loadSchedules(true)
      if (scheduleDetailOpen && selectedSchedule) {
        void loadScheduleRuns(selectedSchedule.id, false, true)
      }
    }
    const timer = window.setInterval(refresh, 2000)
    return () => window.clearInterval(timer)
  }, [
    loadScheduleRuns,
    loadSchedules,
    scheduleDetailOpen,
    scheduleRuns,
    schedules,
    selectedSchedule,
    view,
  ])

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
    if (view === 'plugins') loadPlugins()
    if (view === 'skills') loadSkills()
    if (view === 'schedules') {
      loadSchedules()
      loadSkills(true)
      loadSchedulerHealth()
      loadSignals()
    }
    if (view === 'settings') loadSettings()
  }, [view, loadPlugins, loadSkills, loadSchedules, loadSchedulerHealth, loadSignals, loadSettings])

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
  const handleSessionContainerClick = (
    event: React.MouseEvent<HTMLElement>,
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
        messageApi.warning(`${completed.length} 个任务已处理，${data.skipped.length} 个运行中或不存在的任务已跳过`)
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
    if (scheduleDraft.trigger_type === 'once') {
      if (!scheduleDraft.at || !dayjs(scheduleDraft.at).isValid()) {
        messageApi.warning('请选择执行时间')
        return
      }
      if (dayjs(scheduleDraft.at).valueOf() <= Date.now()) {
        messageApi.warning('执行时间必须晚于当前时间')
        return
      }
    }
    if (scheduleDraft.trigger_type === 'interval' && !scheduleDraft.anchor_at) {
      messageApi.warning('请选择首次执行时间')
      return
    }
    if (['daily', 'weekly', 'weekdays', 'monthly'].includes(scheduleDraft.trigger_type) && !scheduleDraft.time_of_day) {
      messageApi.warning('请选择每天的执行时间')
      return
    }
    if (scheduleDraft.trigger_type === 'signal' && !scheduleDraft.signal_name.trim()) {
      messageApi.warning('请选择或填写要等待的信号')
      return
    }
    try {
      setScheduleSaving(true)
      const target = editingScheduleId
        ? `/api/schedules/${encodeURIComponent(editingScheduleId)}`
        : '/api/schedules'
      const resp = await api(target, {
        method: editingScheduleId ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(scheduleRequestBody(scheduleDraft)),
      })
      const data = await resp.json()
      setScheduleModalOpen(false)
      setEditingScheduleId(null)
      setScheduleDraft(defaultScheduleDraft(sessionState?.workspace_root || config?.workspace_root || ''))
      await loadSchedules()
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
  const acknowledgeScheduleRun = async (task: ScheduleInfo, run: ScheduleRun) => {
    try {
      const resp = await api(
        `/api/schedules/${encodeURIComponent(task.id)}/runs/${encodeURIComponent(run.id)}/acknowledge`,
        { method: 'POST' },
      )
      const data = await resp.json()
      const consumed = data.acknowledged ? 1 : 0
      setScheduleRuns(prev => prev.map(item => item.id === run.id
        ? { ...item, needs_attention: false, acknowledged_at: new Date().toISOString() }
        : item))
      const dropOne = (count?: number) => Math.max(0, (count || 0) - consumed)
      setSchedules(prev => prev.map(item => item.id === task.id
        ? { ...item, unseen_attention: dropOne(item.unseen_attention) }
        : item))
      setSelectedSchedule(current => current && current.id === task.id
        ? { ...current, unseen_attention: dropOne(current.unseen_attention) }
        : current)
      setUnseenFailures(Number(data.unseen_attention || 0))
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
      setUnseenFailures(Number(data.unseen_attention || 0))
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

  const saveSettings = async () => {
    try {
      const values = await form.validateFields()
      const cfg = JSON.parse(configText || '{}')
      cfg.active_provider = values.active_provider
      cfg.providers = cfg.providers || {}
      cfg.providers[values.active_provider] =
        cfg.providers[values.active_provider] || {}
      cfg.providers[values.active_provider].default_model = values.model
      cfg.providers[values.active_provider].max_tokens = values.max_tokens
      cfg.channels = cfg.channels || {}
      cfg.channels.web = cfg.channels.web || {}
      cfg.channels.web.enabled = values.web_enabled
      cfg.channels.feishu = cfg.channels.feishu || {}
      cfg.channels.feishu.enabled = values.feishu_enabled

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
    const providers = config?.providers || {}
    const active = providers[config?.active_provider] || {}
    form.setFieldsValue({
      active_provider: config?.active_provider,
      model: active.default_model,
      max_tokens: active.max_tokens,
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
    { key: 'plugins', icon: <AppstoreOutlined />, label: '插件' },
    { key: 'skills', icon: <ApiOutlined />, label: '技能' },
    {
      key: 'schedules',
      icon: <ClockCircleOutlined />,
      // The badge lives on the navigation entry, not on the schedules page,
      // because the entire problem is a failure that finished while the page
      // was closed.
      label: (
        <span className="nav-label">
          自动化
          {unseenFailures > 0 && (
            <span className="nav-badge" aria-label={`${unseenFailures} 次运行失败未查看`}>
              {unseenFailures > 99 ? '99+' : unseenFailures}
            </span>
          )}
        </span>
      ),
    },
    { key: 'settings', icon: <SettingOutlined />, label: '设置' },
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
    plugins: {
      title: '插件',
      subtitle: `${plugins.length} 个已加载插件`,
    },
    skills: {
      title: '技能',
      subtitle: `${skills.length} 个可用技能`,
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

  const renderAttachment = (item: Message) => {
    const media = item.link ? mediaKindForUrl(item.link) : 'file'
    const label = (item.content || '附件').split(/[\\/]/).pop() || '附件'

    if (media === 'image') {
      return (
        <div className="attachment-card attachment-image-card" key={item.id}>
          <a href={item.link} target="_blank" rel="noreferrer" className="attachment-preview">
            <img src={item.link} alt={label} loading="lazy" />
          </a>
          <div className="attachment-caption">
            <FileTextOutlined />
            <span title={label}>{label}</span>
            <a href={item.link} target="_blank" rel="noreferrer">打开</a>
          </div>
        </div>
      )
    }

    if (media === 'audio' || media === 'video') {
      return (
        <div className="attachment-card attachment-player-card" key={item.id}>
          <div className="attachment-caption">
            <FileTextOutlined />
            <span title={label}>{label}</span>
            <a href={item.link} target="_blank" rel="noreferrer">打开</a>
          </div>
          {media === 'audio' ? (
            <audio className="attachment-player" controls preload="metadata" src={item.link} />
          ) : (
            <video className="attachment-player attachment-video" controls preload="metadata" src={item.link} />
          )}
        </div>
      )
    }

    return (
      <a className="attachment-file-card" href={item.link} target="_blank" rel="noreferrer" key={item.id}>
        <span className="attachment-file-icon"><FileTextOutlined /></span>
        <span className="attachment-file-main">
          <strong title={label}>{label}</strong>
          <small>附件 · 点击打开</small>
        </span>
        <DownOutlined className="attachment-file-arrow" rotate={-90} />
      </a>
    )
  }

  const renderToolEvent = (item: Message) => {
    if (item.tool === 'attachment' || item.link) return renderAttachment(item)
    return (
      <div className="tool-event" key={item.id}>
        <span className={`tool-event-dot ${item.toolState || 'done'}`} />
        <span className="tool-event-name">{item.tool || '文件'}</span>
        <span className="tool-event-state">{toolStateLabel(item.toolState)}</span>
        {(item.content || item.link) && (
          <div className="tool-event-details">
            {item.content && <div className="tool-event-detail">{item.content}</div>}
            {item.link && (
              <a
                className="tool-event-link"
                href={item.link}
                target="_blank"
                rel="noreferrer"
              >
                <FileTextOutlined /> 打开文件
              </a>
            )}
          </div>
        )}
      </div>
    )
  }

  const renderMessage = (item: Message, traceSummary?: React.ReactNode) => {
    if (item.role === 'tool') return renderToolEvent(item)

    if (item.role === 'subagent' && item.subagent) {
      const note = item.subagent
      const { done, total } = subagentCounts(note)
      const roleCount = Math.max(note.roles.length, note.doneRoles.length, note.total)
      const seconds = Math.max(0, (note.endedAt - note.startedAt) / 1000)
      const latest = note.logs[note.logs.length - 1] || ''
      const tone = note.failed > 0 ? 'failed' : note.running ? 'running' : 'done'
      const stateText = note.running
        ? roleCount > 0 ? `${roleCount} 个代理运行中` : '正在运行'
        : note.failed > 0
          ? roleCount > 0 ? `${roleCount} 个代理已结束` : '已结束'
          : roleCount > 0 ? `${roleCount} 个代理已完成` : '已完成'
      return (
        <div key={item.id} className={`subagent-note subagent-note-${tone}`}>
          <button
            type="button"
            className="subagent-note-head"
            aria-expanded={!!note.open}
            onClick={() =>
              updateMessage(item.id, { subagent: { ...note, open: !note.open } })
            }
          >
            <span className="subagent-note-dot" />
            <span className="subagent-note-title">子代理协作</span>
            <span className="subagent-note-state">{stateText}</span>
            {note.failed > 0 && (
              <span className="subagent-note-failed">{note.failed} 个失败</span>
            )}
            {note.running && latest && (
              <span className="subagent-note-latest">{latest}</span>
            )}
            <span className="subagent-note-metrics">
              {note.running ? `${done}/${total || '?'}` : `${seconds.toFixed(1)}s`}
            </span>
            <DownOutlined className="subagent-note-chevron" rotate={note.open ? 180 : 0} />
          </button>
          {note.open && (
            <ol className="subagent-note-logs">
              {note.logs.map((line, index) => (
                <li key={`${item.id}-${index}`}>{line}</li>
              ))}
            </ol>
          )}
        </div>
      )
    }

    if (item.role === 'command' || item.role === 'error') {
      return (
        <div
          key={item.id}
          className={`system-note ${item.role === 'error' ? 'system-note-error' : ''}`}
        >
          <div
            className="markdown"
            dangerouslySetInnerHTML={{ __html: markdownToHtml(item.content, activeSession, token) }}
          />
        </div>
      )
    }

    const isUser = item.role === 'user'
    return (
      <div
        key={item.id}
        ref={isUser ? element => { turnRefs.current[item.id] = element } : undefined}
        className={`message-row ${isUser ? 'message-row-user' : 'message-row-assistant'}`}
      >
        {!isUser && (
          <Avatar className="message-avatar assistant" icon={<RobotOutlined />} />
        )}
        <div className="message-stack">
          <div className={`message-meta ${traceSummary ? 'message-meta-trace' : ''}`}>
            <span className="message-author">{isUser ? '你' : 'Simple Agent'}</span>
            {item.queued && <span className="message-queued">排队中</span>}
            {item.streaming && <span className="message-streaming">正在生成</span>}
            {traceSummary}
          </div>
          <div className={`bubble ${isUser ? 'bubble-user' : 'bubble-assistant'} ${item.attachments?.length ? 'bubble-with-attachments' : ''}`}>
            {item.attachments && item.attachments.length > 0 && (
              <div className="message-attachments">
                {item.attachments.map(attachment => {
                  const href = fileHref(attachment.path, activeSession, token)
                  return (
                    <a className="message-attachment" key={attachment.id || attachment.path} href={href} target="_blank" rel="noreferrer">
                      {attachment.kind === 'image' ? <img src={href} alt={attachment.filename} /> : <FileTextOutlined />}
                      <span>{attachment.filename}</span>
                    </a>
                  )
                })}
              </div>
            )}
            <div
              className="markdown"
              dangerouslySetInnerHTML={{ __html: markdownToHtml(item.content, activeSession, token) }}
            />
            {item.streaming && (
              <span className="typing-dots" aria-label="正在输入">
                <i /><i /><i />
              </span>
            )}
          </div>
          {!isUser && item.content && (
            <Button
              type="text"
              size="small"
              className="message-copy"
              icon={<CopyOutlined />}
              onClick={() => copyMessage(item.content)}
            >
              复制
            </Button>
          )}
        </div>
        {isUser && (
          <Avatar className="message-avatar user" icon={<UserOutlined />} />
        )}
      </div>
    )
  }

  const renderToolTraceContent = (tools: Message[]) => {
    const traceId = tools[0].id
    const expanded = !!expandedTraces[traceId]
    const dots = summariseToolDots(tools)
    const summarised = dots.length < tools.length
    return (
      <div
        className={`tool-trace ${expanded ? 'tool-trace-expanded' : ''}`}
        key={`trace-${traceId}`}
      >
        <button
          type="button"
          className="tool-trace-summary"
          onClick={() => toggleTraceExpanded(traceId)}
        >
          <span
            className="tool-trace-dots"
            aria-hidden="true"
            title={summarised
              ? `${tools.length} 步已归并为 ${dots.length} 段显示，每段取其中最严重的一步；点击展开可看全部`
              : undefined}
          >
            {dots.map((dot, index) => (
              <span
                key={index}
                className={`tool-dot ${dot.state}`}
              />
            ))}
          </span>
          <span className="tool-trace-label">工具轨迹 · {tools.length} 步</span>
          <DownOutlined className="tool-trace-chevron" />
        </button>
        {expanded &&
          createPortal(
            <div
              className="tool-trace-mask"
              onClick={() => toggleTraceExpanded(traceId)}
            >
              <div
                className="tool-trace-modal"
                onClick={event => event.stopPropagation()}
              >
                <div className="tool-trace-modal-head">
                  <span className="tool-trace-modal-title">
                    工具轨迹 · {tools.length} 步
                  </span>
                  <button
                    type="button"
                    className="tool-trace-modal-close"
                    onClick={() => toggleTraceExpanded(traceId)}
                  >
                    <CloseOutlined />
                  </button>
                </div>
                <div className="tool-trace-modal-body">
                  {tools.map(renderToolEvent)}
                </div>
              </div>
            </div>,
            document.body,
          )}
      </div>
    )
  }

  const renderToolTrace = (tools: Message[]) => renderToolTraceContent(tools)

  const renderMessageList = () => {
    const nodes: React.ReactNode[] = []
    // A turn is a user message plus everything up to the next one. Items are
    // grouped without consulting local state so an assistant row received
    // without its user message (another tab's turn, or a restored transcript)
    // still owns its tool trace instead of orphaning it onto its own row.
    let groupUser: Message | null = null
    let groupItems: Message[] = []
    let groupTools: Message[] = []

    const flushTurn = () => {
      if (groupUser) nodes.push(renderMessage(groupUser))
      let traceAttached = false
      groupItems.forEach(item => {
        const shouldAttachTrace =
          !traceAttached && item.role === 'assistant' && groupTools.length > 0
        if (shouldAttachTrace) {
          nodes.push(renderMessage(item, renderToolTraceContent(groupTools)))
          traceAttached = true
        } else {
          nodes.push(renderMessage(item))
        }
      })
      if (groupTools.length && !traceAttached) {
        nodes.push(renderToolTrace(groupTools))
      }
      groupUser = null
      groupItems = []
      groupTools = []
    }

    messages.forEach(item => {
      if (item.role === 'user') {
        flushTurn()
        groupUser = item
      } else if (item.role === 'tool' && item.tool !== 'attachment' && !item.link) {
        // Attachments (image/audio/video/file) are real inline content and
        // must appear in the chat stream, not hidden inside the collapsible
        // tool trace overlay. Regular tool-trace rows carry no ``link``.
        groupTools.push(item)
      } else {
        groupItems.push(item)
      }
    })
    flushTurn()
    return nodes
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

  const renderChat = () => (
    <div className="chat-view">
      {/* .chat-scroll-area spans only the message pane, so the conversation
       * rail is bounded by the composer in pure CSS — no JS height tracking
       * needed when banners or the composer resize. */}
      <div className="chat-scroll-area">
      {conversationTurns.length > 1 && (
        <div
          className="conversation-indicator"
          ref={conversationRailRef}
          aria-label="对话历史"
          onMouseEnter={keepTurnSummary}
          onMouseLeave={scheduleHideTurnSummary}
        >
          <div
            className="conversation-rail"
            role="list"
            style={{ '--conversation-gap': `${conversationGap}px` } as React.CSSProperties}
          >
            <div className="conversation-rail-hitbox" onMouseMove={handleRailMouseMove} onMouseLeave={scheduleHideTurnSummary} />
            {conversationTurns.map((item, index) => (
              <button
                type="button"
                key={item.id}
                ref={element => {
                  // Delete on unmount (React 18 passes null) so refs for
                  // removed turns don't accumulate forever.
                  if (element) conversationMarkerRefs.current[item.id] = element
                  else delete conversationMarkerRefs.current[item.id]
                }}
                className={`conversation-marker ${hoveredTurn?.id === item.id ? 'active' : ''}`}
                style={{
                  '--marker-width': `${hoveredTurnIndex === null || Math.abs(index - hoveredTurnIndex) > 5 ? 11 : Math.max(11, 27 - Math.abs(index - hoveredTurnIndex) * 3)}px`,
                } as React.CSSProperties}
                aria-label={`第 ${index + 1} 轮：${truncate(item.content || '附件', 40)}`}
                onMouseEnter={() => activateTurnIndex(index)}
                onFocus={() => activateTurnIndex(index)}
                onClick={() => turnRefs.current[item.id]?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
              >
                <span className="conversation-marker-line" />
              </button>
            ))}
          </div>
          {hoveredTurn && (() => {
            const item = conversationTurns.find(turn => turn.id === hoveredTurn.id)
            if (!item) return null
            const lines = (item.content || '附件').split(/\n+/).map(line => line.trim()).filter(Boolean)
            return (
              <div
                className="conversation-summary"
                style={{ top: hoveredTurn.top }}
                onMouseEnter={keepTurnSummary}
                onMouseLeave={scheduleHideTurnSummary}
              >
                <strong>{truncate(lines[0] || '附件', 52)}</strong>
                {lines.slice(1, 3).map((line, lineIndex) => <span key={`${item.id}-${lineIndex}`}>{truncate(line, 68)}</span>)}
                <small>第 {conversationTurns.findIndex(turn => turn.id === item.id) + 1} 轮 · 点击定位</small>
              </div>
            )
          })()}
        </div>
      )}
      <div
        className="chat-scroll"
        ref={chatScrollRef}
        onScroll={handleChatScroll}
      >
        <div className="chat-inner">
          {messages.length === 0 ? (
            <div className="chat-empty">
              <div className="chat-empty-mark">S</div>
              <h2>有什么可以帮你？</h2>
              <p>
                输入消息开始对话，或按 <kbd>/</kbd> 使用命令，按{' '}
                <kbd>⌘ K</kbd> 打开命令面板
              </p>
              <div className="chat-empty-suggestions">
                {[
                  '帮我总结当前项目',
                  '查看最近会话',
                  '用 /help 查看可用命令',
                ].map(item => (
                  <Button
                    key={item}
                    size="small"
                    onClick={() => {
                      if (item.startsWith('/')) {
                        setInput(item)
                      } else {
                        setInput(`${item}`)
                      }
                    }}
                  >
                    {item}
                  </Button>
                ))}
              </div>
            </div>
          ) : (
            <>
              {renderMessageList()}
              {isStreaming && !messages.some(item => item.streaming) && (
                <div className="message-row message-row-assistant">
                  <Avatar className="message-avatar assistant" icon={<RobotOutlined />} />
                  <div className="message-stack">
                    <div className="message-meta">
                      <span className="message-author">Simple Agent</span>
                      <span className="message-streaming">
                        正在生成…
                      </span>
                    </div>
                    <div className="bubble bubble-assistant">
                      <span className="typing-dots" aria-label="正在输入">
                        <i /><i /><i />
                      </span>
                    </div>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
      </div>

      <div className="composer-wrap">
        {confirmReq && (() => {
          const risk = confirmRisk(confirmReq.risk_level)
          const timedOut = confirmRemaining <= 0
          const commandText = confirmReq.command || '未知命令'
          const showDetailToggle = confirmOverflowing || confirmDetailOpen
          return (
            <div
              className={`approval-bar approval-risk-${risk}`}
              role="alertdialog"
              aria-label="工具审批"
              aria-live="assertive"
            >
              <div className="approval-head">
                <SafetyCertificateOutlined className="approval-icon" />
                <span className="approval-title">需要你的批准</span>
                <span className={`approval-risk-tag approval-risk-tag-${risk}`}>
                  {CONFIRM_RISK_LABELS[risk]}
                </span>
                <span className="approval-tool">{confirmToolLabel(confirmReq.name)}</span>
                <span className={`approval-timer ${timedOut ? 'expired' : ''}`}>
                  {timedOut ? '已超时，等待服务器确认' : `${confirmRemaining}s 后自动拒绝`}
                </span>
              </div>
              {confirmReq.reason && (
                <div className="approval-reason">{confirmReq.reason}</div>
              )}
              <div
                className={`approval-command ${confirmDetailOpen ? 'expanded' : ''}`}
                ref={approvalCommandRef}
              >
                <code>{commandText}</code>
              </div>
              {showDetailToggle && (
                <button
                  type="button"
                  className="approval-detail-toggle"
                  onClick={() => setConfirmDetailOpen(open => !open)}
                >
                  {confirmDetailOpen ? '收起命令' : '展开完整命令'}
                  <DownOutlined rotate={confirmDetailOpen ? 180 : 0} />
                </button>
              )}
              <div className="approval-actions">
                <span className="approval-hints">
                  <kbd>Esc</kbd> 拒绝
                  <span className="approval-hint-sep" />
                  <kbd>⌘</kbd><kbd>↵</kbd> 允许本次
                  {confirmReq.allow_session && (
                    <>
                      <span className="approval-hint-sep" />
                      <kbd>⌘</kbd><kbd>⇧</kbd><kbd>↵</kbd> 本会话总是允许
                    </>
                  )}
                </span>
                <Button size="small" disabled={timedOut} onClick={() => sendConfirm('deny')}>
                  拒绝
                </Button>
                {confirmReq.allow_session && (
                  <Button
                    size="small"
                    disabled={timedOut}
                    onClick={() => sendConfirm('allow_session')}
                  >
                    本会话总是允许
                  </Button>
                )}
                <Button
                  size="small"
                  type="primary"
                  disabled={timedOut}
                  onClick={() => sendConfirm('allow_once')}
                >
                  允许本次
                </Button>
              </div>
            </div>
          )
        })()}
        {Math.max(
          queuedMessages.length,
          Number(sessionState?.queue?.pending || 0),
        ) > 0 && (
          <div className="message-queue-banner">
            <span className="message-queue-dot" />
            <span>
              已排队 {Math.max(queuedMessages.length, Number(sessionState?.queue?.pending || 0))} 条消息，将按顺序处理
            </span>
            {queuedMessages.length > 0 && <button type="button" onClick={() => {
              const ids = new Set(queuedMessages.map(item => item.id))
              messagesRef.current = messagesRef.current.filter(item => !ids.has(item.id))
              setMessages([...messagesRef.current])
              queuedMessagesRef.current = []
              setQueuedMessages([])
            }}>清空</button>}
          </div>
        )}
        {sessionState?.task?.active_goal &&
          !isStreaming &&
          resumingTaskId !== (sessionState.task.task_id || sessionState.task.active_goal || '') &&
          TASK_INTERRUPTED_STATUSES.has(
            String(sessionState.task.status || '').toLowerCase(),
          ) && (
            <div className="task-guidance-card">
              <div className="task-guidance-head">
                <span className="task-guidance-label">当前任务</span>
                {sessionState.task.status && (
                  <span className="task-guidance-status">
                    {TASK_STATUS_LABELS[String(sessionState.task.status).toLowerCase()]
                      || sessionState.task.status}
                  </span>
                )}
              </div>
              <strong>{truncate(sessionState.task.active_goal, 140)}</strong>
              {sessionState.task.progress && (
                <span>{truncate(sessionState.task.progress, 180)}</span>
              )}
              <div className="task-guidance-next">
                <span>{sessionState.task.next_action ? `下一步：${truncate(sessionState.task.next_action, 180)}` : '任务已中断，可选择继续或放弃'}</span>
                <div className="task-guidance-actions">
                  <Button
                    type="text"
                    size="small"
                    onClick={continueTask}
                  >
                    继续任务
                  </Button>
                  <Button
                    type="text"
                    size="small"
                    danger
                    onClick={dismissTaskGuidance}
                  >
                    放弃任务
                  </Button>
                </div>
              </div>
            </div>
          )}
        {activity && (
          <div
            className={`composer-activity ${interrupting ? 'is-interrupting' : ''}`}
            role="status"
            aria-live="polite"
          >
            <span className="composer-activity-dot" />
            <span>{activity}</span>
          </div>
        )}
        <div className="composer">
          {pendingAttachments.length > 0 && (
            <div className="composer-attachments" aria-label="待发送附件">
              {pendingAttachments.map(item => {
                const fileUrl = fileHref(item.path, activeSession, token)
                const kindLabel = item.kind === 'image'
                  ? '图片'
                  : item.kind === 'document'
                    ? '文档'
                    : item.kind === 'archive'
                      ? '压缩包'
                      : '文件'
                return (
                  <div className="composer-attachment" key={item.id}>
                    {item.kind === 'image' ? (
                      <img src={fileUrl} alt="" />
                    ) : (
                      <span className="composer-attachment-icon"><FileTextOutlined /></span>
                    )}
                    <span className="composer-attachment-main">
                      <strong title={item.filename}>{item.filename}</strong>
                      <small>{kindLabel}{item.size_bytes ? ` · ${Math.max(1, Math.round(item.size_bytes / 1024))} KB` : ''}</small>
                    </span>
                    <Tooltip title="移除附件">
                      <button
                        type="button"
                        className="composer-attachment-remove"
                        aria-label={`移除 ${item.filename}`}
                        onClick={() => setPendingAttachments(prev => prev.filter(attachment => attachment.id !== item.id))}
                      >
                        <CloseOutlined />
                      </button>
                    </Tooltip>
                  </div>
                )
              })}
            </div>
          )}
          {(inlineCommandOpen || inlineCommandEmpty) && (
            <div className="command-popover" role="listbox" aria-label="可用命令">
              <div className="command-popover-head">
                <span>可用命令</span>
                <span>↑ ↓ 选择 · {sendShortcutLabel} 发送 · Tab 补全 · Esc 关闭</span>
              </div>
              {inlineCommandOpen ? filteredCommands.map((command, index) => (
                <button
                  type="button"
                  key={command.name}
                  ref={element => {
                    if (element) commandItemRefs.current[command.name] = element
                    else delete commandItemRefs.current[command.name]
                  }}
                  className={`command-item ${index === commandIndex ? 'active' : ''}`}
                  role="option"
                  aria-selected={index === commandIndex}
                  onMouseEnter={() => {
                    setCommandIndex(index)
                    setCommandIndexPinned(true)
                  }}
                  onMouseDown={event => {
                    event.preventDefault()
                    setInput(`/${command.name} `)
                    setCommandIndex(0)
                  }}
                >
                  {command.kind === 'skill' ? <ApiOutlined /> : <CodeOutlined />}
                  <span className="command-item-main">
                    <strong>{command.usage || `/${command.name}`}</strong>
                    <small>{command.kind === 'skill' ? '技能 · ' : ''}{command.description || '无描述'}</small>
                  </span>
                  <kbd>/</kbd>
                </button>
              )) : (
                <div className="command-empty">没有匹配的命令，{sendShortcutLabel} 将按原文发送</div>
              )}
            </div>
          )}
          <TextArea
            value={input}
            onChange={event => {
              setInput(event.target.value)
              // Any edit re-opens the popover if it was dismissed with Esc.
              setCommandDismissed(false)
            }}
            onKeyDown={handleComposerKeyDown}
            placeholder="给 Simple Agent 发送消息"
            autoSize={{ minRows: 1, maxRows: 6 }}
            variant="borderless"
            disabled={false}
          />
          <div className="composer-footer">
            <div className="composer-tools">
              <Tooltip title="添加图片或文件">
                <Button type="text" className="composer-icon-button" aria-label="添加附件" icon={<PaperClipOutlined />} onClick={() => fileInputRef.current?.click()} />
              </Tooltip>
              <input ref={fileInputRef} type="file" multiple hidden onChange={handleFilesSelected} accept="image/*,.pdf,.txt,.csv,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.zip" />
              <div className="composer-context-controls">
                <Tooltip title={sessionState?.workspace_root ? `切换工作区：${sessionState.workspace_root}` : '选择 Agent 接下来工作的项目文件夹'}>
                  <button
                    type="button"
                    className={`workspace-picker ${sessionState?.workspace_status === 'missing' ? 'workspace-picker-missing' : ''}`}
                    aria-label={sessionState?.workspace_root
                      ? `当前工作区：${sessionState.workspace_root}，${sessionState.workspace_write ? '可写' : '只读'}，点击切换`
                      : '选择工作区'}
                    onClick={pickWorkspace}
                  >
                    <FolderOpenOutlined aria-hidden="true" />
                    {sessionState?.workspace_root ? (
                      <>
                        <strong title={sessionState.workspace_root}>{compactWorkspacePath(sessionState.workspace_root)}</strong>
                        <span className="workspace-status-dot" aria-hidden="true" />
                      </>
                    ) : (
                      <strong>选择工作区</strong>
                    )}
                  </button>
                </Tooltip>
                <Dropdown
                  trigger={['click']}
                  placement="topLeft"
                  menu={{
                  items: [
                    {
                      key: 'permission-title',
                      label: '当前会话权限',
                      disabled: true,
                    },
                    ...[
                      ['ask', '需确认', '敏感操作逐项确认'],
                      ['medium', '中权限', '高风险操作仍需确认'],
                      ['high', '高权限', '大多数操作自动执行'],
                      ['full', '完全访问', '最高权限，仍保留安全拦截'],
                    ].map(([key, label, description]) => ({
                      key: `level:${key}`,
                      label: (
                        <span className="permission-menu-item">
                          <span>
                            <strong>{label}</strong>
                            <small>{description}</small>
                          </span>
                          {permissionLevel === key && <CheckCircleFilled />}
                        </span>
                      ),
                      onClick: () => updateSessionPermissions({ level: key }),
                    })),
                    { type: 'divider' as const },
                    {
                      key: 'sandbox-title',
                      label: '文件沙箱',
                      disabled: true,
                    },
                    ...[
                      ['restricted', '工作区', '仅限当前工作区'],
                      ['read_all', '全盘可读', '读取范围更广，写入仍受限'],
                      ['none', '无沙箱', '整机访问，仅完全访问可用'],
                    ].map(([key, label, description]) => ({
                      key: `sandbox:${key}`,
                      disabled: key === 'none' && permissionLevel !== 'full',
                      label: (
                        <span className="permission-menu-item">
                          <span>
                            <strong>{label}</strong>
                            <small>{description}</small>
                          </span>
                          {sandboxMode === key && <CheckCircleFilled />}
                        </span>
                      ),
                      onClick: () => updateSessionPermissions({ sandbox: key }),
                    })),
                  ],
                  }}
                >
                  <Button
                    type="text"
                    className="permission-button"
                    icon={<SafetyCertificateOutlined />}
                    aria-label={`当前权限：${permissionLabel}`}
                  >
                    <span className="permission-button-label">{permissionLabel}</span>
                  </Button>
                </Dropdown>
                <Select
                  value={currentModel || undefined}
                  placeholder={modelSelectPlaceholder}
                  onChange={handleModelChange}
                  options={modelOptions}
                  className="model-select"
                  style={{ width: modelSelectWidth }}
                  popupMatchSelectWidth={false}
                  variant="borderless"
                />
              </div>
            </div>
            <div className="composer-actions">
              {isStreaming && (
                <Tooltip title={interrupting ? '正在中断…' : '终止生成'}>
                  <Button
                    type="default"
                    danger
                    className="send-button stop-button"
                    aria-label={interrupting ? '正在中断' : '终止生成'}
                    icon={<StopOutlined />}
                    loading={interrupting}
                    onClick={stopStreaming}
                  />
                </Tooltip>
              )}
              <Tooltip title={isStreaming ? '排队发送' : `发送 (${sendShortcutLabel})`}>
                <Button
                  type="primary"
                  className="send-button"
                  aria-label={isStreaming ? '排队发送' : '发送'}
                  icon={<ArrowUpOutlined />}
                  disabled={!isStreaming && (!composerSendable || creatingSession)}
                  onClick={() => sendMessage(resolvedComposerText)}
                />
              </Tooltip>
            </div>
          </div>
        </div>
      </div>
    </div>
  )

  const renderSessions = () => (
    <div className="page-view">
      <div className="page-head">
        <div>
          <h2>{pageMeta.sessions.title}</h2>
          <p>{pageMeta.sessions.subtitle}</p>
        </div>
        <Space>
          {filteredSessions.length > 0 && (
            <Checkbox
              checked={allFilteredSessionsSelected}
              indeterminate={selectedSessionIds.length > 0 && !allFilteredSessionsSelected}
              onChange={event => {
                setSelectedSessionIds(event.target.checked
                  ? Array.from(new Set([...selectedSessionIds, ...filteredSessions.map(item => item.session_id)]))
                  : selectedSessionIds.filter(id => !filteredSessions.some(item => item.session_id === id)))
              }}
            >
              全选
            </Checkbox>
          )}
          {selectedSessionIds.length > 0 && (
            <Button danger icon={<DeleteOutlined />} onClick={deleteSelectedSessions}>
              删除选中 ({selectedSessionIds.length})
            </Button>
          )}
          <Input
            prefix={<SearchOutlined />}
            placeholder="搜索会话"
            value={sessionSearch}
            onChange={event => setSessionSearch(event.target.value)}
            allowClear
            style={{ width: 240 }}
          />
        </Space>
      </div>

      {loadingSessions ? (
        <Skeleton active paragraph={{ rows: 8 }} />
      ) : filteredSessions.length === 0 ? (
        <Empty description="没有匹配的会话" className="page-empty" />
      ) : (
        <Row gutter={[16, 16]}>
          {filteredSessions.map(item => (
            <Col xs={24} sm={12} xl={8} key={item.session_id}>
              <Card
                className={`session-card ${item.session_id === activeSession ? 'session-card-active' : ''}`}
                hoverable
                onClick={event => handleSessionContainerClick(event, item.session_id)}
              >
                <div className="session-card-head">
                  <div className="session-card-title">
                    <Checkbox
                      checked={selectedSessionIds.includes(item.session_id)}
                      onClick={event => event.stopPropagation()}
                      onChange={event => {
                        setSelectedSessionIds(current => event.target.checked
                          ? [...current, item.session_id]
                          : current.filter(id => id !== item.session_id))
                      }}
                    />
                    <span>{item.title || '未命名会话'}</span>
                    {item.live && <Badge status="processing" />}
                  </div>
                  {pendingDeleteSessionId === item.session_id ? (
                    <div
                      className="session-item-delete-actions session-card-delete-actions"
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
                <div className="session-card-id">
                  <Typography.Text code>{item.session_id.slice(0, 18)}</Typography.Text>
                </div>
                <div className="session-card-footer">
                  <Tag>
                    {item.live ? '动态会话' : '持久会话'}
                  </Tag>
                  <span>
                    <MessageOutlined /> {item.turn_count || 0} 轮
                  </span>
                  <span>
                    <ClockCircleOutlined /> {relativeTime(item.last_activity)}
                  </span>
                </div>
              </Card>
            </Col>
          ))}
        </Row>
      )}
    </div>
  )

  const renderPlugins = () => (
    <div className="page-view plugins-view">
      <div className="page-head">
        <div>
          <h2>{pageMeta.plugins.title}</h2>
          <p>{pageMeta.plugins.subtitle}</p>
        </div>
        <Input
          prefix={<SearchOutlined />}
          placeholder="搜索插件"
          value={pluginSearch}
          onChange={event => setPluginSearch(event.target.value)}
          allowClear
          className="workspace-search"
        />
      </div>

      {loadingView ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : filteredPlugins.length === 0 ? (
        <Empty description="暂无插件" className="page-empty" />
      ) : (
        <Row gutter={[16, 16]}>
          {filteredPlugins.map(item => (
            <Col xs={24} md={12} xl={8} key={item.name}>
              <Card className="entity-card">
                <div className="entity-card-head">
                  <span className="entity-icon">
                    <ThunderboltOutlined />
                  </span>
                  <div className="entity-title">
                    <strong>{item.name}</strong>
                    <small>v{item.version || '—'}</small>
                  </div>
                  <Switch
                    size="small"
                    checked={item.enabled}
                    onChange={checked => togglePlugin(item, checked)}
                  />
                  {item.source === 'user' && <Button type="text" danger size="small" icon={<DeleteOutlined />} onClick={() => deletePlugin(item)} />}
                </div>
                <p className="entity-description">
                  {item.description || '暂无描述'}
                </p>
                <div className="entity-meta">
                  <span>来源</span>
                  <code>{item.source || '-'}</code>
                </div>
              </Card>
            </Col>
          ))}
        </Row>
      )}
    </div>
  )

  const renderSkills = () => (
    <div className="page-view skills-view">
      <div className="page-head skills-head">
        <div>
          <div className="eyebrow">CAPABILITIES</div>
          <h2>{pageMeta.skills.title}</h2>
          <p>管理 Agent 可调用的能力，查看来源与调用范围。</p>
        </div>
        <div className="skills-summary">
          <span><strong>{skills.length}</strong> 全部</span>
          <span><strong>{skills.filter(item => item.user_invocable).length}</strong> 可调用</span>
          <span><strong>{skills.filter(item => !item.user_invocable).length}</strong> 内部</span>
        </div>
      </div>

      <div className="skills-toolbar">
        <div className="segmented-control" role="tablist" aria-label="技能筛选">
          {[
            ['all', '全部'],
            ['callable', '可调用'],
            ['internal', '内部'],
          ].map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="tab"
              aria-selected={skillFilter === value}
              className={skillFilter === value ? 'active' : ''}
              onClick={() => setSkillFilter(value as 'all' | 'callable' | 'internal')}
            >
              {label}
            </button>
          ))}
        </div>
        <Input
          prefix={<SearchOutlined />}
          placeholder="按名称、ID 或来源搜索"
          value={skillSearch}
          onChange={event => setSkillSearch(event.target.value)}
          allowClear
          className="workspace-search skills-search"
        />
      </div>

      {loadingView ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : filteredSkills.length === 0 ? (
        <Empty description="暂无技能" className="page-empty" />
      ) : (
        <div className="skills-list">
          {filteredSkills.map(item => (
            <div className="skill-row" key={item.id}>
              <span className="skill-row-icon"><ApiOutlined /></span>
              <div className="skill-row-main">
                <div className="skill-row-title">
                  <strong>{item.name || item.id}</strong>
                  <Tag>{item.user_invocable ? '可调用' : '内部'}</Tag>
                </div>
                <div className="skill-row-id">{item.id}</div>
                <p>{item.description || '暂无描述'}</p>
              </div>
              <div className="skill-row-side">
                <span className="skill-source">{item.source || '未知来源'}</span>
                <Button
                  type="text"
                  size="small"
                  icon={<CopyOutlined />}
                  onClick={() => copyMessage(item.id)}
                >复制 ID</Button>
                {item.source === 'user' && <Button type="text" danger size="small" icon={<DeleteOutlined />} onClick={() => deleteSkill(item)} />}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )

  const renderSchedules = () => (
    <div className="page-view schedules-view">
      <div className="page-head schedule-page-head">
        <div>
          <div className="schedule-title-row">
            <h2>自动化</h2>
            <span className={`scheduler-health ${schedulerHealth.status === 'online' ? 'online' : 'offline'}`}>
              <i />{schedulerHealth.status === 'online' ? '调度器在线' : '调度器离线'}
            </span>
          </div>
          <p>让 Agent 在指定时间执行，或等某个信号发生后接着执行。</p>
        </div>
        <Space>
          <Tooltip title="刷新运行状态">
            <Button
              aria-label="刷新运行状态"
              icon={<ReloadOutlined />}
              onClick={() => loadSchedules()}
            />
          </Tooltip>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreateSchedule}>新建任务</Button>
        </Space>
      </div>
      <div className="schedule-toolbar">
        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder="搜索任务、项目目录或执行内容"
          value={scheduleQuery}
          onChange={event => setScheduleQuery(event.target.value)}
        />
        <div className="schedule-filter" role="group" aria-label="任务状态筛选">
          {[
            ['all', '全部'],
            ['running', '运行中'],
            ['failed', '失败'],
            ['paused', '已暂停'],
          ].map(([value, label]) => (
            <button key={value} type="button" className={scheduleStatusFilter === value ? 'active' : ''} onClick={() => setScheduleStatusFilter(value)}>{label}</button>
          ))}
        </div>
        {selectedScheduleIds.length > 0 && (
          <Space className="schedule-bulk-actions">
            <span>已选 {selectedScheduleIds.length} 个</span>
            <Button size="small" onClick={() => bulkScheduleAction('enable')}>启用</Button>
            <Button size="small" onClick={() => bulkScheduleAction('disable')}>暂停</Button>
            <Button size="small" danger icon={<DeleteOutlined />} onClick={() => bulkScheduleAction('delete')}>删除</Button>
          </Space>
        )}
      </div>
      {/* Not "定时任务" any more, because the form no longer only offers
          times -- calling it that while the user is choosing a signal to wait
          for would be the form contradicting itself. */}
      <Modal
        open={scheduleModalOpen}
        title={editingScheduleId ? '编辑任务' : '新建任务'}
        width={700}
        okText={editingScheduleId ? '保存修改' : '创建任务'}
        cancelText="取消"
        confirmLoading={scheduleSaving}
        onCancel={() => {
          setScheduleModalOpen(false)
          setEditingScheduleId(null)
        }}
        onOk={saveSchedule}
        className="schedule-modal"
      >
        <div className="schedule-form">
          <div className="schedule-field">
            <label>任务名称</label>
            <Input
              maxLength={80}
              placeholder="例如：生成每日项目进展摘要"
              value={scheduleDraft.name}
              onChange={event => setScheduleDraft({ ...scheduleDraft, name: event.target.value })}
            />
          </div>

          <div className="schedule-field">
            <label>任务类型</label>
            <div className="schedule-action-picker" role="radiogroup" aria-label="任务类型">
              <button
                type="button"
                role="radio"
                aria-checked={scheduleDraft.action_type === 'agent_task'}
                className={scheduleDraft.action_type === 'agent_task' ? 'active' : ''}
                onClick={() => setScheduleDraft({ ...scheduleDraft, action_type: 'agent_task' })}
              >
                <RobotOutlined />
                <span><strong>Agent 执行任务</strong><small>让 Agent 按要求完成具体工作</small></span>
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={scheduleDraft.action_type === 'message'}
                className={scheduleDraft.action_type === 'message' ? 'active' : ''}
                onClick={() => setScheduleDraft({ ...scheduleDraft, action_type: 'message' })}
              >
                <MessageOutlined />
                <span><strong>定时提醒</strong><small>到时间后发送一条固定内容</small></span>
              </button>
            </div>
          </div>

          {scheduleDraft.action_type === 'agent_task' && (
            <>
              <div className="schedule-form-section-title">运行环境</div>
              <div className="schedule-field">
                <label>项目文件夹</label>
                <Input
                  prefix={<FolderOpenOutlined />}
                  placeholder="Agent 执行任务时使用的项目目录"
                  value={scheduleDraft.workspace_root}
                  onChange={event => setScheduleDraft({ ...scheduleDraft, workspace_root: event.target.value })}
                />
                <small className="schedule-field-hint">任务会固定使用此目录，不受当前会话切换影响。</small>
              </div>
              <div className="schedule-field-grid schedule-field-grid-three">
                <div className="schedule-field">
                  <label>模型</label>
                  <Select
                    allowClear
                    showSearch
                    placeholder="默认模型"
                    value={scheduleDraft.model_override || undefined}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, model_override: value || '' })}
                    options={modelOptions}
                  />
                </div>
                <div className="schedule-field">
                  <label>上下文</label>
                  <Select
                    value={scheduleDraft.context_policy}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, context_policy: value })}
                    options={[
                      { value: 'stateless', label: '每次独立（推荐）' },
                      { value: 'task_history', label: '参考历史成功摘要' },
                      { value: 'shared_memory', label: '共享任务长期上下文' },
                    ]}
                  />
                </div>
                <div className="schedule-field">
                  <label>权限</label>
                  <Select
                    value={scheduleDraft.permission_profile}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, permission_profile: value })}
                    options={permissionProfileOptions.map(item => ({
                      value: item.key,
                      label: item.label,
                    }))}
                  />
                  {activePermissionProfile && (
                    <small className="schedule-field-hint">
                      {activePermissionProfile.detail || activePermissionProfile.summary}
                    </small>
                  )}
                </div>
              </div>
              <div className="schedule-field">
                <label>指定技能</label>
                <Select
                  mode="multiple"
                  allowClear
                  showSearch
                  optionFilterProp="label"
                  placeholder="不指定时由 Agent 自主选择"
                  value={scheduleDraft.selected_skills}
                  onChange={value => setScheduleDraft({ ...scheduleDraft, selected_skills: value })}
                  options={skills.filter(item => item.user_invocable).map(item => ({
                    value: item.id,
                    label: item.name || item.id,
                  }))}
                />
                <small className="schedule-field-hint">任务启动前会验证技能；被删除或停用的技能会让运行明确失败。</small>
              </div>
            </>
          )}

          <div className="schedule-form-section-title">
            {scheduleDraft.trigger_type === 'signal' ? '触发方式' : '执行时间'}
          </div>

          <div className="schedule-field">
            <label>执行计划</label>
            <Select
              value={scheduleDraft.trigger_type}
              onChange={value => setScheduleDraft({ ...scheduleDraft, trigger_type: value })}
              options={[
                { value: 'once', label: '指定日期和时间' },
                { value: 'interval', label: '固定间隔' },
                { value: 'daily', label: '每天' },
                { value: 'weekly', label: '每周' },
                { value: 'weekdays', label: '工作日（周一至周五）' },
                { value: 'monthly', label: '每月指定日期' },
                { value: 'signal', label: '当某个信号发生时' },
              ]}
            />
          </div>

          {scheduleDraft.trigger_type === 'signal' && (
            <div className="schedule-field">
              <label>等待的信号</label>
              {/* Autocomplete rather than a plain select: the known names are
                  the ones that can actually fire today, but a name nobody has
                  emitted yet is legitimate when the emitter is being set up in
                  the same breath. Matching is exact, so a near-miss never
                  fires — which is exactly why the known names are offered
                  instead of left to memory. */}
              <AutoComplete
                value={scheduleDraft.signal_name}
                onChange={value => setScheduleDraft({ ...scheduleDraft, signal_name: String(value || '') })}
                placeholder="选择已有信号，或填写一个将要发出的信号名"
                options={signals.map(item => ({
                  value: item.name,
                  label: `${describeSignalName(item.name, schedules)}${item.subscriber_count > 0 ? `（已有 ${item.subscriber_count} 个任务等待）` : ''}`,
                }))}
                filterOption={(input, option) => String(option?.value || '').toLowerCase().includes(String(input || '').toLowerCase())}
                style={{ width: '100%' }}
              />
              {signalsWaiting.length > 0 && (
                <div className="schedule-field-hint">
                  还没有发出过的信号：{signalsWaiting.map(item => item.name).join('、')}
                </div>
              )}
            </div>
          )}

          {scheduleDraft.trigger_type === 'once' && (
            <div className="schedule-field">
              <label>执行时间</label>
              <DatePicker
                showTime={{ format: 'HH:mm' }}
                format="YYYY年M月D日 HH:mm"
                value={scheduleDraft.at ? dayjs(scheduleDraft.at) : null}
                onChange={value => setScheduleDraft({ ...scheduleDraft, at: value?.toISOString() || '' })}
                disabledDate={current => !!current && current.endOf('day').valueOf() < Date.now()}
                style={{ width: '100%' }}
              />
            </div>
          )}

          {scheduleDraft.trigger_type === 'interval' && (
            <div className="schedule-field-grid">
              <div className="schedule-field">
                <label>重复间隔</label>
                <Space.Compact block>
                  <InputNumber
                    min={1}
                    max={999}
                    value={scheduleDraft.every}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, every: Number(value || 1) })}
                    style={{ width: '45%' }}
                  />
                  <Select
                    value={scheduleDraft.unit}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, unit: value })}
                    options={[
                      { value: 'minutes', label: '分钟' },
                      { value: 'hours', label: '小时' },
                      { value: 'days', label: '天' },
                      { value: 'weeks', label: '周' },
                    ]}
                    style={{ width: '55%' }}
                  />
                </Space.Compact>
              </div>
              <div className="schedule-field">
                <label>首次执行</label>
                <DatePicker
                  showTime={{ format: 'HH:mm' }}
                  format="YYYY-MM-DD HH:mm"
                  value={scheduleDraft.anchor_at ? dayjs(scheduleDraft.anchor_at) : null}
                  onChange={value => setScheduleDraft({ ...scheduleDraft, anchor_at: value?.toISOString() || '' })}
                  disabledDate={current => !!current && current.endOf('day').valueOf() < Date.now()}
                  style={{ width: '100%' }}
                />
              </div>
            </div>
          )}

          {(['daily', 'weekly', 'weekdays', 'monthly'].includes(scheduleDraft.trigger_type)) && (
            <div className={`schedule-field-grid ${['daily', 'weekdays'].includes(scheduleDraft.trigger_type) ? 'single' : ''}`}>
              {scheduleDraft.trigger_type === 'weekly' && (
                <div className="schedule-field">
                  <label>星期</label>
                  <Select
                    value={scheduleDraft.day_of_week}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, day_of_week: value })}
                    options={WEEKDAY_OPTIONS}
                  />
                </div>
              )}
              {scheduleDraft.trigger_type === 'monthly' && (
                <div className="schedule-field">
                  <label>日期</label>
                  <InputNumber
                    min={1}
                    max={31}
                    value={scheduleDraft.day_of_month}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, day_of_month: Number(value || 1) })}
                    style={{ width: '100%' }}
                  />
                  <small className="schedule-field-hint">没有该日期的月份将自动跳过。</small>
                </div>
              )}
              <div className="schedule-field">
                <label>执行时间</label>
                <TimePicker
                  format="HH:mm"
                  minuteStep={5}
                  value={scheduleTimeValue(scheduleDraft.time_of_day)}
                  onChange={value => setScheduleDraft({ ...scheduleDraft, time_of_day: value?.format('HH:mm') || '' })}
                  style={{ width: '100%' }}
                />
              </div>
            </div>
          )}

          <div className="schedule-field">
            <label>{scheduleDraft.action_type === 'agent_task' ? '任务执行要求' : '提醒内容'}</label>
            {scheduleDraft.action_type === 'agent_task' ? (
              <TextArea
                rows={5}
                maxLength={6000}
                placeholder="说明要完成的工作、涉及的范围和期望输出。任务会在独立运行环境中交给 Agent 执行。"
                value={scheduleDraft.prompt}
                onChange={event => setScheduleDraft({ ...scheduleDraft, prompt: event.target.value })}
              />
            ) : (
              <TextArea
                rows={4}
                maxLength={2000}
                placeholder="输入到时间后需要发送的提醒内容"
                value={scheduleDraft.message_text}
                onChange={event => setScheduleDraft({ ...scheduleDraft, message_text: event.target.value })}
              />
            )}
            <div className="schedule-field-meta">
              <small>时区：{Intl.DateTimeFormat().resolvedOptions().timeZone}</small>
              <small>{scheduleDraft.action_type === 'agent_task'
                ? `${scheduleDraft.prompt.length} / 6000`
                : `${scheduleDraft.message_text.length} / 2000`}</small>
            </div>
          </div>

          {scheduleDraft.action_type === 'agent_task' && (
            <>
              <div className="schedule-form-section-title">失败与超时</div>
              <div className="schedule-field-grid schedule-field-grid-three">
                <div className="schedule-field">
                  <label>超时（分钟）</label>
                  <InputNumber
                    min={1}
                    max={10080}
                    value={Math.ceil(scheduleDraft.timeout_seconds / 60)}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, timeout_seconds: Number(value || 1) * 60 })}
                    style={{ width: '100%' }}
                  />
                </div>
                <div className="schedule-field">
                  <label>最大尝试次数</label>
                  <InputNumber
                    min={1}
                    max={5}
                    value={scheduleDraft.max_attempts}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, max_attempts: Number(value || 1) })}
                    style={{ width: '100%' }}
                  />
                </div>
                <div className="schedule-field">
                  <label>重试间隔（秒）</label>
                  <InputNumber
                    min={0}
                    max={86400}
                    disabled={scheduleDraft.max_attempts <= 1}
                    value={scheduleDraft.backoff_seconds}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, backoff_seconds: Number(value || 0) })}
                    style={{ width: '100%' }}
                  />
                </div>
              </div>
            </>
          )}

          <div className="schedule-preview">
            <div><ClockCircleOutlined /><strong>{scheduleDraft.trigger_type === 'signal' ? '触发条件' : '未来执行时间'}</strong></div>
            {scheduleDraft.trigger_type === 'signal' ? (
              // No list of times to show, and showing an empty one would read
              // as "this will never run" rather than "this waits".
              <span>
                {scheduleDraft.signal_name.trim()
                  ? `收到信号「${scheduleDraft.signal_name.trim()}」时运行一次，没有固定时间。`
                  : '选择一个信号后，任务会在它被发出时运行。'}
              </span>
            ) : schedulePreview.length > 0 ? (
              <ol>{schedulePreview.map(item => <li key={item}>{new Date(item).toLocaleString()}</li>)}</ol>
            ) : (
              <span>{schedulePreviewError || '填写完整任务信息后显示未来 5 次执行时间'}</span>
            )}
          </div>
        </div>
      </Modal>
      <Drawer
        open={scheduleDetailOpen}
        width="min(880px, calc(100vw - 20px))"
        title={selectedSchedule?.name || '任务运行详情'}
        onClose={() => setScheduleDetailOpen(false)}
        className="schedule-detail-drawer"
        extra={selectedSchedule && (
          <Space>
            <Button icon={<ThunderboltOutlined />} onClick={() => runScheduleNow(selectedSchedule)} disabled={schedulerHealth.status !== 'online' || !!selectedSchedule.active_run_id}>立即运行</Button>
            <Button icon={<EditOutlined />} onClick={() => openEditSchedule(selectedSchedule)}>编辑</Button>
            <Dropdown
              menu={{ items: [
                { key: 'duplicate', icon: <CopyOutlined />, label: '创建副本', onClick: () => duplicateSchedule(selectedSchedule) },
                { key: 'delete', icon: <DeleteOutlined />, label: '删除任务', danger: true, disabled: !!selectedSchedule.active_run_id, onClick: () => deleteSchedule(selectedSchedule) },
              ] }}
            >
              <Button aria-label="更多任务操作" icon={<MoreOutlined />} />
            </Dropdown>
            <Tooltip title="刷新运行记录">
              <Button
                aria-label="刷新运行记录"
                icon={<ReloadOutlined />}
                loading={scheduleRunsLoading}
                onClick={() => loadScheduleRuns(selectedSchedule.id)}
              />
            </Tooltip>
          </Space>
        )}
      >
        {selectedSchedule && (
          <div className="schedule-detail">
            <div className="schedule-detail-overview">
              <div className="schedule-detail-overview-main">
                <span className="schedule-card-icon">
                  {selectedSchedule.kind === 'agent_prompt' ? <RobotOutlined /> : <MessageOutlined />}
                </span>
                <div>
                  <div className="schedule-detail-tags">
                    <Tag>{selectedSchedule.kind === 'agent_prompt' ? 'Agent 任务' : selectedSchedule.kind === 'message' ? '定时提醒' : '系统任务'}</Tag>
                    <Tag>{scheduleTriggerLabel(selectedSchedule, schedules)}</Tag>
                    <Tag>{selectedSchedule.enabled === false ? '已暂停' : '已启用'}</Tag>
                    {selectedSchedule.context_policy && <Tag>{selectedSchedule.context_policy === 'stateless' ? '独立上下文' : selectedSchedule.context_policy === 'task_history' ? '任务历史' : '共享记忆'}</Tag>}
                    {selectedSchedule.kind === 'agent_prompt' && (
                      <Tag>{permissionProfileLabel(selectedSchedule.permission_profile)}</Tag>
                    )}
                  </div>
                  <p>{selectedSchedule.kind === 'agent_prompt'
                    ? selectedSchedule.payload?.prompt
                    : selectedSchedule.kind === 'system_job'
                      ? selectedSchedule.payload?.job_name
                      : selectedSchedule.payload?.message_text}</p>
                  {selectedSchedule.workspace_root && (
                    <div className="schedule-workspace"><FolderOpenOutlined />{selectedSchedule.workspace_root}</div>
                  )}
                </div>
              </div>
              <div className="schedule-detail-next">
                <small>下次执行</small>
                <strong>{describeNextRun(selectedSchedule)}</strong>
              </div>
            </div>

            {scheduleRunsLoading && scheduleRuns.length === 0 ? (
              <div className="schedule-detail-loading"><Spin /><span>正在读取运行记录</span></div>
            ) : scheduleRuns.length === 0 ? (
              <Empty description="该任务尚未执行" className="schedule-runs-empty" />
            ) : (
              <div className="schedule-detail-grid">
                <aside className="schedule-run-list" aria-label="运行历史">
                  <div className="schedule-detail-section-title">
                    <strong>运行历史</strong>
                    <span className="schedule-run-history-tail">
                      {scheduleRuns.length} 次
                      {(selectedSchedule.unseen_attention || 0) > 0 && (
                        <Button
                          type="link"
                          size="small"
                          onClick={() => clearScheduleAttention(selectedSchedule.id)}
                        >全部标记已读</Button>
                      )}
                    </span>
                  </div>
                  <div className="schedule-run-items">
                    {scheduleRuns.map(run => (
                      <button
                        key={run.id}
                        type="button"
                        className={`schedule-run-item ${selectedScheduleRunId === run.id ? 'active' : ''}`}
                        onClick={() => setSelectedScheduleRunId(run.id)}
                      >
                        <span className={`schedule-run-status-icon status-${run.status}`}>
                          {scheduleRunStatusIcon(run.status)}
                        </span>
                        <span className="schedule-run-item-main">
                          <strong>{scheduleRunStatusLabel(run.status)}</strong>
                          <small>{run.started_at ? new Date(run.started_at).toLocaleString() : '等待开始'}</small>
                        </span>
                        {run.needs_attention && (
                          <span
                            className="schedule-run-unseen"
                            title={scheduleRunAttentionReason(run)}
                            aria-label={scheduleRunAttentionReason(run)}
                          />
                        )}
                        {(run.missed_count || 0) > 0 && (
                          <span
                            className="schedule-run-missed"
                            title={`本次运行前有 ${run.missed_count} 次计划未能执行`}
                          >跳过 {run.missed_count} 次</span>
                        )}
                        <span className="schedule-run-duration">{formatScheduleDuration(run.duration_ms)}</span>
                      </button>
                    ))}
                  </div>
                </aside>

                <section className="schedule-run-detail" aria-label="运行结果">
                  {selectedScheduleRun && (
                    <>
                      <div className="schedule-run-detail-head">
                        <div>
                          <span className={`schedule-run-status status-${selectedScheduleRun.status}`}>
                            {scheduleRunStatusIcon(selectedScheduleRun.status)}
                            {scheduleRunStatusLabel(selectedScheduleRun.status)}
                          </span>
                          <div
                            className="schedule-run-summary markdown"
                            dangerouslySetInnerHTML={{ __html: markdownToHtml(selectedScheduleRun.summary || '暂无运行摘要') }}
                          />
                        </div>
                        <Space>
                        {selectedScheduleRun.needs_attention && (
                          <Button
                            size="small"
                            icon={<CheckOutlined />}
                            title={scheduleRunAttentionReason(selectedScheduleRun)}
                            onClick={() => acknowledgeScheduleRun(selectedSchedule, selectedScheduleRun)}
                          >标记已读</Button>
                        )}
                        {selectedScheduleRun.status === 'running' && (
                          <Button danger size="small" icon={<StopOutlined />} loading={!!selectedScheduleRun.cancel_requested_at} onClick={() => cancelScheduleRun(selectedSchedule, selectedScheduleRun)}>
                            {selectedScheduleRun.cancel_requested_at ? '正在取消' : '取消运行'}
                          </Button>
                        )}
                        {selectedScheduleRun.status !== 'running' && (
                          <Dropdown menu={{ items: [
                            { key: 'snapshot', label: '使用本次运行配置', onClick: () => retryScheduleRun(selectedSchedule, selectedScheduleRun, false) },
                            { key: 'latest', label: '使用任务当前配置', onClick: () => retryScheduleRun(selectedSchedule, selectedScheduleRun, true) },
                          ] }}>
                            <Button size="small" icon={<ReloadOutlined />} disabled={!!selectedSchedule.active_run_id}>重试</Button>
                          </Dropdown>
                        )}
                        {scheduleRunOutput?.available && scheduleRunOutput.output_url && (
                          <Button
                            size="small"
                            icon={<FileTextOutlined />}
                            href={`${scheduleRunOutput.output_url}${token ? `${scheduleRunOutput.output_url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}` : ''}`}
                            target="_blank"
                          >打开原始文件</Button>
                        )}
                        </Space>
                      </div>

                      <div className="schedule-run-meta">
                        {/* A signal-triggered run has no planned time -- the
                            value here is when the signal arrived, and calling
                            that "计划时间" would imply a schedule it never had. */}
                        <div><small>{selectedScheduleRun.trigger_source?.startsWith('signal:') ? '触发时间' : '计划时间'}</small><span>{selectedScheduleRun.scheduled_for ? new Date(selectedScheduleRun.scheduled_for).toLocaleString() : '—'}</span></div>
                        <div><small>开始时间</small><span>{selectedScheduleRun.started_at ? new Date(selectedScheduleRun.started_at).toLocaleString() : '—'}</span></div>
                        <div><small>完成时间</small><span>{selectedScheduleRun.finished_at ? new Date(selectedScheduleRun.finished_at).toLocaleString() : '—'}</span></div>
                        <div><small>执行耗时</small><span>{formatScheduleDuration(selectedScheduleRun.duration_ms)}</span></div>
                        <div><small>触发方式</small><span>{describeRunTrigger(selectedScheduleRun, schedules)}</span></div>
                        {describeCascade(selectedScheduleRun) && (
                          <div><small>信号链</small><span>{describeCascade(selectedScheduleRun)}</span></div>
                        )}
                        <div><small>运行模型</small><span>{selectedScheduleRun.config_snapshot?.model_override || '默认模型'}</span></div>
                      </div>

                      {selectedScheduleRun.error && (
                        <div className="schedule-run-error">
                          <strong>执行错误</strong>
                          <pre>{selectedScheduleRun.error}</pre>
                        </div>
                      )}

                      <div className="schedule-output-head">
                        <strong>完整输出</strong>
                        {selectedScheduleRun.delivery_status && <span>{scheduleDeliveryStatusLabel(selectedScheduleRun.delivery_status)}</span>}
                      </div>
                      {selectedScheduleRun.status === 'running' ? (
                        <div className="schedule-output-state"><Spin size="small" /><span>任务正在执行，结果会自动刷新</span></div>
                      ) : scheduleOutputLoading ? (
                        <div className="schedule-output-state"><Spin size="small" /><span>正在加载输出</span></div>
                      ) : scheduleRunOutput?.available ? (
                        <>
                          {scheduleRunOutput.truncated && <div className="schedule-output-notice">输出较长，页面仅展示前 2 MB，可打开原始文件查看全部内容。</div>}
                          <div
                            className="schedule-output markdown"
                            dangerouslySetInnerHTML={{ __html: markdownToHtml(scheduleRunOutput.content) }}
                          />
                        </>
                      ) : (
                        <div className="schedule-output-state muted">本次运行没有可展示的文本输出</div>
                      )}
                      {scheduleArtifacts.length > 0 && (
                        <div className="schedule-artifacts">
                          <div className="schedule-output-head">
                            <strong>输出文件</strong>
                            <span>{scheduleArtifacts.length} 个</span>
                          </div>
                          <div className="schedule-artifact-list">
                            {scheduleArtifacts.map(artifact => {
                              const url = `${artifact.url}${token ? `?token=${encodeURIComponent(token)}` : ''}`
                              return (
                                <a key={artifact.path} href={url} target="_blank" rel="noreferrer" className="schedule-artifact-item">
                                  {artifact.mime_type.startsWith('image/')
                                    ? <img src={url} alt="" />
                                    : <span><FileTextOutlined /></span>}
                                  <div><strong>{artifact.name}</strong><small>{formatFileSize(artifact.size_bytes)}</small></div>
                                </a>
                              )
                            })}
                          </div>
                        </div>
                      )}
                    </>
                  )}
                </section>
              </div>
            )}
          </div>
        )}
      </Drawer>
      {loadingView ? <Skeleton active paragraph={{ rows: 6 }} /> : filteredSchedules.length === 0 ? <Empty description={schedules.length ? '没有符合条件的任务' : '暂无自动化任务'} className="page-empty" /> : (
        <div className="schedule-list">
          {filteredSchedules.map(task => {
            const description = task.kind === 'agent_prompt'
              ? task.payload?.prompt
              : task.kind === 'system_job'
                ? task.payload?.job_name
                : task.payload?.message_text
            const latestRun = task.latest_run
            return (
              <Card
                key={task.id}
                hoverable
                role="button"
                tabIndex={0}
                aria-label={`查看 ${task.name} 的运行记录`}
                className={`schedule-card ${task.enabled === false ? 'schedule-card-disabled' : ''}`}
                onClick={() => openScheduleDetails(task)}
                onKeyDown={event => {
                  if (event.target !== event.currentTarget) return
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    openScheduleDetails(task)
                  }
                }}
              >
                <div className="schedule-card-head">
                  <div className="schedule-card-title">
                    <span className="schedule-card-icon">{task.kind === 'agent_prompt' ? <RobotOutlined /> : <MessageOutlined />}</span>
                    <div>
                      <strong>{task.name}</strong>
                      <span>{task.kind === 'agent_prompt' ? 'Agent 任务' : task.kind === 'message' ? '定时提醒' : '系统任务'}</span>
                    </div>
                  </div>
                  <Space onClick={event => event.stopPropagation()}>
                    <Checkbox
                      aria-label={`选择 ${task.name}`}
                      checked={selectedScheduleIds.includes(task.id)}
                      onChange={event => setSelectedScheduleIds(prev => event.target.checked
                        ? [...prev, task.id]
                        : prev.filter(id => id !== task.id))}
                    />
                    <Switch size="small" checked={task.enabled !== false} onChange={value => toggleSchedule(task, value)} />
                    <Tooltip title={schedulerHealth.status === 'online' ? '立即运行' : '调度器离线'}><Button type="text" icon={<ThunderboltOutlined />} disabled={schedulerHealth.status !== 'online' || !!task.active_run_id} onClick={() => runScheduleNow(task)} /></Tooltip>
                    <Tooltip title="编辑"><Button type="text" icon={<EditOutlined />} onClick={() => openEditSchedule(task)} /></Tooltip>
                    <Button danger type="text" icon={<DeleteOutlined />} onClick={() => deleteSchedule(task)}>删除</Button>
                  </Space>
                </div>
                <p className="schedule-card-description">{description || '暂无任务描述'}</p>
                <div className="schedule-card-footer">
                  <span className={`schedule-run-status status-${latestRun?.status || 'pending'}`}>
                    {scheduleRunStatusIcon(latestRun?.status)}
                    {scheduleRunStatusLabel(latestRun?.status)}
                  </span>
                  {latestRun?.duration_ms !== null && latestRun?.duration_ms !== undefined && (
                    <span>耗时 {formatScheduleDuration(latestRun.duration_ms)}</span>
                  )}
                  <span>下次执行：{describeNextRun(task)}</span>
                  {task.last_run_at && <span>上次执行：{new Date(task.last_run_at).toLocaleString()}</span>}
                  <Button type="text" size="small" icon={<FileTextOutlined />} className="schedule-card-detail-button">运行记录</Button>
                </div>
              </Card>
            )
          })}
        </div>
      )}
    </div>
  )

  const renderSettings = () => (
    <div className="page-view settings-view">
      <div className="page-head settings-page-head">
        <div>
          <div className="eyebrow">WORKSPACE</div>
          <h2>{pageMeta.settings.title}</h2>
          <p>配置访问权限、模型偏好与消息频道。</p>
        </div>
        <Space>
          <span className={`save-state ${settingsDirty ? 'dirty' : ''}`}>
            <span className="save-state-dot" />
            {settingsDirty ? '有未保存更改' : '已同步'}
          </span>
          <Button className="settings-save-button" type="primary" icon={<CheckCircleFilled />} onClick={saveSettings} disabled={!settingsDirty}>
            保存设置
          </Button>
        </Space>
      </div>

      {loadingView ? (
        <Skeleton active paragraph={{ rows: 10 }} />
      ) : (
        <div className="settings-grid">
          <Card className="settings-card" title="访问令牌" extra={<span className="card-kicker">SECURITY</span>}>
            <p className="settings-hint">
              Web 频道默认只绑定本地地址，因此令牌通常可以为空。对外暴露端口时请填写鉴权令牌。
            </p>
            <Space.Compact style={{ width: '100%' }}>
              <Input.Password
                defaultValue={token}
                placeholder="auth_token（可选）"
                id="token-input"
              />
              <Button
                type="primary"
                className="settings-inline-save"
                onClick={() => {
                  const element = document.getElementById(
                    'token-input',
                  ) as HTMLInputElement | null
                  localStorage.setItem(
                    'agent_token',
                    element?.value.trim() || '',
                  )
                  messageApi.success('令牌已保存，正在刷新…')
                  setTimeout(() => location.reload(), 500)
                }}
              >
                保存令牌
              </Button>
            </Space.Compact>
          </Card>

          <Card className="settings-card" title="发送偏好" extra={<span className="card-kicker">UX</span>}>
            <p className="settings-hint">
              修改发送快捷键。中文输入法用 Enter 上屏，切换成 Ctrl/Cmd + Enter 可避免误发送。
            </p>
            <label style={{ display: 'block', marginBottom: 6, fontWeight: 500 }}>发送快捷键</label>
            <Select
              value={sendShortcut}
              onChange={value => {
                setSendShortcut(value)
                localStorage.setItem('send_shortcut', value)
                messageApi.success(`发送快捷键已改为：${value === 'ctrl-enter' ? 'Ctrl/Cmd + Enter' : 'Enter'}`)
              }}
              options={[
                { value: 'enter', label: 'Enter' },
                { value: 'ctrl-enter', label: 'Ctrl/Cmd + Enter' },
              ]}
              style={{ width: '100%' }}
            />
          </Card>

          <Card className="settings-card" title="模型与频道" extra={<span className="card-kicker">RUNTIME</span>}>
            <Form form={form} layout="vertical" onValuesChange={() => setSettingsDirty(true)}>
              <Row gutter={16}>
                <Col xs={24} md={12}>
                  <Form.Item
                    name="active_provider"
                    label="Provider"
                    rules={[{ required: true, message: '请选择 Provider' }]}
                  >
                    <Select
                      options={Object.keys(config?.providers || {}).map(key => ({
                        value: key,
                        label: key,
                      }))}
                    />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item name="model" label="默认模型">
                    <Select
                      options={settingsModelOptions}
                      placeholder="选择模型"
                      showSearch
                      optionFilterProp="label"
                    />
                  </Form.Item>
                </Col>
                <Col xs={24} md={8}>
                  <Form.Item name="max_tokens" label="Max tokens">
                    <InputNumber min={1} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col xs={12} md={8}>
                  <Form.Item name="web_enabled" valuePropName="checked" label="Web 频道">
                    <Switch />
                  </Form.Item>
                </Col>
                <Col xs={12} md={8}>
                  <Form.Item name="feishu_enabled" valuePropName="checked" label="飞书频道">
                    <Switch />
                  </Form.Item>
                </Col>
              </Row>
            </Form>
          </Card>

          <Card
            className="settings-card settings-json-card"
            title="高级 JSON"
            extra={
              <Space>
                <Button
                  icon={<ReloadOutlined />}
                  onClick={resetSettings}
                >
                  恢复已加载配置
                </Button>
              </Space>
            }
          >
            <div className={`json-status ${jsonStatus.valid ? 'valid' : 'invalid'}`}>
              <span className="json-status-dot" /> {jsonStatus.label}
            </div>
            <TextArea
              value={configText}
              onChange={event => {
                setConfigText(event.target.value)
                setSettingsDirty(true)
              }}
              className="settings-json"
              spellCheck={false}
            />
          </Card>
        </div>
      )}
    </div>
  )

  const renderCurrentView = () => {
    if (view === 'chat') return renderChat()
    if (view === 'sessions') return renderSessions()
    if (view === 'plugins') return renderPlugins()
    if (view === 'skills') return renderSkills()
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
          fontFamily:
            "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif",
        },
        components: {
          Layout: {
            bodyBg: 'transparent',
            headerBg: 'transparent',
            siderBg: 'transparent',
          },
          Button: {
            controlHeight: 38,
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
                setView(event.key)
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
                      onClick={event => handleSessionContainerClick(event, item.session_id)}
                    >
                      <div className="session-item-status">
                        <span className={item.live ? 'live' : 'durable'} />
                      </div>
                      <div className="session-item-main">
                        <div className="session-item-title">
                          {item.title || '未命名会话'}
                        </div>
                        <div className="session-item-meta">
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
                  setView(item.key)
                  setCommandPaletteOpen(false)
                  setPaletteQuery('')
                }}
              >
                {item.icon}
                {item.label}
              </button>
            ))}
          </div>
        </div>
      </Modal>
    </ConfigProvider>
  )
}

export default App
