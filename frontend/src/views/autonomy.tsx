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
  Empty,
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
  const decided = Number(proposalCounts.accepted || 0) + Number(proposalCounts.declined || 0)
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

  return (
    <div className="page-view autonomy-view">
      <div className="page-head">
        <div>
          <div className="schedule-title-row">
            <span className="proposal-count">
              {proposed > 0 ? `${proposed} 条待答复` : '没有待答复的提议'}
            </span>
            {decided > 0 && <span className="proposal-muted">已答复 {decided}</span>}
            {expired > 0 && <span className="proposal-muted">已过期 {expired}</span>}
          </div>
          <p>
            它找到的、<strong>还没有做</strong>的事。答复只是记录你的判断——这一版不会执行任何一条。
          </p>
          {/* How much was looked at to find these. Without it, "nothing proposed"
              invites the reader to guess whether there was nothing to do. */}
          {funnel && funnel.passes > 0 && (
            <p className="proposal-funnel">
              最近 7 天巡逻 {funnel.passes} 趟，看了 {funnel.examined} 个会话，
              跳过（有人在用 {funnel.skipped_warm} · 太久没动 {funnel.skipped_stale}），
              产出 {funnel.proposed} 条提议。
            </p>
          )}
        </div>
        <Space>
          <Tooltip title="重新读一遍提议">
            <Button onClick={() => void loadAutonomy()}>刷新</Button>
          </Tooltip>
        </Space>
      </div>

      {!proposalsLoaded ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : proposals.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            proposed === 0
              ? '现在没有提议。它在后台每十分钟看一遍会话，有发现会出现在这里——不会执行，也不会打扰你。'
              : '没有需要答复的提议。'
          }
          className="page-empty"
        />
      ) : (
        <div className="proposal-list">{proposals.map(renderRow)}</div>
      )}
      {/* What was answered, with what happened. Collapsed under the queue on
          purpose: the live list is what needs a person, and this is the record. */}
      {answeredProposals.length > 0 && (
        <div className="proposal-answered">
          <div className="proposal-answered-title">已答复</div>
          {answeredProposals.map(row => (
            <div key={row.id} className="proposal-answered-row">
              <span className={`proposal-state proposal-state-${row.state}`}>
                {row.state === 'acted' ? '已执行' : row.state === 'accepted' ? '同意' : '不要'}
              </span>
              <span className="proposal-answered-action">{row.action}</span>
              {row.outcome?.status && (
                <span className={`proposal-outcome proposal-outcome-${row.outcome.status}`}>
                  {OUTCOME_LABEL[row.outcome.status] || row.outcome.status}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
      {/* The honest footnote: a page about autonomy has to say what it is not. */}
      <p className="proposal-footnote">
        这一版是影子模式：巡逻只写记录，<strong>0 次自动执行</strong>。你答复过的建议它不会
        再提第二次；它判断"不可逆"的动作永远只会准备好，不会自己动手。
      </p>
    </div>
  )
}
