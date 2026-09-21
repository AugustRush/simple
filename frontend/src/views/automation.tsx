/** The automation view. Rendered by the App container from its context object. */

import type { AppCtx } from '../app/AppCtx'
import { WorkflowGraph } from '../components/WorkflowGraph'
import { WEEKDAY_OPTIONS } from '../constants'
import {
  compactWorkspacePath,
  formatDateTime,
  formatFileSize,
  formatScheduleDuration,
} from '../lib/format'
import { markdownToHtml } from '../lib/markdown'
import {
  describeAcceptance,
  describeCascade,
  describeElapsed,
  describeFreshness,
  describeNextRun,
  describeRunTrigger,
  describeSignalName,
  scheduleDeliveryStatusLabel,
  scheduleRunAttentionReason,
  scheduleRunStatusIcon,
  scheduleRunStatusLabel,
  scheduleRunVerdictLabel,
  scheduleRunVerificationDetail,
  scheduleTimeValue,
  scheduleTriggerLabel,
} from '../lib/schedule'
import {
  cleanStepKeys,
  defaultWorkflowTrigger,
  describeChainTrigger,
  describeStepTrigger,
  planWorkflowStepMove,
  stepsInRunningOrder,
  storedEntryStep,
} from '../lib/workflow'
import type { WorkflowTriggerDraft } from '../types'
import {
  ApartmentOutlined,
  ArrowDownOutlined,
  ArrowUpOutlined,
  CheckOutlined,
  ClockCircleOutlined,
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  MessageOutlined,
  MoreOutlined,
  PlusOutlined,
  ReloadOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SaveOutlined,
  SearchOutlined,
  SendOutlined,
  StopOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import {
  AutoComplete,
  Button,
  Card,
  Checkbox,
  DatePicker,
  Drawer,
  Dropdown,
  Empty,
  Input,
  InputNumber,
  Modal,
  Select,
  Skeleton,
  Space,
  Spin,
  Switch,
  Tag,
  TimePicker,
  Tooltip,
} from 'antd'
import dayjs from 'dayjs'

const { TextArea } = Input

export function createAutomationView(ctx: AppCtx) {
  const { acknowledgeScheduleRun, activePermissionProfile, addWorkflowStep, attentionByTask, attentionRuns, automationTab, bulkScheduleAction, cancelScheduleRun, changeWorkflowStepUpstreams, clearScheduleAttention, clock, deleteSchedule, deleteWorkflow, duplicateSchedule, editingScheduleId, editingStep, editingWorkflow, editingWorkflowId, feishuChats, feishuChatsError, feishuChatsLoaded, feishuChatsLoading, feishuTesting, filteredSchedules, filteredWorkflows, insertWorkflowStepAfter, loadFeishuChats, loadingView, loadScheduleRuns, loadSchedules, loadWorkflows, modelOptions, moveWorkflowStep, openCreateSchedule, openCreateWorkflow, openEditSchedule, openEditWorkflow, openScheduleDetails, openStepDetails, patchWorkflowStep, permissionProfileLabel, permissionProfileOptions, pickDirectory, pickingDirectory, recentWorkspaceRoots, removeWorkflowStep, retryScheduleRun, runScheduleNow, runWorkflowNow, saveSchedule, saveWorkflow, scheduleArtifacts, scheduleDetailOpen, scheduleDraft, scheduleModalOpen, scheduleOutputLoading, schedulePreview, schedulePreviewError, scheduleQuery, schedulerHealth, schedulerRefreshedAt, schedulerStale, scheduleRunOutput, scheduleRuns, scheduleRunsLoading, schedules, scheduleSaving, scheduleStatusFilter, selectedSchedule, selectedScheduleIds, selectedScheduleRun, selectedScheduleRunId, selectedScheduleRunTask, sendFeishuTest, setAutomationTab, setEditingScheduleId, setEditingWorkflowId, setScheduleDetailOpen, setScheduleDraft, setScheduleModalOpen, setScheduleQuery, setScheduleStatusFilter, setSelectedScheduleIds, setSelectedScheduleRunId, setWorkflowDraft, setWorkflowKeyRewrite, setWorkflowModalOpen, setWorkflowQuery, signals, signalsWaiting, skills, toggleSchedule, toggleWorkflow, token, unseenFailures, workflowAttention, workflowDraft, workflowGraph, workflowKeyRewrite, workflowModalOpen, workflowOrderDiffersFromArray, workflowQuery, workflows, workflowSaving } = ctx


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
            description="没有需要查看的运行"
            className="page-empty"
          />
        ) : (
          rows
        )}
      </div>
    )
  }

  const renderWorkflowTriggerEditor = (index: number, trigger: WorkflowTriggerDraft) => (
    <div className="workflow-step-trigger">
      <span className="workflow-step-trigger-label">入口触发</span>
      <Space wrap size={8}>
        <Select
          value={trigger.trigger_type}
          onChange={value => patchWorkflowStep(index, { trigger: { ...trigger, trigger_type: value } })}
          options={[
            { value: 'daily', label: '每天' },
            { value: 'weekdays', label: '工作日' },
            { value: 'weekly', label: '每周' },
            { value: 'monthly', label: '每月' },
            { value: 'interval', label: '固定间隔' },
            { value: 'once', label: '指定时间' },
            { value: 'signal', label: '等某个信号' },
          ]}
          style={{ width: 132 }}
        />
        {trigger.trigger_type === 'weekly' && (
          <Select
            value={trigger.day_of_week}
            onChange={value => patchWorkflowStep(index, { trigger: { ...trigger, day_of_week: value } })}
            options={WEEKDAY_OPTIONS}
            style={{ width: 96 }}
          />
        )}
        {trigger.trigger_type === 'monthly' && (
          <Space.Compact>
            <InputNumber
              min={1}
              max={31}
              value={trigger.day_of_month || 1}
              onChange={value => patchWorkflowStep(index, { trigger: { ...trigger, day_of_month: Number(value || 1) } })}
              style={{ width: 74 }}
            />
            <span className="workflow-step-trigger-suffix">日</span>
          </Space.Compact>
        )}
        {['daily', 'weekdays', 'weekly', 'monthly'].includes(trigger.trigger_type) && (
          // `needConfirm` defaults to true for every TimePicker and every
          // DatePicker with `showTime`, so a cell click only moves the *pending*
          // value and 确定 is the sole way to commit. Close the panel any other
          // way and the selection is thrown away, which reads as the field
          // jumping back to the old time. Off restores commit-on-close, which is
          // what this field was written against. `minuteStep={5}` went with it —
          // see the schedule picker for why.
          <TimePicker
            needConfirm={false}
            format="HH:mm"
            value={scheduleTimeValue(trigger.time_of_day)}
            onChange={value => patchWorkflowStep(index, { trigger: { ...trigger, time_of_day: value?.format('HH:mm') || '' } })}
            style={{ width: 104 }}
          />
        )}
        {trigger.trigger_type === 'interval' && (
          <Space.Compact>
            <InputNumber
              min={1}
              max={999}
              value={trigger.every}
              onChange={value => patchWorkflowStep(index, { trigger: { ...trigger, every: Number(value || 1) } })}
              style={{ width: 72 }}
            />
            <Select
              value={trigger.unit}
              onChange={value => patchWorkflowStep(index, { trigger: { ...trigger, unit: value } })}
              options={[
                { value: 'minutes', label: '分钟' },
                { value: 'hours', label: '小时' },
                { value: 'days', label: '天' },
                { value: 'weeks', label: '周' },
              ]}
              style={{ width: 88 }}
            />
          </Space.Compact>
        )}
        {trigger.trigger_type === 'once' && (
          <DatePicker
            needConfirm={false}
            showTime={{ format: 'HH:mm' }}
            format="M月D日 HH:mm"
            value={trigger.at ? dayjs(trigger.at) : null}
            onChange={value => patchWorkflowStep(index, { trigger: { ...trigger, at: value?.toISOString() || '' } })}
            disabledDate={current => !!current && current.endOf('day').valueOf() < Date.now()}
            style={{ width: 176 }}
          />
        )}
        {trigger.trigger_type === 'signal' && (
          <AutoComplete
            value={trigger.signal_name}
            onChange={value => patchWorkflowStep(index, { trigger: { ...trigger, signal_name: String(value || '') } })}
            placeholder="信号名"
            options={signals.map(item => ({ value: item.name, label: describeSignalName(item.name, schedules) }))}
            filterOption={(input, option) => String(option?.value || '').toLowerCase().includes(String(input || '').toLowerCase())}
            style={{ width: 220 }}
          />
        )}
      </Space>
    </div>
  )

  const renderWorkflows = () => (
    <>
      <Modal
        open={workflowModalOpen}
        title={editingWorkflowId ? '编辑流程' : '新建流程'}
        width={860}
        okText={editingWorkflowId ? '保存流程' : '创建流程'}
        cancelText="取消"
        confirmLoading={workflowSaving}
        okButtonProps={{ disabled: workflowGraph.problems.length > 0 }}
        onCancel={() => {
          setWorkflowModalOpen(false)
          setEditingWorkflowId(null)
        }}
        onOk={saveWorkflow}
        className="schedule-modal workflow-modal"
      >
        <div className="schedule-form">
          <div className="schedule-field-grid">
            <div className="schedule-field">
              <label>流程名称</label>
              <Input
                maxLength={60}
                placeholder="例如：夜间报告"
                value={workflowDraft.name}
                onChange={event => setWorkflowDraft({ ...workflowDraft, name: event.target.value })}
              />
            </div>
            <div className="schedule-field">
              <label>说明</label>
              <Input
                maxLength={200}
                placeholder="这条链在做什么"
                value={workflowDraft.description}
                onChange={event => setWorkflowDraft({ ...workflowDraft, description: event.target.value })}
              />
            </div>
          </div>

          <div className="schedule-form-section-title">步骤与依赖</div>
          <p className="workflow-editor-hint">
            每一步都是一个任务。把「上游」留空的步骤是入口，它需要自己的触发方式；
            填了上游的步骤等上游全部成功后才运行，所以不需要时间。
            顺序由依赖决定，编号就是真正会跑的次序。
          </p>

          {workflowOrderDiffersFromArray && (
            <p className="workflow-editor-order">
              执行顺序：{workflowGraph.order.join(' → ')}
            </p>
          )}

          <div className="workflow-step-editor">
            {workflowDraft.steps.map((step, index) => {
              const key = step.key.trim()
              // While a key is being rewritten, the step is still the stored
              // one for everything else: its task, its schedule, whether it was
              // the entry. Otherwise opening the field to retype `step2` would
              // make the editor forget what it is editing halfway.
              const rewriting = workflowKeyRewrite?.at === index ? workflowKeyRewrite : null
              const stored = editingWorkflow?.steps.find(
                item => item.key === (rewriting ? rewriting.key : key),
              )
              // The card is numbered by when it runs, not by where it sits in
              // the array. Those are the same thing until an edit separates
              // them, and the number is the one people read as "order".
              const position = workflowGraph.order.indexOf(key)
              const isEntry = cleanStepKeys(step.depends_on).length === 0
              const earlier = planWorkflowStepMove(workflowDraft.steps, key, 'earlier')
              const later = planWorkflowStepMove(workflowDraft.steps, key, 'later')
              const handoverWords = isEntry && step.triggerFrom
                ? describeChainTrigger(workflowDraft.steps, editingWorkflow?.steps, key)
                : ''
              const optionKeys = workflowDraft.steps
                .map((item, at) => ({ key: item.key.trim(), at }))
                .filter(item => item.key && item.at !== index)
              return (
                <div className="workflow-step-card" key={`step-${index}`}>
                  <div className="workflow-step-card-head">
                    <span className="workflow-step-index">{position >= 0 ? position + 1 : index + 1}</span>
                    {stored && !rewriting ? (
                      <span className="workflow-step-key-static">
                        <code>{key}</code>
                        <Tooltip title="改 key 等于换一个任务，这一步的运行记录会留在旧任务上">
                          <Button
                            type="link"
                            size="small"
                            onClick={() => setWorkflowKeyRewrite({ at: index, key })}
                          >
                            重命名
                          </Button>
                        </Tooltip>
                      </span>
                    ) : (
                      <Input
                        className="workflow-step-key"
                        maxLength={40}
                        placeholder="key"
                        value={step.key}
                        onChange={event => patchWorkflowStep(index, { key: event.target.value })}
                      />
                    )}
                    <Input
                      className="workflow-step-name"
                      maxLength={60}
                      placeholder="步骤名称"
                      value={step.name}
                      onChange={event => patchWorkflowStep(index, { name: event.target.value })}
                    />
                    <Select
                      value={step.kind}
                      onChange={value => patchWorkflowStep(index, { kind: value })}
                      options={[
                        { value: 'agent_prompt', label: 'Agent 任务' },
                        { value: 'message', label: '提醒' },
                      ]}
                      style={{ width: 124 }}
                    />
                    <Space size={2} className="workflow-step-tools">
                      <Tooltip title="在它后面插入一步">
                        <Button
                          type="text"
                          size="small"
                          icon={<PlusOutlined />}
                          onClick={() => insertWorkflowStepAfter(index)}
                        />
                      </Tooltip>
                      {/* Wrapped in a span because a disabled button swallows the
                          hover, and here the tooltip is the only place the reason
                          it cannot move is written down. */}
                      <Tooltip title={earlier.pair ? '和上一步换位' : earlier.problem}>
                        <span className="workflow-step-tool">
                          <Button
                            type="text"
                            size="small"
                            icon={<ArrowUpOutlined />}
                            disabled={!earlier.pair}
                            onClick={() => moveWorkflowStep(index, 'earlier')}
                          />
                        </span>
                      </Tooltip>
                      <Tooltip title={later.pair ? '和下一步换位' : later.problem}>
                        <span className="workflow-step-tool">
                          <Button
                            type="text"
                            size="small"
                            icon={<ArrowDownOutlined />}
                            disabled={!later.pair}
                            onClick={() => moveWorkflowStep(index, 'later')}
                          />
                        </span>
                      </Tooltip>
                      <Tooltip title={workflowDraft.steps.length > 1 ? '删除这一步' : '流程至少要有一个步骤'}>
                        <Button
                          type="text"
                          danger
                          icon={<DeleteOutlined />}
                          disabled={workflowDraft.steps.length <= 1}
                          onClick={() => removeWorkflowStep(index)}
                        />
                      </Tooltip>
                    </Space>
                  </div>
                  {rewriting && (
                    <p className="workflow-step-key-warning">
                      改名不是换个写法：保存后会按新 key 建一个任务，
                      这一步现在的运行记录留在旧任务上，不会跟过来。
                    </p>
                  )}
                  <TextArea
                    rows={3}
                    maxLength={6000}
                    placeholder={step.kind === 'message' ? '到这一步时发送的内容' : '这一步要完成的工作'}
                    value={step.content}
                    onChange={event => patchWorkflowStep(index, { content: event.target.value })}
                  />
                  <div className="workflow-step-card-foot">
                    <label className="workflow-step-upstreams">
                      <span>上游</span>
                      <Select
                        mode="multiple"
                        allowClear
                        placeholder="留空 = 入口步骤"
                        value={step.depends_on}
                        onChange={value => changeWorkflowStepUpstreams(index, value)}
                        options={optionKeys.map(item => ({ value: item.key, label: item.key }))}
                        optionFilterProp="label"
                        style={{ minWidth: 220, flex: '1 1 220px' }}
                      />
                    </label>
                    <Space.Compact className="workflow-step-workspace">
                      <Input
                        prefix={<FolderOpenOutlined />}
                        placeholder="项目目录（留空跟随入口步骤）"
                        value={step.workspace_root}
                        onChange={event => patchWorkflowStep(index, { workspace_root: event.target.value })}
                      />
                      <Button
                        loading={pickingDirectory}
                        onClick={() => pickDirectory(path => patchWorkflowStep(index, { workspace_root: path }))}
                      >
                        浏览…
                      </Button>
                    </Space.Compact>
                  </div>
                  {isEntry && (
                    step.trigger
                      ? renderWorkflowTriggerEditor(index, step.trigger)
                      : step.triggerFrom
                        ? (
                          <div className="workflow-step-trigger workflow-step-trigger-stored">
                            <span className="workflow-step-trigger-label">入口触发</span>
                            <span>
                              {`沿用「${step.triggerFrom}」的触发方式`}
                              {handoverWords ? `（${handoverWords}）` : ''}
                            </span>
                          </div>
                        )
                        : storedEntryStep(stored)
                          ? (
                            <div className="workflow-step-trigger workflow-step-trigger-stored">
                              <span className="workflow-step-trigger-label">入口触发</span>
                              <span>{describeStepTrigger(stored!)}</span>
                              <Button
                                type="link"
                                size="small"
                                onClick={() => {
                                  const task = schedules.find(item => item.id === stored!.task_id)
                                  if (task) {
                                    setWorkflowModalOpen(false)
                                    openEditSchedule(task)
                                  }
                                }}
                              >
                                改时间
                              </Button>
                            </div>
                          )
                          : (
                            <div className="workflow-step-trigger workflow-step-trigger-stored">
                              <span className="workflow-step-trigger-label">入口触发</span>
                              <span>它现在没有上游，还没有触发方式</span>
                              <Button
                                type="link"
                                size="small"
                                onClick={() => patchWorkflowStep(index, { trigger: defaultWorkflowTrigger() })}
                              >
                                指定
                              </Button>
                            </div>
                          )
                  )}
                </div>
              )
            })}
          </div>

          <Button type="dashed" block icon={<PlusOutlined />} onClick={addWorkflowStep}>
            在最后一步后面添加步骤
          </Button>

          {workflowGraph.problems.length > 0 && (
            <div className="workflow-problems" role="alert">
              <strong>这条链还不能保存</strong>
              <ul>{workflowGraph.problems.map(item => <li key={item}>{item}</li>)}</ul>
            </div>
          )}

          {workflowDraft.steps.length > 0 && workflowGraph.problems.length === 0 && (
            <>
              <div className="schedule-form-section-title">依赖关系</div>
              <WorkflowGraph
                nodes={workflowDraft.steps.map((step, index) => ({
                  key: step.key.trim() || `第${index + 1}步`,
                  name: step.name.trim() || step.key.trim() || `第 ${index + 1} 步`,
                  kind: step.kind,
                  depends_on: step.depends_on,
                }))}
                title="将要保存的步骤依赖关系"
              />
            </>
          )}
        </div>
      </Modal>

      {loadingView && workflows.length === 0 ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : filteredWorkflows.length === 0 ? (
        <Empty
          description={workflows.length
            ? '没有符合条件的流程'
            : '还没有流程。流程把几个任务串成一条链：上一步成功，下一步才运行。'}
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
                      disabled={schedulerHealth.status !== 'online' || flow.enabled === false}
                      onClick={() => runWorkflowNow(flow)}
                    />
                  </Tooltip>
                  <Tooltip title="编辑流程">
                    <Button type="text" icon={<EditOutlined />} aria-label={`编辑 ${flow.name}`} onClick={() => openEditWorkflow(flow)} />
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
                  if (step) openStepDetails(step)
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
                      onClick={() => openStepDetails(step)}
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

  const renderSchedules = () => (
    <div className="page-view schedules-view">
      <div className="page-head schedule-page-head">
        <div>
          <div className="schedule-title-row">
            <h2>自动化</h2>
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
            {automationTab === 'tasks'
              ? '让 Agent 在指定时间执行，或等某个信号发生后接着执行。'
              : automationTab === 'workflows'
                ? '把几个任务串成一条链：上一步完成后，下一步才运行。'
                : '这些运行结束得不好，或者中间有该跑却没跑成的次数，还没有人看过。'}
          </p>
        </div>
        <Space>
          <Tooltip title="刷新运行状态">
            <Button
              aria-label="刷新运行状态"
              icon={<ReloadOutlined />}
              onClick={() => {
                void loadSchedules()
                if (automationTab === 'workflows') void loadWorkflows()
              }}
            />
          </Tooltip>
          {automationTab === 'tasks' && (
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreateSchedule}>新建任务</Button>
          )}
          {automationTab === 'workflows' && (
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreateWorkflow}>新建流程</Button>
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
        <button
          type="button"
          role="tab"
          aria-selected={automationTab === 'tasks'}
          className={automationTab === 'tasks' ? 'active' : ''}
          onClick={() => setAutomationTab('tasks')}
        >
          任务
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={automationTab === 'workflows'}
          className={automationTab === 'workflows' ? 'active' : ''}
          onClick={() => setAutomationTab('workflows')}
        >
          流程
          {workflowAttention > 0 && (
            // Scoped to this tab: attention belonging to a step of a workflow.
            // A standalone task's failure is deliberately not counted here, and
            // saying so is what keeps this from reading as a discrepancy
            // against the 待处理 total.
            <span className="schedule-tab-badge" title={`流程里有 ${workflowAttention} 次运行需要查看`}>
              {workflowAttention}
            </span>
          )}
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
        ) : automationTab === 'tasks' ? (
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索任务、项目目录或执行内容"
            value={scheduleQuery}
            onChange={event => setScheduleQuery(event.target.value)}
          />
        ) : (
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索流程、步骤名称或执行内容"
            value={workflowQuery}
            onChange={event => setWorkflowQuery(event.target.value)}
          />
        )}
        {automationTab === 'tasks' && (
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
      <Modal
        open={scheduleModalOpen}
        title={editingScheduleId ? '编辑任务' : '新建任务'}
        width={700}
        okText={editingScheduleId ? '保存修改' : '创建任务'}
        cancelText="取消"
        confirmLoading={scheduleSaving}
        onCancel={() => {
          setScheduleModalOpen(false)
          setEditingScheduleId(null)
        }}
        onOk={saveSchedule}
        className="schedule-modal"
      >
        <div className="schedule-form">
          <div className="schedule-field">
            <label>任务名称</label>
            <Input
              maxLength={80}
              placeholder="例如：生成每日项目进展摘要"
              value={scheduleDraft.name}
              onChange={event => setScheduleDraft({ ...scheduleDraft, name: event.target.value })}
            />
          </div>

          <div className="schedule-field">
            <label>任务类型</label>
            <div className="schedule-action-picker" role="radiogroup" aria-label="任务类型">
              <button
                type="button"
                role="radio"
                aria-checked={scheduleDraft.action_type === 'agent_task'}
                className={scheduleDraft.action_type === 'agent_task' ? 'active' : ''}
                onClick={() => setScheduleDraft({ ...scheduleDraft, action_type: 'agent_task' })}
              >
                <RobotOutlined />
                <span><strong>Agent 执行任务</strong><small>让 Agent 按要求完成具体工作</small></span>
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={scheduleDraft.action_type === 'message'}
                className={scheduleDraft.action_type === 'message' ? 'active' : ''}
                onClick={() => setScheduleDraft({ ...scheduleDraft, action_type: 'message' })}
              >
                <MessageOutlined />
                <span><strong>定时提醒</strong><small>到时间后发送一条固定内容</small></span>
              </button>
            </div>
          </div>

          {scheduleDraft.action_type === 'agent_task' && (
            <>
              <div className="schedule-form-section-title">运行环境</div>
              <div className="schedule-field">
                <label>项目文件夹</label>
                <Space.Compact block>
                  <Input
                    prefix={<FolderOpenOutlined />}
                    placeholder="Agent 执行任务时使用的项目目录"
                    value={scheduleDraft.workspace_root}
                    onChange={event => setScheduleDraft({ ...scheduleDraft, workspace_root: event.target.value })}
                  />
                  <Button
                    loading={pickingDirectory}
                    onClick={() => pickDirectory(path => setScheduleDraft(current => ({ ...current, workspace_root: path })))}
                  >
                    浏览…
                  </Button>
                </Space.Compact>
                {recentWorkspaceRoots.length > 0 && (
                  <div className="schedule-workspace-recents">
                    <span>最近使用：</span>
                    {recentWorkspaceRoots.map(path => (
                      <button
                        key={path}
                        type="button"
                        className={scheduleDraft.workspace_root === path ? 'active' : ''}
                        title={path}
                        onClick={() => setScheduleDraft({ ...scheduleDraft, workspace_root: path })}
                      >
                        {compactWorkspacePath(path)}
                      </button>
                    ))}
                  </div>
                )}
                <small className="schedule-field-hint">任务会固定使用此目录，不受当前会话切换影响。</small>
              </div>
              <div className="schedule-field-grid schedule-field-grid-three">
                <div className="schedule-field">
                  <label>模型</label>
                  <Select
                    allowClear
                    showSearch
                    placeholder="默认模型"
                    value={scheduleDraft.model_override || undefined}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, model_override: value || '' })}
                    options={modelOptions}
                  />
                </div>
                <div className="schedule-field">
                  <label>上下文</label>
                  <Select
                    value={scheduleDraft.context_policy}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, context_policy: value })}
                    options={[
                      { value: 'stateless', label: '每次独立（推荐）' },
                      { value: 'task_history', label: '参考历史成功摘要' },
                      { value: 'shared_memory', label: '共享任务长期上下文' },
                    ]}
                  />
                </div>
                <div className="schedule-field">
                  <label>权限</label>
                  <Select
                    value={scheduleDraft.permission_profile}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, permission_profile: value })}
                    options={permissionProfileOptions.map(item => ({
                      value: item.key,
                      label: item.label,
                    }))}
                  />
                  {activePermissionProfile && (
                    <small className="schedule-field-hint">
                      {activePermissionProfile.detail || activePermissionProfile.summary}
                    </small>
                  )}
                </div>
              </div>
              <div className="schedule-field">
                <label>指定技能</label>
                <Select
                  mode="multiple"
                  allowClear
                  showSearch
                  optionFilterProp="label"
                  placeholder="不指定时由 Agent 自主选择"
                  value={scheduleDraft.selected_skills}
                  onChange={value => setScheduleDraft({ ...scheduleDraft, selected_skills: value })}
                  options={skills.filter(item => item.user_invocable).map(item => ({
                    value: item.id,
                    label: item.name || item.id,
                  }))}
                />
                <small className="schedule-field-hint">任务启动前会验证技能；被删除或停用的技能会让运行明确失败。</small>
              </div>
            </>
          )}

          {/* A step that follows upstreams has no schedule of its own: it runs
              when they succeed. Showing a schedule picker here would offer a
              choice the server refuses, and a wrong one would detach the step
              from the chain it is part of. */}
          {editingStep?.followsUpstreams ? (
            <>
              <div className="schedule-form-section-title">触发方式</div>
              <div className="schedule-field">
                <div className="schedule-step-trigger-note">
                  <ApartmentOutlined />
                  <span>
                    这一步属于流程「{editingStep.flow?.name}」，在上游步骤
                    {editingStep.step.depends_on.map(key => `「${key}」`).join('、')}
                    成功后才运行，没有自己的执行时间。
                  </span>
                </div>
                <small className="schedule-field-hint">
                  依赖关系在「流程」里编辑；确认上游都成功是运行的前提。
                </small>
              </div>
            </>
          ) : (
            <>
          <div className="schedule-form-section-title">
            {scheduleDraft.trigger_type === 'signal' ? '触发方式' : '执行时间'}
          </div>

          <div className="schedule-field">
            <label>执行计划</label>
            <Select
              value={scheduleDraft.trigger_type}
              onChange={value => setScheduleDraft({ ...scheduleDraft, trigger_type: value })}
              options={[
                { value: 'once', label: '指定日期和时间' },
                { value: 'interval', label: '固定间隔' },
                { value: 'daily', label: '每天' },
                { value: 'weekly', label: '每周' },
                { value: 'weekdays', label: '工作日（周一至周五）' },
                { value: 'monthly', label: '每月指定日期' },
                { value: 'signal', label: '当某个信号发生时' },
              ]}
            />
          </div>

          {scheduleDraft.trigger_type === 'signal' && (
            <div className="schedule-field">
              <label>等待的信号</label>
              {/* Autocomplete rather than a plain select: the known names are
                  the ones that can actually fire today, but a name nobody has
                  emitted yet is legitimate when the emitter is being set up in
                  the same breath. Matching is exact, so a near-miss never
                  fires — which is exactly why the known names are offered
                  instead of left to memory. */}
              <AutoComplete
                value={scheduleDraft.signal_name}
                onChange={value => setScheduleDraft({ ...scheduleDraft, signal_name: String(value || '') })}
                placeholder="选择已有信号，或填写一个将要发出的信号名"
                options={signals.map(item => ({
                  value: item.name,
                  label: `${describeSignalName(item.name, schedules)}${item.subscriber_count > 0 ? `（已有 ${item.subscriber_count} 个任务等待）` : ''}`,
                }))}
                filterOption={(input, option) => String(option?.value || '').toLowerCase().includes(String(input || '').toLowerCase())}
                style={{ width: '100%' }}
              />
              {signalsWaiting.length > 0 && (
                <div className="schedule-field-hint">
                  还没有发出过的信号：{signalsWaiting.map(item => item.name).join('、')}
                </div>
              )}
            </div>
          )}

          {scheduleDraft.trigger_type === 'once' && (
            <div className="schedule-field">
              <label>执行时间</label>
              <DatePicker
                needConfirm={false}
                showTime={{ format: 'HH:mm' }}
                format="YYYY年M月D日 HH:mm"
                value={scheduleDraft.at ? dayjs(scheduleDraft.at) : null}
                onChange={value => setScheduleDraft({ ...scheduleDraft, at: value?.toISOString() || '' })}
                disabledDate={current => !!current && current.endOf('day').valueOf() < Date.now()}
                style={{ width: '100%' }}
              />
            </div>
          )}

          {scheduleDraft.trigger_type === 'interval' && (
            <div className="schedule-field-grid">
              <div className="schedule-field">
                <label>重复间隔</label>
                <Space.Compact block>
                  <InputNumber
                    min={1}
                    max={999}
                    value={scheduleDraft.every}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, every: Number(value || 1) })}
                    style={{ width: '45%' }}
                  />
                  <Select
                    value={scheduleDraft.unit}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, unit: value })}
                    options={[
                      { value: 'minutes', label: '分钟' },
                      { value: 'hours', label: '小时' },
                      { value: 'days', label: '天' },
                      { value: 'weeks', label: '周' },
                    ]}
                    style={{ width: '55%' }}
                  />
                </Space.Compact>
              </div>
              <div className="schedule-field">
                <label>首次执行</label>
                <DatePicker
                  needConfirm={false}
                  showTime={{ format: 'HH:mm' }}
                  format="YYYY-MM-DD HH:mm"
                  value={scheduleDraft.anchor_at ? dayjs(scheduleDraft.anchor_at) : null}
                  onChange={value => setScheduleDraft({ ...scheduleDraft, anchor_at: value?.toISOString() || '' })}
                  disabledDate={current => !!current && current.endOf('day').valueOf() < Date.now()}
                  style={{ width: '100%' }}
                />
              </div>
            </div>
          )}

          {(['daily', 'weekly', 'weekdays', 'monthly'].includes(scheduleDraft.trigger_type)) && (
            <div className={`schedule-field-grid ${['daily', 'weekdays'].includes(scheduleDraft.trigger_type) ? 'single' : ''}`}>
              {scheduleDraft.trigger_type === 'weekly' && (
                <div className="schedule-field">
                  <label>星期</label>
                  <Select
                    value={scheduleDraft.day_of_week}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, day_of_week: value })}
                    options={WEEKDAY_OPTIONS}
                  />
                </div>
              )}
              {scheduleDraft.trigger_type === 'monthly' && (
                <div className="schedule-field">
                  <label>日期</label>
                  <InputNumber
                    min={1}
                    max={31}
                    value={scheduleDraft.day_of_month}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, day_of_month: Number(value || 1) })}
                    style={{ width: '100%' }}
                  />
                  <small className="schedule-field-hint">没有该日期的月份将自动跳过。</small>
                </div>
              )}
              <div className="schedule-field">
                <label>执行时间</label>
                {/* Two things used to make this field refuse the time you picked.
                    (1) `needConfirm` is not a no-op default: rc-picker sets it to
                    `internalPicker === 'time' || internalPicker === 'datetime'`,
                    i.e. true for every TimePicker and every DatePicker with
                    `showTime`. While it is on, clicking a cell only moves the
                    pending value and 确定 is the *only* way to commit; any other
                    way of closing the panel — clicking the next field, Escape —
                    drops the selection and the field snaps back to the previous
                    time. Off restores commit-on-close, which is the behaviour
                    this field was written against.
                    (2) `minuteStep={5}` cut the minute column to 00…55, but
                    `defaultScheduleDraft` seeds `time_of_day` from
                    `dayjs().add(1, 'hour')` — an arbitrary minute — so the
                    picker could open on 15:41 with no cell in its own column
                    selected, and 41 was unselectable. The date-time picker
                    above already offers all 60 minutes; this one now matches. */}
                <TimePicker
                  needConfirm={false}
                  format="HH:mm"
                  value={scheduleTimeValue(scheduleDraft.time_of_day)}
                  onChange={value => setScheduleDraft({ ...scheduleDraft, time_of_day: value?.format('HH:mm') || '' })}
                  style={{ width: '100%' }}
                />
              </div>
            </div>
          )}
            </>
          )}

          <div className="schedule-field">
            <label>{scheduleDraft.action_type === 'agent_task' ? '任务执行要求' : '提醒内容'}</label>
            {scheduleDraft.action_type === 'agent_task' ? (
              <TextArea
                rows={5}
                maxLength={6000}
                placeholder="说明要完成的工作、涉及的范围和期望输出。任务会在独立运行环境中交给 Agent 执行。"
                value={scheduleDraft.prompt}
                onChange={event => setScheduleDraft({ ...scheduleDraft, prompt: event.target.value })}
              />
            ) : (
              <TextArea
                rows={4}
                maxLength={2000}
                placeholder="输入到时间后需要发送的提醒内容"
                value={scheduleDraft.message_text}
                onChange={event => setScheduleDraft({ ...scheduleDraft, message_text: event.target.value })}
              />
            )}
            <div className="schedule-field-meta">
              <small>时区：{Intl.DateTimeFormat().resolvedOptions().timeZone}</small>
              <small>{scheduleDraft.action_type === 'agent_task'
                ? `${scheduleDraft.prompt.length} / 6000`
                : `${scheduleDraft.message_text.length} / 2000`}</small>
            </div>
          </div>

          <div className="schedule-form-section-title">投递方式</div>
          <div className="schedule-field">
            <div className="schedule-action-picker" role="radiogroup" aria-label="投递方式">
              <button
                type="button"
                role="radio"
                aria-checked={scheduleDraft.delivery_mode === 'standalone'}
                className={scheduleDraft.delivery_mode === 'standalone' ? 'active' : ''}
                onClick={() => setScheduleDraft({ ...scheduleDraft, delivery_mode: 'standalone' })}
              >
                <SaveOutlined />
                <span><strong>存到本机</strong><small>
                  {scheduleDraft.action_type === 'agent_task'
                    ? '运行结果写入输出目录，在任务卡片和运行记录里查看'
                    : '提醒内容写入输出目录，不会发到任何聊天'}
                </small></span>
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={scheduleDraft.delivery_mode === 'channel'}
                className={scheduleDraft.delivery_mode === 'channel' ? 'active' : ''}
                onClick={() => setScheduleDraft({ ...scheduleDraft, delivery_mode: 'channel' })}
              >
                <SendOutlined />
                <span><strong>发到飞书</strong><small>
                  {scheduleDraft.action_type === 'agent_task'
                    ? '运行结束后把执行摘要发到所选飞书会话'
                    : '到时间后把提醒内容发到所选飞书会话'}
                </small></span>
              </button>
            </div>
            {scheduleDraft.delivery_mode === 'channel' && (
              <>
                <div className="schedule-field" style={{ marginTop: 10 }}>
                  <label>发到哪个会话</label>
                  <Space.Compact block>
                    <Select
                      showSearch
                      optionFilterProp="label"
                      loading={feishuChatsLoading}
                      placeholder={feishuChatsLoading ? '正在获取会话列表…' : '选择机器人所在的飞书会话'}
                      value={scheduleDraft.delivery_chat_id || undefined}
                      onChange={value => setScheduleDraft({ ...scheduleDraft, delivery_chat_id: String(value || '') })}
                      options={(() => {
                        const options = feishuChats.map(chat => ({
                          value: chat.chat_id,
                          label: chat.name ? `${chat.name}（${chat.chat_id}）` : chat.chat_id,
                        }))
                        // A chat the task already targets must stay selectable
                        // even when the list has not loaded or the bot has
                        // since left it -- dropping the option would make the
                        // form silently blank out a value it is about to save.
                        if (
                          scheduleDraft.delivery_chat_id
                          && !options.some(item => item.value === scheduleDraft.delivery_chat_id)
                        ) {
                          options.push({
                            value: scheduleDraft.delivery_chat_id,
                            label: scheduleDraft.delivery_chat_id,
                          })
                        }
                        return options
                      })()}
                    />
                    <Button
                      disabled={!scheduleDraft.delivery_chat_id}
                      loading={feishuTesting}
                      onClick={() => void sendFeishuTest(scheduleDraft.delivery_chat_id)}
                    >
                      发送测试
                    </Button>
                  </Space.Compact>
                  {feishuChatsError ? (
                    <div className="schedule-field-hint schedule-delivery-error">
                      {feishuChatsError}
                      <Button type="link" size="small" onClick={() => void loadFeishuChats()}>重新获取</Button>
                    </div>
                  ) : (
                    feishuChatsLoaded && !feishuChatsLoading && feishuChats.length === 0 && (
                      /* A bot that is in no group yet is the normal first-run
                         state, not an error -- but it left the field with zero
                         options and no way out: the hint below told the user to
                         press 重新获取 and 重新获取 only existed on the error
                         branch. So the list could never be refreshed into a
                         usable one, and "发到飞书" was a dead end. Carry the
                         action here too, and keep the muted (not error) colour.

                         Gate on `feishuChatsLoaded`, not just an empty array:
                         "the bot is in no group" is a claim about the server's
                         answer, and before the first request returns we have no
                         answer to make it from.

                         The second line exists because an empty picker reads as
                         "the bot's chat is missing", and the obvious guess --
                         that the bot's own 1:1 conversation should be in here --
                         is wrong for a reason the user cannot see: 飞书's
                         im/v1/chats returns groups only ("获取到的群列表中，
                         不包含单聊（群模式为 p2p）"). Without saying so, the user
                         keeps looking for a chat that can never be listed. */
                      <div className="schedule-field-hint">
                        机器人当前不在任何群里，所以没有可选的会话。把机器人拉进一个群，再点
                        <Button type="link" size="small" onClick={() => void loadFeishuChats()}>重新获取</Button>
                        <br />
                        与机器人的单聊不会出现在这里：飞书的会话列表接口只返回群，不返回单聊。
                      </div>
                    )
                  )}
                  <small className="schedule-field-hint">
                    使用设置页里填写的飞书应用发消息；不确定是否可用时，先点「发送测试」验证。
                  </small>
                </div>
              </>
            )}
          </div>

          {scheduleDraft.action_type === 'agent_task' && (
            <>
              <div className="schedule-form-section-title">失败与超时</div>
              <div className="schedule-field-grid schedule-field-grid-three">
                <div className="schedule-field">
                  <label>超时（分钟）</label>
                  <InputNumber
                    min={1}
                    max={10080}
                    value={Math.ceil(scheduleDraft.timeout_seconds / 60)}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, timeout_seconds: Number(value || 1) * 60 })}
                    style={{ width: '100%' }}
                  />
                </div>
                <div className="schedule-field">
                  <label>最大尝试次数</label>
                  <InputNumber
                    min={1}
                    max={5}
                    value={scheduleDraft.max_attempts}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, max_attempts: Number(value || 1) })}
                    style={{ width: '100%' }}
                  />
                </div>
                <div className="schedule-field">
                  <label>重试间隔（秒）</label>
                  <InputNumber
                    min={0}
                    max={86400}
                    disabled={scheduleDraft.max_attempts <= 1}
                    value={scheduleDraft.backoff_seconds}
                    onChange={value => setScheduleDraft({ ...scheduleDraft, backoff_seconds: Number(value || 0) })}
                    style={{ width: '100%' }}
                  />
                </div>
              </div>
            </>
          )}

          <div className="schedule-preview">
            <div><ClockCircleOutlined /><strong>{editingStep?.followsUpstreams ? '触发条件' : scheduleDraft.trigger_type === 'signal' ? '触发条件' : '未来执行时间'}</strong></div>
            {editingStep?.followsUpstreams ? (
              <span>
                上游步骤
                {editingStep.step.depends_on.map(key => `「${key}」`).join('、')}
                全部成功后才运行，没有固定时间。
              </span>
            ) : scheduleDraft.trigger_type === 'signal' ? (
              // No list of times to show, and showing an empty one would read
              // as "this will never run" rather than "this waits".
              <span>
                {scheduleDraft.signal_name.trim()
                  ? `收到信号「${scheduleDraft.signal_name.trim()}」时运行一次，没有固定时间。`
                  : '选择一个信号后，任务会在它被发出时运行。'}
              </span>
            ) : schedulePreview.length > 0 ? (
              <ol>{schedulePreview.map(item => <li key={item}>{formatDateTime(item)}</li>)}</ol>
            ) : (
              <span>{schedulePreviewError || '填写完整任务信息后显示未来 5 次执行时间'}</span>
            )}
          </div>
        </div>
      </Modal>
      <Drawer
        open={scheduleDetailOpen}
        width="min(880px, calc(100vw - 20px))"
        title={selectedSchedule?.name || '任务运行详情'}
        onClose={() => setScheduleDetailOpen(false)}
        className="schedule-detail-drawer"
        extra={selectedSchedule && (
          <Space>
            <Button icon={<ThunderboltOutlined />} onClick={() => runScheduleNow(selectedSchedule)} disabled={schedulerHealth.status !== 'online' || !!selectedSchedule.active_run_id}>立即运行</Button>
            <Button icon={<EditOutlined />} onClick={() => openEditSchedule(selectedSchedule)}>编辑</Button>
            <Dropdown
              menu={{ items: [
                { key: 'duplicate', icon: <CopyOutlined />, label: '创建副本', onClick: () => duplicateSchedule(selectedSchedule) },
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
                          <strong>执行错误</strong>
                          <pre>{selectedScheduleRun.error}</pre>
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
      {automationTab === 'attention' ? renderAttention() : automationTab === 'workflows' ? renderWorkflows() : (
      loadingView ? <Skeleton active paragraph={{ rows: 6 }} /> : filteredSchedules.length === 0 ? <Empty description={scheduleQuery.trim() || scheduleStatusFilter !== 'all' ? '没有符合条件的任务' : '还没有独立任务。流程里的步骤在「流程」页签里。'} className="page-empty" /> : (
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
                    <Tooltip title="编辑"><Button type="text" icon={<EditOutlined />} aria-label={`编辑 ${task.name}`} onClick={() => openEditSchedule(task)} /></Tooltip>
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
  return { renderSchedules }
}
