/** The transcript: every message row, grouped into turns.
 *
 * A component rather than an inline render function for one reason: streaming.
 * A streaming answer produces a new message list per token, and re-rendering
 * every row each time -- re-running the markdown renderer over the whole
 * conversation -- is what made a long chat crawl while the agent was writing.
 * Each row is memoised on the message object it renders, and an unchanged row
 * keeps its object identity (the store only replaces the row a patch touches),
 * so a chunk now re-renders the one row being written.
 */

import type { MutableRefObject } from 'react'
import React, { memo, useMemo } from 'react'
import { createPortal } from 'react-dom'
import {
  CloseOutlined,
  CopyOutlined,
  DownOutlined,
  FileTextOutlined,
  RobotOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { Avatar, Button } from 'antd'
import { formatDateTime, relativeTime } from '../lib/format'
import { markdownToHtml } from '../lib/markdown'
import { fileHref, mediaKindForUrl } from '../lib/media'
import { subagentCounts } from '../lib/subagent'
import { isInTurnUserMessage, summariseToolDots, toolStateLabel } from '../lib/tools'
import type { Message, SubAgentNote } from '../types'

export interface MessageListProps {
  messages: Message[]
  activeSession: string | null
  token: string
  expandedTraces: Record<string, boolean>
  turnRefs: MutableRefObject<Record<string, HTMLDivElement | null>>
  updateMessage: (id: string, patch: Partial<Message>) => void
  copyMessage: (content: string) => void
  toggleTraceExpanded: (id: string) => void
}


function AttachmentRow({ item, activeSession, token }: {
  item: Message
  activeSession: string | null
  token: string
}) {
  const media = item.link ? mediaKindForUrl(item.link) : 'file'
  const label = (item.content || '附件').split(/[\\/]/).pop() || '附件'

  if (media === 'image') {
    return (
      <div className="attachment-card attachment-image-card">
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
      <div className="attachment-card attachment-player-card">
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
    <a className="attachment-file-card" href={item.link} target="_blank" rel="noreferrer">
      <span className="attachment-file-icon"><FileTextOutlined /></span>
      <span className="attachment-file-main">
        <strong title={label}>{label}</strong>
        <small>附件 · 点击打开</small>
      </span>
      <DownOutlined className="attachment-file-arrow" rotate={-90} />
    </a>
  )
}


function ToolEvent({ item, activeSession, token }: {
  item: Message
  activeSession: string | null
  token: string
}) {
  if (item.tool === 'attachment' || item.link) {
    return <MemoAttachmentRow item={item} activeSession={activeSession} token={token} />
  }
  return (
    <div className="tool-event">
      <span className={`tool-event-dot ${item.toolState || 'done'}`} />
      <span className="tool-event-name">{item.tool || '文件'}</span>
      <span className="tool-event-state">{toolStateLabel(item.toolState)}</span>
      {(item.content || item.link) && (
        <div className="tool-event-details">
          {item.content && <div className="tool-event-detail">{item.content}</div>}
          {item.link && (
            <a className="tool-event-link" href={item.link} target="_blank" rel="noreferrer">
              <FileTextOutlined /> 打开文件
            </a>
          )}
        </div>
      )}
    </div>
  )
}





/**
 * One batch's progress strip.
 *
 * Rendered inside the assistant row it belongs to rather than as a row of its
 * own: a full-width block landing after the reply reads as a second message
 * from the agent, which is what it looked like when it was a sibling row.
 */
function SubAgentNote({ item, updateMessage }: {
  item: Message
  updateMessage: (id: string, patch: Partial<Message>) => void
}) {
  const note = item.subagent as SubAgentNote
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
    <div className={`subagent-note subagent-note-${tone}`}>
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


/**
 * The model's thinking for one turn, collapsed above the reply.
 *
 * Collapsed by default because it is not what the reader came for; the strip
 * still has to move while it streams, so the dot pulses and the count climbs
 * even shut. Opening it shows the text as it arrives — rendered verbatim in
 * a pre-wrapped block rather than through the markdown renderer, because
 * thinking is raw model text (newlines and asterisks are structural to it,
 * not formatting) and a half-finished markdown document would reflow on
 * every token.
 *
 * Returns null rather than an empty strip: a provider that does not think
 * must leave no trace in the transcript.
 */
function ReasoningNote({ item, updateMessage }: {
  item: Message
  updateMessage: (id: string, patch: Partial<Message>) => void
}) {
  const text = item.reasoning || ''
  if (!text) return null
  const running = !!item.streaming
  return (
    <div className={`thinking-note ${running ? 'thinking-note-running' : ''}`}>
      <button
        type="button"
        className="thinking-note-head"
        aria-expanded={!!item.reasoningOpen}
        onClick={() =>
          updateMessage(item.id, { reasoningOpen: !item.reasoningOpen })
        }
      >
        <span className="thinking-note-dot" />
        <span className="thinking-note-title">思考过程</span>
        <span className="thinking-note-state">{running ? '思考中' : '已思考'}</span>
        <span className="thinking-note-metrics">{text.length} 字</span>
        <DownOutlined
          className="thinking-note-chevron"
          rotate={item.reasoningOpen ? 180 : 0}
        />
      </button>
      {item.reasoningOpen && <div className="thinking-note-body">{text}</div>}
    </div>
  )
}


function SystemNote({ item, activeSession, token }: {
  item: Message
  activeSession: string | null
  token: string
}) {
  return (
    <div className={`system-note ${item.role === 'error' ? 'system-note-error' : ''}`}>
      <div
        className="markdown"
        dangerouslySetInnerHTML={{ __html: markdownToHtml(item.content, activeSession, token) }}
      />
    </div>
  )
}


/**
 * A message the user sent during the open turn.
 *
 * Rendered as an aside inside the turn: a "你" bubble here would read as a
 * second request and push the turn's own trace below it, which is exactly the
 * layout this replaced.
 */
function InterjectionRow({ item, activeSession, token }: {
  item: Message
  activeSession: string | null
  token: string
}) {
  return (
    <div className={`message-interjection ${item.queued ? 'is-pending' : ''}`}>
      <span className="message-interjection-mark" aria-hidden="true">插话</span>
      <div className="message-interjection-body">
        <div
          className="markdown"
          dangerouslySetInnerHTML={{ __html: markdownToHtml(item.content, activeSession, token) }}
        />
      </div>
      {item.queued && <span className="message-interjection-state">待读</span>}
    </div>
  )
}


const MemoAttachmentRow = memo(AttachmentRow)
const MemoToolEvent = memo(ToolEvent)
const MemoSubAgentNote = memo(SubAgentNote)
const MemoReasoningNote = memo(ReasoningNote)
const MemoSystemNote = memo(SystemNote)
const MemoInterjectionRow = memo(InterjectionRow)


function ToolTrace({ tools, expanded, onToggle, traceId, activeSession, token }: {
  tools: Message[]
  expanded: boolean
  onToggle: (traceId: string) => void
  traceId: string
  activeSession: string | null
  token: string
}) {
  const dots = summariseToolDots(tools)
  const summarised = dots.length < tools.length
  return (
    <div className={`tool-trace ${expanded ? 'tool-trace-expanded' : ''}`}>
      <button type="button" className="tool-trace-summary" onClick={() => onToggle(traceId)}>
        <span
          className="tool-trace-dots"
          aria-hidden="true"
          title={summarised
            ? `${tools.length} 步已归并为 ${dots.length} 段显示，每段取其中最严重的一步；点击展开可看全部`
            : undefined}
        >
          {dots.map((dot, index) => (
            <span key={index} className={`tool-dot ${dot.state}`} />
          ))}
        </span>
        <span className="tool-trace-label">工具轨迹 · {tools.length} 步</span>
        <DownOutlined className="tool-trace-chevron" />
      </button>
      {expanded &&
        createPortal(
          <div className="tool-trace-mask" onClick={() => onToggle(traceId)}>
            <div className="tool-trace-modal" onClick={event => event.stopPropagation()}>
              <div className="tool-trace-modal-head">
                <span className="tool-trace-modal-title">工具轨迹 · {tools.length} 步</span>
                <button
                  type="button"
                  className="tool-trace-modal-close"
                  onClick={() => onToggle(traceId)}
                  aria-label="关闭"
                >
                  <CloseOutlined />
                </button>
              </div>
              <div className="tool-trace-modal-body">
                {tools.map(item => (
                  <MemoToolEvent
                    key={item.id}
                    item={item}
                    activeSession={activeSession}
                    token={token}
                  />
                ))}
              </div>
            </div>
          </div>,
          document.body,
        )}
    </div>
  )
}


const MemoToolTrace = memo(ToolTrace)


function MessageRow({ item, activeSession, token, turnRefs, updateMessage, copyMessage, toggleTraceExpanded, traceTools, traceId, traceExpanded, subagentNotes }: {
  item: Message
  activeSession: string | null
  token: string
  turnRefs: MutableRefObject<Record<string, HTMLDivElement | null>>
  updateMessage: (id: string, patch: Partial<Message>) => void
  copyMessage: (content: string) => void
  toggleTraceExpanded: (id: string) => void
  traceTools?: Message[]
  traceId?: string
  traceExpanded?: boolean
  subagentNotes?: Message[]
}) {
  if (item.role === 'tool') {
    return <MemoToolEvent item={item} activeSession={activeSession} token={token} />
  }

  if (item.role === 'subagent' && item.subagent) {
    return <MemoSubAgentNote item={item} updateMessage={updateMessage} />
  }

  if (item.role === 'command' || item.role === 'error') {
    return <MemoSystemNote item={item} activeSession={activeSession} token={token} />
  }

  if (item.role === 'user' && isInTurnUserMessage(item)) {
    return <MemoInterjectionRow item={item} activeSession={activeSession} token={token} />
  }

  const isUser = item.role === 'user'
  // The strip belongs to the assistant row that produced it, so it is handed
  // to that row rather than pushed as a row of its own.
  const traceSummary =
    traceTools && traceTools.length > 0 && traceId !== undefined ? (
      <MemoToolTrace
        tools={traceTools}
        expanded={!!traceExpanded}
        onToggle={toggleTraceExpanded}
        traceId={traceId}
        activeSession={activeSession}
        token={token}
      />
    ) : null

  return (
    <div
      ref={isUser ? element => { turnRefs.current[item.id] = element } : undefined}
      className={`message-row ${isUser ? 'message-row-user' : 'message-row-assistant'}`}
    >
      {!isUser && (
        <Avatar className="message-avatar assistant" icon={<RobotOutlined />} />
      )}
      <div className="message-stack">
        <div className={`message-meta ${traceSummary ? 'message-meta-trace' : ''}`}>
          <span className="message-author">{isUser ? '你' : 'Simple Agent'}</span>
          {/* When the record says this row was written. A plain ``title`` rather
              than an antd Tooltip: one per message, and a tooltip component per
              row is a cost paid by every conversation that has ever been
              opened, to say what the browser can already say on hover. */}
          {item.createdAt && (
            <span className="message-time" title={formatDateTime(item.createdAt, true)}>
              {relativeTime(item.createdAt)}
            </span>
          )}
          {item.queued && <span className="message-queued">排队中</span>}
          {item.streaming && <span className="message-streaming">正在生成</span>}
          {traceSummary}
        </div>
        {!isUser && <MemoReasoningNote item={item} updateMessage={updateMessage} />}
        {!!subagentNotes?.length && (
          <div className="message-subagents">
            {subagentNotes.map(note => (
              <MemoSubAgentNote key={note.id} item={note} updateMessage={updateMessage} />
            ))}
          </div>
        )}
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


const MemoMessageRow = memo(MessageRow)


/**
 * One turn: the request that opened it, the rows it produced, the tool calls
 * it made and the sub-agent notes it raised.
 *
 * Plain data rather than elements. Building elements here would mean every row
 * is a fresh object on every render, which is exactly what ``memo`` compares --
 * so the grouping is the thing that has to be memoised, and its output has to
 * be data that stays identical when the transcript has not changed.
 */
interface Turn {
  user: Message | null
  items: Message[]
  tools: Message[]
  notes: Message[]
}


/**
 * Split the transcript into turns.
 *
 * A turn is a user message plus everything up to the next one. A message the
 * user sent *while a turn was running* is not a boundary: it belongs to the
 * turn already open. Flushing on one split the running turn in half -- its
 * tool trace was orphaned to the bottom of the new group, and the answer to the
 * interjection ended up rendered above the interjection itself. Two sources,
 * one rule: the local `queued`` flag while the turn is live, and the server's
 * `interjection` flag once the record has it.
 */
function groupIntoTurns(messages: Message[]): Turn[] {
  const turns: Turn[] = []
  let current: Turn = { user: null, items: [], tools: [], notes: [] }
  for (const item of messages) {
    if (item.role === 'user' && !isInTurnUserMessage(item)) {
      if (current.user || current.items.length || current.tools.length || current.notes.length) {
        turns.push(current)
      }
      current = { user: item, items: [], tools: [], notes: [] }
    } else if (item.role === 'tool' && item.tool !== 'attachment' && !item.link) {
      // Attachments (image/audio/video/file) are real inline content and must
      // appear in the chat stream, not hidden inside the collapsible tool
      // trace overlay. Regular tool-trace rows carry no ``link``.
      current.tools.push(item)
    } else if (item.role === 'subagent' && item.subagent) {
      current.notes.push(item)
    } else {
      current.items.push(item)
    }
  }
  if (current.user || current.items.length || current.tools.length || current.notes.length) {
    turns.push(current)
  }
  return turns
}


export function MessageList(props: MessageListProps) {
  const {
    messages,
    activeSession,
    token,
    expandedTraces,
    turnRefs,
    updateMessage,
    copyMessage,
    toggleTraceExpanded,
  } = props

  // Grouped once per transcript, not once per render. This is what makes the
  // rows skippable: while a reply streams, the transcript's shape does not
  // change -- only one row's content does -- so every other row keeps the props
  // it already had and `memo` can skip it. Regrouping inside the render body
  // instead hands every row a fresh array each time and quietly undoes that.
  const turns = useMemo(() => groupIntoTurns(messages), [messages])

  const nodes: React.ReactNode[] = []
  for (const turn of turns) {
    const { user, items, tools, notes } = turn
    if (user) {
      nodes.push(
        <MemoMessageRow
          key={user.id}
          item={user}
          activeSession={activeSession}
          token={token}
          turnRefs={turnRefs}
          updateMessage={updateMessage}
          copyMessage={copyMessage}
          toggleTraceExpanded={toggleTraceExpanded}
        />,
      )
    }
    let traceAttached = false
    let notesAttached = false
    const traceId = tools[0]?.id
    for (const item of items) {
      const shouldAttachTrace =
        !traceAttached && item.role === 'assistant' && tools.length > 0
      nodes.push(
        <MemoMessageRow
          key={item.id}
          item={item}
          activeSession={activeSession}
          token={token}
          turnRefs={turnRefs}
          updateMessage={updateMessage}
          copyMessage={copyMessage}
          toggleTraceExpanded={toggleTraceExpanded}
          // The tool trace and the sub-agent strips belong to the assistant row
          // that produced them, so they are handed to it rather than pushed as
          // rows of their own.
          traceTools={shouldAttachTrace ? tools : undefined}
          traceId={shouldAttachTrace ? traceId : undefined}
          traceExpanded={traceId ? !!expandedTraces[traceId] : undefined}
          // The trace's id, not a closure over it: a fresh arrow per render
          // would be a new prop on every row every render, which is the one
          // thing memoisation cannot see through.
          subagentNotes={
            !notesAttached && item.role === 'assistant' && notes.length > 0
              ? notes
              : undefined
          }
        />,
      )
      if (shouldAttachTrace) traceAttached = true
      if (!notesAttached && item.role === 'assistant' && notes.length > 0) {
        notesAttached = true
      }
    }
    if (tools.length && !traceAttached && traceId) {
      nodes.push(
        <MemoToolTrace
          key={`trace-${traceId}`}
          tools={tools}
          expanded={!!expandedTraces[traceId]}
          onToggle={toggleTraceExpanded}
          traceId={traceId}
          activeSession={activeSession}
          token={token}
        />,
      )
    }
    // A transcript can arrive without its assistant row (another tab's turn, or
    // a restored session). The strip is the only record that a batch ran, so it
    // must not disappear with the row it would have attached to.
    if (!notesAttached) {
      for (const note of notes) {
        nodes.push(
          <MemoSubAgentNote key={note.id} item={note} updateMessage={updateMessage} />,
        )
      }
    }
  }

  return <>{nodes}</>
}