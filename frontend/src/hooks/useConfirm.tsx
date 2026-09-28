import { useCallback, useEffect, useRef, useState } from 'react'
import type { ConfirmDecision, ConfirmRequest } from '../types'
import { Modal } from 'antd'
import { useApiClient } from './useApiClient'

type Deps = ReturnType<typeof useApiClient>

export function useConfirm(deps: Deps) {
  const { wsRef, messageApi } = deps

  const [confirmReq, setConfirmReq] = useState<ConfirmRequest | null>(null)

  const [confirmRemaining, setConfirmRemaining] = useState(0)

  const [confirmDetailOpen, setConfirmDetailOpen] = useState(false)

  const [confirmOverflowing, setConfirmOverflowing] = useState(false)

  // The command box element, held as state via a callback ref rather than a
  // useRef: the bar now has two render sites (the chat composer and the
  // floating bar shown on every other view), so a view switch swaps the
  // element mid-prompt. A useRef never tells the measuring effect that
  // happened; a state-setter ref does.
  const [approvalCommandEl, setApprovalCommandEl] = useState<HTMLDivElement | null>(null)
  const attachApprovalCommand = setApprovalCommandEl

  // Absolute deadline rather than a decremented counter: background tabs get
  // their timers throttled, and a counter would drift behind the server.
  const confirmDeadlineRef = useRef(0)

  // Clear the prompt before sending: a double click on "允许" must not emit two
  // replies for one token, and the bar should not linger while the turn resumes.
  const sendConfirm = useCallback(
    (decision: ConfirmDecision) => {
      const request = confirmReq
      if (!request) return
      const socket = wsRef.current
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        // The decision reached nobody. Clearing the bar here would read as
        // "answered" while the server never heard it: the turn keeps waiting
        // on the deadline and fails closed, so a clicked "允许本次" is what
        // the tool is denied. Keep the bar so the decision can be re-made --
        // a reconnecting browser is re-offered the prompt server-side, with
        // the time that actually remains.
        messageApi.error('连接已断开，决策未送达；恢复连接后可重新决定')
        return
      }
      setConfirmReq(null)
      setConfirmDetailOpen(false)
      socket.send(
        JSON.stringify({
          type: 'confirm_response',
          decision,
          confirmation_token: request.confirmation_token || '',
        }),
      )
    },
    [confirmReq, wsRef, messageApi],
  )

  // Locally drop an expired prompt. The server auto-denies at its own
  // deadline regardless, so there is nothing to answer by the time this can
  // be clicked -- only a dead bar to take off the screen.
  const dismissConfirm = useCallback(() => {
    setConfirmReq(null)
    setConfirmDetailOpen(false)
  }, [])

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
    const element = approvalCommandEl
    if (!element) {
      setConfirmOverflowing(false)
      return
    }
    const measure = () => setConfirmOverflowing(element.scrollHeight > element.clientHeight + 1)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [confirmReq, confirmDetailOpen, approvalCommandEl])

  useEffect(() => {
    if (!confirmReq) return
    const onKeyDown = (event: KeyboardEvent) => {
      // Another layer already consumed this key: the composer's own Esc
      // (close the command popover, clear a "/" draft) and its Cmd/Ctrl+Enter
      // send both call preventDefault(), and without this guard the same
      // keystroke would also answer the approval -- denying a call the user
      // meant to keep deciding on, or approving one while sending a message.
      if (event.defaultPrevented || event.isComposing) return
      // Focus inside an open overlay (dropdown menu, select popup, popover,
      // modal): Esc there means "close the overlay", and the overlay closes
      // without preventing the event, so it would still bubble here.
      const target = event.target
      if (
        target instanceof HTMLElement &&
        target.closest('.ant-modal, .ant-dropdown, .ant-select-dropdown, .ant-popover')
      ) {
        return
      }
      if (event.key === 'Escape') {
        event.preventDefault()
        // Past the deadline the server has already auto-denied, so there is
        // no decision left to send: Esc clears the dead bar locally, the
        // keyboard twin of the "知道了" button, instead of emitting a
        // confirm_response for a token the server will only drop.
        if (confirmDeadlineRef.current - Date.now() <= 0) {
          dismissConfirm()
        } else {
          sendConfirm('deny')
        }
        return
      }
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        // Same deadline: the visible UI no longer offers an allow button
        // after "已自动拒绝", so the shortcut must not quietly "answer"
        // anyway -- the user could believe a late approval landed.
        if (confirmDeadlineRef.current - Date.now() <= 0) return
        event.preventDefault()
        sendConfirm(event.shiftKey && confirmReq.allow_session ? 'allow_session' : 'allow_once')
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [confirmReq, sendConfirm, dismissConfirm])

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

  return { attachApprovalCommand, confirmDeadlineRef, confirmDetailOpen, confirmOverflowing, confirmRemaining, confirmReq, confirmResourceDeletion, dismissConfirm, sendConfirm, setConfirmDetailOpen, setConfirmOverflowing, setConfirmRemaining, setConfirmReq } as const
}
