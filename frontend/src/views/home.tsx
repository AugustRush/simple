/** The agent's screen: the conversation, and nothing else.
 *
 * Why there is nothing above it any more
 * --------------------------------------
 * This view used to stack two feeds: the conversation, and above it a strip
 * ("它自己做的事") listing runs and other threads with a "加载更早" pager. That
 * strip was the last place the app still said *the agent's autonomous work is a
 * separate thing you go and check* -- which is exactly the relationship this
 * product is trying to end.
 *
 * A finished run now says what it did in the conversation itself (see
 * `agent/reports.py`): the sentence arrives where the person is, at its place in
 * time, and it survives a reload because it is journalled as a message. What the
 * strip used to hold is therefore already here:
 *
 * * runs -> report rows, in the transcript, at the time they finished;
 * * other threads -> the 线程 drawer, which lists every session there is;
 * * older history -> the transcript's own paging (`loadEarlierMessages`).
 *
 * Keeping both would have been two records of one history, drifting apart one
 * refresh at a time -- the failure this codebase keeps paying for. So the feed
 * is gone, and the conversation is the record.
 *
 * The one thing this file still owns is the boot: until the conversation arrives
 * from `POST /api/session/main` there is nothing to show, and a skeleton says so
 * without pretending to be a landing page.
 */

import type { AppCtx } from '../app/AppCtx'
import { renderChat } from './chat'
import { Skeleton } from 'antd'

export function renderHome(ctx: AppCtx) {
  if (!ctx.activeSession) {
    // Boot only. The conversation is the install's, created on first ask, so
    // this state lasts one request -- and it must look like "opening" rather
    // than like a page with a box on it, which is what used to be here.
    return (
      <div className="home-boot" role="status" aria-live="polite">
        <Skeleton active paragraph={{ rows: 6 }} title={false} />
      </div>
    )
  }
  return <div className="home-room">{renderChat(ctx)}</div>
}
