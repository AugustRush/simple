import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { CONVERSATION_SUMMARY_HALF_HEIGHT, INTERRUPT_RETRY_MS, INTERRUPT_STUCK_MS, TASK_INTERRUPTED_STATUSES } from '../constants'
import { truncate } from '../lib/format'
import { parseRoute } from '../lib/route'
import { fileHref, withFileSession } from '../lib/media'
import { findOpenSubAgentNote, foldSubAgentEvent, newSubAgentNote, sealSubAgentNotes } from '../lib/subagent'
import { isInTurnUserMessage, isSessionBusy, sessionStateBusy } from '../lib/tools'
import type { AttachmentInfo, CommandInfo, ConfirmRequest, Message, MessageRole, QueuedMessage, SessionInfo, SessionState, ToolState } from '../types'
import type { TextAreaRef } from 'antd/es/input/TextArea'
import { useUi } from './useUi'
import { useApiClient } from './useApiClient'
import { useConfirm } from './useConfirm'

type Deps = ReturnType<typeof useUi> & ReturnType<typeof useApiClient> & ReturnType<typeof useConfirm>

/**
 * Frame types that can arrive over an *idle* session: a keep-alive, a notice,
 * or a workspace change.  Every other frame on the session socket is the server
 * narrating a turn -- a tool call starting, a progress line, a chunk.
 *
 * The list is deliberately a deny-list.  A frame type nobody has taught this
 * list about is treated as work, which costs at most one extra state reading;
 * the other default would let a new frame type reintroduce the bug this exists
 * to prevent -- a card offering to continue a task the agent is executing.
 */
const IDLE_FRAMES = new Set(['heartbeat', 'notification', 'workspace_changed'])

const narratesTurnWork = (type: string): boolean =>
  Boolean(type) && !IDLE_FRAMES.has(type)

