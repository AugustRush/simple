/** The address bar as one more view of the same state.
 *
 * The app is a single page with six views and a conversation, and none of it
 * used to have an address: a refresh always landed on 对话, a conversation
 * could not be linked to, and the browser's back button left the app instead of
 * walking back through it. The hash is the smallest thing that fixes all three
 * -- no router dependency, no server route (the gateway serves the same
 * ``index.html`` for every path it does not own).
 *
 * Shapes: ``#/`` (the conversation) and ``#/<view>`` for the four pages.
 *
 * There is no thread address any more. With one conversation, ``#/t/<id>``
 * named a thing that cannot change -- the id was the same on every load -- so
 * the only content it carried was the possibility of being wrong after a
 * delete. The address names the screen, which is what a person can navigate.
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


export function parseRoute(hash: string): { view: ViewName } {
  const raw = String(hash || '').replace(/^#\/?/, '')
  const [head] = raw.split('/')
  // An old `#/t/<id>` link lands on the conversation: that is the only screen it
  // could have meant, and `t` is not a view name.
  const view = isViewName(head) ? head : 'home'
  return { view }
}


export function routeFor(view: string): string {
  return view === 'home' ? '#/' : `#/${view}`
}
