import { useEffect, useRef, useState } from 'react'

export function useUi() {
  const [themeMode, setThemeMode] = useState<string>(
    () => localStorage.getItem('agent_theme') || 'dark',
  )

  const [view, setView] = useState<string>('chat')

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

  useEffect(() => {
    document.documentElement.dataset.theme = themeMode
    document.body.dataset.theme = themeMode
    document.body.style.background = ''
  }, [themeMode])

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
    const collapseOnMobile = () => {
      if (window.innerWidth <= 768) setCollapsed(true)
    }
    window.addEventListener('resize', collapseOnMobile)
    return () => window.removeEventListener('resize', collapseOnMobile)
  }, [])

  return { clock, collapsed, commandDismissed, commandIndex, commandIndexPinned, commandItemRefs, commandPaletteOpen, loadingView, paletteQuery, searchOpen, setClock, setCollapsed, setCommandDismissed, setCommandIndex, setCommandIndexPinned, setCommandPaletteOpen, setLoadingView, setPaletteQuery, setSearchOpen, setThemeMode, setView, themeMode, view } as const
}
