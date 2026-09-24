import { CloseOutlined, CodeOutlined, DeleteOutlined, EditOutlined, FolderOpenOutlined, MenuOutlined, MoonOutlined, MoreOutlined, PlusOutlined, RobotOutlined, SearchOutlined, SettingOutlined, SunOutlined } from '@ant-design/icons'
import { Button, ConfigProvider, Dropdown, Empty, Input, Layout, Menu, Modal, Skeleton, theme, Tooltip } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import dayjs from 'dayjs'
import React from 'react'
import 'dayjs/locale/zh-cn'
import './index.css'
import type { AppCtx } from './app/AppCtx'
import { renderChat } from './views/chat'
import { renderSessions } from './views/sessions'
import { renderExtensions } from './views/extensions'
import { renderSchedules } from './views/automation'
import { createSettingsView } from './views/settings'
import { relativeTime } from './lib/format'
import { isSessionBusy, sessionStatusOf } from './lib/tools'
import { SessionTitle } from './components/SessionTitle'

import { useUi } from './hooks/useUi'
import { useApiClient } from './hooks/useApiClient'
import { useConfirm } from './hooks/useConfirm'
import { useConversations } from './hooks/useConversations'
import { useExtensions } from './hooks/useExtensions'
import { useSettings } from './hooks/useSettings'
import { useAutomation } from './hooks/useAutomation'
import { useShell } from './hooks/useShell'

const { Content, Header, Sider } = Layout
dayjs.locale('zh-cn')

