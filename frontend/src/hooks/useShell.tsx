import { useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react'
import { SCHEDULE_FAST_POLL_MS, SCHEDULE_IDLE_POLL_MS } from '../constants'
import { BulbOutlined, ClockCircleOutlined, HomeOutlined, SettingOutlined } from '@ant-design/icons'
import { Modal } from 'antd'
import type { CommandInfo } from '../types'
import { parseRoute, routeFor } from '../lib/route'
import { useUi } from './useUi'
import { useConversations } from './useConversations'
import { useExtensions } from './useExtensions'
import { useSettings } from './useSettings'
import { useAutomation } from './useAutomation'

type Deps = ReturnType<typeof useUi> & ReturnType<typeof useConversations> & ReturnType<typeof useExtensions> & ReturnType<typeof useSettings> & ReturnType<typeof useAutomation>

export function useShell(deps: Deps) {
  const { activeSession, applyCommand, automationKind, automationTab, boardOpen, loadAutonomy, loadHome, loadAutomations, loadSessions, commandPaletteOpen, commands, composerRef, connected, connecting, input, loadPlugins, loadScheduleRuns, loadSchedulerHealth, loadSchedules, loadSettings, loadSkills, loadWorkflows, paletteQuery, resetSettings, scheduleDetailOpen, schedulerWatchful, selectedSchedule, selectSession, sessions, setClock, setCommandPaletteOpen, setInput, setPaletteQuery, setView, settingsDirty, tokenDirty, view } = deps

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

  // "/" starts a command, which is what the empty state tells the reader to do
  // ("按 / 使用命令"). The key was only handled *inside* the textarea, and a freshly
  // opened page has focus on the body -- so on the screen that prints the hint,
  // pressing "/" did nothing at all. A typed draft is never overwritten: when there
  // is text, focusing the composer is the whole gesture.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== '/' || event.metaKey || event.ctrlKey || event.altKey) return
      const target = event.target as HTMLElement | null
      if (
        target?.tagName === 'INPUT'
        || target?.tagName === 'TEXTAREA'
        || target?.isContentEditable
      ) {
        return
      }
      const composer = composerRef.current
      if (!composer) return
      event.preventDefault()
      composer.focus()
      if (!input.trim()) setInput('/')
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [composerRef, input, setInput])

  useEffect(() => {
    if (view !== 'schedules') return
    const cadence = schedulerWatchful ? SCHEDULE_FAST_POLL_MS : SCHEDULE_IDLE_POLL_MS
    const refresh = () => {
      void loadSchedules(true)
      // The graph draws each step's liveness from the workflow payload, so a
      // workflow watched from this view has to refresh that too -- otherwise the
      // steps sit still while the run behind them moves.
      if (automationTab === 'automation' && automationKind !== 'task') void loadWorkflows(true)
      // The unified list is where a chain's status is read, so its own poll keeps
      // it honest between mutations.
      if (automationTab === 'automation' && automationKind === 'all') void loadAutomations(true)
      if (scheduleDetailOpen && selectedScheduleId) {
        void loadScheduleRuns(selectedScheduleId, false, true)
      }
    }
    const timer = window.setInterval(refresh, cadence)
    return () => window.clearInterval(timer)
  }, [
    automationKind,
    automationTab,
    loadAutomations,
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
    // The home record is re-read on entering the page *and* when the open thread
    // changes. Both edits happen inside the same view now -- starting a
    // conversation and going back to the landing are the same screen -- so a
    // view-change-only effect left the landing showing the record from before
    // the conversation the reader just had.
    if (view === 'home') {
      void loadHome()
      void loadSessions()
      loadSchedules()
    }
    // Both lists load on entering the merged page: the counts on the tabs and
    // the page subtitle are about both, and switching tabs is not a data
    // event -- it is the same visit continuing.
    // 设置 holds the capability lists now (they were a page of their own), so
    // entering it loads plugins and skills alongside the config.
    if (view === 'settings') {
      loadSettings()
      loadPlugins()
      loadSkills()
    }
    if (view === 'schedules') {
      // Three readers of the same facts: the task list, the chain list, and the
      // unified list the page actually opens on. The last one used to load only
      // from the poll, so the page greeted every visit with "还没有自动化" over
      // automations that existed -- found by opening the page in a browser.
      loadSchedules()
      loadWorkflows(true)
      loadAutomations()
      loadSkills(true)
      loadSchedulerHealth()
    }
  }, [view, activeSession, loadPlugins, loadSkills, loadSchedules, loadWorkflows, loadSchedulerHealth, loadSettings, loadHome, loadSessions])

  // The board panel is where a proposal is answered now -- 自主 was a second
  // screen showing the same rows. So the panel fetches them when it opens, and
  // only then: a request on behalf of a panel nobody opened is a poll of a
  // screen that is not on screen.
  useEffect(() => {
    if (!boardOpen) return
    void loadAutonomy()
  }, [boardOpen, loadAutonomy])

  /** Move to another view, asking first if the settings page has unsaved edits.
   *
   * Entering the settings view refetches the config and clears the dirty flag,
   * so without this a click on any other nav item quietly threw the edits away
   * -- and coming back showed the saved values, as though nothing had happened.
   */
  const navigateTo = (next: string, options: { fromHistory?: boolean } = {}) => {
    if (next === view) return
    if (view !== 'settings' || !(settingsDirty || tokenDirty)) {
      setView(next)
      return
    }
    // This came from the address bar, and the guard is about to ask whether the
    // edits may be dropped. Until the answer is yes the page *stayed* on
    // settings, so the address has to say so again -- otherwise a cancelled
    // confirm leaves the bar describing a page that is not on screen.
    if (options.fromHistory) {
      window.history.replaceState(null, '', routeFor(view, activeSession))
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

  // ── The address bar ───────────────────────────────────────────────────────
  //
  // One writer, one reader. The writer records what is on screen after the fact
  // -- it never drives the view -- so every existing way of changing state
  // (nav, palette, a session row, a creating/deleting session) gets an address
  // without being taught about it. The reader is the browser's popstate, which
  // is the only source of change the app does not own.
  const route = routeFor(view, activeSession)

  // The entry the app was opened on is replaced, not pushed: a click on 设置
  // straight after load should leave one "back" press between the two pages,
  // not a duplicate entry for the landing page that says the same thing.
  const firstRouteRef = useRef(true)
  useEffect(() => {
    const current = window.location.hash
    if (current === route) {
      firstRouteRef.current = false
      return
    }
    const from = parseRoute(current)
    const to = parseRoute(route)
    // A thread is a *state of the same screen*, not a page: opening one from the
    // landing page replaces the address instead of stacking `#/t/x` on top of
    // `#/`. Otherwise "back" walks through a landing page the reader never left,
    // and pressing it looks like a no-op. A change of *page* still pushes,
    // because that is a place they can return to.
    const sameScreen = from.view === to.view
    if (firstRouteRef.current || sameScreen) {
      firstRouteRef.current = false
      window.history.replaceState(null, '', route)
      return
    }
    // push, not replace: the browser's back button is the second way to move
    // between the pages the reader has visited, and replaceState would leave
    // only the latest one.
    firstRouteRef.current = false
    window.history.pushState(null, '', route)
  }, [route])

  useEffect(() => {
    // Both events, one handler. ``popstate`` is history traversal; ``hashchange``
    // is the reader editing the address bar -- a page whose bar is part of its
    // state has to answer both, or a pasted ``#/settings`` over a running app
    // changes the URL and nothing else. Our own writer uses ``pushState``, which
    // fires neither, so this cannot feed back into itself.
    const applyAddress = () => {
      const target = parseRoute(window.location.hash)
      if (target.sessionId && target.sessionId !== activeSession) {
        selectSession(target.sessionId)
        return
      }
      if (target.view !== view) {
        navigateTo(target.view, { fromHistory: true })
        return
      }
      // The bare chat entry while a conversation is open. Closing that
      // conversation here would detach a live socket to show a welcome screen;
      // the bar goes back to naming what is actually open instead.
      const current = routeFor(view, activeSession)
      if (window.location.hash !== current) {
        window.history.replaceState(null, '', current)
      }
    }
    window.addEventListener('popstate', applyAddress)
    window.addEventListener('hashchange', applyAddress)
    return () => {
      window.removeEventListener('popstate', applyAddress)
      window.removeEventListener('hashchange', applyAddress)
    }
  }, [activeSession, navigateTo, selectSession, view])

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

  // The two places the ⋯ menu holds. Not a navigation column: the app's
  // day-to-day surface is one screen (the agent), so what is left over is the
  // things you visit deliberately and briefly. 扩展 and 自主 used to sit here
  // too, and both were the same mistake in different clothes -- a destination
  // that is not a place you *go*: the capability lists are configuration (they
  // live on 设置 now), and a proposal is answered where it is shown (the board).
  // A menu entry's whole job is to be a place; an entry that opens a page whose
  // content another screen already shows is a second door into one room.
  const navItems = [
    // What the agent *knows*. (What it *can* do is on 设置 with the rest of the
    // configuration, which is also where you would look to change it.)
    { key: 'memory', icon: <BulbOutlined />, label: '记忆' },
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
      { view: 'home', label: '主页', icon: <HomeOutlined />, terms: 'home 主页 首页 对话 chat' },
      { view: 'schedules', label: '自动化', icon: <ClockCircleOutlined />, terms: '定时任务 工作流 schedules workflows automation' },
      { view: 'memory', label: '记忆', icon: <BulbOutlined />, terms: '记忆 memory 事实' },
      { view: 'settings', label: '设置', icon: <SettingOutlined />, terms: 'settings 插件 技能 plugins skills' },
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
    home: {
      // The open conversation's own name, not "当前对话". With one conversation
      // the phrase was a placeholder that never became informative; the name the
      // agent or the person gave it is the only thing up here that can change.
      title: activeSession
        ? (sessions.find(item => item.session_id === activeSession)?.title || '对话')
        : '对话',
      // The subtitle is the connection state once a thread is open: it is the
      // part that changes while you read, and the thread's own name is in the
      // title. Three states, not two -- a socket that has not opened yet is not
      // a dropped one.
      subtitle: activeSession
        ? connected
          ? '实时连接中'
          : connecting
            ? '正在连接…'
            : '连接已断开'
        : '说一句话交给它，或者看它做了什么',
    },

    sessions: {
      title: '会话管理',
      subtitle: `${sessions.length} 个会话，${sessions.filter(item => item.live).length} 个动态会话`,
    },
    memory: {
      title: '记忆',
      subtitle: '它记得的关于你的事 —— 看得见，也改得动',
    },
    // Not "定时任务": the page now holds tasks that wait for a signal, and a
    // name that promises a time would be wrong for them. "自动化" is what the
    // page actually is -- work that runs without being asked each time.
    schedules: { title: '自动化', subtitle: '管理定时执行与等待信号的任务。' },
    settings: {
      title: '设置',
      subtitle: '令牌、模型、频道，以及它能用的插件和技能',
    },
  }

  return { navItems, navigateTo, pageMeta, paletteCommands, paletteEntries, paletteIndex, setPaletteIndex, handlePaletteKeyDown, runPaletteEntry } as const
}
