import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { createPortal } from 'react-dom'
import {
  Avatar,
  Badge,
  Button,
  Card,
  Checkbox,
  Col,
  ConfigProvider,
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
  Tooltip,
  Typography,
  message,
} from 'antd'
import {
  ApiOutlined,
  AppstoreOutlined,
  CheckCircleFilled,
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
  GlobalOutlined,
  LoadingOutlined,
  MenuOutlined,
  MessageOutlined,
  MoonOutlined,
  MoreOutlined,
  PlusOutlined,
  ReloadOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SearchOutlined,
  SendOutlined,
  SettingOutlined,
  StopOutlined,
  SunOutlined,
  TagsOutlined,
  ThunderboltOutlined,
  UserOutlined,
} from '@ant-design/icons'
import './index.css'

const { Sider, Header, Content } = Layout
const { TextArea } = Input
const { Paragraph, Text } = Typography

type MessageRole = 'user' | 'assistant' | 'tool' | 'command' | 'error'
type ToolState = 'running' | 'done' | 'blocked' | 'interrupted'

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

function markdownToHtml(text: string): string {
  let t = escapeHtml(text || '')
  const codeBlocks: string[] = []

  // Replace fenced code with placeholders so line-level parsing can't corrupt it.
  t = t.replace(
    /```([\w-]*)[ \t]*\n?([\s\S]*?)```/g,
    (_m, lang: string, code: string) => {
      const label = lang ? escapeHtml(lang) : 'code'
      const body = code.replace(/\n$/, '')
      const index = codeBlocks.length
      codeBlocks.push(
        `<div class="code-block"><div class="code-block-head"><span>${label}</span></div>` +
        `<pre><code>${body}</code></pre></div>`,
      )
      return `\u0000CODE${index}\u0000`
    },
  )

  t = t.replace(/`([^`]+)`/g, '<code>$1</code>')
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  t = t.replace(/\*([^*]+)\*/g, '<em>$1</em>')
  t = t.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, '<img class="md-img" src="$2" alt="$1" loading="lazy" />')
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
  t = t.replace(/\n{2,}/g, '<br /><br />')
  t = t.replace(/\n/g, '<br />')

  return t
}

function truncate(value: string, length = 64): string {
  return value.length > length ? `${value.slice(0, length)}…` : value
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
  const [config, setConfig] = useState<any>(null)
  const [configText, setConfigText] = useState<string>('')
  const [confirmReq, setConfirmReq] = useState<any>(null)
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
        localStorage.setItem(`chat_messages:${activeSession}`, JSON.stringify(messages))
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
  const [currentModel, setCurrentModel] = useState('默认模型')
  const [currentProvider, setCurrentProvider] = useState('')
  const [permissionLevel, setPermissionLevel] = useState('ask')
  const [sandboxMode, setSandboxMode] = useState('read_all')
  const [connected, setConnected] = useState(false)
  const [isStreaming, setIsStreaming] = useState(false)
  const [activity, setActivity] = useState('')
  const [loadingSessions, setLoadingSessions] = useState(false)
  const [loadingView, setLoadingView] = useState(false)
  const [creatingSession, setCreatingSession] = useState(false)
  const [collapsed, setCollapsed] = useState(() => window.innerWidth <= 768)
  const [searchOpen, setSearchOpen] = useState(false)
  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false)
  const [paletteQuery, setPaletteQuery] = useState('')
  const [commandIndex, setCommandIndex] = useState(0)
  const [expandedTraces, setExpandedTraces] = useState<Record<string, boolean>>({})
  const [settingsDirty, setSettingsDirty] = useState(false)
  const [sendShortcut, setSendShortcut] = useState<'enter' | 'ctrl-enter'>(
    () => (localStorage.getItem('send_shortcut') === 'ctrl-enter' ? 'ctrl-enter' : 'enter'),
  )
  const sendShortcutLabel = sendShortcut === 'ctrl-enter' ? 'Ctrl/Cmd + Enter' : 'Enter'
  const [sessionState, setSessionState] = useState<SessionState | null>(null)
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([])
  const [form] = Form.useForm()
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
    } catch {
      // API errors are surfaced by the shared request helper.
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
        const resp = await api(`/api/sessions/${sid}/messages`)
        const data = await resp.json()
        if (
          requestId !== loadMessagesRequestRef.current ||
          activeSessionRef.current !== sid
        ) {
          return
        }
        const loaded = (data.messages || []).map(
          (item: any): Message => {
            let link = item.link || ''
            if (link && token) {
              link += (link.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token)
            }
            return {
              id: makeId(),
              role: (item.role as MessageRole) || 'assistant',
              content: item.content || '',
              link,
              tool: item.tool,
              toolState: item.toolState as ToolState | undefined,
            }
          },
        )
        // Keep transient events that arrived through the newly connected
        // socket while this HTTP history request was in flight. This is what
        // makes switching back to a running session show its partial reply
        // immediately instead of replacing it with only the persisted user
        // messages.
        const transient = messagesRef.current.filter(item =>
          item.streaming || (item.role === 'tool' && item.toolState === 'running'),
        )
        const merged = [...loaded, ...transient]
        messagesRef.current = merged
        setMessages(merged)
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
        // Restoring a session while its agent is still working should bring
        // back the generating state (and stop button) even before the first
        // snapshot/chunk arrives on the newly opened socket.
        setIsStreaming(String(data.operation_state || 'idle') !== 'idle')
      }
    } catch {
      if (activeSessionRef.current === sid) {
        setSessionState(null)
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
        setIsStreaming(false)
        setActivity('连接已断开')
      }

      ws.onerror = () => {
        if (wsRef.current !== ws || activeSessionRef.current !== sid) return
        setConnected(false)
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
          appendMessage({
            id: makeId(),
            role: 'tool',
            content: `${evt.name || evt.path}`,
            link: `/api/files?path=${encodeURIComponent(evt.path)}${t ? `&token=${encodeURIComponent(t)}` : ''}`,
          })
          return
        }

        if (evt.type === 'subagent_event') {
          appendMessage({
            id: makeId(),
            role: 'command',
            content: `**${evt.agent || '子代理'}** · ${evt.event || evt.detail || '状态更新'}`,
          })
          return
        }

        if (evt.type === 'heartbeat') {
          if (evt.current_op) {
            setActivity(evt.op_detail || evt.current_op)
          }
          return
        }

        if (evt.type === 'error') {
          appendMessage({
            id: makeId(),
            role: 'error',
            content: evt.error || '发生错误',
          })
          setIsStreaming(false)
          return
        }

        if (evt.type === 'confirm_request') {
          setConfirmReq(evt)
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
            messagesRef.current = arr
            setMessages(arr)
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
      loadSessionState(sid)
    },
    [loadMessages, loadSessionPermissions, loadSessionState],
  )

  useEffect(() => {
    loadSessions()
    api('/api/commands')
      .then(r => r.json())
      .then(data => setCommands(data.commands || []))
      .catch(() => {})
  }, [api, loadSessions])

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

  const sendMessage = async (overrideText?: string) => {
    const text = (overrideText ?? input).trim()
    if (!text || creatingSession) return

    // A newly submitted turn is an explicit request to see the response.
    // Re-enable bottom following even if the reader had previously scrolled
    // up to inspect older messages.
    followChatRef.current = true
    const queueWhileBusy =
      isStreaming && !/^\/(?:cancel|now)(?:\s|$)/i.test(text)
    const messageId = makeId()
    appendMessage({ id: messageId, role: 'user', content: text, queued: queueWhileBusy })
    setInput('')
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
      }))
      return
    }

    messageApi.warning('连接已断开，正在重新连接…')
    connectWs(activeSession)
  }

  const stopStreaming = () => {
    // Ask the backend to cancel the running turn. The stream flow emits
    // `turn_complete` (with whatever partial text was generated), which resets
    // `isStreaming` and flips the send button back to "发送".
    if (!activeSession) return
    api(`/api/sessions/${activeSession}/cancel`, { method: 'POST' }).catch(() => {})
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
      messageApi.success(`项目文件夹已切换：${data.workspace_root}`)
    } catch {
      // api helper already reports the error
    }
  }

  const sendConfirm = (approved: boolean) => {
    if (!confirmReq || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      setConfirmReq(null)
      return
    }
    wsRef.current.send(
      JSON.stringify({
        type: 'confirm_response',
        approved,
        confirmation_token: confirmReq.confirmation_token || '',
      }),
    )
    setConfirmReq(null)
  }

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

  const loadSkills = useCallback(async () => {
    try {
      setLoadingView(true)
      const resp = await api('/api/skills')
      const data = await resp.json()
      setSkills(data.skills || [])
    } catch {
      // Handled by api helper.
    } finally {
      setLoadingView(false)
    }
  }, [api])

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
      setCurrentModel(active.default_model || '默认模型')
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
    if (view === 'settings') loadSettings()
  }, [view, loadPlugins, loadSkills, loadSettings])

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
    // A running session is built from the configured active provider. Showing
    // models from other providers in this control is misleading because a
    // model id alone cannot switch the underlying API client/base URL.
    const providerName = config?.active_provider
    const provider = providerName ? config?.providers?.[providerName] : undefined
    const models = provider?.models?.length
      ? provider.models
      : [provider?.default_model].filter(Boolean)
    return providerName
      ? [{
          label: providerName,
          options: (models || []).map((model: string) => ({
            value: model,
            label: model,
          })),
        }]
      : []
  }, [config])

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

  useEffect(() => {
    setCommandIndex(0)
  }, [filteredCommands])

  // Keep the highlighted command visible while navigating with ↑/↓. The
  // popover is its own scroll container, so relying on browser focus would
  // not scroll the active item (and would also move focus away from the
  // composer). ``nearest`` avoids jumping the surrounding page while only
  // adjusting the command list when necessary.
  useEffect(() => {
    if (!input.startsWith('/') || filteredCommands.length === 0) return
    const command = filteredCommands[commandIndex] || filteredCommands[0]
    commandItemRefs.current[command.name]?.scrollIntoView({ block: 'nearest' })
  }, [commandIndex, filteredCommands, input])

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
    { key: 'settings', icon: <SettingOutlined />, label: '设置' },
  ]

  const pageMeta: Record<string, { title: string; subtitle: string }> = {
    chat: {
      title: activeSession ? '当前对话' : '开始新的对话',
      subtitle: activeSession
        ? `${activeSession.slice(0, 12)} · ${connected ? '实时连接中' : '连接已断开'}${sessionState?.workspace_root ? ` · ${sessionState.workspace_root}` : ''}`
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

  const handleComposerKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // 输入法（IME）组合输入时，Enter 用于选中候选字/上屏，不应触发送出。
    if (event.nativeEvent?.isComposing || event.keyCode === 229) {
      return
    }

    const ctrlOrCmd = event.ctrlKey || event.metaKey

    const send = () => {
      if (input.startsWith('/') && filteredCommands.length > 0) {
        const command = filteredCommands[commandIndex] || filteredCommands[0]
        const parts = input.trim().slice(1).split(/\s+/)
        const args = parts.slice(1).join(' ')
        sendMessage(
          args ? `/${command.name} ${args}` : `/${command.name}`,
        )
        return
      }
      if (input.trim()) sendMessage()
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

    if (!input.startsWith('/')) return

    if (event.key === 'Escape') {
      event.preventDefault()
      setInput('')
    } else if (event.key === 'ArrowDown' && filteredCommands.length > 0) {
      event.preventDefault()
      setCommandIndex(prev => (prev + 1) % filteredCommands.length)
    } else if (event.key === 'ArrowUp' && filteredCommands.length > 0) {
      event.preventDefault()
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

    if (item.role === 'command' || item.role === 'error') {
      return (
        <div
          key={item.id}
          className={`system-note ${item.role === 'error' ? 'system-note-error' : ''}`}
        >
          <div
            className="markdown"
            dangerouslySetInnerHTML={{ __html: markdownToHtml(item.content) }}
          />
        </div>
      )
    }

    const isUser = item.role === 'user'
    return (
      <div
        key={item.id}
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
          <div className={`bubble ${isUser ? 'bubble-user' : 'bubble-assistant'}`}>
            <div
              className="markdown"
              dangerouslySetInnerHTML={{ __html: markdownToHtml(item.content) }}
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
          <span className="tool-trace-dots">
            {tools.map(tool => (
              <span
                key={tool.id}
                className={`tool-dot ${tool.toolState || 'done'}`}
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
    let currentUser: Message | null = null
    let currentTools: Message[] = []
    let currentTail: Message[] = []
    let hasTurn = false

    const flushTurn = () => {
      if (currentUser) nodes.push(renderMessage(currentUser))
      let traceAttached = false
      currentTail.forEach(item => {
        const shouldAttachTrace =
          !traceAttached && item.role === 'assistant' && currentTools.length > 0
        if (shouldAttachTrace) {
          nodes.push(renderMessage(item, renderToolTraceContent(currentTools)))
          traceAttached = true
        } else {
          nodes.push(renderMessage(item))
        }
      })
      if (currentTools.length && !traceAttached) {
        nodes.push(renderToolTrace(currentTools))
      }
      currentUser = null
      currentTools = []
      currentTail = []
      hasTurn = false
    }

    messages.forEach(item => {
      if (item.role === 'user') {
        if (hasTurn) flushTurn()
        currentUser = item
        currentTools = []
        currentTail = []
        hasTurn = true
      } else if (item.role === 'tool') {
        // Attachments (image/audio/video/file) are real inline content and must
        // appear in the chat stream, not hidden inside the collapsible tool
        // trace overlay.  Regular tool-trace rows carry no ``link``.  Feed them
        // through ``currentTail`` so they render right after the user message
        // (in order) rather than being collected into the trace.
        if (item.tool === 'attachment' || item.link) {
          if (hasTurn) {
            currentTail.push(item)
          } else {
            nodes.push(renderMessage(item))
          }
        } else {
          currentTools.push(item)
        }
      } else if (hasTurn) {
        currentTail.push(item)
      } else {
        nodes.push(renderMessage(item))
      }
    })
    flushTurn()
    return nodes
  }

  const renderChat = () => (
    <div className="chat-view">
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

      <div className="composer-wrap">
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
          !['completed', 'success', 'done', 'updated'].includes(
            String(sessionState.task.status || '').toLowerCase(),
          ) && (
            <div className="task-guidance-card">
              <div className="task-guidance-head">
                <span className="task-guidance-label">当前任务</span>
                {sessionState.task.status && (
                  <span className="task-guidance-status">{sessionState.task.status}</span>
                )}
              </div>
              <strong>{truncate(sessionState.task.active_goal, 140)}</strong>
              {sessionState.task.progress && (
                <span>{truncate(sessionState.task.progress, 180)}</span>
              )}
              {sessionState.task.next_action && (
                <div className="task-guidance-next">
                  <span>下一步：{truncate(sessionState.task.next_action, 180)}</span>
                  <Button
                    type="text"
                    size="small"
                    onClick={() => sendMessage('继续当前任务')}
                  >
                    继续任务
                  </Button>
                </div>
              )}
            </div>
          )}
        {input.startsWith('/') && (
          <div className="command-popover">
            <div className="command-popover-head">
              <span>可用命令</span>
              <span>↑ ↓ 选择 · Enter 发送 · Esc 关闭</span>
            </div>
            {filteredCommands.length > 0 ? (
              filteredCommands.map((command, index) => (
                <button
                  type="button"
                  key={command.name}
                  ref={element => {
                    commandItemRefs.current[command.name] = element
                  }}
                  className={`command-item ${index === commandIndex ? 'active' : ''}`}
                  onMouseDown={event => {
                    event.preventDefault()
                    setInput(`/${command.name} `)
                  }}
                >
                  <CodeOutlined />
                  <span className="command-item-main">
                    <strong>{command.usage || `/${command.name}`}</strong>
                    <small>{command.description || '无描述'}</small>
                  </span>
                  <kbd>/</kbd>
                </button>
              ))
            ) : (
              <div className="command-empty">没有匹配命令</div>
            )}
          </div>
        )}

        <div className="composer">
          <TextArea
            value={input}
            onChange={event => setInput(event.target.value)}
            onKeyDown={handleComposerKeyDown}
            placeholder={sendShortcut === 'ctrl-enter'
              ? '输入消息，/ 查看命令，Ctrl/Cmd + Enter 发送，Enter 换行'
              : '输入消息，/ 查看命令，Enter 发送，Shift + Enter 换行'}
            autoSize={{ minRows: 1, maxRows: 6 }}
            variant="borderless"
            disabled={false}
          />
          <div className="composer-footer">
            <Space size={4}>
              <Tooltip title="选择项目文件夹">
                <Button
                  type="text"
                  className="workspace-button"
                  aria-label="选择项目文件夹"
                  icon={<FolderOpenOutlined />}
                  onClick={pickWorkspace}
                />
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
                  icon={<GlobalOutlined />}
                >
                  {permissionLabel}
                </Button>
              </Dropdown>
              <Select
                value={currentModel}
                onChange={handleModelChange}
                options={modelOptions}
                className="model-select"
                popupMatchSelectWidth={false}
                variant="borderless"
              />
            </Space>
            <Space size={4}>
              {isStreaming && (
                <Tooltip title="终止生成">
                  <Button
                    type="default"
                    danger
                    className="send-button stop-button"
                    aria-label="终止生成"
                    icon={<StopOutlined />}
                    onClick={stopStreaming}
                  />
                </Tooltip>
              )}
              <Tooltip title={isStreaming ? '排队发送' : `发送 (${sendShortcutLabel})`}>
                <Button
                  type="primary"
                  className="send-button"
                  aria-label={isStreaming ? '排队发送' : '发送'}
                  icon={<SendOutlined />}
                  disabled={!isStreaming && (!input.trim() || creatingSession)}
                  onClick={() => sendMessage()}
                />
              </Tooltip>
            </Space>
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
              </div>
            </div>
          ))}
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
    if (view === 'settings') return renderSettings()
    return renderChat()
  }

  const currentMeta = pageMeta[view] || pageMeta.chat
  const activeSessionInfo = sessions.find(item => item.session_id === activeSession)

  return (
    <ConfigProvider
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
        open={!!confirmReq}
        title={
          <Space>
            <SafetyCertificateOutlined />
            工具审批
          </Space>
        }
        onCancel={() => sendConfirm(false)}
        footer={[
          <Button key="deny" danger onClick={() => sendConfirm(false)}>
            拒绝
          </Button>,
          <Button key="allow" type="primary" onClick={() => sendConfirm(true)}>
            允许执行
          </Button>,
        ]}
      >
        <div className="confirm-command">
          <code>{confirmReq?.command || '未知命令'}</code>
        </div>
        <div className="confirm-meta">
          <Tag>
            风险等级：{confirmReq?.risk_level || 'unknown'}
          </Tag>
          <Text type="secondary">{confirmReq?.reason || '需要你的确认后才会执行。'}</Text>
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
