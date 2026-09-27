/** The tool-approval bar.
 *
 * One component, two render sites. In the chat view it sits in the composer
 * stack, right above the input, where the conversation that prompted the
 * approval is still visible. Outside the chat view the App renders it as a
 * floating bar: the server-side deadline keeps running no matter which page
 * the user is looking at, and before this existed a prompt raised while they
 * browsed sessions or schedules counted down invisibly and was auto-denied.
 */

import { CONFIRM_RISK_LABELS } from '../constants'
import { confirmRisk, confirmToolLabel } from '../lib/tools'
import type { ConfirmDecision, ConfirmRequest } from '../types'
import { DownOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { Button } from 'antd'

export interface ApprovalBarProps {
  confirmReq: ConfirmRequest
  /** Seconds left on the server-side deadline; 0 means it has expired. */
  remaining: number
  detailOpen: boolean
  overflowing: boolean
  /** Callback ref: the measured element differs per render site, so the
   * measuring effect needs to hear when it is swapped, not just read it. */
  commandRef: (element: HTMLDivElement | null) => void
  onToggleDetail: () => void
  onDecide: (decision: ConfirmDecision) => void
  /** Locally drop an expired prompt. The server has already auto-denied it. */
  onDismiss: () => void
}

export function ApprovalBar({
  confirmReq,
  remaining,
  detailOpen,
  overflowing,
  commandRef,
  onToggleDetail,
  onDecide,
  onDismiss,
}: ApprovalBarProps) {
  const risk = confirmRisk(confirmReq.risk_level)
  const timedOut = remaining <= 0
  const commandText = confirmReq.command || '（无可显示内容）'
  const showDetailToggle = overflowing || detailOpen
  return (
    <div
      className={`approval-bar approval-risk-${risk}`}
      role="alertdialog"
      aria-label="工具审批"
      aria-live="assertive"
    >
      <div className="approval-head">
        <SafetyCertificateOutlined className="approval-icon" />
        <span className="approval-title">需要你的批准</span>
        <span className={`approval-risk-tag approval-risk-tag-${risk}`}>
          {CONFIRM_RISK_LABELS[risk]}
        </span>
        <span className="approval-tool">{confirmToolLabel(confirmReq.name)}</span>
        <span className={`approval-timer ${timedOut ? 'expired' : ''}`}>
          {timedOut ? '已自动拒绝' : `${remaining}s 后自动拒绝`}
        </span>
      </div>
      {confirmReq.reason && (
        <div className="approval-reason">{confirmReq.reason}</div>
      )}
      <div
        className={`approval-command ${detailOpen ? 'expanded' : ''}`}
        ref={commandRef}
      >
        <code>{commandText}</code>
      </div>
      {showDetailToggle && (
        <button
          type="button"
          className="approval-detail-toggle"
          onClick={onToggleDetail}
        >
          {detailOpen ? '收起命令' : '展开完整命令'}
          <DownOutlined rotate={detailOpen ? 180 : 0} />
        </button>
      )}
      <div className="approval-actions">
        {timedOut ? (
          <>
            <span className="approval-hints">超时未批准，本次调用已被拒绝</span>
            <Button size="small" onClick={onDismiss}>
              知道了
            </Button>
          </>
        ) : (
          <>
            <span className="approval-hints">
              <kbd>Esc</kbd> 拒绝
              <span className="approval-hint-sep" />
              <kbd>⌘</kbd><kbd>↵</kbd> 允许本次
              {confirmReq.allow_session && (
                <>
                  <span className="approval-hint-sep" />
                  <kbd>⌘</kbd><kbd>⇧</kbd><kbd>↵</kbd> 本会话总是允许
                </>
              )}
            </span>
            <Button size="small" onClick={() => onDecide('deny')}>
              拒绝
            </Button>
            {confirmReq.allow_session && (
              <Button
                size="small"
                onClick={() => onDecide('allow_session')}
              >
                本会话总是允许
              </Button>
            )}
            <Button
              size="small"
              type="primary"
              onClick={() => onDecide('allow_once')}
            >
              允许本次
            </Button>
          </>
        )}
      </div>
    </div>
  )
}
