/** The chat view. Rendered by the App container from its context object. */

import type { AppCtx } from '../app/AppCtx'
import { ApprovalBar } from '../components/ApprovalBar'
import { MessageList } from '../components/MessageList'
import { TASK_STATUS_LABELS } from '../constants'
import { compactWorkspacePath, truncate } from '../lib/format'
import { fileHref } from '../lib/media'
import {
  ApiOutlined,
  ArrowDownOutlined,
  ArrowUpOutlined,
  CheckCircleFilled,
  ClockCircleOutlined,
  CodeOutlined,
  CloseOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  PaperClipOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  StopOutlined,
} from '@ant-design/icons'
import { Avatar, Button, Dropdown, Input, Select, Tooltip } from 'antd'
import React from 'react'

const { TextArea } = Input

export function renderChat(ctx: AppCtx) {
  const { activateTurnIndex, activeSession, activity, attachApprovalCommand, awayFromLatest, chatScrollRef, commandIndex, commandItemRefs, composerRef, composerSendable, confirmDetailOpen, confirmOverflowing, confirmRemaining, confirmReq, continueTask, conversationGap, conversationMarkerRefs, conversationRailRef, conversationTurns, copyMessage, creatingSession, currentModel, dismissConfirm, dismissTaskGuidance, expandedTraces, fileDragActive, fileInputRef, filteredCommands, focusComposer, handleChatDragLeave, handleChatDragOver, handleChatDrop, handleChatScroll, handleComposerKeyDown, handleComposerPaste, handleFilesSelected, handleModelChange, handleRailMouseMove, hasMoreHistory, hoveredTurn, hoveredTurnIndex, inlineCommandEmpty, inlineCommandOpen, input, interrupting, isStreaming, jumpToLatest, keepTurnSummary, loadEarlierMessages, loadingHistory, messages, modelOptions, modelSelectPlaceholder, modelSelectWidth, pendingAttachments, permissionLabel, permissionLevel, pickWorkspace, queueView, resolvedComposerText, sandboxMode, scheduleHideTurnSummary, sendConfirm, sendMessage, sendShortcutLabel, sessionState, setCommandDismissed, setCommandIndex, setCommandIndexPinned, setConfirmDetailOpen, setInput, setPendingAttachments, stopStreaming, taskGuidanceVisible, toggleTraceExpanded, token, turnRefs, updateMessage, updateSessionPermissions, withdrawQueuedMessages } = ctx


  return (
    <div
      className={`chat-view ${fileDragActive ? 'file-drag-active' : ''}`}
      onDragOver={handleChatDragOver}
      onDragLeave={handleChatDragLeave}
      onDrop={handleChatDrop}
    >
      {fileDragActive && (
        <div className="chat-drop-hint" aria-hidden="true">
          <PaperClipOutlined />
          <span>松开即可添加为附件</span>
        </div>
      )}
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
          {/* Reaching the beginning of a long conversation is a click, not an
              assumption: the server pages by sequence, so what is behind this
              window is reachable rather than simply absent. */}
          {hasMoreHistory && (
            <div className="chat-history-earlier">
              <Button
                size="small"
                icon={<ClockCircleOutlined rotate={180} />}
                loading={loadingHistory}
                onClick={() => { void loadEarlierMessages() }}
              >
                加载更早的消息
              </Button>
            </div>
          )}
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
                      setInput(item)
                      // Filled but not focused, the suggestion left the
                      // reader to go and click the box it had just typed in.
                      focusComposer()
                    }}
                  >
                    {item}
                  </Button>
                ))}
              </div>
            </div>
          ) : (
            <>
              <MessageList
                messages={messages}
                activeSession={activeSession}
                token={token}
                expandedTraces={expandedTraces}
                turnRefs={turnRefs}
                updateMessage={updateMessage}
                copyMessage={copyMessage}
                toggleTraceExpanded={toggleTraceExpanded}
              />
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
      {awayFromLatest && messages.length > 0 && (
        <button type="button" className="chat-jump-latest" onClick={jumpToLatest}>
          <ArrowDownOutlined />
          {isStreaming ? '回到正在生成的回复' : '回到最新'}
        </button>
      )}
      </div>

      <div className="composer-wrap">
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
        {taskGuidanceVisible && sessionState?.task?.active_goal && (
          // The rule itself -- recorded status, the server's snapshot, the
          // frames narrated since, this prompt already answered -- lives in the
          // hook, next to the state it reads. Rendering it here as a second
          // condition is how the card came to offer 继续任务 over a session the
          // agent was still working in.
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
        {/* While an approval is pending, its bar covers the composer instead
            of stacking above it: the turn is waiting on a decision, not on
            the next message, so the input reads as unavailable rather than
            offering a box that is not the thing to answer.

            The cover is a sibling of the composer, never a child of it, and
            that is load-bearing: with the bar inside, taking the covered
            composer out of the tab order and the pointer path also reached
            the bar's own buttons, and 允许本次 stopped being clickable (a
            real page probe caught it; the unit tests and the type checker
            cannot see that). See .composer-stage in 04-composer.css. */}
        <div className="composer-stage">
          <div className={`composer${confirmReq ? ' approval-pending' : ''}`}>
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
              ref={composerRef}
              value={input}
              onChange={event => {
                setInput(event.target.value)
                // Any edit re-opens the popover if it was dismissed with Esc.
                setCommandDismissed(false)
              }}
              onKeyDown={handleComposerKeyDown}
              onPaste={handleComposerPaste}
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
          {confirmReq && (
            <div className="approval-cover">
              <ApprovalBar
                confirmReq={confirmReq}
                remaining={confirmRemaining}
                detailOpen={confirmDetailOpen}
                overflowing={confirmOverflowing}
                commandRef={attachApprovalCommand}
                onToggleDetail={() => setConfirmDetailOpen(open => !open)}
                onDecide={sendConfirm}
                onDismiss={dismissConfirm}
              />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
