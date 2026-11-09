import { ArrowLeftOutlined, CodeOutlined, SearchOutlined, MoonOutlined, MoreOutlined, RobotOutlined, SafetyCertificateOutlined, SettingOutlined, SunOutlined } from '@ant-design/icons'
import type { MenuProps } from 'antd'
import { Badge, Button, ConfigProvider, Drawer, Dropdown, Layout, Modal, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import dayjs from 'dayjs'
import React from 'react'
import 'dayjs/locale/zh-cn'
import './index.css'
import type { AppCtx } from './app/AppCtx'
import { renderHome } from './views/home'
import { renderBoardPanel } from './views/board'
import { renderMemory } from './views/memory'
import { renderSchedules } from './views/automation'
import { createSettingsView } from './views/settings'

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

  const { boardOpen, clock, commandIndex, commandItemRefs, commandPaletteOpen, loadingView, paletteQuery, setBoardOpen, setCommandDismissed, setCommandIndex, setCommandIndexPinned, setCommandPaletteOpen, setPaletteQuery, setThemeMode, themeMode, view } = ui

  const { contextHolder, messageApi, token } = apiClient

  const { attachApprovalCommand, confirmDetailOpen, confirmOverflowing, confirmRemaining, confirmReq, confirmSession, dismissConfirm, sendConfirm, setConfirmDetailOpen } = confirm

  const { activateTurnIndex, activeSession, openConversation, activity, awayFromLatest, canRewind, chatScrollRef, editLastTurn, regenerateLastTurn, composerRef, composerSendable, continueTask, conversationGap, conversationMarkerRefs, conversationRailRef, conversationTurns, copyMessage, creatingSession, currentModel, dismissTaskGuidance, expandedTraces, fileDragActive, fileInputRef, filteredCommands, flushComposerFocus, focusComposer, handleChatDragLeave, handleChatDragOver, handleChatDrop, handleChatScroll, handleComposerKeyDown, handleComposerPaste, handleFilesSelected, handleRailMouseMove, hasMoreHistory, hoveredTurn, hoveredTurnIndex, inlineCommandEmpty, inlineCommandOpen, input, interrupting, isStreaming, jumpToLatest, keepTurnSummary, loadEarlierMessages, loadingHistory, messages, pendingAttachments, permissionLabel, permissionLevel, pickWorkspace, queueView, resolvedComposerText, resumingTaskId, sandboxMode, scheduleHideTurnSummary, selectSession, sendMessage, sendShortcut, sendShortcutLabel, sessionState, sessions, setInput, setPendingAttachments, setSendShortcut, stopStreaming, taskGuidanceVisible, toggleTraceExpanded, turnRefs, updateMessage, updateSessionPermissions, withdrawQueuedMessages } = conversations

  const { deletePlugin, deleteSkill, extensionsTab, filteredPlugins, filteredSkills, pluginSearch, setExtensionsTab, setPluginSearch, setSkillFilter, setSkillSearch, skillFilter, skillSearch, skills, togglePlugin, toggleSkill } = extensions

  const { activateProvider, applyToken, config, configText, deleteProvider, feishuChats, feishuChatsError, feishuChatsLoaded, feishuChatsLoading, feishuTesting, form, handleModelChange, handleSettingsFormChange, jsonStatus, loadFeishuChats, modelOptions, modelSelectPlaceholder, modelSelectWidth, pickDirectory, pickingDirectory, providerBusy, providerFields, resetSettings, saveProvider, saveSettings, sendFeishuTest, testProvider, setConfigText, setSettingsDirty, setTokenDraft, settingsDirty, settingsModelOptions, thinkingEffortOptions, tokenDirty, tokenDraft } = settings

  const { acknowledgeScheduleRun, attentionByTask, attentionRuns, automationKind, automationQuery, automations, automationsLoaded, automationTab, loadRunTranscript, runTranscript, setRunTranscript, runTranscriptLoading, loadAutonomy, decideProposal, answeredProposals, funnel, home, homeLoaded, loadHome, bulkScheduleAction, cancelScheduleRun, clearScheduleAttention, deleteSchedule, deleteWorkflow, editingWorkflow, editingWorkflowId, filteredSchedules, filteredWorkflows, loadAutomations, loadScheduleRuns, loadSchedules, loadWorkflows, openScheduleDetails, permissionProfileLabel, retryScheduleRun, runScheduleNow, runWorkflowNow, scheduleArtifacts, scheduleDetailOpen, scheduleOutputLoading, scheduleQuery, scheduleRunOutput, scheduleRuns, scheduleRunsLoading, scheduleStatusFilter, schedulerHealth, schedulerRefreshedAt, schedulerStale, schedules, selectedSchedule, selectedScheduleIds, selectedScheduleRun, selectedScheduleRunId, selectedScheduleRunTask, setAutomationKind, setAutomationQuery, setAutomationTab, setEditingWorkflowId, setScheduleDetailOpen, setScheduleQuery, setScheduleStatusFilter, setSelectedScheduleIds, setSelectedScheduleRunId, setWorkflowQuery, startingWorkflowIds, toggleSchedule, toggleWorkflow, unseenFailures, workflowQuery, workflows } = automation

  const { handlePaletteKeyDown, navItems, navigateTo, pageMeta, paletteCommands, paletteEntries, paletteIndex, runPaletteEntry, setPaletteIndex } = shell

  const paletteInputRef = React.useRef<HTMLInputElement | null>(null)

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
    openConversation,
    sendMessage,
    resolvedComposerText,
    pageMeta,
    sessions,
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
    home,
    homeLoaded,
    loadHome,
    loadAutonomy,
    loadRunTranscript,
    runTranscript,
    setRunTranscript,
    runTranscriptLoading,
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
    if (view === 'memory') return renderMemory(ctx)
    if (view === 'schedules') return renderSchedules(ctx)
    if (view === 'settings') return renderSettings()
    return renderHome(ctx)
  }

  // The thread name is the screen's title when one is open -- it is the only
  // place that answers "which conversation is this", now that no column does.
  const currentMeta = pageMeta[view] || pageMeta.home

  const topbarMenu: MenuProps['items'] = [
    ...navItems.filter(item => item.key !== 'home' && item.key !== 'schedules'),
    { type: 'divider' },
    { key: 'settings', icon: <SettingOutlined />, label: '设置' },
    {
      key: 'theme',
      icon: themeMode === 'dark' ? <SunOutlined /> : <MoonOutlined />,
      label: themeMode === 'dark' ? '切到浅色模式' : '切到深色模式',
    },
  ]

  const handleTopbarMenu: MenuProps['onClick'] = ({ key }) => {
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
          Button: {
            // antd's default is `0 2px 0 <colour>`, a hard 2px bar under every
            // primary button. In light mode the primary is near-black and that
            // bar is near-white: a button with a second edge under it. Nothing
            // here wants a fake bevel, and in a toolbar it read as a rendering
            // fault rather than a style.
            primaryShadow: 'none',
            dangerShadow: 'none',
          },
        },
      }}
    >
      {contextHolder}
      <Layout className="app-shell app-shell-flat">
        {/* One bar, not a column. The app's day-to-day surface is one screen --
            the agent -- so navigation is the exception: two pages in a menu, and
            threads / the board in drawers that open when you want them. A 260px
            sidebar charged every screen for six entries, five of which are
            opened once a week. */}
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
            {/* Three entries, not eight. A menu is for the pages opened once a
                week; the ones a person moves between while working stay on the
                bar, where they cost a click less and, more to the point, are
                visible -- a menu hides its contents, and a destination nobody
                can see is a destination nobody uses. The ⋯ now holds 记忆 / 设置
                / 搜索 / 主题 -- four things, none of which is a place you work
                from. 扩展 and 自主 were pages here; both were content that
                belongs to a page that already existed (设置 and 看板). */}
            <Button
              type="text"
              className="header-schedules"
              aria-current={view === 'schedules' ? 'page' : undefined}
              onClick={() => navigateTo('schedules')}
            >
              自动化
            </Button>
            {/* The board is a *state* panel rather than one page's content: it is
                a control you open on whatever screen you are on, so it sits here
                as one. The badge is the only thing that speaks up -- a count
                over zero. */}
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

      {/* No mask: the panel is a control you keep open while working, not a modal
          that stops the page until answered. It runs the full height, so its own
          header sits on the same line as the app bar and its × is the way out --
          a panel that starts below the bar leaves a band of page above it that
          reads as a mistake. */}
      <Drawer
        placement="right"
        width={400}
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
