/** The automation view. Rendered by the App container from its context object. */

import type { AppCtx } from '../app/AppCtx'
import type { ScheduleInfo } from '../types'
import { WorkflowGraph } from '../components/WorkflowGraph'
import {
  formatDateTime,
  formatFileSize,
  formatScheduleDuration,
} from '../lib/format'
import { markdownToHtml } from '../lib/markdown'
import {
  describeAcceptance,
  describeRunInstruction,
  describeRunProfile,
  describeCascade,
  describeElapsed,
  describeFreshness,
  describeNextRun,
  describeRunTrigger,
  scheduleDeliveryStatusLabel,
  scheduleRunAttentionReason,
  scheduleRunStatusIcon,
  scheduleRunStatusLabel,
  scheduleRunVerdictLabel,
  scheduleRunVerificationDetail,
  scheduleTriggerLabel,
} from '../lib/schedule'
import {
  describeStepTrigger,
  stepsInRunningOrder,
} from '../lib/workflow'
import {
  ApartmentOutlined,
  CheckOutlined,
  DeleteOutlined,
  EditOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  MessageOutlined,
  MoreOutlined,
  ReloadOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SearchOutlined,
  StopOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import {
  Button,
  Card,
  Checkbox,
  Drawer,
  Dropdown,
  Empty,
  Input,
  Skeleton,
  Space,
  Spin,
  Switch,
  Tag,
  Tooltip,
} from 'antd'

/** What a new automation conversation starts with: the questions it will ask anyway.
 *
 * A template rather than a sentence, because the agent's next move is to ask these three
 * things, and a person who can see them answers all three at once instead of one at a
 * time. It is a draft in the composer, not a prompt: the agent may rewrite it.
 */
const NEW_AUTOMATION_SEED = `帮我建一个自动化任务：
- 要做什么：
- 什么时候做（一次 / 每天 / 每周 / 等某个信号）：
- 在哪个项目目录里：`

/** The same door, for a change instead of a creation.
 *
 * The id travels with it because the thing being edited is a row in a store, and
 * "the task called 每天早上看看日志" stops being a unique description the moment somebody
 * makes a second one. The agent still has to quote this request for `schedule_update`,
 * which is why the sentence is a request and not a note.
 */
function editAutomationSeed(row: { kind: string; name: string; task_id: string; workflow_id: string }): string {
  const ids = row.kind === 'chain'
    ? `- 流程 id：${row.workflow_id}`
    : `- 任务 id：${row.task_id}`
  return `请修改这个自动化：
- 名称：${row.name}
${ids}
- 要改的地方：`
}

export function renderSchedules(ctx: AppCtx) {
  const { acknowledgeScheduleRun, attentionByTask, attentionRuns, automationKind, automationQuery, automations, automationsLoaded, automationTab, bulkScheduleAction, editingWorkflow, cancelScheduleRun, clearScheduleAttention, clock, deleteSchedule, deleteWorkflow, filteredSchedules, filteredWorkflows, loadAutomations, loadingView, loadScheduleRuns, loadSchedules, loadWorkflows, openScheduleDetails, permissionProfileLabel, retryScheduleRun, runScheduleNow, runWorkflowNow, scheduleArtifacts, scheduleDetailOpen, scheduleOutputLoading, scheduleQuery, schedulerHealth, schedulerRefreshedAt, schedulerStale, scheduleRunOutput, scheduleRuns, scheduleRunsLoading, schedules, scheduleStatusFilter, selectedSchedule, selectedScheduleIds, selectedScheduleRun, selectedScheduleRunId, selectedScheduleRunTask, setAutomationKind, setAutomationQuery, setAutomationTab, setEditingWorkflowId, setScheduleDetailOpen, setScheduleQuery, setScheduleStatusFilter, setSelectedScheduleIds, setSelectedScheduleRunId, setWorkflowQuery, openConversation, startingWorkflowIds, toggleSchedule, toggleWorkflow, token, unseenFailures, workflowQuery, workflows } = ctx


  /**
   * The list the badge on the navigation is counting.
   *
   * Every row names the task, where the task came from, why the run is asking
   * and what it said when it ended -- because a row that only says "failed"
   * sends the person to the same hunt the badge did. "查看运行" opens the task's
   * history already on that run, not on the newest one: the newest one is
   * usually fine, which is the second half of why the run was hard to find.
   */
  const renderAttention = () => {
    // Looked up by id rather than found per row: the list is the badge's own
    // length, and a find inside its map is the page re-scanning every task
    // for every row on every poll.
    const taskById = new Map(schedules.map(item => [item.id, item]))
    const rows = attentionRuns.map(run => {
      const task = taskById.get(run.task_id)
      const origin = run.workflow_name
        ? `流程「${run.workflow_name}」${run.step_key ? ` · 步骤 ${run.step_key}` : ''}`
        : run.workflow_deleted
          // A step whose workflow is gone still has to be locatable, or the
          // count that includes it is a count with a hole in it.
          ? `原属的流程已删除${run.step_key ? ` · 步骤 ${run.step_key}` : ''}`
          : '独立任务'
      const when = run.finished_at || run.started_at
      return (
        <div className="schedule-attention-row" key={run.run_id}>
          <span className={`schedule-run-status-icon status-${run.status}`}>
            {scheduleRunStatusIcon(run.status)}
          </span>
          <div className="schedule-attention-main">
            <strong>{run.task_name}</strong>
            <span className="schedule-attention-origin">{origin}</span>
            <span className="schedule-attention-reason">
              {scheduleRunAttentionReason(run)}
            </span>
            {run.error && (
              // Truncated by CSS rather than here: the message is the answer to
              // "why", and cutting it in JS would hide the end of a long one.
              <span className="schedule-attention-error">{run.error}</span>
            )}
          </div>
          <span className="schedule-attention-time">
            {when ? describeElapsed(when, clock) : ''}
          </span>
          <Space>
            <Button
              size="small"
              icon={<FileTextOutlined />}
              disabled={!task}
              title={task ? undefined : '任务已被删除，只剩运行记录'}
              onClick={() => task && openScheduleDetails(task, run.run_id)}
            >
              查看运行
            </Button>
            <Button
              size="small"
              icon={<CheckOutlined />}
              onClick={() => acknowledgeScheduleRun(run.task_id, run.run_id)}
            >
              标记已读
            </Button>
          </Space>
        </div>
      )
    })
    return (
      <div className="schedule-attention-list" aria-label="需要查看的运行">
        {attentionRuns.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="没有需要查看的运行"
            className="page-empty"
          />
        ) : (
          rows
        )}
      </div>
    )
  }


  /**
   * The landing list: one row per thing a person manages.
   *
   * A row is a chain when it has several steps and a task when it has one, which is the
   * whole of the difference -- so the row says the shape and the count, and the click
   * opens whichever editor that shape has. The numbers (attention, next run, last
   * status) come from `GET /api/automations`, which computes them from the same
   * snapshot the badge uses; a row that summed its own steps could disagree with it.
   */
  const renderAutomations = () => {
    const needle = automationQuery.trim().toLowerCase()
    const visible = needle
      ? automations.filter(row => row.name.toLowerCase().includes(needle) ||
          (row.description || '').toLowerCase().includes(needle))
      : automations
    // Not `loadingView`: that flag belongs to the task list, and the unified list has
    // its own first load. An empty list is only empty once it has been read.
    if (!automationsLoaded) {
      return <Skeleton active paragraph={{ rows: 6 }} />
    }
    if (visible.length === 0) {
      return (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={needle
            ? '没有符合条件的任务或流程'
            : '还没有自动化。点「对话新建」，说清楚要它什么时候、做什么。'}
          className="page-empty"
        />
      )
    }
    return (
      <div className="schedule-list">
        {visible.map(row => {
          const task = schedules.find(item => item.id === row.task_id)
          const chain = workflows.find(item => item.id === row.workflow_id)
          const open = () => {
            if (row.kind === 'chain') {
              if (chain) setEditingWorkflowId(chain.id)
              return
            }
            if (task) openScheduleDetails(task)
          }
          return (
            <Card
              key={`${row.kind}:${row.id}`}
              hoverable
              role="button"
              tabIndex={0}
              aria-label={`打开 ${row.name}`}
              className={`schedule-card ${row.enabled === false ? 'schedule-card-disabled' : ''}`}
              onClick={open}
              onKeyDown={event => {
                if (event.target !== event.currentTarget) return
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  open()
                }
              }}
            >
              <div className="schedule-card-head">
                <div className="schedule-card-title">
                  <span className="schedule-card-icon">
                    {row.kind === 'chain' ? <ApartmentOutlined /> : <RobotOutlined />}
                  </span>
                  <div>
                    <strong>{row.name}</strong>
                    {/* Status, attention and the next run, on one line under the
                        name. They used to sit at the two far ends of a 1200px
                        row -- the tag beside the switch, the countdown beside
                        the title -- so reading one row meant crossing an empty
                        middle twice. The row now reads left to right: what it
                        is, how it went, when it happens next. */}
                    <span className="schedule-card-meta">
                      {row.attention > 0 && (
                        <Tag color="error" className="schedule-card-attention-tag">
                          {row.attention} 条待处理
                        </Tag>
                      )}
                      {/* The status is the one word a row exists to report, so it
                          cannot share a colour with the word that means its
                          opposite: measured, 执行成功 and 执行失败 rendered as the
                          same grey pill on the same background. #2f7a57 */}
                      {row.last_status ? (
                        <span className={`schedule-status-tag status-${row.last_status}`}>
                          {scheduleRunStatusLabel(row.last_status)}
                        </span>
                      ) : null}
                      <span className="schedule-card-meta-rest">
                        {row.kind === 'chain' ? `流程 · ${row.step_count} 个步骤` : '任务'}
                        {/* A countdown, not a timestamp: the rest of the page says
                            "还有 21 小时", and a row that says "2026-11-03 09:00"
                            makes the reader do the subtraction. `describeNextRun`
                            takes a ScheduleInfo, and the row already carries the
                            two fields it reads. */}
                        {row.next_run_at
                          ? ` · 下次执行：${describeNextRun(
                              { next_run_at: row.next_run_at, trigger_type: row.trigger_type } as ScheduleInfo,
                              clock,
                            )}`
                          : row.trigger_type === 'signal'
                            ? ' · 等待信号触发'
                            : ''}
                      </span>
                    </span>
                  </div>
                </div>
                <Space className="schedule-card-controls">
                  {/* Editing is a conversation too: the row opens one seeded with
                      this automation's name and id, so "改成每周一" is a sentence rather
                      than a form the agent has to be transcribed into. */}
                  <Tooltip title="在对话里修改">
                    <Button
                      type="text"
                      size="small"
                      icon={<EditOutlined />}
                      aria-label={`在对话里修改 ${row.name}`}
                      onClick={event => {
                        event.stopPropagation()
                        void openConversation(editAutomationSeed(row))
                      }}
                    />
                  </Tooltip>
                  {/* The switch sits inside a clickable card, so the click has to
                      stop here: toggling a row must not also open it. */}
                  <Tooltip title={row.enabled === false ? '启用' : '暂停'}>
                    <span onClick={event => event.stopPropagation()}>
                      <Switch
                        size="small"
                        checked={row.enabled !== false}
                        onChange={value => {
                          if (row.kind === 'chain') {
                            if (chain) void toggleWorkflow(chain, value)
                            return
                          }
                          if (task) void toggleSchedule(task, value)
                        }}
                      />
                    </span>
                  </Tooltip>
                </Space>
              </div>
            </Card>
          )
        })}
      </div>
    )
  }

  const renderWorkflows = () => (
    <>
      
      {loadingView && workflows.length === 0 ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : filteredWorkflows.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={workflows.length
            ? '没有符合条件的流程'
            : '还没有流程。流程把几个任务串成一条链——在对话里说「先做这个再做那个」即可。'}
          className="page-empty"
        />
      ) : (
        <div className="workflow-list">
          {filteredWorkflows.map(flow => (
            <Card key={flow.id} className={`workflow-card ${flow.enabled === false ? 'schedule-card-disabled' : ''}`}>
              <div className="schedule-card-head">
                <div className="schedule-card-title">
                  <span className="schedule-card-icon"><ApartmentOutlined /></span>
                  <div>
                    <strong>{flow.name}</strong>
                    <span>
                      {flow.steps.length} 个步骤
                      {flow.description ? ` · ${flow.description}` : ''}
                    </span>
                  </div>
                </div>
                <Space>
                  {(flow.unseen_attention || 0) > 0 && (
                    <Tag color="error">{flow.unseen_attention} 条待处理</Tag>
                  )}
                  <Tooltip title={flow.enabled === false ? '启用这个流程' : '暂停这个流程'}>
                    <Switch
                      size="small"
                      checked={flow.enabled !== false}
                      onChange={value => toggleWorkflow(flow, value)}
                    />
                  </Tooltip>
                  <Tooltip title={schedulerHealth.status === 'online' ? '从现在开始跑一遍入口步骤' : '调度器离线'}>
                    <Button
                      type="text"
                      icon={<ThunderboltOutlined />}
                      aria-label={`立即运行 ${flow.name}`}
                      loading={startingWorkflowIds.includes(flow.id)}
                      disabled={schedulerHealth.status !== 'online' || flow.enabled === false}
                      onClick={() => runWorkflowNow(flow)}
                    />
                  </Tooltip>
                  <Tooltip title="编辑流程">
                    <Tooltip title="在对话里修改">
                        <Button
                          type="text"
                          icon={<EditOutlined />}
                          aria-label={`在对话里修改 ${flow.name}`}
                          onClick={() => void openConversation(editAutomationSeed({
                            kind: 'chain',
                            name: flow.name,
                            task_id: '',
                            workflow_id: flow.id,
                          }))}
                        />
                      </Tooltip>
                  </Tooltip>
                  <Button danger type="text" icon={<DeleteOutlined />} onClick={() => deleteWorkflow(flow)}>删除</Button>
                </Space>
              </div>

              <WorkflowGraph
                nodes={flow.steps.map(step => ({
                  key: step.key,
                  name: step.name || step.key,
                  kind: step.kind,
                  depends_on: step.depends_on || [],
                  status: step.latest_run?.status,
                  taskId: step.task_id,
                }))}
                title={`流程「${flow.name}」的步骤依赖关系`}
                onSelect={node => {
                  const step = flow.steps.find(item => item.key === node.key)
                  const task = step ? schedules.find(item => item.id === step.task_id) : undefined
                  if (task) openScheduleDetails(task)
                }}
              />

              <div className="workflow-step-list">
                {stepsInRunningOrder(flow.steps).map(step => (
                  <div className="workflow-step-row" key={step.key}>
                    <span className={`workflow-step-dot status-${step.latest_run?.status || 'pending'}`}>
                      {scheduleRunStatusIcon(step.latest_run?.status)}
                    </span>
                    <div className="workflow-step-main">
                      <strong>{step.name || step.key}</strong>
                      <span>
                        {step.key} · {step.kind === 'message' ? '提醒' : 'Agent 任务'} · {describeStepTrigger(step)}
                      </span>
                    </div>
                    {(step.unseen_attention || 0) > 0 && (
                      <span className="schedule-run-unseen" aria-label="有未读的运行结果" />
                    )}
                    {step.latest_run && (
                      <span className={`workflow-step-status status-${step.latest_run.status}`}>
                        {scheduleRunStatusLabel(step.latest_run.status, step.latest_run)}
                      </span>
                    )}
                    <Button
                      type="text"
                      size="small"
                      icon={<FileTextOutlined />}
                      disabled={!step.task_id}
                      onClick={() => {
                        // A step *is* a task, so its history is the task drawer the
                        // rest of the page already opens.
                        const task = schedules.find(item => item.id === step.task_id)
                        if (task) openScheduleDetails(task)
                      }}
                    >
                      运行记录
                    </Button>
                  </div>
                ))}
              </div>
            </Card>
          ))}
        </div>
      )}
    </>
  )

  return (
    <div className="page-view schedules-view">
      <div className="page-head schedule-page-head">
        <div>
          {/* The bar above already says 自动化; the row keeps only the two
              facts that change -- whether the scheduler is up, and how old
              this reading is. */}
          <div className="schedule-title-row">
            <span className={`scheduler-health ${schedulerHealth.status === 'online' ? 'online' : 'offline'}`}>
              <i />{schedulerHealth.status === 'online' ? '调度器在线' : '调度器离线'}
            </span>
            <Tooltip
              title={
                schedulerRefreshedAt
                  ? `最近一次同步：${formatDateTime(new Date(schedulerRefreshedAt).toISOString(), true)}`
                  : '尚未取得状态'
              }
            >
              <span className={`schedule-freshness ${schedulerStale ? 'stale' : 'live'}`}>
                <i />{describeFreshness(schedulerStale, schedulerRefreshedAt, clock)}
              </span>
            </Tooltip>
          </div>
          <p>
            {automationTab === 'attention'
              ? '这些运行结束得不好，或者中间有该跑却没跑成的次数，还没有人看过。'
              : automationKind === 'task'
                ? '让 Agent 在指定时间执行，或等某个信号发生后接着执行。'
                : automationKind === 'workflow'
                  ? '把几个任务串成一条链：上一步完成后，下一步才运行。'
                  : '一件事，一步还是多步。一步是一个任务；多步就是一条链，上一步完成后下一步才运行。'}
          </p>
        </div>
        <Space>
          <Tooltip title="刷新运行状态">
            <Button
              aria-label="刷新运行状态"
              icon={<ReloadOutlined />}
              onClick={() => {
                // One refresh for the page: the lists behind each filter, and the
                // unified list, all read the same facts.
                void loadSchedules()
                void loadWorkflows(true)
                void loadAutomations()
              }}
            />
          </Tooltip>
          {automationTab === 'automation' && (
            // Creating is a conversation, not a form. The page does not know what an
            // automation should look like for this person; the agent does, and it can
            // ask one question at a time instead of showing eleven fields and hoping.
            // So this puts the first sentence in the box of the one conversation -- it
            // used to start a *new* session, which left the real conversation behind.
            <Tooltip title="在对话里说清楚要什么，agent 会把任务或流程建好">
              <Button
                type="primary"
                icon={<MessageOutlined />}
                onClick={() => void openConversation(NEW_AUTOMATION_SEED)}
              >
                对话新建
              </Button>
            </Tooltip>
          )}
          {/* Nothing to create from the attention list: it is a list of things
              that already happened, and a "new" button here would suggest that
              making another one is how you deal with these. */}
          {automationTab === 'attention' && unseenFailures > 0 && (
            <Tooltip title="把这些运行都标记为已查看">
              <Button icon={<CheckOutlined />} onClick={() => clearScheduleAttention()}>
                全部标记已读
              </Button>
            </Tooltip>
          )}
        </Space>
      </div>
      <div className="schedule-tabs" role="tablist" aria-label="自动化视图">
        {/* First, because the number on the navigation points here. Ordering it
            after the two lists is what made the badge a dead end: the page it
            opened on showed forty tasks for a count of two, and finding which
            two meant opening them one at a time. */}
        <button
          type="button"
          role="tab"
          aria-selected={automationTab === 'attention'}
          className={automationTab === 'attention' ? 'active' : ''}
          onClick={() => setAutomationTab('attention')}
        >
          待处理
          {unseenFailures > 0 && (
            // The number the navigation badge is showing. Same payload, same
            // total -- the two are meant to be read as one figure.
            <span className="schedule-tab-badge" title={`${unseenFailures} 次运行需要查看`}>
              {unseenFailures > 99 ? '99+' : unseenFailures}
            </span>
          )}
        </button>
        {/* One list. "Task or workflow" was a choice offered on arrival, and the two
            answers were the same mechanism with one step or several (see
            agent/scheduler/automations.py). The split survives as a filter in the
            toolbar, where it belongs: it narrows a list rather than choosing a world. */}
        <button
          type="button"
          role="tab"
          aria-selected={automationTab === 'automation'}
          className={automationTab === 'automation' ? 'active' : ''}
          onClick={() => setAutomationTab('automation')}
        >
          自动化
        </button>
      </div>
      <div className="schedule-toolbar">
        {automationTab === 'attention' ? (
          /* No search box: the list is the answer, not the place you go to
             look for one, and filtering it would let a run be hidden from the
             very count that is on the tab. */
          <span className="schedule-attention-count">
            {unseenFailures > 0
              ? `${unseenFailures} 次运行需要查看`
              : '没有需要查看的运行'}
          </span>
        ) : automationKind === 'task' ? (
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索任务、项目目录或执行内容"
            value={scheduleQuery}
            onChange={event => setScheduleQuery(event.target.value)}
            className="workspace-search"
          />
        ) : automationKind === 'workflow' ? (
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索流程、步骤名称或执行内容"
            value={workflowQuery}
            onChange={event => setWorkflowQuery(event.target.value)}
            className="workspace-search"
          />
        ) : (
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索任务或流程"
            value={automationQuery}
            onChange={event => setAutomationQuery(event.target.value)}
            className="workspace-search"
          />
        )}
        {/* The old tabs, as a filter: they narrow the one list instead of picking
            which list exists. */}
        {automationTab === 'automation' && (
          <div className="schedule-filter" role="group" aria-label="自动化类型筛选">
            {[
              ['all', '全部'],
              ['task', '任务'],
              ['workflow', '流程'],
            ].map(([value, label]) => (
              <button
                key={value}
                type="button"
                className={automationKind === value ? 'active' : ''}
                onClick={() => setAutomationKind(value as 'all' | 'task' | 'workflow')}
              >
                {label}
              </button>
            ))}
          </div>
        )}
        {automationTab === 'automation' && automationKind === 'task' && (
          <>
            <div className="schedule-filter" role="group" aria-label="任务状态筛选">
              {[
                ['all', '全部'],
                ['running', '运行中'],
                ['failed', '失败'],
                ['paused', '已暂停'],
              ].map(([value, label]) => (
                <button key={value} type="button" className={scheduleStatusFilter === value ? 'active' : ''} onClick={() => setScheduleStatusFilter(value)}>{label}</button>
              ))}
            </div>
            {selectedScheduleIds.length > 0 && (
              <Space className="schedule-bulk-actions">
                <span>已选 {selectedScheduleIds.length} 个</span>
                <Button size="small" onClick={() => bulkScheduleAction('enable')}>启用</Button>
                <Button size="small" onClick={() => bulkScheduleAction('disable')}>暂停</Button>
                <Button size="small" danger icon={<DeleteOutlined />} onClick={() => bulkScheduleAction('delete')}>删除</Button>
              </Space>
            )}
          </>
        )}
      </div>
      {/* Not "定时任务" any more, because the form no longer only offers
          times -- calling it that while the user is choosing a signal to wait
          for would be the form contradicting itself. */}
            {/* A chain in this page is read-only: the steps it is made of, and when each
          one runs. Changing it is a conversation -- the pencil on its row opens one
          seeded with this chain's name and id -- because a graph editor is a second
          way to describe the same thing, and the one that drifts. */}
      <Drawer
        open={!!editingWorkflow}
        title={editingWorkflow ? `流程：${editingWorkflow.name}` : '流程'}
        onClose={() => setEditingWorkflowId(null)}
        width={560}
        className="schedule-detail"
      >
        {editingWorkflow && (
          <div className="workflow-steps">
            {(editingWorkflow.steps || []).map(step => (
              <Card key={step.key} size="small" className="workflow-step-card">
                <div className="schedule-card-head">
                  <div className="schedule-card-title">
                    <span className="schedule-card-icon"><ApartmentOutlined /></span>
                    <div>
                      <strong>{step.name || step.key}</strong>
                      <span>
                        {step.kind === 'agent_prompt' ? 'Agent 任务' : '提醒'}
                        {step.depends_on.length
                          ? ` · 在 ${step.depends_on.join('、')} 之后`
                          : ' · 入口步骤'}
                      </span>
                    </div>
                  </div>
                  {step.latest_run?.status && (
                    <Tag>{scheduleRunStatusLabel(step.latest_run.status, step.latest_run)}</Tag>
                  )}
                </div>
              </Card>
            ))}
            <p className="schedule-detail-note">
              流程由 agent 在对话里修改：点这一行的铅笔，或新建一个会话说明要改什么。
            </p>
          </div>
        )}
      </Drawer>
      <Drawer
        open={scheduleDetailOpen}
        width="min(880px, calc(100vw - 20px))"
        title={selectedSchedule?.name || '任务运行详情'}
        onClose={() => setScheduleDetailOpen(false)}
        className="schedule-detail-drawer"
        extra={selectedSchedule && (
          <Space>
            <Button icon={<ThunderboltOutlined />} onClick={() => runScheduleNow(selectedSchedule)} disabled={schedulerHealth.status !== 'online' || !!selectedSchedule.active_run_id}>立即运行</Button>
            <Button
              icon={<EditOutlined />}
              onClick={() => void openConversation(editAutomationSeed({
                kind: 'task',
                name: selectedSchedule.name,
                task_id: selectedSchedule.id,
                workflow_id: '',
              }))}
            >
              在对话里改
            </Button>
            <Dropdown
              menu={{ items: [
                { key: 'delete', icon: <DeleteOutlined />, label: '删除任务', danger: true, disabled: !!selectedSchedule.active_run_id, onClick: () => deleteSchedule(selectedSchedule) },
              ] }}
            >
              <Button aria-label="更多任务操作" icon={<MoreOutlined />} />
            </Dropdown>
            <Tooltip title="刷新运行记录">
              <Button
                aria-label="刷新运行记录"
                icon={<ReloadOutlined />}
                loading={scheduleRunsLoading}
                onClick={() => loadScheduleRuns(selectedSchedule.id)}
              />
            </Tooltip>
          </Space>
        )}
      >
        {selectedSchedule && (
          <div className="schedule-detail">
            <div className="schedule-detail-overview">
              <div className="schedule-detail-overview-main">
                <span className="schedule-card-icon">
                  {selectedSchedule.kind === 'agent_prompt' ? <RobotOutlined /> : <MessageOutlined />}
                </span>
                <div>
                  <div className="schedule-detail-tags">
                    <Tag>{selectedSchedule.kind === 'agent_prompt' ? 'Agent 任务' : selectedSchedule.kind === 'message' ? '定时提醒' : '系统任务'}</Tag>
                    <Tag>{scheduleTriggerLabel(selectedSchedule, schedules)}</Tag>
                    <Tag>{selectedSchedule.enabled === false ? '已暂停' : '已启用'}</Tag>
                    {selectedSchedule.context_policy && <Tag>{selectedSchedule.context_policy === 'stateless' ? '独立上下文' : selectedSchedule.context_policy === 'task_history' ? '任务历史' : '共享记忆'}</Tag>}
                    {selectedSchedule.kind === 'agent_prompt' && (
                      <Tag>{permissionProfileLabel(selectedSchedule.permission_profile)}</Tag>
                    )}
                    <Tag>{selectedSchedule.delivery_mode === 'channel' ? '投递：飞书' : '投递：本机'}</Tag>
                  </div>
                  <p>{selectedSchedule.kind === 'agent_prompt'
                    ? selectedSchedule.payload?.prompt
                    : selectedSchedule.kind === 'system_job'
                      ? selectedSchedule.payload?.job_name
                      : selectedSchedule.payload?.message_text}</p>
                  {selectedSchedule.workspace_root && (
                    <div className="schedule-workspace"><FolderOpenOutlined />{selectedSchedule.workspace_root}</div>
                  )}
                  {/* What a run of this task is judged by, said before it ever
                      runs.  A criterion nobody was shown is indistinguishable
                      from a run that failed for no reason. */}
                  {describeAcceptance(selectedSchedule.acceptance) && (
                    <div className="schedule-workspace schedule-acceptance">
                      <SafetyCertificateOutlined />判定依据：{describeAcceptance(selectedSchedule.acceptance)}
                    </div>
                  )}
                  {/* Why this task exists, in the asker's own words.  Shown so
                      a task that appeared without anyone asking for it can be
                      noticed for what it is -- which is the whole reason the
                      sentence is recorded. */}
                  {String(selectedSchedule.request_quote || '').trim() && (
                    <div className="schedule-workspace schedule-request-quote">
                      <MessageOutlined />来自：{String(selectedSchedule.request_quote).trim()}
                    </div>
                  )}
                </div>
              </div>
              <div className="schedule-detail-next">
                <small>下次执行</small>
                <strong>{describeNextRun(selectedSchedule, clock)}</strong>
                {/* The countdown answers "when, from now"; this answers "when",
                    which is the question when the two of you disagree about
                    what time it is. */}
                {selectedSchedule.next_run_at && (
                  <span>{formatDateTime(selectedSchedule.next_run_at, true)}</span>
                )}
              </div>
            </div>

            {scheduleRunsLoading && scheduleRuns.length === 0 ? (
              <div className="schedule-detail-loading"><Spin /><span>正在读取运行记录</span></div>
            ) : scheduleRuns.length === 0 ? (
              <Empty description="该任务尚未执行" className="schedule-runs-empty" />
            ) : (
              <div className="schedule-detail-grid">
                <aside className="schedule-run-list" aria-label="运行历史">
                  <div className="schedule-detail-section-title">
                    <strong>运行历史</strong>
                    <span className="schedule-run-history-tail">
                      {scheduleRuns.length} 次
                      {(selectedSchedule.unseen_attention || 0) > 0 && (
                        <Button
                          type="link"
                          size="small"
                          onClick={() => clearScheduleAttention(selectedSchedule.id)}
                        >全部标记已读</Button>
                      )}
                    </span>
                  </div>
                  <div className="schedule-run-items">
                    {scheduleRuns.map(run => (
                      <button
                        key={run.id}
                        type="button"
                        className={`schedule-run-item ${selectedScheduleRunId === run.id ? 'active' : ''}`}
                        onClick={() => setSelectedScheduleRunId(run.id)}
                      >
                        <span className={`schedule-run-status-icon status-${run.status}`}>
                          {scheduleRunStatusIcon(run.status)}
                        </span>
                        <span className="schedule-run-item-main">
                          <strong>{scheduleRunStatusLabel(run.status, run)}</strong>
                          <small>{run.started_at ? formatDateTime(run.started_at, true) : '等待开始'}</small>
                        </span>
                        {run.needs_attention && (
                          <span
                            className="schedule-run-unseen"
                            title={scheduleRunAttentionReason(run)}
                            aria-label={scheduleRunAttentionReason(run)}
                          />
                        )}
                        {(run.missed_count || 0) > 0 && (
                          <span
                            className="schedule-run-missed"
                            title={`本次运行前有 ${run.missed_count} 次计划未能执行`}
                          >跳过 {run.missed_count} 次</span>
                        )}
                        <span className="schedule-run-duration">{formatScheduleDuration(run.duration_ms)}</span>
                      </button>
                    ))}
                  </div>
                </aside>

                <section className="schedule-run-detail" aria-label="运行结果">
                  {selectedScheduleRun && (
                    <>
                      <div className="schedule-run-detail-head">
                        <div>
                          <span className={`schedule-run-status status-${selectedScheduleRun.status}`}>
                            {scheduleRunStatusIcon(selectedScheduleRun.status)}
                            {scheduleRunStatusLabel(selectedScheduleRun.status, selectedScheduleRun)}
                          </span>
                          <div
                            className="schedule-run-summary markdown"
                            dangerouslySetInnerHTML={{ __html: markdownToHtml(selectedScheduleRun.summary || '暂无运行摘要') }}
                          />
                        </div>
                        <Space>
                        {selectedScheduleRun.needs_attention && (
                          <Button
                            size="small"
                            icon={<CheckOutlined />}
                            title={scheduleRunAttentionReason(selectedScheduleRun)}
                            onClick={() => acknowledgeScheduleRun(selectedSchedule.id, selectedScheduleRun.id)}
                          >标记已读</Button>
                        )}
                        {selectedScheduleRun.status === 'running' && (
                          <Button danger size="small" icon={<StopOutlined />} loading={!!selectedScheduleRun.cancel_requested_at} onClick={() => cancelScheduleRun(selectedSchedule, selectedScheduleRun)}>
                            {selectedScheduleRun.cancel_requested_at ? '正在取消' : '取消运行'}
                          </Button>
                        )}
                        {selectedScheduleRun.status !== 'running' && (
                          <Dropdown menu={{ items: [
                            { key: 'snapshot', label: '使用本次运行配置', onClick: () => retryScheduleRun(selectedSchedule, selectedScheduleRun, false) },
                            { key: 'latest', label: '使用任务当前配置', onClick: () => retryScheduleRun(selectedSchedule, selectedScheduleRun, true) },
                          ] }}>
                            <Button size="small" icon={<ReloadOutlined />} disabled={!!selectedSchedule.active_run_id}>重试</Button>
                          </Dropdown>
                        )}
                        {scheduleRunOutput?.available && scheduleRunOutput.output_url && (
                          <Button
                            size="small"
                            icon={<FileTextOutlined />}
                            href={`${scheduleRunOutput.output_url}${token ? `${scheduleRunOutput.output_url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}` : ''}`}
                            target="_blank"
                          >打开原始文件</Button>
                        )}
                        </Space>
                      </div>

                      <div className="schedule-run-meta">
                        {/* A signal-triggered run has no planned time -- the
                            value here is when the signal arrived, and calling
                            that "计划时间" would imply a schedule it never had. */}
                        {/* All three carry seconds, deliberately: this row sits
                            beside "执行耗时 4 分 37 秒", and a person checks
                            that number by subtracting 开始 from 完成. Dropping
                            seconds on any one of them makes the row ragged and
                            the subtraction impossible. */}
                        <div><small>{selectedScheduleRun.trigger_source?.startsWith('signal:') ? '触发时间' : '计划时间'}</small><span>{formatDateTime(selectedScheduleRun.scheduled_for, true)}</span></div>
                        <div><small>开始时间</small><span>{formatDateTime(selectedScheduleRun.started_at, true)}</span></div>
                        <div><small>完成时间</small><span>{formatDateTime(selectedScheduleRun.finished_at, true)}</span></div>
                        <div><small>执行耗时</small><span>{formatScheduleDuration(selectedScheduleRun.duration_ms)}</span></div>
                        <div><small>触发方式</small><span>{describeRunTrigger(selectedScheduleRun, schedules)}</span></div>
                        {describeCascade(selectedScheduleRun) && (
                          <div><small>信号链</small><span>{describeCascade(selectedScheduleRun)}</span></div>
                        )}
                        <div><small>运行模型</small><span>{selectedScheduleRun.config_snapshot?.model_override || '默认模型'}</span></div>
                      </div>

                      {/* What this run was told, from its own snapshot. The
                          task's definition may have changed since; the words
                          below are the ones that produced what follows. */}
                      {describeRunInstruction(selectedScheduleRun) && (
                        <div className="schedule-run-instruction">
                          <div className="schedule-output-head">
                            <strong>这次运行的指令</strong>
                            {describeRunProfile(selectedScheduleRun) && (
                              <span>{permissionProfileLabel(describeRunProfile(selectedScheduleRun))}</span>
                            )}
                          </div>
                          <pre>{describeRunInstruction(selectedScheduleRun)}</pre>
                        </div>
                      )}

                      {scheduleRunVerdictLabel(selectedScheduleRun) && (
                        <div className={`schedule-run-verdict verdict-${selectedScheduleRun.verdict || 'unknown'}`}>
                          <strong>{scheduleRunVerdictLabel(selectedScheduleRun)}</strong>
                          {scheduleRunVerificationDetail(selectedScheduleRun) && (
                            <pre>{scheduleRunVerificationDetail(selectedScheduleRun)}</pre>
                          )}
                          {describeAcceptance(selectedScheduleRunTask?.acceptance) && (
                            <small>
                              判定依据：{describeAcceptance(selectedScheduleRunTask?.acceptance)}
                            </small>
                          )}
                        </div>
                      )}

                      {selectedScheduleRun.error && (
                        <div className="schedule-run-error">
                          {selectedScheduleRun.error_reason ? (
                            <>
                              {/* The classification first, the provider's own
                                  words second: `connection lost mid-reply
                                  (unexpected EOF)` is not a sentence a person
                                  can act on, but it is what a support thread
                                  needs, so both are here. */}
                              <strong>
                                {selectedScheduleRun.error_reason.label}
                                {selectedScheduleRun.error_reason.transient && (
                                  <span className="schedule-run-error-retry">会自动重试一次</span>
                                )}
                              </strong>
                              {selectedScheduleRun.error_reason.hint && (
                                <p className="schedule-run-error-hint">
                                  {selectedScheduleRun.error_reason.hint}
                                </p>
                              )}
                              <pre>{selectedScheduleRun.error}</pre>
                            </>
                          ) : (
                            <>
                              <strong>执行错误</strong>
                              <pre>{selectedScheduleRun.error}</pre>
                            </>
                          )}
                        </div>
                      )}

                      <div className="schedule-output-head">
                        <strong>完整输出</strong>
                        {selectedScheduleRun.delivery_status && <span>{scheduleDeliveryStatusLabel(selectedScheduleRun.delivery_status)}</span>}
                      </div>
                      {selectedScheduleRun.status === 'running' ? (
                        <div className="schedule-output-state"><Spin size="small" /><span>任务正在执行，结果会自动刷新</span></div>
                      ) : scheduleOutputLoading ? (
                        <div className="schedule-output-state"><Spin size="small" /><span>正在加载输出</span></div>
                      ) : scheduleRunOutput?.available ? (
                        <>
                          {scheduleRunOutput.truncated && <div className="schedule-output-notice">输出较长，页面仅展示前 2 MB，可打开原始文件查看全部内容。</div>}
                          <div
                            className="schedule-output markdown"
                            dangerouslySetInnerHTML={{ __html: markdownToHtml(scheduleRunOutput.content) }}
                          />
                        </>
                      ) : (
                        <div className="schedule-output-state muted">本次运行没有可展示的文本输出</div>
                      )}
                      {scheduleArtifacts.length > 0 && (
                        <div className="schedule-artifacts">
                          <div className="schedule-output-head">
                            <strong>输出文件</strong>
                            <span>{scheduleArtifacts.length} 个</span>
                          </div>
                          <div className="schedule-artifact-list">
                            {scheduleArtifacts.map(artifact => {
                              const url = `${artifact.url}${token ? `?token=${encodeURIComponent(token)}` : ''}`
                              return (
                                <a key={artifact.path} href={url} target="_blank" rel="noreferrer" className="schedule-artifact-item">
                                  {artifact.mime_type.startsWith('image/')
                                    ? <img src={url} alt="" />
                                    : <span><FileTextOutlined /></span>}
                                  <div><strong>{artifact.name}</strong><small>{formatFileSize(artifact.size_bytes)}</small></div>
                                </a>
                              )
                            })}
                          </div>
                        </div>
                      )}
                    </>
                  )}
                </section>
              </div>
            )}
          </div>
        )}
      </Drawer>
      {automationTab === 'attention' ? renderAttention() : automationKind === 'workflow' ? renderWorkflows() : automationKind === 'all' ? renderAutomations() : (
      loadingView ? <Skeleton active paragraph={{ rows: 6 }} /> : filteredSchedules.length === 0 ? <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={scheduleQuery.trim() || scheduleStatusFilter !== 'all'
            ? '没有符合条件的任务'
            : '还没有任务。任务由 agent 在对话里创建。'}
          className="page-empty"
        /> : (
        <div className="schedule-list">
          {filteredSchedules.map(task => {
            const description = task.kind === 'agent_prompt'
              ? task.payload?.prompt
              : task.kind === 'system_job'
                ? task.payload?.job_name
                : task.payload?.message_text
            const latestRun = task.latest_run
            // A step of a live workflow never reaches this list -- the filter
            // that builds it is what decides that -- so a task that still
            // carries a workflow_id here is exactly a step whose workflow is
            // gone. Deleting a workflow leaves its steps behind on purpose:
            // their run history is the record that it ran. From that moment
            // nothing owns them -- no save will rewrite them and no graph
            // knows they are steps -- so they are ordinary disabled tasks, and
            // the only thing left to do with one is delete it. That deletion
            // used to be refused by a pointer to a workflow nobody could open.
            const orphanedStep = !!task.workflow_id
            return (
              <Card
                key={task.id}
                hoverable
                role="button"
                tabIndex={0}
                aria-label={`查看 ${task.name} 的运行记录`}
                className={`schedule-card ${task.enabled === false ? 'schedule-card-disabled' : ''}`}
                onClick={() => openScheduleDetails(task)}
                onKeyDown={event => {
                  if (event.target !== event.currentTarget) return
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    openScheduleDetails(task)
                  }
                }}
              >
                <div className="schedule-card-head">
                  <div className="schedule-card-title">
                    <span className="schedule-card-icon">{task.kind === 'agent_prompt' ? <RobotOutlined /> : <MessageOutlined />}</span>
                    <div>
                      <strong>{task.name}</strong>
                      <span>{task.kind === 'agent_prompt' ? 'Agent 任务' : task.kind === 'message' ? '定时提醒' : '系统任务'}</span>
                    </div>
                    {/* The same number the navigation badge is counting, put
                        where the task is. Without it the count lived only on
                        the nav and on nothing it was counting, so the two
                        could never be checked against each other. */}
                    {(task.unseen_attention || 0) > 0 && (
                      <button
                        type="button"
                        className="schedule-card-attention"
                        title={`${task.unseen_attention} 次运行需要查看`}
                        onClick={event => {
                          event.stopPropagation()
                          openScheduleDetails(task, attentionByTask.get(task.id))
                        }}
                      >
                        {task.unseen_attention} 条待查看
                      </button>
                    )}
                  </div>
                  <Space onClick={event => event.stopPropagation()}>
                    <Checkbox
                      aria-label={`选择 ${task.name}`}
                      checked={selectedScheduleIds.includes(task.id)}
                      onChange={event => setSelectedScheduleIds(prev => event.target.checked
                        ? [...prev, task.id]
                        : prev.filter(id => id !== task.id))}
                    />
                    <Switch size="small" checked={task.enabled !== false} onChange={value => toggleSchedule(task, value)} />
                    <Tooltip title={schedulerHealth.status === 'online' ? '立即运行' : '调度器离线'}><Button type="text" icon={<ThunderboltOutlined />} aria-label={`立即运行 ${task.name}`} disabled={schedulerHealth.status !== 'online' || !!task.active_run_id} onClick={() => runScheduleNow(task)} /></Tooltip>
                    <Tooltip title="在对话里修改"><Button type="text" icon={<EditOutlined />} aria-label={`在对话里修改 ${task.name}`} onClick={() => void openConversation(editAutomationSeed({ kind: 'task', name: task.name, task_id: task.id, workflow_id: '' }))} /></Tooltip>
                    <Button danger type="text" icon={<DeleteOutlined />} onClick={() => deleteSchedule(task)}>删除</Button>
                  </Space>
                </div>
                {orphanedStep ? (
                  <div className="schedule-step-origin">
                    <ApartmentOutlined />
                    <span>
                      原属的流程已删除{task.step_key ? ` · 步骤 ${task.step_key}` : ''}
                    </span>
                  </div>
                ) : null}
                <p className="schedule-card-description">{description || '暂无任务描述'}</p>
                <div className="schedule-card-footer">
                  <span className={`schedule-run-status status-${latestRun?.status || 'pending'}`}>
                    {scheduleRunStatusIcon(latestRun?.status)}
                    {scheduleRunStatusLabel(latestRun?.status, latestRun)}
                  </span>
                  {latestRun?.duration_ms !== null && latestRun?.duration_ms !== undefined && (
                    <span>耗时 {formatScheduleDuration(latestRun.duration_ms)}</span>
                  )}
                  {/* Both read as distances from now, so the two agree about
                      what they are telling you. The exact times are one hover
                      away rather than two lines of arithmetic apart. */}
                  <span title={task.next_run_at ? formatDateTime(task.next_run_at, true) : undefined}>
                    下次执行：{describeNextRun(task, clock)}
                  </span>
                  {task.last_run_at && (
                    <span title={formatDateTime(task.last_run_at, true)}>
                      上次执行：{describeElapsed(task.last_run_at, clock)}
                    </span>
                  )}
                  <Button type="text" size="small" icon={<FileTextOutlined />} className="schedule-card-detail-button">运行记录</Button>
                </div>
              </Card>
            )
          })}
        </div>
      ))}
    </div>
  )
}
