/** The address bar as one more view of the same state.
 *
 * The app is a single page with six views and a conversation, and none of it
 * used to have an address: a refresh always landed on 对话, a conversation
 * could not be linked to, and the browser's back button left the app instead of
 * walking back through it. The hash is the smallest thing that fixes all three
 * -- no router dependency, no server route (the gateway serves the same
 * ``index.html`` for every path it does not own).
 *
 * Shapes: ``#/`` (home, optionally with a thread: ``#/t/<session>``),
 * ``#/memory``, ``#/schedules``, ``#/settings``.
 * An unknown or empty hash means home: the app opens on the agent, not on
 * whichever thread was last touched -- a URL someone typed by hand should not be
 * an error page either.
 *
 * There is no ``#/chat``. A conversation is what the agent's screen shows, not a
 * second screen: the page you land on and the page you talk on are the same page,
 * so "which page am I on" and "which thread am I in" stopped being two questions.
 */

export const VIEW_NAMES = ['home', 'memory', 'schedules', 'settings'] as const

export type ViewName = (typeof VIEW_NAMES)[number]


export function isViewName(value: string): value is ViewName {
  return (VIEW_NAMES as readonly string[]).includes(value)
}


export function parseRoute(hash: string): { view: ViewName; sessionId: string | null } {
  const raw = String(hash || '').replace(/^#\/?/, '')
  const [head, ...rest] = raw.split('/')
  // `#/t/<id>` is the one thread-address there is: the id belongs to the same
  // screen, so it rides on home rather than naming a page of its own.
  if (head === 't') {
    const session = rest.join('/')
    return { view: 'home', sessionId: session ? decodeURIComponent(session) : null }
  }
  const view = isViewName(head) ? head : 'home'
  return { view, sessionId: null }
}


export function routeFor(view: string, sessionId: string | null): string {
  if (view !== 'home') return `#/${view}`
  return sessionId ? `#/t/${encodeURIComponent(sessionId)}` : '#/'
}
