/** The chat view. Rendered by the App container from its context object. */

import type { AppCtx } from '../app/AppCtx'
import { CONFIRM_RISK_LABELS, TASK_INTERRUPTED_STATUSES, TASK_STATUS_LABELS } from '../constants'
import { compactWorkspacePath, truncate } from '../lib/format'
import { markdownToHtml } from '../lib/markdown'
import { fileHref, mediaKindForUrl } from '../lib/media'
import { subagentCounts } from '../lib/subagent'
import { confirmRisk, confirmToolLabel, summariseToolDots, toolStateLabel } from '../lib/tools'
import type { Message, SubAgentNote } from '../types'
import {
  ApiOutlined,
  ArrowUpOutlined,
  CheckCircleFilled,
  CloseOutlined,
  CodeOutlined,
  CopyOutlined,
  DownOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  PaperClipOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  StopOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { Avatar, Button, Dropdown, Input, Select, Tooltip } from 'antd'
import React from 'react'
import { createPortal } from 'react-dom'

const { TextArea } = Input

export function renderChat(ctx: AppCtx) {
  const { activateTurnIndex, activeSession, activity, approvalCommandRef, chatScrollRef, commandIndex, commandItemRefs, composerSendable, confirmDetailOpen, confirmOverflowing, confirmRemaining, confirmReq, continueTask, conversationGap, conversationMarkerRefs, conversationRailRef, conversationTurns, copyMessage, creatingSession, currentModel, dismissTaskGuidance, expandedTraces, fileInputRef, filteredCommands, handleChatScroll, handleComposerKeyDown, handleFilesSelected, handleModelChange, handleRailMouseMove, hoveredTurn, hoveredTurnIndex, inlineCommandEmpty, inlineCommandOpen, input, interrupting, isStreaming, keepTurnSummary, messages, modelOptions, modelSelectPlaceholder, modelSelectWidth, pendingAttachments, permissionLabel, permissionLevel, pickWorkspace, queueView, resolvedComposerText, resumingTaskId, sandboxMode, scheduleHideTurnSummary, sendConfirm, sendMessage, sendShortcutLabel, sessionState, setCommandDismissed, setCommandIndex, setCommandIndexPinned, setConfirmDetailOpen, setInput, setPendingAttachments, stopStreaming, toggleTraceExpanded, token, turnRefs, updateMessage, updateSessionPermissions, withdrawQueuedMessages } = ctx


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

  /**
   * One batch's progress strip.
   *
   * Rendered inside the assistant row it belongs to rather than as a row of its
   * own: a full-width block landing after the reply reads as a second message
   * from the agent, which is what it looked like when it was a sibling row.
   */
  const renderSubAgentNote = (item: Message) => {
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
  const renderReasoningNote = (item: Message) => {
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

  const renderMessage = (
    item: Message,
    traceSummary?: React.ReactNode,
    subagentNotes?: Message[],
  ) => {
    if (item.role === 'tool') return renderToolEvent(item)

    if (item.role === 'subagent' && item.subagent) return renderSubAgentNote(item)

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
          {!isUser && renderReasoningNote(item)}
          {!!subagentNotes?.length && (
            <div className="message-subagents">
              {subagentNotes.map(note => renderSubAgentNote(note))}
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
    let groupNotes: Message[] = []

    const flushTurn = () => {
      if (groupUser) nodes.push(renderMessage(groupUser))
      let traceAttached = false
      let notesAttached = false
      groupItems.forEach(item => {
        const shouldAttachTrace =
          !traceAttached && item.role === 'assistant' && groupTools.length > 0
        // The strip belongs to the assistant row that produced it, so it is
        // handed to that row rather than pushed as a row of its own.
        const notes =
          !notesAttached && item.role === 'assistant' ? groupNotes : undefined
        if (notes?.length) notesAttached = true
        if (shouldAttachTrace) {
          nodes.push(renderMessage(item, renderToolTraceContent(groupTools), notes))
          traceAttached = true
        } else {
          nodes.push(renderMessage(item, undefined, notes))
        }
      })
      if (groupTools.length && !traceAttached) {
        nodes.push(renderToolTrace(groupTools))
      }
      // A transcript can arrive without its assistant row (another tab's turn,
      // or a restored session). The strip is the only record that a batch ran,
      // so it must not disappear with the row it would have attached to.
      if (!notesAttached) groupNotes.forEach(note => nodes.push(renderSubAgentNote(note)))
      groupUser = null
      groupItems = []
      groupTools = []
      groupNotes = []
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
      } else if (item.role === 'subagent' && item.subagent) {
        groupNotes.push(item)
      } else {
        groupItems.push(item)
      }
    })
    flushTurn()
    return nodes
  }

  return (
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
        {queueView.length > 0 && (
          <div className="message-queue-banner">
            <div className="message-queue-head">
              <span className="message-queue-dot" />
              <span>已排队 {queueView.length} 条消息，将按顺序处理</span>
              {queueView.some(item => item.withdrawable) && (
                <button
                  type="button"
                  onClick={() => {
                    void withdrawQueuedMessages(
                      queueView.filter(item => item.withdrawable).map(item => item.id),
                    )
                  }}
                >
                  全部撤回
                </button>
              )}
            </div>
            <ul className="message-queue-list">
              {queueView.map((item, index) => (
                <li key={item.id || `queued-${index}`}>
                  <span className="message-queue-text">
                    {truncate(item.text, 60) || '（无文字内容）'}
                  </span>
                  {item.withdrawable ? (
                    <button
                      type="button"
                      onClick={() => { void withdrawQueuedMessages([item.id]) }}
                    >
                      撤回
                    </button>
                  ) : (
                    <span className="message-queue-pending">发送中…</span>
                  )}
                </li>
              ))}
            </ul>
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
}
