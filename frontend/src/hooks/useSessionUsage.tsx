/** 一个会话花了多少 token，以及它离上限还有多远。
 *
 * 与服务器同源：这个数字来自 `/api/sessions/{id}/usage`，而那个端点读的账本
 * 正是回合循环在每一步边界上检查的那一份。所以"12.3k / 200k"和"这一轮被预算
 * 停下"不可能互相矛盾——两处都从 `usage_events` 求和，不是两套算法。
 *
 * 刷新时机只有两个：换会话，以及一轮结束（`isStreaming` 由真变假）。流式过程
 * 中每一步都去问是不必要的：一轮之内数字只增不减，而中间值没有人会读。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { SessionUsage } from '../types'
import { useUi } from './useUi'
import { useApiClient } from './useApiClient'
import { useConversations } from './useConversations'

type Deps = ReturnType<typeof useUi> & ReturnType<typeof useApiClient> & ReturnType<typeof useConversations>

export function useSessionUsage(deps: Deps) {
  const { activeSession, apiHeaders, isStreaming } = deps

  const [sessionUsage, setSessionUsage] = useState<SessionUsage | null>(null)
  //: 上一次是哪个会话的数字，用来在切换会话的那一帧先把旧数字清掉——把上一个
  //: 会话的用量挂在新的会话旁边比空着更糟，它是一个看起来正确的错误答案。
  const shownFor = useRef<string | null>(null)

  const loadUsage = useCallback(async () => {
    if (!activeSession) {
      setSessionUsage(null)
      shownFor.current = null
      return
    }
    if (shownFor.current !== activeSession) {
      setSessionUsage(null)
      shownFor.current = activeSession
    }
    try {
      const resp = await fetch(
        `/api/sessions/${encodeURIComponent(activeSession)}/usage`,
        { headers: apiHeaders() },
      )
      if (!resp.ok) return
      setSessionUsage(await resp.json())
    } catch {
      // 用量是附加信息；读不到就保持上一次的显示，不打断对话。
    }
  }, [activeSession, apiHeaders])

  useEffect(() => {
    void loadUsage()
  }, [loadUsage, isStreaming])

  return { loadUsage, sessionUsage } as const
}
