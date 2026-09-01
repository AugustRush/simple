import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import {
  Avatar,
  Badge,
  Button,
  Card,
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
type ToolState = 'running' | 'done' | 'blocked'

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

function escapeHtml(s: string): string {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[c]!))
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
  const [activeSession, setActiveSession] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [commands, setCommands] = useState<CommandInfo[]>([])
  const [plugins, setPlugins] = useState<PluginInfo[]>([])
  const [skills, setSkills] = useState<SkillInfo[]>([])
  const [config, setConfig] = useState<any>(null)
  const [configText, setConfigText] = useState<string>('')
  const [confirmReq, setConfirmReq] = useState<any>(null)
  const [input, setInput] = useState('')
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
  const [form] = Form.useForm()
  const wsRef = useRef<WebSocket | null>(null)
  const activeSessionRef = useRef<string | null>(null)
  const pendingSendRef = useRef<string | null>(null)
  const streamIdRef = useRef<string | null>(null)
  const messagesRef = useRef<Message[]>([])
  const loadMessagesRequestRef = useRef(0)
  const chatEndRef = useRef<HTMLDivElement | null>(null)
  const idRef = useRef(0)
  const token = localStorage.getItem('agent_token') || ''

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
    if (!chatEndRef.current) return
    const frame = requestAnimationFrame(() => {
      chatEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
    })
    return () => cancelAnimationFrame(frame)
  }, [messages])

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
      setSessions(data.sessions || [])
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
          (item: any): Message => ({
            id: makeId(),
            role: (item.role as MessageRole) || 'assistant',
            content: item.content || '',
            link: item.link,
            tool: item.tool,
            toolState: item.toolState as ToolState | undefined,
          }),
        )
        messagesRef.current = loaded
        setMessages(loaded)
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
    [api, makeId],
  )

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
          ws.send(JSON.stringify({ type: 'message', text: pending }))
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
    [appendMessage, loadSessions, makeId, token, updateMessage],
  )

  const selectSession = useCallback(
    (sid: string) => {
      activeSessionRef.current = sid
      setActiveSession(sid)
      setView('chat')
      streamIdRef.current = null
      setIsStreaming(false)
      setActivity('')
      setMessages([])
      messagesRef.current = []
      loadMessages(sid)
      loadSessionPermissions(sid)
    },
    [loadMessages, loadSessionPermissions],
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

  const sendMessage = async (overrideText?: string) => {
    const text = (overrideText ?? input).trim()
    if (!text || creatingSession) return

    appendMessage({ id: makeId(), role: 'user', content: text })
    setInput('')
    setActivity('等待模型响应')

    if (!activeSession) {
      try {
        setCreatingSession(true)
        pendingSendRef.current = text
        const resp = await api('/api/sessions', { method: 'POST' })
        const data = await resp.json()
        const sid = data.session_id as string
        activeSessionRef.current = sid
        await loadSessions()
        setView('chat')
        setActiveSession(sid)
      } catch {
        pendingSendRef.current = null
        setActivity('')
      } finally {
        setCreatingSession(false)
      }
      return
    }

    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      setIsStreaming(true)
      ws.send(JSON.stringify({ type: 'message', text }))
      return
    }

    messageApi.warning('连接已断开，正在重新连接…')
    connectWs(activeSession)
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

  const deleteSession = (item: SessionInfo) => {
    Modal.confirm({
      title: '删除会话',
      content: `确定删除「${item.title || '未命名会话'}」的历史记录吗？`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        await api(`/api/sessions/${encodeURIComponent(item.session_id)}`, {
          method: 'DELETE',
        })
        messageApi.success('会话已删除')
        if (activeSession === item.session_id) {
          activeSessionRef.current = null
          setActiveSession(null)
          setMessages([])
          messagesRef.current = []
          streamIdRef.current = null
        }
        loadSessions()
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
    return Object.keys(config?.providers || {}).map(providerName => {
      const provider = config.providers[providerName]
      const models = provider?.models?.length
        ? provider.models
        : [provider?.default_model].filter(Boolean)
      return {
        label: providerName,
        options: (models || []).map((model: string) => ({
          value: model,
          label: model,
        })),
      }
    })
  }, [config])

  const handleModelChange = (model: string) => {
    setCurrentModel(model)
    const providerName = Object.keys(config?.providers || {}).find(name => {
      const provider = config.providers[name]
      const models = provider?.models?.length
        ? provider.models
        : [provider?.default_model].filter(Boolean)
      return (models || []).includes(model)
    })
    if (providerName) setCurrentProvider(providerName)
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
    if (event.key === 'Enter') {
      if (event.shiftKey) return
      event.preventDefault()
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
      return
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

  const renderToolEvent = (item: Message) => (
    <div className="tool-event" key={item.id}>
      <span className={`tool-event-dot ${item.toolState || 'done'}`} />
      <span className="tool-event-name">{item.tool || 'tool'}</span>
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
        {expanded && (
          <div className="tool-trace-items">{tools.map(renderToolEvent)}</div>
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
        currentTools.push(item)
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
      <div className="chat-scroll">
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
                <div className="thinking-indicator">
                  <span className="thinking-indicator-icon">
                    <LoadingOutlined spin />
                  </span>
                  <div>
                    <strong>{activity || '正在思考…'}</strong>
                    <small>正在处理，请稍候</small>
                  </div>
                </div>
              )}
            </>
          )}
          <div ref={chatEndRef} />
        </div>
      </div>

      <div className="composer-wrap">
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
            placeholder="输入消息，/ 查看命令，Enter 发送，Shift + Enter 换行"
            autoSize={{ minRows: 1, maxRows: 6 }}
            variant="borderless"
            disabled={false}
          />
          <div className="composer-footer">
            <Space size={4}>
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
                <span className="composer-busy">
                  <LoadingOutlined spin /> {activity || '正在生成'}
                </span>
              )}
              <Tooltip title="发送 (Enter)">
                <Button
                  type="primary"
                  className="send-button"
                  icon={<SendOutlined />}
                  disabled={!input.trim() || creatingSession}
                  onClick={() => sendMessage()}
                >
                  发送
                </Button>
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
        <Input
          prefix={<SearchOutlined />}
          placeholder="搜索会话"
          value={sessionSearch}
          onChange={event => setSessionSearch(event.target.value)}
          allowClear
          style={{ width: 240 }}
        />
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
                onClick={() => selectSession(item.session_id)}
              >
                <div className="session-card-head">
                  <div className="session-card-title">
                    <span>{item.title || '未命名会话'}</span>
                    {item.live && <Badge status="processing" />}
                  </div>
                  <Dropdown
                    trigger={['click']}
                    menu={{
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
                          onClick: () => deleteSession(item),
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
    <div className="page-view">
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
          style={{ width: 240 }}
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
          className="skills-search"
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
      <div className="page-head">
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
          <Button type="primary" icon={<CheckCircleFilled />} onClick={saveSettings} disabled={!settingsDirty}>
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
                    <Input placeholder="default model" />
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
                      onClick={() => selectSession(item.session_id)}
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
                      <Dropdown
                        trigger={['click']}
                        menu={{
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
                              onClick: () => deleteSession(item),
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
