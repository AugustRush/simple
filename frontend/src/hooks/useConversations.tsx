import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { CONVERSATION_SUMMARY_HALF_HEIGHT, INTERRUPT_RETRY_MS, INTERRUPT_STUCK_MS, TASK_INTERRUPTED_STATUSES } from '../constants'
import { truncate } from '../lib/format'
import { fileHref, withFileSession } from '../lib/media'
import { findOpenSubAgentNote, foldSubAgentEvent, newSubAgentNote, sealSubAgentNotes } from '../lib/subagent'
import { compareSessions, isSessionBusy } from '../lib/tools'
import type { AttachmentInfo, CommandInfo, ConfirmRequest, Message, MessageRole, QueuedMessage, SessionInfo, SessionState, ToolState } from '../types'
import { Modal } from 'antd'
import type { TextAreaRef } from 'antd/es/input/TextArea'
import { useUi } from './useUi'
import { useApiClient } from './useApiClient'
import { useConfirm } from './useConfirm'

type Deps = ReturnType<typeof useUi> & ReturnType<typeof useApiClient> & ReturnType<typeof useConfirm>

export function useConversations(deps: Deps) {
  const { api, commandDismissed, commandIndex, commandIndexPinned, commandItemRefs, confirmDeadlineRef, makeId, messageApi, setCommandDismissed, setCommandIndex, setCommandIndexPinned, setCommandPaletteOpen, setConfirmDetailOpen, setConfirmRemaining, setConfirmReq, setPaletteQuery, setView, token, wsRef } = deps

  const [sessions, setSessions] = useState<SessionInfo[]>([])

  const [selectedSessionIds, setSelectedSessionIds] = useState<string[]>([])

  const [pendingDeleteSessionId, setPendingDeleteSessionId] = useState<string | null>(null)

  const [activeSession, setActiveSession] = useState<string | null>(null)

  const [messages, setMessages] = useState<Message[]>([])

  const [commands, setCommands] = useState<CommandInfo[]>([])

  const [pendingAttachments, setPendingAttachments] = useState<AttachmentInfo[]>([])

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

  // '' means "provider default": the backend resolves no override to the
  // active provider's default model. Never a display string — the backend
  // must not have to know the UI's wording.
  const [currentModel, setCurrentModel] = useState('')

  const [permissionLevel, setPermissionLevel] = useState('ask')

  const [sandboxMode, setSandboxMode] = useState('read_all')

  const [connected, setConnected] = useState(false)

  const [isStreaming, setIsStreaming] = useState(false)

  // True between the user asking to stop and the turn actually unwinding.
  // The abort is cooperative, so this cannot be derived from `isStreaming`.
  const [interrupting, setInterrupting] = useState(false)

  const [activity, setActivity] = useState('')

  const [loadingSessions, setLoadingSessions] = useState(false)

  const [creatingSession, setCreatingSession] = useState(false)

  const [expandedTraces, setExpandedTraces] = useState<Record<string, boolean>>({})

  const [hoveredTurn, setHoveredTurn] = useState<{ id: string; top: number } | null>(null)

  const [hoveredTurnIndex, setHoveredTurnIndex] = useState<number | null>(null)

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

  const fileInputRef = useRef<HTMLInputElement | null>(null)

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

  const turnRefs = useRef<Record<string, HTMLDivElement | null>>({})

  const conversationRailRef = useRef<HTMLDivElement | null>(null)

  const conversationMarkerRefs = useRef<Record<string, HTMLButtonElement | null>>({})

  const hoverClearTimerRef = useRef<number | null>(null)

  // Scrolling up stops the view following new output, which is right -- but
  // until now nothing said so, and the only way back was to scroll the whole
  // way by hand while the answer kept growing underneath.
  const [awayFromLatest, setAwayFromLatest] = useState(false)

  const scrollChatToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    const container = chatScrollRef.current
    if (!container) return
    followChatRef.current = true
    setAwayFromLatest(false)
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
    // Scroll events fire per frame; React bails out when the value is
    // unchanged, so this only renders when the threshold is crossed.
    setAwayFromLatest(!nearBottom)
  }, [])

  // Instant, not smooth: a smooth scroll emits intermediate scroll events that
  // read as "away from the bottom" and would switch following back off while
  // a streaming answer is still growing past the animation's target.
  const jumpToLatest = useCallback(() => {
    scrollChatToBottom('auto')
  }, [scrollChatToBottom])

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

  // The skeleton is for "nothing to show yet". Every send, turn end and queue
  // change refreshes this list, and each refresh used to swap the whole
  // sidebar for a skeleton and back -- a flash per message, and a list that
  // jumped under the pointer.
  const sessionsLoadedRef = useRef(false)
  // Titles saved in place but not yet confirmed. A list refresh already in
  // flight when the edit landed would otherwise put the old title back.
  const pendingTitlesRef = useRef(new Map<string, string>())

  const loadSessions = useCallback(async () => {
    try {
      if (!sessionsLoadedRef.current) setLoadingSessions(true)
      const resp = await api('/api/sessions')
      const data = await resp.json()
      const pendingTitles = pendingTitlesRef.current
      const nextSessions: SessionInfo[] = (data.sessions || []).map((item: SessionInfo) =>
        pendingTitles.has(item.session_id) ? { ...item, title: pendingTitles.get(item.session_id) } : item,
      )
      sessionsLoadedRef.current = true
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

  // The list's busy badges only changed on this tab's own events -- a send, a
  // ``done`` on the open socket. Switch away from a running session and its
  // socket closes, so its ``done`` never arrives here and the badge read
  // 运行中 for good; a turn started from another tab or channel never
  // appeared at all. While anything is working, re-read the list; when
  // nothing is, stop. A hidden tab skips the poll and catches up on return.
  const anySessionBusy = isStreaming || sessions.some(isSessionBusy)
  useEffect(() => {
    const refreshIfVisible = () => {
      if (document.visibilityState === 'visible') void loadSessions()
    }
    document.addEventListener('visibilitychange', refreshIfVisible)
    window.addEventListener('focus', refreshIfVisible)
    const timer = anySessionBusy ? window.setInterval(refreshIfVisible, 3000) : undefined
    return () => {
      document.removeEventListener('visibilitychange', refreshIfVisible)
      window.removeEventListener('focus', refreshIfVisible)
      if (timer !== undefined) window.clearInterval(timer)
    }
  }, [anySessionBusy, loadSessions])

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
    await addFiles(files)
  }

  /** The one way a file becomes a pending attachment -- the picker, a paste
   * and a drop all land here, so the session bootstrap and the 12-file cap
   * cannot drift between them. */
  const addFiles = async (files: File[]) => {
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

  /** A screenshot on the clipboard used to paste as nothing at all. Files win
   * unless the clipboard also carries both plain and rich text: copying cells
   * from Excel or a paragraph from Word brings a rendered image along, and
   * that paste is meant as text. (A browser's "copy image" has HTML but no
   * plain text, so it still counts as a file.) */
  const handleComposerPaste = (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const data = event.clipboardData
    if (!data || !data.files.length) return
    if (data.types.includes('text/plain') && data.types.includes('text/html')) return
    event.preventDefault()
    void addFiles(Array.from(data.files))
  }

  // Dropping a file anywhere on the chat used to hand it to the browser, which
  // navigated away from the app to show it -- losing the draft and the stream.
  const [fileDragActive, setFileDragActive] = useState(false)

  const draggingFiles = (event: React.DragEvent) => Array.from(event.dataTransfer?.types || []).includes('Files')

  const handleChatDragOver = (event: React.DragEvent<HTMLDivElement>) => {
    if (!draggingFiles(event)) return
    event.preventDefault()
    event.dataTransfer.dropEffect = 'copy'
    setFileDragActive(true)
  }

  const handleChatDragLeave = (event: React.DragEvent<HTMLDivElement>) => {
    // Leaving for a child is not leaving the target.
    if (event.relatedTarget instanceof Node && event.currentTarget.contains(event.relatedTarget)) return
    setFileDragActive(false)
  }

  const handleChatDrop = (event: React.DragEvent<HTMLDivElement>) => {
    if (!draggingFiles(event)) return
    event.preventDefault()
    setFileDragActive(false)
    void addFiles(Array.from(event.dataTransfer.files))
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

  // Which title is open for editing, as `${place}:${session_id}`: the sidebar
  // and the sessions page show the same session at once, and only the one the
  // person acted on should turn into an input.
  const [renamingSession, setRenamingSession] = useState<string | null>(null)

  const commitSessionTitle = async (item: SessionInfo, next: string) => {
    setRenamingSession(null)
    const title = next.trim().slice(0, 120)
    // Clearing the box reads as "never mind", not "remove the title": an
    // emptied input is far more often a slip than a wish for 未命名会话.
    if (!title || title === (item.title || '')) return
    const sid = item.session_id
    const previous = item.title
    const retitle = (value: string | undefined) =>
      setSessions(current => current.map(s => (s.session_id === sid ? { ...s, title: value } : s)))
    pendingTitlesRef.current.set(sid, title)
    retitle(title)
    try {
      await api(`/api/sessions/${encodeURIComponent(sid)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title }),
      })
    } catch {
      // api() has said why; the row goes back to what the server still has.
      retitle(previous)
    } finally {
      pendingTitlesRef.current.delete(sid)
    }
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
    // The second click of a double-click: the first one already opened the
    // session, and the double-click itself means "rename" -- running the whole
    // switch (state reset, history reload) again would be pure waste.
    if (event.detail > 1) return
    if (
      target?.closest('.ant-dropdown') ||
      target?.closest('.ant-dropdown-trigger') ||
      target?.closest('.session-item-delete-actions') ||
      target?.closest('.ant-typography-copy')
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

  const filteredSessions = useMemo(() => {
    const query = sessionSearch.trim().toLowerCase()
    const filtered = sessions.filter(
      item =>
        !query ||
        (item.title || '').toLowerCase().includes(query) ||
        item.session_id.toLowerCase().includes(query),
    )
    // Active sessions lead: this list used to push every live session to the
    // bottom, so the conversation the agent was working in sat under all of
    // history. ``sort`` is stable, so ties keep the server's order.
    return [...filtered].sort(compareSessions)
  }, [sessions, sessionSearch])

  const allFilteredSessionsSelected = filteredSessions.length > 0 &&
    filteredSessions.every(item => selectedSessionIds.includes(item.session_id))

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
    // The command is half-written -- it is waiting for its arguments -- so the
    // caret belongs in the composer. It cannot move there yet: the closing
    // palette hands focus back to whatever held it before it opened, so this
    // waits for the palette's `afterClose`.
    composerFocusPendingRef.current = true
  }

  const composerRef = useRef<TextAreaRef | null>(null)

  const composerFocusPendingRef = useRef(false)

  const focusComposer = () => {
    requestAnimationFrame(() => composerRef.current?.focus({ cursor: 'end' }))
  }

  const flushComposerFocus = () => {
    if (!composerFocusPendingRef.current) return
    composerFocusPendingRef.current = false
    focusComposer()
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

  return { MAX_CACHED_SESSIONS, activateTurnIndex, activeSession, activeSessionRef, activity, addFiles, allFilteredSessionsSelected, appendMessage, applyCommand, applyStreamingSnapshot, awayFromLatest, chatScrollRef, clearInterruptTimers, commandPrefixActive, commands, composerRef, composerSendable, connectWs, connected, continueTask, conversationGap, conversationMarkerRefs, conversationRailRef, conversationTurns, copyMessage, createSession, creatingSession, currentModel, currentModelRef, deleteSelectedSessions, deleteSession, dismissTaskGuidance, draftKey, drafts, dropQueuedLocally, expandedTraces, fileDragActive, fileInputRef, filteredCommands, filteredSessions, flushComposerFocus, focusComposer, followChatRef, handleChatDragLeave, handleChatDragOver, handleChatDrop, handleChatScroll, handleComposerKeyDown, handleComposerPaste, handleFilesSelected, handleRailMouseMove, handleSessionContainerClick, hoverClearTimerRef, hoveredTurn, hoveredTurnIndex, idleSnapshotSeenRef, inlineCommandEmpty, inlineCommandOpen, input, interruptAttemptsRef, interruptTimersRef, interrupting, isStreaming, jumpToLatest, keepTurnSummary, loadMessages, loadMessagesRequestRef, loadSessionPermissions, loadSessionState, loadSessions, loadingSessions, messageSendSeqRef, messages, messagesRef, pendingAttachments, pendingDeleteSessionId, pendingMessageIdRef, pendingModelRef, pendingSendRef, permissionLabel, permissionLevel, pickWorkspace, queueView, queuedMessages, queuedMessagesRef, railGeometryRef, renamingSession, commitSessionTitle, requestInterrupt, resolveComposerText, resolvedComposerText, resumingTaskId, revealSession, sandboxMode, scheduleHideTurnSummary, scrollChatToBottom, selectSession, selectedSessionIds, sendMessage, sendShortcut, sendShortcutLabel, sessionSearch, sessionState, sessions, setActiveSession, setActivity, setCommands, setConnected, setCreatingSession, setCurrentModel, setDrafts, setExpandedTraces, setHoveredTurn, setHoveredTurnIndex, setInput, setInterrupting, setIsStreaming, setLoadingSessions, setMessages, setPendingAttachments, setPendingDeleteSessionId, setPermissionLevel, setQueuedMessages, setRenamingSession, setResumingTaskId, setSandboxMode, setSelectedSessionIds, setSendShortcut, setSessionSearch, setSessionState, setSessions, stopStreaming, streamIdRef, taskActionRef, toggleTraceExpanded, turnRefs, updateMessage, updateSessionPermissions, uploadPendingAttachments, withdrawOneQueued, withdrawQueuedMessages } as const
}
