import { useCallback, useEffect, useRef, useState } from 'react'
import { parseRoute } from '../lib/route'
import {
  applyTheme,
  readThemePreference,
  resolveTheme,
  THEME_STORAGE_KEY,
  watchSystemTheme,
  type ResolvedTheme,
  type ThemePreference,
} from '../lib/theme'

export type SettingsTab = 'general' | 'model' | 'advanced'

export function useUi() {
  const [themeMode, setThemeMode] = useState<ThemePreference>(readThemePreference)
  //: What the stylesheets should show. `system` is not a theme, it is a rule for
  //: choosing one, and every consumer wants the answer rather than the rule.
  const [resolvedTheme, setResolvedTheme] = useState<ResolvedTheme>(() =>
    resolveTheme(readThemePreference()),
  )

  //: The hash is read once, here, rather than applied by an effect: an effect
  //: that set the view after the first render would paint 对话 for a frame over
  //: a page that asked for 设置, and the address-bar writer would then record
  //: the wrong address before the correction.
  const [route, setRoute] = useState<{ view: string; page?: string }>(
    () => parseRoute(window.location.hash),
  )
  const view = route.view
  const setView = (next: string) => setRoute({ view: next })
  //: Navigating by name leaves any plugin page: the two are different screens
  //: and `#/p/<id>` is not a view, so a stale id would re-open the page the
  //: person just navigated away from.
  const openPage = (pageId: string | null) =>
    setRoute(prev => ({ view: prev.view, page: pageId || undefined }))
  //: The plugin page the address names, if any. `#/p/<id>` has no fixed view
  //: behind it -- the set of pages is whatever the installed plugins declare.
  const pluginPage = route.page
  //: Which panel of 设置 is showing. Shell state rather than view state so the
  //: panel survives leaving the page and coming back -- and so a future entry
  //: point can open 设置 on a named panel instead of always on 常规.
  const [settingsTab, setSettingsTab] = useState<SettingsTab>('general')
  //: Whether the board panel is pulled out. Shell state, not page state, because
  //: pages here are plain render functions rather than components: a `useState`
  //: inside one runs only while that view is current, and React counts hooks per
  //: component -- a varying count is error #300, a blank page.
  //:
  //: **Closed by default.** A 380px rail that appears before anybody asks takes a
  //: quarter of the window from the thing they came for; the header button carries
  //: a count when there is something to see, so "nothing is hidden" does not depend
  //: on the panel being open. Once opened, the choice is remembered.
  const [boardOpen, setBoardOpenState] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem('simple.board.panelOpen') === '1'
    } catch {
      return false
    }
  })
  const setBoardOpen = useCallback((next: boolean) => {
    setBoardOpenState(next)
    try {
      window.localStorage.setItem('simple.board.panelOpen', next ? '1' : '0')
    } catch {
      // Storage refused; the toggle still works for this visit.
    }
  }, [])

  //: A clock for the two things on this page that are about the present moment
  //: rather than about the data: how long until the next run, and how long ago
  //: this was last refreshed. Neither can be read off the payload.
  const [clock, setClock] = useState(() => Date.now())

  const [loadingView, setLoadingView] = useState(false)

  const [collapsed, setCollapsed] = useState(() => window.innerWidth <= 768)

  const [searchOpen, setSearchOpen] = useState(false)

  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false)

  const [paletteQuery, setPaletteQuery] = useState('')

  const [commandIndex, setCommandIndex] = useState(0)

  // Esc only hides the inline command popover for the current input; typing
  // again re-opens it. Without this state, closing the popover would require
  // destroying the user's draft.
  const [commandDismissed, setCommandDismissed] = useState(false)

  // Whether the highlight was moved by the user (arrow keys / hover) rather
  // than merely defaulting to the first suggestion. A bare "/" must not send
  // that default, but an explicitly chosen entry should still win.
  const [commandIndexPinned, setCommandIndexPinned] = useState(false)

  const commandItemRefs = useRef<Record<string, HTMLButtonElement | null>>({})

  // The theme actually applied: resolved from the preference, and re-resolved
  // when the OS changes under a `system` preference. Applying it goes through
  // one function so the first paint, a manual switch and an OS switch cannot
  // disagree about where the attribute goes.
  useEffect(() => {
    const theme = resolveTheme(themeMode)
    setResolvedTheme(theme)
    applyTheme(theme, { suppressTransitions: true })
    return watchSystemTheme(() => {
      if (themeMode !== 'system') return
      const next = resolveTheme('system')
      setResolvedTheme(next)
      applyTheme(next, { suppressTransitions: true })
    })
  }, [themeMode])

  /** Persist a preference and apply it. Not a toggle: there are three answers. */
  const chooseTheme = useCallback((next: ThemePreference) => {
    setThemeMode(next)
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next)
    } catch {
      // A blocked storage is not a reason to refuse the switch for this visit.
    }
  }, [])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setCommandPaletteOpen(true)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  useEffect(() => {
    // Follow the breakpoint, not every resize: crossing 768px flips the sidebar
    // either way, while a resize that stays on one side leaves the reader's own
    // choice alone. The old listener only ever collapsed, so widening a window --
    // or rotating a tablet -- left the sidebar closed until it was reopened by
    // hand, and with it went the theme and settings entries: the collapsed sider
    // is a zero-width one, not an icon rail.
    let narrow = window.innerWidth <= 768
    const onResize = () => {
      const next = window.innerWidth <= 768
      if (next === narrow) return
      narrow = next
      setCollapsed(next)
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  return { boardOpen, clock, collapsed, commandDismissed, commandIndex, commandIndexPinned, commandItemRefs, commandPaletteOpen, loadingView, paletteQuery, searchOpen, setBoardOpen, setClock, setCollapsed, setCommandDismissed, setCommandIndex, setCommandIndexPinned, setCommandPaletteOpen, setLoadingView, setPaletteQuery, setSearchOpen, chooseTheme, setThemeMode, setView, openPage, pluginPage, resolvedTheme, themeMode, view, settingsTab, setSettingsTab } as const
}