function App() {
  const ui = useUi()

  const apiClient = useApiClient()

  const confirm = useConfirm({ ...apiClient })

  const conversations = useConversations({ ...ui, ...apiClient, ...confirm })

  const extensions = useExtensions({ ...ui, ...apiClient, ...confirm })

  const settings = useSettings({ ...ui, ...apiClient, ...conversations })

  const automation = useAutomation({ ...ui, ...apiClient, ...confirm, ...conversations, ...settings })

  const shell = useShell({ ...ui, ...conversations, ...extensions, ...settings, ...automation })

  const { clock, collapsed, commandIndex, commandItemRefs, commandPaletteOpen, loadingView, paletteQuery, searchOpen, setCollapsed, setCommandDismissed, setCommandIndex, setCommandIndexPinned, setCommandPaletteOpen, setPaletteQuery, setSearchOpen, setThemeMode, themeMode, view } = ui

  const { contextHolder, messageApi, token } = apiClient

  const { approvalCommandRef, confirmDetailOpen, confirmOverflowing, confirmRemaining, confirmReq, sendConfirm, setConfirmDetailOpen } = confirm

  const { activateTurnIndex, activeSession, activity, allFilteredSessionsSelected, awayFromLatest, chatScrollRef, composerRef, composerSendable, connected, continueTask, conversationGap, conversationMarkerRefs, conversationRailRef, conversationTurns, copyMessage, createSession, creatingSession, currentModel, deleteSelectedSessions, deleteSession, dismissTaskGuidance, expandedTraces, fileDragActive, fileInputRef, filteredCommands, filteredSessions, flushComposerFocus, focusComposer, handleChatDragLeave, handleChatDragOver, handleChatDrop, handleChatScroll, handleComposerKeyDown, handleComposerPaste, handleFilesSelected, handleRailMouseMove, handleSessionContainerClick, hoveredTurn, hoveredTurnIndex, inlineCommandEmpty, inlineCommandOpen, input, interrupting, isStreaming, jumpToLatest, keepTurnSummary, loadingSessions, messages, pendingAttachments, pendingDeleteSessionId, permissionLabel, permissionLevel, pickWorkspace, queueView, renamingSession, commitSessionTitle, setRenamingSession, resolvedComposerText, resumingTaskId, revealSession, sandboxMode, scheduleHideTurnSummary, selectSession, selectedSessionIds, sendMessage, sendShortcut, sendShortcutLabel, sessionSearch, sessionState, sessions, setInput, setPendingAttachments, setPendingDeleteSessionId, setSelectedSessionIds, setSendShortcut, setSessionSearch, stopStreaming, toggleTraceExpanded, turnRefs, updateMessage, updateSessionPermissions, withdrawQueuedMessages } = conversations

  const { deletePlugin, deleteSkill, extensionsTab, filteredPlugins, filteredSkills, pluginSearch, setExtensionsTab, setPluginSearch, setSkillFilter, setSkillSearch, skillFilter, skillSearch, skills, togglePlugin, toggleSkill } = extensions

  const { activateProvider, applyToken, config, configText, deleteProvider, feishuChats, feishuChatsError, feishuChatsLoaded, feishuChatsLoading, feishuTesting, form, handleModelChange, handleSettingsFormChange, jsonStatus, loadFeishuChats, modelOptions, modelSelectPlaceholder, modelSelectWidth, pickDirectory, pickingDirectory, providerBusy, providerFields, resetSettings, saveProvider, saveSettings, sendFeishuTest, testProvider, setConfigText, setSettingsDirty, setTokenDraft, settingsDirty, settingsModelOptions, thinkingEffortOptions, tokenDirty, tokenDraft } = settings

  const { acknowledgeScheduleRun, activePermissionProfile, addWorkflowStep, attentionByTask, attentionRuns, automationTab, bulkScheduleAction, cancelScheduleRun, changeWorkflowStepUpstreams, clearScheduleAttention, deleteSchedule, deleteWorkflow, duplicateSchedule, editingScheduleId, editingStep, editingWorkflow, editingWorkflowId, filteredSchedules, filteredWorkflows, insertWorkflowStepAfter, loadScheduleRuns, loadSchedules, loadWorkflows, moveWorkflowStep, openCreateSchedule, openCreateWorkflow, openEditSchedule, openEditWorkflow, openScheduleDetails, openStepDetails, patchWorkflowStep, permissionProfileLabel, permissionProfileOptions, recentWorkspaceRoots, removeWorkflowStep, retryScheduleRun, runScheduleNow, runWorkflowNow, saveSchedule, saveWorkflow, scheduleArtifacts, scheduleDetailOpen, scheduleDraft, scheduleModalOpen, scheduleOutputLoading, schedulePreview, schedulePreviewError, scheduleQuery, scheduleRunOutput, scheduleRuns, scheduleRunsLoading, scheduleSaving, scheduleStatusFilter, schedulerHealth, schedulerRefreshedAt, schedulerStale, schedules, selectedSchedule, selectedScheduleIds, selectedScheduleRun, selectedScheduleRunId, selectedScheduleRunTask, setAutomationTab, setEditingScheduleId, setEditingWorkflowId, setScheduleDetailOpen, setScheduleDraft, setScheduleModalOpen, setScheduleQuery, setScheduleStatusFilter, setSelectedScheduleIds, setSelectedScheduleRunId, setWorkflowDraft, setWorkflowKeyRewrite, setWorkflowModalOpen, setWorkflowQuery, signals, signalsWaiting, startingWorkflowIds, toggleSchedule, toggleWorkflow, unseenFailures, workflowAttention, workflowDraft, workflowGraph, workflowKeyRewrite, workflowModalOpen, workflowOrderDiffersFromArray, workflowQuery, workflowSaving, workflows, workflowsLoaded } = automation

  const { handlePaletteKeyDown, navItems, navigateTo, pageMeta, paletteCommands, paletteEntries, paletteIndex, runPaletteEntry, setPaletteIndex } = shell

  const paletteInputRef = React.useRef<HTMLInputElement | null>(null)

  // Views are pure renderers built from this one context object.
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
    awayFromLatest,
    jumpToLatest,
    composerRef,
    focusComposer,
    handleComposerPaste,
    fileDragActive,
    handleChatDragOver,
    handleChatDragLeave,
    handleChatDrop,
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
    createSession,
    sendMessage,
    resolvedComposerText,
    pageMeta,
    sessions,
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
    renamingSession,
    setRenamingSession,
    commitSessionTitle,
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
    startingWorkflowIds,
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
    providerFields,
    providerBusy,
    saveProvider,
    deleteProvider,
    activateProvider,
    testProvider,
    settingsModelOptions,
    thinkingEffortOptions,
    configText,
    setConfigText,
    setSettingsDirty,
  }

  const { renderSettings } = createSettingsView(ctx)

  const renderCurrentView = () => {
    if (view === 'chat') return renderChat(ctx)
    if (view === 'sessions') return renderSessions(ctx)
    if (view === 'extensions') return renderExtensions(ctx)
    if (view === 'schedules') return renderSchedules(ctx)
    if (view === 'settings') return renderSettings()
    return renderChat(ctx)
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
                // Live sessions lead the list now, so the divider that
                // matters is where they end: history gets its own label
                // once something live sits above it.
                const showHistoryHeader =
                  !item.live && index > 0 && Boolean(filteredSessions[index - 1].live)
                return (
                  <React.Fragment key={item.session_id}>
                    {showHistoryHeader && (
                      <div className="session-panel-label sub">历史会话</div>
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
                        // F2 renames, as it does for a file in Finder/Explorer.
                        if (event.key === 'F2') {
                          event.preventDefault()
                          setRenamingSession(`sider:${item.session_id}`)
                          return
                        }
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault()
                          handleSessionContainerClick(event, item.session_id)
                        }
                      }}
                    >
                      <div className="session-item-status">
                        <span className={`${item.live ? 'live' : 'durable'}${isSessionBusy(item) ? ' busy' : ''}`} />
                      </div>
                      <div className="session-item-main">
                        <div className="session-item-title">
                          <SessionTitle
                            item={item}
                            editing={renamingSession === `sider:${item.session_id}`}
                            onStartEdit={() => setRenamingSession(`sider:${item.session_id}`)}
                            onCommit={title => void commitSessionTitle(item, title)}
                            onCancel={() => setRenamingSession(null)}
                          />
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
                                onClick: () => setRenamingSession(`sider:${item.session_id}`),
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
              {/* Icon-only, at their natural size.  These two are a footer of
                  utilities rather than a page, and a word beside each icon
                  made two half-width buttons out of two 38px targets -- a
                  label a person reads once, and the footer spends the width
                  of the whole column on it.  The label is not dropped: it
                  moves into the tooltip and into the accessible name, since
                  an icon on its own is a picture, not a label. */}
              <Tooltip title={themeMode === 'dark' ? '切到浅色模式' : '切到深色模式'}>
                <Button
                  icon={themeMode === 'dark' ? <SunOutlined /> : <MoonOutlined />}
                  aria-label={themeMode === 'dark' ? '切到浅色模式' : '切到深色模式'}
                  onClick={() => {
                    const next = themeMode === 'dark' ? 'light' : 'dark'
                    setThemeMode(next)
                    localStorage.setItem('agent_theme', next)
                  }}
                />
              </Tooltip>
              {/* The settings entry, out of the main navigation by design: it
                  sits beside the theme switch -- the two things a person
                  touches once and then rarely -- rather than beside the pages
                  they visit every day.  navigateTo keeps the unsaved-edits
                  guard, so this small entry refuses to lose work exactly as
                  the menu item it replaces did. */}
              <Tooltip title="设置">
                <Button
                  icon={<SettingOutlined />}
                  aria-label="设置"
                  aria-current={view === 'settings' ? 'page' : undefined}
                  onClick={() => navigateTo('settings')}
                />
              </Tooltip>
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
        afterClose={flushComposerFocus}
        // `autoFocus` alone lost: it only fires when the input first mounts,
        // and the modal stays mounted between openings -- so from the second
        // ⌘K on, focus sat on the dialog frame and typing went nowhere.
        afterOpenChange={open => { if (open) paletteInputRef.current?.focus() }}
      >
        <div className="command-palette">
          <div className="command-palette-search">
            <SearchOutlined />
            <input
              ref={paletteInputRef}
              autoFocus
              value={paletteQuery}
              onChange={event => setPaletteQuery(event.target.value)}
              onKeyDown={handlePaletteKeyDown}
              placeholder="搜索命令、导航或输入关键词…"
              role="combobox"
              aria-expanded
              aria-controls="command-palette-list"
              aria-activedescendant={paletteEntries[paletteIndex] ? `palette-${paletteEntries[paletteIndex].key}` : undefined}
            />
            <kbd>ESC</kbd>
          </div>
          <div id="command-palette-list" role="listbox" aria-label="命令与导航">
            <div className="command-palette-section" role="presentation">命令</div>
            {paletteCommands.length === 0 && <div className="command-empty" role="presentation">没有匹配命令</div>}
            {paletteEntries.map((entry, index) => entry.kind === 'command' && (
              <button
                type="button"
                className={`command-item ${index === paletteIndex ? 'active' : ''}`}
                key={entry.key}
                id={`palette-${entry.key}`}
                role="option"
                aria-selected={index === paletteIndex}
                tabIndex={-1}
                ref={element => { if (element && index === paletteIndex) element.scrollIntoView({ block: 'nearest' }) }}
                onMouseMove={() => { if (index !== paletteIndex) setPaletteIndex(index) }}
                onClick={() => runPaletteEntry(entry)}
              >
                <CodeOutlined />
                <span className="command-item-main">
                  <strong>{entry.command.usage || `/${entry.command.name}`}</strong>
                  <small>{entry.command.description || '无描述'}</small>
                </span>
                <kbd className="command-item-enter" aria-hidden="true">↵</kbd>
              </button>
            ))}
            <div className="command-palette-section" role="presentation">导航</div>
            {/* Settings lives in the sidebar footer, but the palette is the
                keyboard's map of the app: leaving it out would make the one
                navigation surface that cannot reach it. */}
            <div className="palette-nav" role="presentation">
              {paletteEntries.map((entry, index) => entry.kind === 'nav' && (
                <button
                  type="button"
                  className={index === paletteIndex ? 'active' : ''}
                  key={entry.key}
                  id={`palette-${entry.key}`}
                  role="option"
                  aria-selected={index === paletteIndex}
                  tabIndex={-1}
                  ref={element => { if (element && index === paletteIndex) element.scrollIntoView({ block: 'nearest' }) }}
                  onMouseMove={() => { if (index !== paletteIndex) setPaletteIndex(index) }}
                  onClick={() => runPaletteEntry(entry)}
                >
                  {entry.icon}
                  {entry.label}
                </button>
              ))}
              {!paletteEntries.some(entry => entry.kind === 'nav') && <div className="command-empty">没有匹配的页面</div>}
            </div>
          </div>
        </div>
      </Modal>
    </ConfigProvider>
  )
}

export default App
