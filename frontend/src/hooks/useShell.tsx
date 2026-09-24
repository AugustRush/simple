import { useEffect, useMemo, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react'
import { SCHEDULE_FAST_POLL_MS, SCHEDULE_IDLE_POLL_MS } from '../constants'
import { AppstoreOutlined, ClockCircleOutlined, FolderOpenOutlined, MessageOutlined, SettingOutlined } from '@ant-design/icons'
import { Modal } from 'antd'
import type { CommandInfo } from '../types'
import { useUi } from './useUi'
import { useConversations } from './useConversations'
import { useExtensions } from './useExtensions'
import { useSettings } from './useSettings'
import { useAutomation } from './useAutomation'

type Deps = ReturnType<typeof useUi> & ReturnType<typeof useConversations> & ReturnType<typeof useExtensions> & ReturnType<typeof useSettings> & ReturnType<typeof useAutomation>

export function useShell(deps: Deps) {
  const { activeSession, applyCommand, automationTab, commandPaletteOpen, commands, connected, loadPlugins, loadScheduleRuns, loadSchedulerHealth, loadSchedules, loadSettings, loadSignals, loadSkills, loadWorkflows, paletteQuery, plugins, resetSettings, scheduleDetailOpen, schedulerWatchful, selectedSchedule, sessions, setAutomationTab, setClock, setCommandPaletteOpen, setPaletteQuery, setView, settingsDirty, skills, tokenDirty, unseenFailures, view } = deps

  // How often to ask the server again -- and, more to the point, that it is
  // asked at all.
  //
  // This used to be "poll every two seconds if the last response said a run was
  // in flight". The only thing that could ever change that answer was the poll
  // itself, so a page opened while nothing was running never started asking,
  // and a task that fired on its own schedule while the page sat open was never
  // seen. The page could only observe the runs it had started itself.
  //
  // The cadence now comes from `schedulerWatchful`, which is also why the timer
  // survives a poll: it used to depend on `schedules`, so every response tore
  // the interval down and rebuilt it, and the real period was the cadence plus
  // a render plus a round trip. The open drawer is the same mistake one level
  // down -- its task is re-stored as a freshly parsed copy on every poll, so
  // the drawer is named by id here and the interval outlives the poll. A
  // signal-triggered step has no clock, so no predicate covers it -- which is
  // why the idle cadence exists at all rather than the timer stopping when
  // nothing is imminent.
  const selectedScheduleId = selectedSchedule?.id || ''

  useEffect(() => {
    if (view !== 'schedules') return
    const cadence = schedulerWatchful ? SCHEDULE_FAST_POLL_MS : SCHEDULE_IDLE_POLL_MS
    const refresh = () => {
      void loadSchedules(true)
      // The graph draws each step's liveness from the workflow payload, so a
      // running workflow watched from this tab has to refresh that too --
      // otherwise the steps sit still while the run behind them moves.
      if (automationTab === 'workflows') void loadWorkflows(true)
      if (scheduleDetailOpen && selectedScheduleId) {
        void loadScheduleRuns(selectedScheduleId, false, true)
      }
    }
    const timer = window.setInterval(refresh, cadence)
    return () => window.clearInterval(timer)
  }, [
    automationTab,
    loadScheduleRuns,
    loadSchedules,
    loadWorkflows,
    scheduleDetailOpen,
    schedulerWatchful,
    selectedScheduleId,
    view,
  ])

  // A countdown that does not tick is a timestamp with extra words. One second
  // is worth it only while something is about to happen; the rest of the time
  // the label is in coarser units and ten is plenty. Both readers -- the next
  // run and the freshness line -- want the same clock, so there is one, and it
  // asks the same question the poll does.
  useEffect(() => {
    if (view !== 'schedules') return
    const tick = () => setClock(Date.now())
    tick()
    const timer = window.setInterval(tick, schedulerWatchful ? 1000 : 10_000)
    return () => window.clearInterval(timer)
  }, [schedulerWatchful, view])

  useEffect(() => {
    if (view !== 'schedules') return
    void loadSchedulerHealth()
    const timer = window.setInterval(loadSchedulerHealth, 10000)
    return () => window.clearInterval(timer)
  }, [loadSchedulerHealth, view])

  useEffect(() => {
    // Both lists load on entering the merged page: the counts on the tabs and
    // the page subtitle are about both, and switching tabs is not a data
    // event -- it is the same visit continuing.
    if (view === 'extensions') {
      loadPlugins()
      loadSkills()
    }
    if (view === 'schedules') {
      loadSchedules()
      loadWorkflows(true)
      loadSkills(true)
      loadSchedulerHealth()
      loadSignals()
    }
    if (view === 'settings') loadSettings()
  }, [view, loadPlugins, loadSkills, loadSchedules, loadWorkflows, loadSchedulerHealth, loadSignals, loadSettings])

  /** Move to another view, asking first if the settings page has unsaved edits.
   *
   * Entering the settings view refetches the config and clears the dirty flag,
   * so without this a click on any other nav item quietly threw the edits away
   * -- and coming back showed the saved values, as though nothing had happened.
   */
  const navigateTo = (next: string) => {
    if (next === view) return
    if (view !== 'settings' || !(settingsDirty || tokenDirty)) {
      setView(next)
      return
    }
    Modal.confirm({
      title: '放弃未保存的设置？',
      content: '离开设置页会丢掉尚未保存的修改。',
      okText: '放弃修改',
      cancelText: '留在本页',
      okButtonProps: { danger: true },
      onOk: () => {
        resetSettings()
        setView(next)
      },
    })
  }

  /**
   * Go straight to the runs that are waiting to be looked at.
   *
   * `navigateTo` alone is not enough: it returns early when the view is
   * already the schedules page, which is exactly when the badge is most
   * likely to be pressed -- someone is on the page and still cannot find what
   * the number is talking about.
   */
  const openAttention = () => {
    navigateTo('schedules')
    setAutomationTab('attention')
  }

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
    // One entry for both lists: plugins and skills answer the same question
    // ("what can the agent do beyond its own tools?") and each list on its
    // own is too short to justify a navigation slot of its own.
    { key: 'extensions', icon: <AppstoreOutlined />, label: '扩展' },
    {
      key: 'schedules',
      icon: <ClockCircleOutlined />,
      // The badge lives on the navigation entry, not on the schedules page,
      // because the entire problem is a failure that finished while the page
      // was closed -- and it is a button rather than a chip, because a number
      // that says "two things need you" and then drops you on an unfiltered
      // list of forty tasks has told you nothing you can act on. Pressing it
      // opens the runs themselves.
      label: (
        <span className="nav-label">
          自动化
          {unseenFailures > 0 && (
            <button
              type="button"
              className="nav-badge"
              aria-label={`${unseenFailures} 次运行需要查看，打开待处理列表`}
              title={`${unseenFailures} 次运行需要查看`}
              onClick={event => {
                // The menu entry behind it would otherwise also fire and
                // settle the tab back to whatever it was.
                event.stopPropagation()
                openAttention()
              }}
            >
              {unseenFailures > 99 ? '99+' : unseenFailures}
            </button>
          )}
        </span>
      ),
    },
    // Settings is deliberately absent from the main navigation: it is visited
    // rarely and briefly, so it lives as a small entry in the sidebar footer
    // next to the theme switch rather than taking a slot beside the pages
    // someone visits every day.
  ]

  // The palette's own list: one index over commands and destinations, so the
  // arrow keys reach everything the palette shows. It used to print "Enter"
  // on every command while the input had no key handling at all -- Enter did
  // nothing, and the only way to use the palette was the mouse.
  //
  // Destinations match on plain text. The navigation entries carry JSX labels
  // (the schedules one holds the failure badge, itself a button, which also
  // made the palette render a button inside a button), and the placeholder
  // promises that navigation is searchable too.
  type PaletteEntry =
    | { kind: 'command'; key: string; command: CommandInfo }
    | { kind: 'nav'; key: string; view: string; label: string; icon: ReactNode }

  const paletteEntries = useMemo<PaletteEntry[]>(() => {
    const query = paletteQuery.trim().toLowerCase()
    const destinations = [
      { view: 'chat', label: '对话', icon: <MessageOutlined />, terms: 'chat' },
      { view: 'sessions', label: '会话管理', icon: <FolderOpenOutlined />, terms: 'sessions' },
      { view: 'extensions', label: '扩展', icon: <AppstoreOutlined />, terms: '插件 技能 extensions plugins skills' },
      { view: 'schedules', label: '自动化', icon: <ClockCircleOutlined />, terms: '定时任务 工作流 schedules workflows automation' },
      { view: 'settings', label: '设置', icon: <SettingOutlined />, terms: 'settings' },
    ].filter(item => !query || `${item.label} ${item.terms}`.toLowerCase().includes(query))
    return [
      ...paletteCommands.slice(0, 8).map(command => ({ kind: 'command' as const, key: `command:${command.name}`, command })),
      ...destinations.map(({ view, label, icon }) => ({ kind: 'nav' as const, key: `nav:${view}`, view, label, icon })),
    ]
  }, [paletteCommands, paletteQuery])

  const [paletteIndex, setPaletteIndex] = useState(0)

  // A new query or a fresh opening starts from the top; a shrinking list must
  // not leave the index pointing past its end.
  useEffect(() => {
    setPaletteIndex(0)
  }, [paletteQuery, commandPaletteOpen])

  const runPaletteEntry = (entry: PaletteEntry) => {
    if (entry.kind === 'command') {
      applyCommand(entry.command)
      return
    }
    navigateTo(entry.view)
    setCommandPaletteOpen(false)
    setPaletteQuery('')
  }

  const handlePaletteKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return
    const count = paletteEntries.length
    if (!count) return
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      const step = event.key === 'ArrowDown' ? 1 : -1
      setPaletteIndex(index => (index + step + count) % count)
    } else if (event.key === 'Enter') {
      event.preventDefault()
      runPaletteEntry(paletteEntries[Math.min(paletteIndex, count - 1)])
    }
  }

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
    // One meta for the merged page: the head the visitor sees is the page's,
    // while each tab keeps its own counts where they already were -- the
    // skills summary strip and the plugins search both belong to their lists.
    extensions: {
      title: '扩展',
      subtitle: `${plugins.length} 个插件 · ${skills.length} 个技能`,
    },
    // Not "定时任务": the page now holds tasks that wait for a signal, and a
    // name that promises a time would be wrong for them. "自动化" is what the
    // page actually is -- work that runs without being asked each time.
    schedules: { title: '自动化', subtitle: '管理定时执行与等待信号的任务。' },
    settings: {
      title: '设置',
      subtitle: '管理访问令牌、模型与频道',
    },
  }

  return { navItems, navigateTo, openAttention, pageMeta, paletteCommands, paletteEntries, paletteIndex, setPaletteIndex, handlePaletteKeyDown, runPaletteEntry } as const
}
