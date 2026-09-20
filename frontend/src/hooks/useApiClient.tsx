import { useCallback, useRef, useState } from 'react'
import { message } from 'antd'

export function useApiClient() {
  const [messageApi, contextHolder] = message.useMessage()

  const wsRef = useRef<WebSocket | null>(null)

  const idRef = useRef(0)

  // The token lives in state, not in a `localStorage.getItem` read on every
  // render, because everything derived from it -- the auth headers, the file
  // and stream links -- has to be rebuilt when it changes.  Reading storage
  // during render cannot announce a change, so the old code reloaded the whole
  // page to make the new token visible, which threw away the conversation.
  const [token, setToken] = useState(() => localStorage.getItem('agent_token') || '')

  const makeId = useCallback(() => {
    idRef.current += 1
    return `msg-${Date.now()}-${idRef.current}`
  }, [])

  const apiHeaders = useCallback((): Record<string, string> => {
    const t = token
    return t ? { Authorization: `Bearer ${t}`, 'X-Auth-Token': t } : {}
  }, [token])

  const api = useCallback(
    async (path: string, options: RequestInit = {}) => {
      const resp = await fetch(path, {
        ...options,
        headers: {
          ...(options.headers as Record<string, string> || {}),
          ...apiHeaders(),
        },
      })
      if (resp.status === 401) {
        messageApi.error('鉴权失败：请检查 auth_token')
        throw new Error('unauthorized')
      }
      if (!resp.ok) {
        let msg = '请求失败'
        try {
          const body = await resp.json()
          msg = body.error || msg
        } catch {
          // Ignore non-JSON error responses.
        }
        messageApi.error(msg)
        throw new Error(msg)
      }
      return resp
    },
    [apiHeaders, messageApi],
  )

  /** A refresh nobody asked for: same request, but a failure goes to the
   *  caller instead of to the user.
   *
   * `api` reports every failure with a toast, which is right when a person has
   * just clicked something and wrong when a timer did it. A gateway that went
   * away would otherwise produce one toast per tick, and -- worse -- the caller
   * would never find out that the numbers on screen had stopped being current.
   */
  const refreshJson = useCallback(async (path: string) => {
    try {
      const resp = await fetch(path, { headers: apiHeaders() })
      if (!resp.ok) return null
      return await resp.json()
    } catch {
      return null
    }
  }, [apiHeaders])

  return { api, apiHeaders, contextHolder, idRef, makeId, messageApi, refreshJson, setToken, token, wsRef } as const
}
