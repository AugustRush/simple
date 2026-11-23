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

export const VIEW_NAMES = ['home', 'memory', 'schedules', 'capabilities', 'settings'] as const

export type ViewName = (typeof VIEW_NAMES)[number]


export function isViewName(value: string): value is ViewName {
  return (VIEW_NAMES as readonly string[]).includes(value)
}


//: A page a plugin contributed: ``#/p/<id>``. A prefix of its own rather than a
//: fifth view name, because the set of them is not fixed -- it is whatever the
//: installed plugins declare -- and a fixed list cannot hold a changing set.
export const PLUGIN_PAGE_PREFIX = 'p'


export function parseRoute(hash: string): { view: ViewName; page?: string } {
  const raw = String(hash || '').replace(/^#\/?/, '')
  const [head, second] = raw.split('/')
  if (head === PLUGIN_PAGE_PREFIX && second) {
    // The nav still highlights something, and "no page in the bar" is the
    // honest answer: the top bar's title will name the plugin's page instead.
    //
    // Decoded defensively: `decodeURIComponent('%')` throws, and this runs inside
    // a `useState` initialiser -- a malformed hash would take the whole app down
    // to a blank page instead of landing on the conversation.
    let page = second
    try {
      page = decodeURIComponent(second)
    } catch {
      page = second
    }
    return { view: 'home', page }
  }
  // An old `#/t/<id>` link lands on the conversation: that is the only screen it
  // could have meant, and `t` is not a view name.
  const view = isViewName(head) ? head : 'home'
  return { view }
}


export function routeFor(view: string): string {
  return view === 'home' ? '#/' : `#/${view}`
}


export function routeForPage(pageId: string): string {
  return `#/${PLUGIN_PAGE_PREFIX}/${encodeURIComponent(pageId)}`
}
