import { ArrowLeftOutlined, BranchesOutlined, CodeOutlined, DeleteOutlined, EditOutlined, FolderOpenOutlined, MoonOutlined, MoreOutlined, PlusOutlined, RobotOutlined, SafetyCertificateOutlined, SearchOutlined, SettingOutlined, SunOutlined } from '@ant-design/icons'
import type { MenuProps } from 'antd'
import { Badge, Button, ConfigProvider, Drawer, Dropdown, Empty, Input, Layout, Modal, Skeleton, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import dayjs from 'dayjs'
import React from 'react'
import 'dayjs/locale/zh-cn'
import './index.css'
import type { AppCtx } from './app/AppCtx'
import { renderHome } from './views/home'
import { renderBoardPanel } from './views/board'
import { renderExtensions } from './views/extensions'
import { renderMemory } from './views/memory'
import { renderAutonomy } from './views/autonomy'
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
import { useMemory } from './hooks/useMemory'
import { useSessionUsage } from './hooks/useSessionUsage'
import { useSettings } from './hooks/useSettings'
import { useAutomation } from './hooks/useAutomation'
import { useShell } from './hooks/useShell'

const { Content, Header } = Layout
dayjs.locale('zh-cn')

//: How many hits the search modal shows before the "还有 N 条" line. The
//: list scrolls, so this is a "don't render a wall" cap rather than a
//: screen-space one -- but the line has to say it, or a session past the cut
//: looks like it does not exist.
const SEARCH_RESULT_LIMIT = 12

/** The query's first hit inside `text`, wrapped in a `<mark>`.
 *
 * Search matches on substring (title or session id), so a hit is a run of
 * characters the user just typed -- marking it is what lets them scan a list
 * of similar titles for the one they meant. Only the first hit is marked:
 * the rest are found by eye from there.
 */
function highlightQuery(text: string, query: string): React.ReactNode {
  const needle = query.trim().toLowerCase()
  if (!needle) return text
  const at = text.toLowerCase().indexOf(needle)
  if (at < 0) return text
  return (
    <>
      {text.slice(0, at)}
      <mark>{text.slice(at, at + needle.length)}</mark>
      {text.slice(at + needle.length)}
    </>
  )
}

function App() {
  const ui = useUi()

  const apiClient = useApiClient()

  const confirm = useConfirm({ ...apiClient })

  const conversations = useConversations({ ...ui, ...apiClient, ...confirm })

  const extensions = useExtensions({ ...ui, ...apiClient, ...confirm })

  // The memory page needs the api client and nothing else: it reads the
  // corpus, not a session.
  const memory = useMemory({ ...ui, ...apiClient })

  // Spend, for the chip beside the composer: it follows the active session and
  // refreshes when a turn ends, which is when the number changed.
  const usage = useSessionUsage({ ...ui, ...apiClient, ...conversations })

  const settings = useSettings({ ...ui, ...apiClient, ...conversations })

  const automation = useAutomation({ ...ui, ...apiClient, ...confirm, ...conversations, ...settings })

  const shell = useShell({ ...ui, ...conversations, ...extensions, ...settings, ...automation })

  const { boardOpen, clock, commandIndex, commandItemRefs, commandPaletteOpen, loadingView, paletteQuery, searchOpen, setBoardOpen, setCommandDismissed, setCommandIndex, setCommandIndexPinned, setCommandPaletteOpen, setPaletteQuery, setSearchOpen, setThemeMode, setThreadsOpen, themeMode, threadsOpen, view } = ui

  const { contextHolder, messageApi, token } = apiClient

  const { attachApprovalCommand, confirmDetailOpen, confirmOverflowing, confirmRemaining, confirmReq, confirmSession, dismissConfirm, sendConfirm, setConfirmDetailOpen } = confirm

  const { activateTurnIndex, activeSession, startNewSession, activity, awayFromLatest, canRewind, chatScrollRef, editLastTurn, regenerateLastTurn, composerRef, composerSendable, continueTask, conversationGap, conversationMarkerRefs, conversationRailRef, conversationTurns, copyMessage, createSession, creatingSession, currentModel, deleteSession, dismissTaskGuidance, expandedTraces, fileDragActive, fileInputRef, filteredCommands, filteredSessions, flushComposerFocus, focusComposer, handleChatDragLeave, handleChatDragOver, handleChatDrop, handleChatScroll, handleComposerKeyDown, handleComposerPaste, handleFilesSelected, handleRailMouseMove, handleSessionContainerClick, hasMoreHistory, hoveredTurn, hoveredTurnIndex, inlineCommandEmpty, inlineCommandOpen, input, interrupting, isStreaming, jumpToLatest, keepTurnSummary, loadEarlierMessages, loadingHistory, loadingSessions, messages, pendingAttachments, pendingDeleteSessionId, permissionLabel, permissionLevel, pickWorkspace, queueView, renamingSession, commitSessionTitle, setRenamingSession, resolvedComposerText, resumingTaskId, revealSession, sandboxMode, scheduleHideTurnSummary, selectSession, sendMessage, sendShortcut, sendShortcutLabel, sessionSearch, sessionState, sessions, setInput, setPendingAttachments, setPendingDeleteSessionId, setSendShortcut, setSessionSearch, stopStreaming, taskGuidanceVisible, toggleTraceExpanded, turnRefs, updateMessage, updateSessionPermissions, withdrawQueuedMessages } = conversations

  const { deletePlugin, deleteSkill, extensionsTab, filteredPlugins, filteredSkills, pluginSearch, setExtensionsTab, setPluginSearch, setSkillFilter, setSkillSearch, skillFilter, skillSearch, skills, togglePlugin, toggleSkill } = extensions

  const { activateProvider, applyToken, config, configText, deleteProvider, feishuChats, feishuChatsError, feishuChatsLoaded, feishuChatsLoading, feishuTesting, form, handleModelChange, handleSettingsFormChange, jsonStatus, loadFeishuChats, modelOptions, modelSelectPlaceholder, modelSelectWidth, pickDirectory, pickingDirectory, providerBusy, providerFields, resetSettings, saveProvider, saveSettings, sendFeishuTest, testProvider, setConfigText, setSettingsDirty, setTokenDraft, settingsDirty, settingsModelOptions, thinkingEffortOptions, tokenDirty, tokenDraft } = settings

  const { acknowledgeScheduleRun, attentionByTask, attentionRuns, automationKind, automationQuery, automations, automationsLoaded, automationTab, proposals, proposalsLoaded, proposalCounts, loadAutonomy, decideProposal, answeredProposals, funnel, home, homeLoaded, loadHome, activityCursor, activityExhausted, activityLoading, loadMoreActivity, olderActivity, bulkScheduleAction, cancelScheduleRun, clearScheduleAttention, deleteSchedule, deleteWorkflow, editingWorkflow, editingWorkflowId, filteredSchedules, filteredWorkflows, loadAutomations, loadScheduleRuns, loadSchedules, loadWorkflows, openScheduleDetails, permissionProfileLabel, retryScheduleRun, runScheduleNow, runWorkflowNow, scheduleArtifacts, scheduleDetailOpen, scheduleOutputLoading, scheduleQuery, scheduleRunOutput, scheduleRuns, scheduleRunsLoading, scheduleStatusFilter, schedulerHealth, schedulerRefreshedAt, schedulerStale, schedules, selectedSchedule, selectedScheduleIds, selectedScheduleRun, selectedScheduleRunId, selectedScheduleRunTask, setAutomationKind, setAutomationQuery, setAutomationTab, setEditingWorkflowId, setScheduleDetailOpen, setScheduleQuery, setScheduleStatusFilter, setSelectedScheduleIds, setSelectedScheduleRunId, setWorkflowQuery, startingWorkflowIds, toggleSchedule, toggleWorkflow, unseenFailures, workflowQuery, workflows } = automation

  const { handlePaletteKeyDown, navItems, navigateTo, pageMeta, paletteCommands, paletteEntries, paletteIndex, runPaletteEntry, setPaletteIndex } = shell

  const paletteInputRef = React.useRef<HTMLInputElement | null>(null)

  //: The search modal's keyboard cursor, on the same plan as the command
  //: palette's: the input keeps focus, arrows move a highlight, Enter opens
  //: the highlighted row. Reset whenever the query (or the modal itself)
  //: changes what the list is, so the highlight never points past the end.
  const [searchIndex, setSearchIndex] = React.useState(0)
  const searchResults = filteredSessions.slice(0, SEARCH_RESULT_LIMIT)
  const activeSearchIndex = Math.min(searchIndex, Math.max(searchResults.length - 1, 0))
  React.useEffect(() => {
    setSearchIndex(0)
  }, [sessionSearch, searchOpen])

  const openSearchResult = (sid: string) => {
    selectSession(sid)
    setSearchOpen(false)
    setSessionSearch('')
  }

  //: Same contract as the command palette's key handler: arrows cycle with
  //: wrap-around (a short list should not dead-end at either end), Enter runs
  //: the highlighted row. Escape needs no case here -- rc-dialog hears it on
  //: the document and closes the modal itself.
  const handleSearchKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (!searchResults.length) return
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setSearchIndex(index => (index + 1) % searchResults.length)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setSearchIndex(index => (index - 1 + searchResults.length) % searchResults.length)
    } else if (event.key === 'Enter') {
      event.preventDefault()
      openSearchResult(searchResults[activeSearchIndex].session_id)
    }
  }

  // A prompt belongs to the conversation that raised it: the decision goes out
  // on that session's socket, so showing it over another chat (or over 设置)
  // asks the reader to answer for a conversation they are not looking at.
  // ``confirmSession`` is recorded when the request arrives; a request with no
  // session recorded (an older backend) keeps the old behaviour.
  const activeConfirm =
    confirmReq && (!confirmSession || confirmSession === activeSession)
      ? confirmReq
      : null
  const approvalSessionTitle =
    (sessions.find(item => item.session_id === activeSession)?.title || '').trim()
    || '未命名会话'

  // Views are pure renderers built from this one context object.
  const ctx: AppCtx = {
    ...memory,
    ...usage,
    navigateTo,
    selectSession,
    updateMessage,
    activeSession,
    canRewind,
    editLastTurn,
    regenerateLastTurn,
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
    hasMoreHistory,
    loadingHistory,
    loadEarlierMessages,
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
    confirmReq: activeConfirm,
    confirmRemaining,
    confirmOverflowing,
    confirmDetailOpen,
    attachApprovalCommand,
    setConfirmDetailOpen,
    sendConfirm,
    dismissConfirm,
    queueView,
    withdrawQueuedMessages,
    sessionState,
    resumingTaskId,
    taskGuidanceVisible,
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
    startNewSession,
    sendMessage,
    resolvedComposerText,
    pageMeta,
    sessions,
    filteredSessions,
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
    editingWorkflowId,
    startingWorkflowIds,
    setEditingWorkflowId,
    editingWorkflow,
    pickingDirectory,
    pickDirectory,
    workflows,
    filteredWorkflows,
    toggleWorkflow,
    schedulerHealth,
    runWorkflowNow,
    deleteWorkflow,
    schedulerRefreshedAt,
    schedulerStale,
    automationTab,
    loadSchedules,
    loadWorkflows,
    unseenFailures,
    clearScheduleAttention,
    setAutomationTab,
    automationKind,
    automationQuery,
    setAutomationKind,
    setAutomationQuery,
    automations,
    automationsLoaded,
    answeredProposals,
    funnel,
    boardOpen,
    setBoardOpen,
    activityCursor,
    activityExhausted,
    activityLoading,
    loadMoreActivity,
    olderActivity,
    home,
    homeLoaded,
    loadHome,
    proposals,
    proposalsLoaded,
    proposalCounts,
    loadAutonomy,
    decideProposal,
    loadAutomations,
    scheduleQuery,
    setScheduleQuery,
    workflowQuery,
    setWorkflowQuery,
    scheduleStatusFilter,
    setScheduleStatusFilter,
    selectedScheduleIds,
    bulkScheduleAction,
    feishuChatsLoading,
    feishuChats,
    feishuTesting,
    sendFeishuTest,
    feishuChatsError,
    loadFeishuChats,
    feishuChatsLoaded,
    scheduleDetailOpen,
    selectedSchedule,
    setScheduleDetailOpen,
    runScheduleNow,
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

  /** How many things the board's first group is holding, for the badge.
   *
   * Read from the same `home` payload the panel renders: a badge counting its own
   * way is the two-numbers-disagree bug that already cost a round. */
  const boardWaiting = Number((home as { board?: { waiting?: unknown[] } })?.board?.waiting?.length || 0)

  const renderCurrentView = () => {
    if (view === 'home') return renderHome(ctx)
    if (view === 'extensions') return renderExtensions(ctx)
    if (view === 'memory') return renderMemory(ctx)
    if (view === 'schedules') return renderSchedules(ctx)
    if (view === 'autonomy') return renderAutonomy(ctx)
    if (view === 'settings') return renderSettings()
    return renderHome(ctx)
  }

  // The thread name is the screen's title when one is open -- it is the only
  // place that answers "which conversation is this", now that no column does.
  const currentMeta = pageMeta[view] || pageMeta.home

  const topbarMenu: MenuProps['items'] = [
    { key: 'threads', icon: <BranchesOutlined />, label: '线程' },
    { key: 'search', icon: <SearchOutlined />, label: '搜索线程' },
    { type: 'divider' },
    ...navItems.filter(item => item.key !== 'home'),
    { type: 'divider' },
    { key: 'settings', icon: <SettingOutlined />, label: '设置' },
    {
      key: 'theme',
      icon: themeMode === 'dark' ? <SunOutlined /> : <MoonOutlined />,
      label: themeMode === 'dark' ? '切到浅色模式' : '切到深色模式',
    },
  ]

  const handleTopbarMenu: MenuProps['onClick'] = ({ key }) => {
    if (key === 'threads') {
      setThreadsOpen(true)
      return
    }
    if (key === 'search') {
      setSearchOpen(true)
      return
    }
    if (key === 'theme') {
      const next = themeMode === 'dark' ? 'light' : 'dark'
      setThemeMode(next)
      localStorage.setItem('agent_theme', next)
      return
    }
    // `navigateTo` keeps the unsaved-edits guard, so a menu entry refuses to lose
    // work exactly as the navigation item it replaces did. 设置 included: it is a
    // destination like any other now that there is no footer to hide it in.
    navigateTo(key)
  }

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
      <Layout className="app-shell app-shell-flat">
        {/* One bar, not a column. The app's day-to-day surface is one screen --
            the agent -- so navigation is the exception: four management pages in
            a menu, and threads in a drawer that opens when you want another one.
            A 260px sidebar charged every screen for six entries, five of which
            are opened once a week. */}
        <Header className="app-topbar">
          <div className="topbar-left">
            {view !== 'home' && (
              <Button
                type="text"
                className="topbar-back"
                icon={<ArrowLeftOutlined />}
                aria-label="返回"
                onClick={() => navigateTo('home')}
              />
            )}
            <button
              type="button"
              className="topbar-brand"
              aria-label="回到主页"
              onClick={() => navigateTo('home')}
            >
              <RobotOutlined />
            </button>
            <div className="topbar-title">
              <div className="page-title">{currentMeta.title}</div>
              <div className="page-subtitle">{currentMeta.subtitle}</div>
            </div>
          </div>
          <div className="header-right">
            {/* The board lives behind this button on every screen: it is a *state*
                panel rather than one page's content, and somebody watching a task
                run should not have to lose where they were to look at it. The
                badge is the only thing here that speaks up -- a count over zero. */}
            <Badge count={boardWaiting} size="small" offset={[-2, 4]}>
              <Button
                type="text"
                className="header-board"
                aria-label={boardOpen ? '收起看板' : '打开看板'}
                aria-expanded={boardOpen}
                onClick={() => setBoardOpen(!boardOpen)}
              >
                看板
              </Button>
            </Badge>
            <Dropdown menu={{ items: topbarMenu, onClick: handleTopbarMenu }} placement="bottomRight" trigger={['click']}>
              <Button type="text" className="topbar-more" icon={<MoreOutlined />} aria-label="更多" />
            </Dropdown>
          </div>
        </Header>
        <Content className="app-content">{renderCurrentView()}</Content>
      </Layout>

      {/* Threads: a drawer, not a column. A thread is a row you click when you
          want to go back to one -- the same shape as the board on the other
          edge, and for the same reason. */}
      <Drawer
        placement="left"
        width={300}
        open={threadsOpen}
        onClose={() => setThreadsOpen(false)}
        rootClassName="threads-drawer"
        title="线程"
        styles={{ body: { padding: '10px 12px 16px' } }}
      >
        {/* Not "新建会话" in the old sense: a thread is something a conversation
            creates. This just clears the open thread so the next message starts
            a new one -- which is what "new" means in a product whose unit of
            conversation is the message, not the document. */}
        <Button
          block
          icon={<PlusOutlined />}
          className="threads-new"
          onClick={() => {
            selectSession('')
            setThreadsOpen(false)
          }}
        >
          新对话
        </Button>
      <div className="session-panel">
        <div className="session-panel-label">
          线程
          {/* No "全部 N" link. It opened a page whose only unique job was
              bulk delete, and the row menu already renames and deletes; a
              screen that lists the same threads as this list, one click
              deeper, was the page saying nothing twice. */}
        </div>
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
                      {/* When it was last touched, and whether it is busy.
                          The turn count is gone: "2 轮 · 6 小时前" on every row
                          was the busiest text in the sidebar and answered a
                          question nobody asks while choosing what to read. */}
                      {sessionStatusOf(item) && (
                        <span
                          className={`session-status-badge ${
                            item.status === 'queued' ? 'is-queued' : 'is-running'
                          }`}
                        >
                          {sessionStatusOf(item)}
                        </span>
                      )}
                      {relativeTime(item.last_activity)}
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
      </Drawer>

      {/* No mask: the panel is a control you keep open while working, not a modal
          that stops the page until answered. Closing is the same button that
          opened it. */}
      <Drawer
        placement="right"
        width={380}
        open={boardOpen}
        onClose={() => setBoardOpen(false)}
        mask={false}
        rootClassName="board-drawer"
        title="任务看板"
        styles={{ body: { padding: '8px 12px 16px' } }}
      >
        {renderBoardPanel(ctx)}
      </Drawer>

      {/* Not the bar itself: an approval is answerable only in the conversation
          that raised it, and a full-width alertdialog over 设置 or 任务 takes the
          page away to ask about a chat the reader is not looking at.  A compact
          notice says which conversation is waiting and offers the way back --
          the deadline keeps running, so saying nothing would be the silent
          auto-deny this replaced. */}
      {view !== 'home' && activeConfirm && confirmRemaining > 0 && (
        <div className="approval-notice" role="status" aria-live="polite">
          <SafetyCertificateOutlined className="approval-notice-icon" />
          <span>
            会话「{approvalSessionTitle}」有一个操作等待你确认
          </span>
          <span className="approval-notice-timer">{confirmRemaining}s</span>
          <Button size="small" type="primary" onClick={() => navigateTo('home')}>
            回到对话
          </Button>
        </div>
      )}

      {/* The search modal. Structure mirrors the command palette: the input
          is the modal's fixed head, only the result list scrolls. It used to
          sit inside the scrolling body, so a long list scrolled the query box
          out of sight -- the one control the user still needed. */}
      <Modal
        open={searchOpen}
        className="global-search-modal"
        width={680}
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
            onKeyDown={handleSearchKeyDown}
            allowClear
            aria-controls="global-search-results"
          />

          <div className="global-search-section-title">会话</div>
          <div
            className="global-search-results"
            id="global-search-results"
            role="listbox"
            aria-label="会话列表"
          >
            {searchResults.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的会话" />
            ) : (
              searchResults.map((item, index) => (
                <button
                  type="button"
                  className={`global-search-item ${item.session_id === activeSession ? 'active' : ''} ${
                    index === activeSearchIndex ? 'cursor' : ''
                  }`}
                  key={item.session_id}
                  role="option"
                  aria-selected={index === activeSearchIndex}
                  ref={element => {
                    if (element && index === activeSearchIndex) {
                      element.scrollIntoView({ block: 'nearest' })
                    }
                  }}
                  onMouseMove={() => { if (index !== activeSearchIndex) setSearchIndex(index) }}
                  onClick={() => openSearchResult(item.session_id)}
                >
                  <span className={`session-status-dot ${item.live ? 'live' : ''}`} />
                  <span className="global-search-item-main">
                    <strong>{highlightQuery(item.title || '未命名会话', sessionSearch)}</strong>
                    <small>
                      {item.turn_count || 0} 轮 · {relativeTime(item.last_activity)} ·{' '}
                      {highlightQuery(item.session_id.slice(0, 12), sessionSearch)}
                    </small>
                  </span>
                  {item.session_id === activeSession && <span className="current-mark">当前</span>}
                  <kbd className="global-search-enter" aria-hidden="true">↵</kbd>
                </button>
              ))
            )}
          </div>
          {filteredSessions.length > searchResults.length && (
            <div className="global-search-more">
              仅显示前 {SEARCH_RESULT_LIMIT} 条，还有 {filteredSessions.length - searchResults.length} 个匹配，换个更具体的关键词
            </div>
          )}
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
