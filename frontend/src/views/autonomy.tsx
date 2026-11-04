/** The autonomy view: what the agent would do, and the one thing you can answer.
 *
 * Two halves, and the order matters. The list first, because the page exists to
 * be glanced at -- "has it been thinking" is answered by the rows, not by a
 * paragraph. The explanation second, because a person who has never seen this
 * page needs to know that nothing has been executed, and that is a sentence, not
 * a tooltip.
 */

import type { AppCtx } from '../app/AppCtx'
import type { ProposalRow } from '../types'
import { formatDateTime } from '../lib/format'
import {
  Button,
  Card,
  Skeleton,
  Space,
  Tag,
  Tooltip,
} from 'antd'
import {
  CheckOutlined,
  CloseOutlined,
  CompassOutlined,
} from '@ant-design/icons'

const REVERSIBILITY_LABEL: Record<string, string> = {
  reversible: '只读',
  workspace: '写工作区',
  irreversible: '不可逆',
}

const REVERSIBILITY_COLOR: Record<string, string> = {
  reversible: 'green',
  workspace: 'gold',
  irreversible: 'red',
}

const ADMISSION_LABEL: Record<string, string> = {
  auto: '本来会直接做',
  prepare: '本来会准备好等你',
}

const OUTCOME_LABEL: Record<string, string> = {
  queued: '已建任务，等调度器接手',
  running: '正在跑',
  succeeded: '跑成功了',
  failed: '失败了',
  interrupted: '被打断',
  cancelled: '已取消',
  skipped: '被跳过',
  unverified: '跑了但没判定',
}

export function renderAutonomy(ctx: AppCtx) {
  const {
    answeredProposals, decideProposal, loadAutonomy, proposalCounts, proposals,
    proposalsLoaded,
  } = ctx

  const proposed = Number(proposalCounts.proposed || 0)
  const funnel = ctx.funnel
  const expired = Number(proposalCounts.expired || 0)

  const renderRow = (row: ProposalRow) => (
    <Card key={row.id} className="proposal-card">
      <div className="proposal-head">
        <div className="proposal-title">
          <span className="proposal-icon"><CompassOutlined /></span>
          <div>
            <strong>{row.action}</strong>
            <span>{row.reason}</span>
          </div>
        </div>
        <Space className="proposal-tags">
          <Tag color={REVERSIBILITY_COLOR[row.reversibility] || 'default'}>
            {REVERSIBILITY_LABEL[row.reversibility] || row.reversibility}
          </Tag>
          {row.admission && (
            <Tag>{ADMISSION_LABEL[row.admission.answer] || row.admission.answer}</Tag>
          )}
          <Tag>置信度 {row.confidence.toFixed(2)}</Tag>
        </Space>
      </div>
      {/* The verdict's own sentence: "why this would not just run" is the part a
          person is actually deciding about. */}
      {row.admission && (
        <p className="proposal-verdict">{row.admission.reason}</p>
      )}
      {row.admission?.prepared && (
        <p className="proposal-prepared">
          会调用 <code>{row.admission.prepared.tool}</code>
          {Object.keys(row.admission.prepared.arguments || {}).length
            ? '，参数已准备好'
            : ''}
        </p>
      )}
      {/* What the run did, when there was one. A proposal that was approved and
          then ran is only useful if the person who approved it can see how it
          went -- otherwise approving is a leap of faith repeated every time. */}
      {row.outcome && row.outcome.status && (
        <p className={`proposal-outcome proposal-outcome-${row.outcome.status}`}>
          执行结果：{OUTCOME_LABEL[row.outcome.status] || row.outcome.status}
          {row.outcome.summary ? ` · ${row.outcome.summary}` : ''}
        </p>
      )}
      <div className="proposal-footer">
        <span title={formatDateTime(row.created_at, true)}>
          {formatDateTime(row.created_at)}
        </span>
        <Space>
          <Tooltip title="记下：如果它真执行，你会同意">
            <Button size="small" icon={<CheckOutlined />} onClick={() => void decideProposal(row.id, 'accepted')}>
              同意
            </Button>
          </Tooltip>
          <Tooltip title="记下：这件事不该做。它会记住，不再提第二次">
            <Button size="small" icon={<CloseOutlined />} onClick={() => void decideProposal(row.id, 'declined')}>
              不要
            </Button>
          </Tooltip>
        </Space>
      </div>
    </Card>
  )

  // What this page can say, in the order a reader needs it -- and nothing else:
  // what is waiting (the only thing anyone can act on), what was answered, how
  // much was looked at, and the one rule the page exists to state.
  //
  // The previous version said all of that as four paragraphs and three
  // horizontal rules at 11px, with a 200px empty illustration in the middle. On
  // a page whose usual state is "nothing waiting" that is a wall of prose saying
  // nothing, and the record it exists for was the smallest thing on it.
  return (
    <div className="page-view autonomy-view">
      <div className="autonomy-head">
        <div className="autonomy-head-text">
          <span className={`autonomy-state${proposed > 0 ? ' is-waiting' : ''}`}>
            {proposed > 0 ? `${proposed} 条等你答复` : '没有要你答复的事'}
          </span>
          <span className="autonomy-note">
            {proposed > 0
              ? '这些动作会改动工作区之外的东西，它不会自己做。'
              : '它在后台每十分钟看一遍，有需要你点头的才出现在这里。'}
          </span>
        </div>
        {/* The evidence, on the head's line rather than in a footnote: "nothing
            waiting" is only worth trusting if you can see that it looked. */}
        {funnel && funnel.passes > 0 && (
          <div className="autonomy-stat">
            <span>
              近 7 天巡逻 <strong>{funnel.passes}</strong> 趟 · 看过{' '}
              <strong>{funnel.examined}</strong> 次会话
            </span>
            <span>
              提出 <strong>{funnel.proposed}</strong> 条 · 自己做了{' '}
              <strong>{funnel.executed}</strong> 件
            </span>
          </div>
        )}
        <Button size="small" onClick={() => void loadAutonomy()}>
          刷新
        </Button>
      </div>

      {!proposalsLoaded ? (
        <Skeleton active paragraph={{ rows: 3 }} title={false} />
      ) : proposals.length > 0 ? (
        <div className="proposal-list">{proposals.map(renderRow)}</div>
      ) : null}

      {answeredProposals.length > 0 && (
        <section className="autonomy-log">
          <div className="autonomy-log-head">
            已答复
            <span className="autonomy-log-count">{answeredProposals.length}</span>
            {expired > 0 && (
              <span className="autonomy-log-count">已过期 {expired}</span>
            )}
          </div>
          {answeredProposals.map(row => (
            <div key={row.id} className="autonomy-log-row">
              <span className={`autonomy-log-state is-${row.state}`}>
                {row.state === 'acted' ? '已执行' : row.state === 'accepted' ? '同意' : '不要'}
              </span>
              <span className="autonomy-log-action" title={row.action}>
                {row.action}
              </span>
              {row.outcome?.status && (
                <span className={`autonomy-log-outcome is-${row.outcome.status}`}>
                  {OUTCOME_LABEL[row.outcome.status] || row.outcome.status}
                </span>
              )}
              <span className="autonomy-log-at">
                {formatDateTime(row.decided_at || row.created_at)}
              </span>
            </div>
          ))}
        </section>
      )}

      <p className="autonomy-rule">
        只读的动作它自己做，不问你；会改动工作区之外的动作，永远不会自己做。
      </p>
    </div>
  )
}
