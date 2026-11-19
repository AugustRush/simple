/** Which theme is showing, and how it is decided.
 *
 * Three modes, because "light" and "dark" are not the only answers a person has:
 * `system` follows the OS and is what most people actually want -- a laptop that
 * dims at sunset should take the app with it. The preference is stored; the
 * *resolved* theme is what every consumer needs.
 *
 * Two defects this file exists to fix, both measured:
 *
 * 1. **The first paint was the wrong theme.** `main.tsx` set `data-theme` on
 *    `#root`, and every theme rule in this app is `html[data-theme=…]`. So the
 *    attribute matched nothing, the page painted with the dark `:root` tokens,
 *    and React's first effect flipped it. A light-theme reader got a dark flash
 *    on every reload.
 * 2. **Switching animated every colour.** `background`, `color` and `border` all
 *    have transitions, so a theme change swept through the page for 150ms. One
 *    frame with transitions off makes it a cut.
 */

export type ThemePreference = 'light' | 'dark' | 'system'
export type ResolvedTheme = 'light' | 'dark'

export const THEME_STORAGE_KEY = 'agent_theme'

const DARK_QUERY = '(prefers-color-scheme: dark)'

export function prefersDark(): boolean {
  return typeof window !== 'undefined' && window.matchMedia(DARK_QUERY).matches
}

export function readThemePreference(): ThemePreference {
  try {
    const raw = localStorage.getItem(THEME_STORAGE_KEY)
    return raw === 'light' || raw === 'dark' || raw === 'system' ? raw : 'dark'
  } catch {
    // Storage can be unavailable (file:// with a strict policy). The default is
    // the theme this app shipped with, not a crash on the first line of main.tsx.
    return 'dark'
  }
}

export function resolveTheme(preference: ThemePreference): ResolvedTheme {
  if (preference === 'system') return prefersDark() ? 'dark' : 'light'
  return preference
}

/** Put the resolved theme on `html`, which is what the stylesheets select on.
 *
 * `suppressTransitions` adds a class for exactly one frame: `transition: none`
 * applied for longer would also swallow the transitions of whatever the person
 * does next.
 */
export function applyTheme(
  theme: ResolvedTheme,
  { suppressTransitions = false }: { suppressTransitions?: boolean } = {},
): void {
  const html = document.documentElement
  if (suppressTransitions) {
    html.classList.add('theme-switching')
    window.setTimeout(() => html.classList.remove('theme-switching'), 0)
  }
  html.dataset.theme = theme
  html.style.colorScheme = theme
  if (document.body) document.body.dataset.theme = theme
  // The browser's own chrome (the address bar on mobile, the window frame on
  // some platforms) reads this, and it does not follow CSS variables.
  const meta = document.querySelector('meta[name="theme-color"]')
  if (meta) {
    const background = getComputedStyle(html).getPropertyValue('--bg').trim()
    if (background) meta.setAttribute('content', background)
  }
}

/** Subscribe to the OS theme changing. Returns the unsubscribe.
 *
 * Only meaningful in `system` mode, but subscribing unconditionally keeps the
 * caller from having to re-subscribe every time the preference changes.
 */
export function watchSystemTheme(onChange: () => void): () => void {
  if (typeof window === 'undefined') return () => {}
  const query = window.matchMedia(DARK_QUERY)
  query.addEventListener('change', onChange)
  return () => query.removeEventListener('change', onChange)
}
