import { useCallback, useEffect, useRef, useState } from 'react'
import type { ConfirmDecision, ConfirmRequest } from '../types'
import { Modal } from 'antd'
import { useApiClient } from './useApiClient'

type Deps = ReturnType<typeof useApiClient>

export function useConfirm(deps: Deps) {
  const { wsRef } = deps

  const [confirmReq, setConfirmReq] = useState<ConfirmRequest | null>(null)

  const [confirmRemaining, setConfirmRemaining] = useState(0)

  const [confirmDetailOpen, setConfirmDetailOpen] = useState(false)

  const [confirmOverflowing, setConfirmOverflowing] = useState(false)

  const approvalCommandRef = useRef<HTMLDivElement | null>(null)

  // Absolute deadline rather than a decremented counter: background tabs get
  // their timers throttled, and a counter would drift behind the server.
  const confirmDeadlineRef = useRef(0)

  // Clear the prompt before sending: a double click on "允许" must not emit two
  // replies for one token, and the bar should not linger while the turn resumes.
  const sendConfirm = useCallback(
    (decision: ConfirmDecision) => {
      const request = confirmReq
      setConfirmReq(null)
      setConfirmDetailOpen(false)
      if (!request) return
      const socket = wsRef.current
      if (!socket || socket.readyState !== WebSocket.OPEN) return
      socket.send(
        JSON.stringify({
          type: 'confirm_response',
          decision,
          confirmation_token: request.confirmation_token || '',
        }),
      )
    },
    [confirmReq],
  )

  // Countdown to the server-side deadline, so "it just silently expired" can't
  // happen while the user is deciding.
  useEffect(() => {
    if (!confirmReq) {
      setConfirmRemaining(0)
      return
    }
    const tick = () => {
      setConfirmRemaining(
        Math.max(0, Math.ceil((confirmDeadlineRef.current - Date.now()) / 1000)),
      )
    }
    tick()
    const timer = window.setInterval(tick, 1000)
    return () => window.clearInterval(timer)
  }, [confirmReq])

  // Whether the command is actually clipped, measured from the rendered box.
  // A character-count guess gets this wrong the moment the text is mostly
  // CJK (one character is a full column wide, not a half) and would leave the
  // user staring at a command they cannot expand.
  useEffect(() => {
    if (!confirmReq) {
      setConfirmOverflowing(false)
      return
    }
    // Keep the toggle available while expanded, otherwise collapsing becomes
    // impossible as soon as the tall box stops overflowing.
    if (confirmDetailOpen) return
    const element = approvalCommandRef.current
    if (!element) {
      setConfirmOverflowing(false)
      return
    }
    const measure = () => setConfirmOverflowing(element.scrollHeight > element.clientHeight + 1)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [confirmReq, confirmDetailOpen])

  useEffect(() => {
    if (!confirmReq) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        sendConfirm('deny')
        return
      }
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        event.preventDefault()
        sendConfirm(event.shiftKey && confirmReq.allow_session ? 'allow_session' : 'allow_once')
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [confirmReq, sendConfirm])

  const confirmResourceDeletion = (
    resourceLabel: string,
    resourceName: string,
    onConfirm: () => Promise<void>,
  ) => {
    Modal.confirm({
      title: `删除${resourceLabel}“${resourceName}”？`,
      content: `此操作会永久删除该${resourceLabel}，且无法恢复。`,
      okText: '确认删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      centered: true,
      className: 'resource-delete-confirm',
      onOk: onConfirm,
    })
  }

  return { approvalCommandRef, confirmDeadlineRef, confirmDetailOpen, confirmOverflowing, confirmRemaining, confirmReq, confirmResourceDeletion, sendConfirm, setConfirmDetailOpen, setConfirmOverflowing, setConfirmRemaining, setConfirmReq } as const
}
