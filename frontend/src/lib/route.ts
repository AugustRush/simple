/** The address bar as one more view of the same state.
 *
 * The app is a single page with five views and a conversation, and none of it
 * used to have an address: a refresh always landed on 对话, a conversation
 * could not be linked to, and the browser's back button left the app instead of
 * walking back through it. The hash is the smallest thing that fixes all three
 * -- no router dependency, no server route (the gateway serves the same
 * ``index.html`` for every path it does not own).
 *
 * Shapes: ``#/chat/<session>``, ``#/chat``, ``#/sessions``, ``#/extensions``,
 * ``#/schedules``, ``#/settings``. An unknown or empty hash means chat: a URL
 * someone typed by hand should not be an error page.
 */

export const VIEW_NAMES = ['chat', 'sessions', 'extensions', 'schedules', 'settings'] as const

export type ViewName = (typeof VIEW_NAMES)[number]


export function isViewName(value: string): value is ViewName {
  return (VIEW_NAMES as readonly string[]).includes(value)
}


export function parseRoute(hash: string): { view: ViewName; sessionId: string | null } {
  const raw = String(hash || '').replace(/^#\/?/, '')
  const [head, ...rest] = raw.split('/')
  const view = isViewName(head) ? head : 'chat'
  const session = view === 'chat' ? rest.join('/') : ''
  return {
    view,
    sessionId: session ? decodeURIComponent(session) : null,
  }
}


export function routeFor(view: string, sessionId: string | null): string {
  if (view !== 'chat') return `#/${view}`
  return sessionId ? `#/chat/${encodeURIComponent(sessionId)}` : '#/chat'
}