export function useConversations(deps: Deps) {
  const { api, commandDismissed, commandIndex, commandIndexPinned, commandItemRefs, confirmDeadlineRef, makeId, messageApi, setCommandDismissed, setCommandIndex, setCommandIndexPinned, setCommandPaletteOpen, setConfirmDetailOpen, setConfirmRemaining, setConfirmReq, setConfirmSession, setPaletteQuery, setView, token, wsRef } = deps

  const [sessions, setSessions] = useState<SessionInfo[]>([])



  //: A session named in the address bar is the one the page was asked for, so
  //: it is the state from the first render rather than something an effect
  //: applies later. An effect would run after the first paint, and the address
  //: writer -- which records what is on screen -- would have already replaced
  //: the link with a bare ``#/chat`` before the restore had a chance to open it.
  //: The restore effect below still validates it against the server's list; a
  //: stale id is cleared there and the bar follows.
  // Always null on the first paint: the conversation is the install's, and it
  // arrives from `POST /api/session/main` (see the boot effect). There used to
  // be a session id in the address here, which meant the first render could
  // open a thread the server no longer had.
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

  // A browser's storage quota is for the whole origin, and this cache holds up
  // to MAX_CACHED_SESSIONS of them. One very long conversation must not be able
  // to crowd the others out, so each entry has its own ceiling and is trimmed
  // from the oldest end -- the rows a reader actually sees are the recent ones,
  // and the rest is one request away from the server.
  const MAX_CACHED_BYTES = 384 * 1024

  // Sessions whose last write was refused. Retrying a write the browser has
  // already refused, every debounce tick, is wasted work; and leaving the
  // session in the index while holding no data for it makes the index claim a
  // cache that does not exist.
  const uncacheableRef = useRef<Set<string>>(new Set())

  useEffect(() => {
    if (!activeSession) return
    const sid = activeSession
    if (uncacheableRef.current.has(sid)) return
    const t = setTimeout(() => {
      // Sub-agent notes are live telemetry with no server-side record, so
      // caching them would only produce a flash of stale strips on reload.
      // Only the record's own rows are cached, so a reload replays exactly
      // what the server can confirm.
      const durable = messages.filter(item => item.role !== 'subagent')
      let payload = JSON.stringify(durable)
      if (payload.length > MAX_CACHED_BYTES) {
        // Trim from the oldest end until it fits. The alternative -- not
        // caching a long conversation at all -- costs the reader the instant
        // restore on switch, which is the whole point of having the cache.
        let keep = durable.length
        while (keep > 1 && payload.length > MAX_CACHED_BYTES) {
          keep = Math.floor(keep / 2)
          payload = JSON.stringify(durable.slice(durable.length - keep))
        }
      }
      try {
        localStorage.setItem(`chat_messages:${sid}`, payload)
        const raw = localStorage.getItem('chat_messages:index')
        const index = (raw ? (JSON.parse(raw) as string[]) : []).filter(
          id => id !== sid,
        )
        index.unshift(sid)
        while (index.length > MAX_CACHED_SESSIONS) {
          const evicted = index.pop()
          if (evicted) localStorage.removeItem(`chat_messages:${evicted}`)
        }
        localStorage.setItem('chat_messages:index', JSON.stringify(index))
      } catch {
        // A refused write used to be swallowed, which left the index listing a
        // session whose transcript was never stored -- the cache silently
        // stopped working for that conversation with nothing to show for it.
        // Drop it from the index and stop retrying, so the failure is a missing
        // cache entry (which the server read covers) rather than a stale one.
        uncacheableRef.current.add(sid)
        try {
          const raw = localStorage.getItem('chat_messages:index')
          const index = raw
            ? (JSON.parse(raw) as string[]).filter(id => id !== sid)
            : []
          localStorage.setItem('chat_messages:index', JSON.stringify(index))
        } catch {
          // Storage is unavailable entirely; there is nothing left to correct.
        }
      }
    }, 300)
    return () => clearTimeout(t)
  }, [messages, activeSession])


  // '' means "provider default": the backend resolves no override to the
  // active provider's default model. Never a display string — the backend
  // must not have to know the UI's wording.
  const [currentModel, setCurrentModel] = useState('')

  const [permissionLevel, setPermissionLevel] = useState('ask')

  const [sandboxMode, setSandboxMode] = useState('read_all')

  const [connected, setConnected] = useState(false)

  // True from the moment a socket is opened until one has actually connected.
  // Without it "not connected" has two meanings -- "still trying" and "dropped"
  // -- and the status line can only report the second of them.
  const [connecting, setConnecting] = useState(false)

  const [isStreaming, setIsStreaming] = useState(false)

  // Whether this session has narrated turn work that no idle reading of
  // ``/state`` has accounted for yet.  `isStreaming` cannot stand in for it: it
  // narrates a *text* stream, and a turn that is running tool calls after a
  // response failed emits none, so it reads "stopped" for the whole stretch
  // while frames keep arriving.  This flag is what that silence is measured
  // against -- it is set by the frames themselves, so it cannot be stale by
  // more than the frame in flight, and a reading that lands with nothing newer
  // behind it retires it.
  const [serverWorking, setServerWorking] = useState(false)

  //: How many work frames have arrived.  A reading can only retire the work it
  //: was asked after: if this moved while the request was in flight, the answer
  //: describes a moment before the work that has arrived since.
  const narrationSeqRef = useRef(0)

  // True between the user asking to stop and the turn actually unwinding.
  // The abort is cooperative, so this cannot be derived from `isStreaming`.
  const [interrupting, setInterrupting] = useState(false)

  const [activity, setActivity] = useState('')

  const [loadingSessions, setLoadingSessions] = useState(false)

  const [creatingSession, setCreatingSession] = useState(false)

  // Which traces are open, keyed by the trace's first row id. Those ids are
  // the server's own message ids, so a key from one conversation can never
  // match a trace in another and the state needs no per-session namespacing.
  const [expandedTraces, setExpandedTraces] = useState<Record<string, boolean>>({})

  const [hoveredTurn, setHoveredTurn] = useState<{ id: string; top: number } | null>(null)

  const [hoveredTurnIndex, setHoveredTurnIndex] = useState<number | null>(null)

  const [sendShortcut, setSendShortcut] = useState<'enter' | 'ctrl-enter'>(
    () => (localStorage.getItem('send_shortcut') === 'ctrl-enter' ? 'ctrl-enter' : 'enter'),
  )

  const sendShortcutLabel = sendShortcut === 'ctrl-enter' ? 'Ctrl/Cmd + Enter' : 'Enter'

  const [sessionState, setSessionState] = useState<SessionState | null>(null)

  // The transcript is a window onto a longer conversation. ``hasMoreHistory``
  // says there is more behind it and ``oldestSeq`` is the cursor that reaches
  // it, so reaching the beginning of a long session is a button rather than a
  // thing that silently was not loaded.
  const [hasMoreHistory, setHasMoreHistory] = useState(false)
  const [loadingHistory, setLoadingHistory] = useState(false)
  const oldestSeqRef = useRef(0)

  // How many rows one read asks for. The server bounds a single read; the
  // limit is a page size here, not a cut-off.
  const historyLimit = 200

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

  //: Seeded with the same value as ``activeSession``: the socket's ``onopen``
  //: ignores a connection whose id does not match this ref, so a linked session
  //: opened at mount would connect and then be told it is not the active one.
  const activeSessionRef = useRef<string | null>(activeSession)

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

  // The turn the open reply row belongs to. The server tags every stream event
  // with the message id that started the turn, which is what keeps a late or
  // retried event for another turn from being written into this row.
  const streamTurnRef = useRef<string | null>(null)

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
          api(
            `/api/sessions/${encodeURIComponent(sid)}/messages?limit=${historyLimit}`,
          ),
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
            const recordId = String(item.message_id || '')
            const seq = Number(item.seq || 0)
            return {
              // Keyed by the row's place in the session's transcript, which is
              // unique and stable: the server assigns it at write time, so a
              // row keeps its identity across reloads (a fresh id per load
              // remounts every row, replaying the whole entry animation) and a
              // row with no message id -- a tool trace -- is still addressable.
              id: seq > 0 ? `seq:${seq}` : `srv:${makeId()}`,
              messageId: recordId || undefined,
              createdAt: item.created_at || undefined,
              seq: seq > 0 ? seq : undefined,
              role: (item.role as MessageRole) || 'assistant',
              content: item.content || '',
              link,
              tool: item.tool,
              toolState: item.toolState as ToolState | undefined,
              // A run report's facts. Without these the row survives a reload as
              // bare text -- no task name, no status -- which is a different row
              // from the one that arrived live.
              title: item.title || undefined,
              status: item.status || undefined,
              taskId: item.taskId || undefined,
              runId: item.runId || undefined,
              proposalId: item.proposalId || undefined,
              reversibility: item.reversibility || undefined,
              attachments: Array.isArray(item.attachments) ? item.attachments : undefined,
              // A message the user sent during the turn that answered it.  It
              // has to survive this mapping: without the flag the row reads as
              // a turn boundary, and the grouping would split that turn again.
              interjection: item.interjection === true ? true : undefined,
              // The thinking the server kept for this assistant row.  It
              // rendered live from reasoning_chunk, so dropping it here is
              // what used to erase the strip on the post-turn re-read.
              reasoning:
                typeof item.reasoning === 'string' && item.reasoning
                  ? item.reasoning
                  : undefined,
            }
          },
        )
        // The window is the newest slice; the cursor pages further back.
        setHasMoreHistory(data.has_more === true)
        oldestSeqRef.current = Number(data.oldest_seq || 0)
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
          // Reconcile by the record's own id first, and only fall back to the
          // tool's name. Matching on the name alone let two concurrent calls to
          // the same tool overwrite each other's row, and left a running row
          // with no durable counterpart stranded at the bottom of the list.
          for (const tool of runningTools) {
            const durableIndex = merged.findIndex(item => {
              if (item.role !== 'tool' || item.toolState !== 'running') return false
              if (tool.operationId && item.operationId) {
                return item.operationId === tool.operationId
              }
              return item.tool === tool.tool
            })
            if (durableIndex >= 0) merged[durableIndex] = tool
            else merged.splice(Math.max(0, merged.length - 1), 0, tool)
          }
          // The reply being written right now, and the notes describing this
          // turn, belong at the end: they are the latest thing that happened.
          // Appending them after the newest durable row is what keeps a
          // mid-turn refresh from rendering a live row *above* recorded
          // history it followed.
          merged.push(...runningSubagents)
          // Messages sent during this turn are not in the transcript yet -- the
          // record gets them when the turn ends -- so a refresh mid-turn would
          // otherwise erase the reader's own words from the screen. Matched by
          // id: the same text twice in one turn is two messages, and content
          // comparison would drop the second one from the screen.
          const knownIds = new Set(
            merged.map(item => item.messageId).filter(Boolean) as string[],
          )
          const pendingInTurn = current.filter(
            item => item.role === 'user' && (item.queued || item.interjection),
          )
          for (const pending of pendingInTurn) {
            if (pending.messageId && knownIds.has(pending.messageId)) continue
            if (pending.messageId) knownIds.add(pending.messageId)
            merged.push(pending)
          }
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
    [api, applyStreamingSnapshot, historyLimit, makeId, token],
  )

  /**
   * Fetch the page of transcript immediately before what is on screen, and
   * prepend it.
   *
   * The rows carry the server's own sequence, so an older page is known to be
   * older rather than guessed: it is prepended wholesale, and the rows already
   * on screen -- including anything the socket has added since -- are left
   * exactly where they are. Nothing is re-sorted and nothing is dropped, so
   * paging cannot disturb a reader who is part-way through the conversation.
   */
  const loadEarlierMessages = useCallback(async () => {
    const sid = activeSessionRef.current
    const before = oldestSeqRef.current
    if (!sid || !before || loadingHistory) return
    setLoadingHistory(true)
    try {
      const resp = await api(
        `/api/sessions/${encodeURIComponent(sid)}/messages`
        + `?limit=${historyLimit}&before_seq=${before}`,
      )
      const data = await resp.json()
      if (activeSessionRef.current !== sid) return
      const older = (data.messages || []).map(
        (item: any): Message => {
          const link = withFileSession(item.link || '', sid, token)
          const recordId = String(item.message_id || '')
          const seq = Number(item.seq || 0)
          return {
            id: seq > 0 ? `seq:${seq}` : `srv:${makeId()}`,
            messageId: recordId || undefined,
            createdAt: item.created_at || undefined,
            seq: seq > 0 ? seq : undefined,
            role: (item.role as MessageRole) || 'assistant',
            content: item.content || '',
            link,
            tool: item.tool,
            toolState: item.toolState as ToolState | undefined,
            attachments: Array.isArray(item.attachments) ? item.attachments : undefined,
            interjection: item.interjection === true ? true : undefined,
            reasoning:
              typeof item.reasoning === 'string' && item.reasoning
                ? item.reasoning
                : undefined,
          }
        },
      )
      if (!older.length) {
        setHasMoreHistory(false)
        return
      }
      // Keep the reader where they were: the pane is tall by exactly the rows
      // added above it.
      const container = chatScrollRef.current
      const previousHeight = container?.scrollHeight ?? 0
      const previousTop = container?.scrollTop ?? 0
      const known = new Set(messagesRef.current.map(item => item.id))
      const fresh = older.filter((item: Message) => !known.has(item.id))
      if (!fresh.length) {
        oldestSeqRef.current = Number(data.oldest_seq || before)
        setHasMoreHistory(data.has_more === true)
        return
      }
      messagesRef.current = [...fresh, ...messagesRef.current]
      setMessages(messagesRef.current)
      oldestSeqRef.current = Number(data.oldest_seq || before)
      setHasMoreHistory(data.has_more === true)
      if (container) {
        requestAnimationFrame(() => {
          const grown = container.scrollHeight - previousHeight
          if (grown > 0) container.scrollTop = previousTop + grown
        })
      }
    } catch {
      messageApi.warning('加载更早的消息失败，请重试')
    } finally {
      setLoadingHistory(false)
    }
  }, [api, historyLimit, makeId, messageApi, token])

  const loadSessionState = useCallback(async (sid: string) => {
    const sendSeq = messageSendSeqRef.current
    const narrationAtRequest = narrationSeqRef.current
    try {
      const resp = await api(`/api/sessions/${encodeURIComponent(sid)}/state`)
      const data = (await resp.json()) as SessionState
      if (activeSessionRef.current === sid) {
        setSessionState(data)
        if (narrationSeqRef.current === narrationAtRequest) {
          // Nothing was narrated while this was in flight, so the answer is the
          // newest word on the session: busy keeps the flag, idle retires it.
          setServerWorking(sessionStateBusy(data))
        }
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
        // A reading that never arrived is not evidence of work, and keeping the
        // flag would keep polling a server that is not answering. Nothing is
        // lost if it comes back: the frames it narrates set the flag again.
        setServerWorking(false)
      }
    }
  }, [api, applyStreamingSnapshot])

  // Both hand React the ref's array directly. Copying it again would allocate a
  // second array of every row on every call -- and `updateMessage` is called
  // once per streamed token, so on a long conversation that copy is paid per
  // chunk for nothing. The ref is only ever reassigned to a fresh array, never
  // mutated in place, so handing it over is safe.
  const appendMessage = useCallback((next: Message) => {
    messagesRef.current = [...messagesRef.current, next]
    setMessages(messagesRef.current)
  }, [])

  const updateMessage = useCallback((id: string, patch: Partial<Message>) => {
    messagesRef.current = messagesRef.current.map(item =>
      item.id === id ? { ...item, ...patch } : item,
    )
    setMessages(messagesRef.current)
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
      // The approval bar belongs to the session its socket narrates. On a
      // session switch the old prompt would sit over the new session's UI,
      // and its reply would be sent on this new socket where the old
      // session's waiter cannot hear it. Dismiss it here: an approval still
      // pending is re-offered (with the time that remains) when the old
      // session's socket reconnects, or fails closed at its own deadline.
      setConfirmReq(null)
      setConfirmDetailOpen(false)
      setConnected(false)
      setConnecting(true)
      setActivity('正在连接…')
      setIsStreaming(false)
      streamIdRef.current = null
      streamTurnRef.current = null
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
        setConnecting(false)
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

      // A dropped socket cannot deliver the turn's end, so the open row is
      // retired here. Otherwise it keeps its "正在生成" label, its typing dots
      // and a pulsing thinking note for a turn nobody is doing any more.
      const sealOpenStream = (activityText: string) => {
        setConnecting(false)
        if (streamIdRef.current) {
          updateMessage(streamIdRef.current, { streaming: false })
          streamIdRef.current = null
        }
        streamTurnRef.current = null
        if (messagesRef.current.some(item => item.role === 'subagent' && item.subagent?.running)) {
          messagesRef.current = sealSubAgentNotes(messagesRef.current)
          setMessages(messagesRef.current)
        }
        setConnected(false)
        setResumingTaskId(null)
        setIsStreaming(false)
        setActivity(activityText)
      }

      ws.onclose = () => {
        if (wsRef.current !== ws || activeSessionRef.current !== sid) return
        sealOpenStream('连接已断开')
      }

      ws.onerror = () => {
        if (wsRef.current !== ws || activeSessionRef.current !== sid) return
        sealOpenStream('连接异常')
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

        // The server is doing turn work in this session, whatever the last
        // snapshot said.  Recorded before the frame is handled, so a frame that
        // fails to match any branch below still counts as the work it narrated.
        if (narratesTurnWork(String(evt.type || ''))) {
          narrationSeqRef.current += 1
          setServerWorking(true)
        }

        // Thinking arrives before the first answer token, so this is often what
        // creates the assistant row — the same row `stream_chunk` will then
        // fill. It does not touch `content`: the note is not the reply.
        // Every event carries the turn it belongs to. A row is only ever
        // written by its own turn's events: without this check a reply for a
        // restarted or withdrawn turn lands inside whichever reply happened to
        // be on screen, and the reader sees one message appended to another.
        const eventTurn = String(evt.turn_id || '')
        const belongsToCurrentTurn = () =>
          !eventTurn || !streamTurnRef.current || eventTurn === streamTurnRef.current

        if (evt.type === 'reasoning_chunk') {
          const chunk = String(evt.chunk || '')
          if (!chunk) return
          if (!belongsToCurrentTurn()) return
          if (eventTurn) streamTurnRef.current = eventTurn
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
          if (!belongsToCurrentTurn()) return
          if (eventTurn) streamTurnRef.current = eventTurn
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
          // A snapshot for a turn this row is not showing replaces nothing: it
          // starts its own row rather than overwriting a reply in progress.
          if (!belongsToCurrentTurn()) {
            if (!text) return
            appendMessage({
              id: makeId(),
              role: 'assistant',
              content: text,
              streaming: true,
              ...(reasoning !== undefined ? { reasoning } : {}),
            })
            return
          }
          if (eventTurn) streamTurnRef.current = eventTurn
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
          if (streamIdRef.current && belongsToCurrentTurn()) {
            updateMessage(streamIdRef.current, {
              content: evt.text || messagesRef.current.find(i => i.id === streamIdRef.current)?.content || '',
              streaming: false,
            })
            streamIdRef.current = null
          }
          if (eventTurn && eventTurn === streamTurnRef.current) streamTurnRef.current = ''
          setIsStreaming(false)
          setActivity('')
          // The turn is over, so any prompt still on screen was answered or
          // timed out server-side; leaving it up would offer dead buttons.
          setConfirmReq(null)
          // A batch that never emitted ``batch_finished`` (interrupted, or an
          // older server) must not keep spinning forever.
          messagesRef.current = sealSubAgentNotes(messagesRef.current)
          setMessages(messagesRef.current)
          // Re-read the record now that the turn is over. Everything the live
          // socket showed for it exists there -- including the rows the server
          // orders differently (all of a turn's tool rows ahead of its reply,
          // which live delivery interleaves) -- so this is what stops the
          // transcript from looking different after a refresh than it did while
          // it was happening.
          loadMessages(sid)
          loadSessions()
          loadSessionState(sid)
          const nextQueued = queuedMessagesRef.current.shift()
          if (nextQueued) {
            setQueuedMessages([...queuedMessagesRef.current])
            updateMessage(nextQueued.id, { queued: false })
          }
          return
        }

        // Find the running row an event closes. By operation id when both
        // sides have one: two calls to the same tool can be in flight at once,
        // and matching on the name alone let the first one's result land on the
        // second one's row.
        const findRunningTool = () => {
          const rows = messagesRef.current
          const operationId = String(evt.operation_id || '')
          if (operationId) {
            const exact = rows.find(
              item =>
                item.role === 'tool' &&
                item.toolState === 'running' &&
                item.operationId === operationId,
            )
            if (exact) return exact
          }
          return [...rows]
            .reverse()
            .find(item => item.role === 'tool' && item.tool === evt.name && item.toolState === 'running')
        }

        if (evt.type === 'tool_start') {
          setActivity(evt.name ? `正在执行 ${evt.name}` : '正在执行工具')
          appendMessage({
            id: makeId(),
            role: 'tool',
            content: '',
            tool: evt.name || 'tool',
            toolState: 'running',
            operationId: String(evt.operation_id || '') || undefined,
          })
          return
        }

        if (evt.type === 'tool_end') {
          const target = findRunningTool()
          if (target) {
            updateMessage(target.id, {
              // The frame carries the executor's verdict. Without it a call
              // that returned an error still closed as "已完成", which is a
              // claim nothing had checked.
              toolState: evt.ok === false ? 'failed' : 'done',
              content: evt.result ? truncate(String(evt.result), 220) : '',
            })
          }
          return
        }

        if (evt.type === 'tool_progress') {
          const target = findRunningTool()
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
          const target = findRunningTool()
          if (target) {
            updateMessage(target.id, {
              toolState: 'blocked',
              content: evt.reason || '操作被阻止',
            })
          }
          return
        }

        if (evt.type === 'message_queued') {
          // A message accepted mid-turn.  The channel now says *which* queue it
          // went into, so the row can be marked for what it is instead of being
          // guessed at from the wording of a status line.  Nothing is appended
          // to the transcript: the message is already there, optimistically,
          // and the server's record will confirm it -- an interjection inside
          // the turn it interrupted, a restart as its own later turn.
          const pendingId = String(evt.message_id || '')
          const kind = String(evt.kind || '')
          const text = String(evt.text || '')
          if (pendingId) {
            updateMessage(pendingId, {
              queued: kind !== 'interjection',
              interjection: kind === 'interjection',
            })
          }
          if (kind === 'interjection') {
            // The running turn has read it, so it is no longer waiting for
            // anything: leaving it in the strip would keep offering a withdraw
            // for a message that cannot be taken back.
            queuedMessagesRef.current = queuedMessagesRef.current.filter(
              item => item.id !== pendingId,
            )
            setQueuedMessages([...queuedMessagesRef.current])
            setActivity('这条插话已交给正在进行的回合')
          } else {
            setActivity(
              `已排队 ${queuedMessagesRef.current.length || 1} 条消息，将在本回合结束后处理`,
            )
            if (text) {
              const entry = queuedMessagesRef.current.find(item => item.id === pendingId)
              if (entry) entry.text = text
            }
          }
          return
        }

        if (evt.type === 'proposal_raised') {
          // The agent's idea, as a message. It has no buttons on purpose: the
          // board is where a proposal is answered, and it is the only surface
          // that knows whether this one is still pending.
          const row = evt.message
          if (row && row.role === 'proposal' && row.title) {
            const id = typeof row.seq === 'number' && row.seq > 0
              ? `seq:${row.seq}`
              : `proposal:${row.proposalId || row.title}`
            if (!messagesRef.current.some(item => item.id === id)) {
              appendMessage({
                id,
                role: 'proposal',
                content: String(row.content || ''),
                title: String(row.title || ''),
                reversibility: String(row.reversibility || ''),
                proposalId: String(row.proposalId || ''),
                createdAt: row.created_at,
                seq: typeof row.seq === 'number' ? row.seq : undefined,
              })
            }
          }
          return
        }

        if (evt.type === 'run_report') {
          // The agent speaking without being asked: a scheduled run finished and
          // its result belongs here, where the person is, rather than in a panel
          // they have to remember to open. The row arrives as the server's own
          // display row (same shape as the reloaded history), so it is appended
          // verbatim -- id included, which is what makes a reconnect that
          // replays it a no-op rather than a duplicate.
          const row = evt.message
          if (row && row.role === 'report' && row.content) {
            // The same id a reload gives this row (`seq:<n>`), so a socket that
            // replays it after a reconnect cannot add a second copy.
            const id = typeof row.seq === 'number' && row.seq > 0
              ? `seq:${row.seq}`
              : `report:${row.runId || row.content.slice(0, 40)}`
            if (!messagesRef.current.some(item => item.id === id)) {
              appendMessage({
                id,
                role: 'report',
                content: String(row.content),
                title: String(row.title || ''),
                status: String(row.status || ''),
                taskId: String(row.taskId || ''),
                runId: String(row.runId || ''),
                createdAt: row.created_at,
                seq: typeof row.seq === 'number' ? row.seq : undefined,
              })
            }
          }
          return
        }

        if (evt.type === 'command_result') {
          // A command's answer, which is a reply rather than what the agent is
          // doing: shown as a row of its own, and journalled by the server, so
          // it is still there after a reload.  As a status it was overwritten by
          // the very next frame and the reader saw a flash and nothing else.
          const text = String(evt.text || '')
          if (text.trim()) {
            appendMessage({
              id: makeId(),
              role: evt.level === 'error' ? 'error' : 'command',
              content: text,
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
          // A status is what the agent is doing *right now*, not
          // something it said, and the server does not journal it. So it reads
          // in the activity line and nowhere else: a transcript row here would
          // be a line the record has no place for, which means it would be
          // replayed from cache on the next open and then dropped by the
          // server's read, leaving the transcript in a different order than the
          // one the reader just watched. Command output arrives here too, so
          // this is also where a command's output is shown while it runs.
          setActivity(truncate(text, 90))
          return
        }

        if (evt.type === 'notification') {
          // Also unjournalled and also not something the agent said: a notice,
          // not a message.
          messageApi.info(
            evt.body ? `${evt.title || '通知'}：${evt.body}` : evt.title || '通知',
          )
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
            streamIdRef.current = null
          }
          streamTurnRef.current = null
          setActivity('')
          setConfirmReq(null)
          messagesRef.current = sealSubAgentNotes(messagesRef.current)
          setMessages(messagesRef.current)
          return
        }

        if (evt.type === 'confirm_request') {
          const timeout = Number(evt.timeout_seconds) > 0 ? Number(evt.timeout_seconds) : 300
          confirmDeadlineRef.current = Date.now() + timeout * 1000
          setConfirmRemaining(timeout)
          setConfirmDetailOpen(false)
          setConfirmSession(activeSessionRef.current || null)
          setConfirmReq(evt as ConfirmRequest)
          // The bar covers the composer, so a still-focused textarea would
          // swallow keystrokes into an input the user can no longer see, and
          // its send shortcut would queue a message instead of answering the
          // approval. Hand focus back to the page.
          composerRef.current?.blur()
        }
      }
    },
    [appendMessage, loadMessages, loadSessionState, loadSessions, makeId, token, updateMessage],
  )

  const selectSession = useCallback(
    (
      sid: string,
      //: Opening a conversation is normally also "go to the conversation". Boot
      //: is the exception: a reload of `#/schedules` must stay on 自动化, and
      //: the conversation it restores is the one the app always has, not a
      //: navigation somebody asked for.
      options: { stayOnView?: boolean } = {},
    ) => {
      followChatRef.current = true
      queuedMessagesRef.current = []
      setQueuedMessages([])
      setSessionState(null)
      activeSessionRef.current = sid
      setActiveSession(sid)
      if (!options.stayOnView) setView('home')
      streamIdRef.current = null
      streamTurnRef.current = null
      setIsStreaming(false)
      // Both describe the session that was open, not the one being opened: the
      // frames that set the flag came from the old session's socket, and the
      // next reading is what re-establishes the truth for this one.
      narrationSeqRef.current = 0
      setServerWorking(false)
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
      // Empty means "no thread open" -- the landing page. There is nothing to
      // fetch: `/api/sessions//state` is a 404, and it used to be a 404 the
      // browser console reported every time somebody went back to the landing.
      if (sid) {
        loadMessages(sid)
        loadSessionPermissions(sid)
      }
    },
    [loadMessages, loadSessionPermissions, token],
  )

  useEffect(() => {
    let cancelled = false
    // One conversation, and the server keeps it.
    //
    // The browser used to remember a session id and restore whichever one was
    // last open -- which made "which conversation am I in" a piece of client
    // state that could be lost (new profile, cleared storage) and that the
    // server knew nothing about. It is now the install's, asked for once at
    // boot: the answer cannot disagree with the sessions on disk because there
    // is nothing on this side to disagree with.
    const route = parseRoute(window.location.hash)
    const restore = async () => {
      let main = ''
      try {
        const resp = await api('/api/session/main', { method: 'POST' })
        const data = await resp.json()
        main = String(data.session_id || '')
      } catch {
        // No conversation available (an older gateway, or a hard failure): the
        // landing shows and the first message mints one, as before.
      }
      if (cancelled) return
      if (main) {
        // Stay on the page the address names: a reload of `#/schedules` is a
        // reload of 自动化, not a jump into the conversation behind it.
        selectSession(main, { stayOnView: route.view !== 'home' })
        return
      }
      activeSessionRef.current = null
      setActiveSession(null)
    }
    restore()
    api('/api/commands')
      .then(r => r.json())
      .then(data => setCommands(data.commands || []))
      .catch(() => {})
    return () => { cancelled = true }
  }, [api, selectSession])

  // No remembered-view key any more. Which conversation is open is the install's
  // fact, not this browser's: a second browser, a cleared profile or a private
  // window used to start a fresh conversation beside the real one. The server
  // answers `POST /api/session/main` and the address bar names the open thread.

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
  //
  // It polls while *any* of the three signs of work is up: the client's stream
  // flag, the server's own word in the snapshot, and the frames that have
  // arrived since that snapshot. The last two are why it keeps going where the
  // stream flag stops: a turn running tool calls emits no chunks, and a turn
  // that reported a failed response clears the flag while the server may still
  // hold the work. A poll that stopped with the flag is what left the client
  // holding a "nothing is running" snapshot over a running session -- the card
  // offering 继续任务 over work the agent was doing.
  const sessionBusy = sessionStateBusy(sessionState)
  useEffect(() => {
    if (!activeSession) return
    const refresh = () => loadSessionState(activeSession)
    refresh()
    if (!isStreaming && !sessionBusy && !serverWorking) return
    const timer = window.setInterval(refresh, 1200)
    return () => window.clearInterval(timer)
  }, [activeSession, isStreaming, sessionBusy, serverWorking, loadSessionState])

  /**
   * Whether the task-guidance card may be shown.
   *
   * The card's premise is "this task has stopped, and only you can restart
   * it". Every part of that premise is a claim about work, so it takes all of
   * them agreeing -- the stream flag, the server's snapshot, and the frames
   * narrated since -- plus a task the record calls interrupted, and no answer
   * to this same prompt already given. Any disagreement hides the card: a card
   * over a working session is the one failure this prompt cannot afford, while
   * a card shown a second late costs nothing.
   */
  const taskGuidanceVisible = Boolean(
    sessionState?.task?.active_goal &&
      TASK_INTERRUPTED_STATUSES.has(
        String(sessionState.task.status || '').toLowerCase(),
      ) &&
      !isStreaming &&
      !sessionBusy &&
      !serverWorking &&
      resumingTaskId !== (sessionState.task.task_id || sessionState.task.active_goal || ''),
  )

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
    // The server names the conversation from this sentence (see
    // `SessionService.ensure_auto_title`). Read the list once the turn is
    // running, so the sidebar shows the name while the answer is still coming --
    // waiting for the turn to end would leave the row unnamed exactly when
    // somebody is watching it.
    void refreshSessionTitles()

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
        setView('home')
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
    setMessages(messagesRef.current)
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
      messageApi.info('请先打开对话')
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

  /**
   * Re-read the list after a turn has been named.
   *
   * The title is written by the server on the first message, and the drawer is
   * the only place it shows. Without this the row stayed "未命名会话" until
   * something else happened to reload the list.
   */
  const refreshSessionTitles = useCallback(async () => {
    try {
      const resp = await api('/api/sessions')
      const data = await resp.json()
      const pending = pendingTitlesRef.current
      setSessions((current) =>
        (data.sessions || []).map((item: SessionInfo) => {
          if (pending.has(item.session_id)) {
            return { ...item, title: pending.get(item.session_id) }
          }
          return item
        }) as SessionInfo[],
      )
    } catch {
      // A drawer that cannot refresh keeps the names it had.
    }
  }, [api])

  /**
   * Open the conversation, optionally with *seed* already in the box.
   *
   * This is how a capability is offered now: a page does not know how to build
   * the thing, so it opens a conversation about it and puts the first sentence in
   * the box. It used to mint a *new* session for that, which is how a product
   * with one relationship accumulated dozens of half-conversations -- "在对话里修改"
   * left your actual conversation behind to start a parallel one.
   *
   * The seed is keyed by the conversation's id because `setInput` writes to the
   * *current* draft key, which is still the previous conversation's at this point.
   */
  const openConversation = async (seed?: string) => {
    try {
      setCreatingSession(true)
      const resp = await api('/api/session/main', { method: 'POST' })
      const data = await resp.json()
      const sid = String(data.session_id || '')
      if (!sid) return
      if (seed) setDrafts(prev => ({ ...prev, [sid]: seed }))
      activeSessionRef.current = sid
      await loadSessions()
      selectSession(sid)
      focusComposer()
    } catch {
      // Handled by api helper.
    } finally {
      setCreatingSession(false)
    }
  }

  const updateSessionPermissions = async (patch: { level?: string; sandbox?: string }) => {
    if (!activeSession) {
      messageApi.info('请先打开对话')
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

  // Stable across renders on purpose. These reach memoised message rows as
  // props, and a fresh function identity every render would defeat every one of
  // them -- the rows would re-render exactly as they did before.
  const copyMessage = useCallback((content: string) => {
    navigator.clipboard?.writeText(content).then(() => {
      messageApi.success('已复制')
    }).catch(() => {
      messageApi.error('复制失败')
    })
  }, [messageApi])

  const applyCommand = (command: CommandInfo) => {
    setInput(`/${command.name} `)
    setView('home')
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

  // Taking the newest turn back is one server call, and the client does not
  // splice its own copy afterwards: the cut is by transcript position, and the
  // optimistic rows the client holds do not carry positions. Reloading is what
  // makes the screen show what the record now says -- and it also drops the
  // cached copy, so a refresh cannot resurrect the removed turn.
  const rewindLastTurn = useCallback(async (): Promise<string | null> => {
    const sid = activeSessionRef.current
    if (!sid) return null
    try {
      const resp = await api(`/api/sessions/${encodeURIComponent(sid)}/rewind`, {
        method: 'POST',
      })
      const data = await resp.json().catch(() => ({}))
      if (!resp.ok) {
        messageApi.error(String(data.error || `回退失败（${resp.status}）`))
        return null
      }
      await loadMessages(sid)
      void loadSessions()
      return String(data.user_text || '')
    } catch {
      messageApi.error('回退失败')
      return null
    }
  }, [api, loadMessages, loadSessions, messageApi])

  //: These two keep their identity (they are props of every memoised row) while
  //: still calling the *current* handlers.  ``setInput`` closes over the draft
  //: key, and ``sendMessage`` closes over the open session and its attachments;
  //: a captured copy writes the editor's text into another session's draft, or
  //: sends into the session that was open when the page first rendered.
  const rewindHandlersRef = useRef({ setInput, focusComposer, sendMessage })
  rewindHandlersRef.current = { setInput, focusComposer, sendMessage }

  // Edit: rewind, then hand the words back to the composer. Nothing is sent --
  // the reader asked to change the sentence, and they get to change it before
  // it goes anywhere.
  const editLastTurn = useCallback(async () => {
    const text = await rewindLastTurn()
    if (text === null) return
    rewindHandlersRef.current.setInput(text)
    rewindHandlersRef.current.focusComposer()
  }, [rewindLastTurn])

  // Regenerate: rewind and send the same words. The rewind tells us what they
  // were, so the client is not guessing which turn the server removed.
  const regenerateLastTurn = useCallback(async () => {
    const text = await rewindLastTurn()
    if (text === null) return
    await rewindHandlersRef.current.sendMessage(text)
  }, [rewindLastTurn])

  // Stable for the same reason as ``copyMessage``: it is a prop of every row that
  // owns a tool trace.
  const toggleTraceExpanded = useCallback((id: string) => {
    setExpandedTraces(prev => ({ ...prev, [id]: !prev[id] }))
  }, [])

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

  // A turn, for the rail. A message sent *during* a turn is not one: counting
  // it gave the rail two markers for one exchange and made "第 N 轮" name a
  // number that no longer matched the turns above it.
  const conversationTurns = messages.filter(
    item => item.role === 'user' && !isInTurnUserMessage(item),
  )

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

  //: The newest turn can be cut only while nothing is running and nothing is
  //: queued: the server refuses otherwise, and a button that exists and is
  //: refused is worse than one that is disabled.
  const canRewind =
    Boolean(activeSession) &&
    !isStreaming &&
    String(sessionState?.operation_state || 'idle') === 'idle'

  return { MAX_CACHED_SESSIONS, activateTurnIndex, refreshSessionTitles, openConversation, canRewind, editLastTurn, regenerateLastTurn, rewindLastTurn, activeSession, activeSessionRef, activity, addFiles, appendMessage, applyCommand, applyStreamingSnapshot, awayFromLatest, chatScrollRef, clearInterruptTimers, commandPrefixActive, commands, composerRef, composerSendable, connectWs, connected, connecting, continueTask, conversationGap, conversationMarkerRefs, conversationRailRef, conversationTurns, copyMessage, creatingSession, currentModel, currentModelRef, dismissTaskGuidance, draftKey, drafts, dropQueuedLocally, expandedTraces, fileDragActive, fileInputRef, filteredCommands, flushComposerFocus, focusComposer, followChatRef, handleChatDragLeave, handleChatDragOver, handleChatDrop, handleChatScroll, handleComposerKeyDown, handleComposerPaste, handleFilesSelected, handleRailMouseMove, hasMoreHistory, historyLimit, hoverClearTimerRef, hoveredTurn, hoveredTurnIndex, idleSnapshotSeenRef, inlineCommandEmpty, inlineCommandOpen, input, interruptAttemptsRef, interruptTimersRef, interrupting, isStreaming, jumpToLatest, keepTurnSummary, loadEarlierMessages, loadMessages, loadMessagesRequestRef, loadSessionPermissions, loadSessionState, loadSessions, loadingHistory, loadingSessions, messageSendSeqRef, messages, messagesRef, pendingAttachments, pendingMessageIdRef, pendingModelRef, pendingSendRef, permissionLabel, permissionLevel, pickWorkspace, queueView, queuedMessages, queuedMessagesRef, railGeometryRef, requestInterrupt, resolveComposerText, resolvedComposerText, resumingTaskId, sandboxMode, scheduleHideTurnSummary, scrollChatToBottom, selectSession, sendMessage, sendShortcut, sendShortcutLabel, sessionState, sessions, setActiveSession, setActivity, setCommands, setConnected, setConnecting, setCreatingSession, setCurrentModel, setDrafts, setExpandedTraces, setHoveredTurn, setHoveredTurnIndex, setInput, setInterrupting, setIsStreaming, setLoadingSessions, setMessages, setPendingAttachments, setPermissionLevel, setQueuedMessages, setResumingTaskId, setSandboxMode, setSendShortcut, setSessionState, setSessions, stopStreaming, streamIdRef, taskActionRef, taskGuidanceVisible, toggleTraceExpanded, turnRefs, updateMessage, updateSessionPermissions, uploadPendingAttachments, withdrawOneQueued, withdrawQueuedMessages } as const
}
