/** Label tables and tunable constants for the web UI. */

import type { ConfirmRisk, PermissionProfileOption, ToolState } from './types'


/**
 * How hard the active provider should think, as offered on the settings page.
 *
 * The words themselves come from the server (`shared.THINKING_EFFORTS`,
 * delivered with the config), so the page offers what this backend will
 * validate. The empty one is ours and is not a level — it means the agent
 * sends no thinking parameter at all, so the provider's own default stands.
 * That is why it is first and why saving it removes the key rather than
 * writing a word.
 */
export const THINKING_EFFORT_LABELS: Record<string, string> = {
  off: '关闭',
  low: '低',
  medium: '中',
  high: '高',
}


/** The default until /api/config answers; replaced by whatever it carries. */
export const DEFAULT_THINKING_EFFORTS = ['off', 'low', 'medium', 'high']


// Display-only fallback, used only until the first /api/schedules response
// arrives. The backend list is authoritative -- it is what decides which keys
// are accepted, so duplicating the set here permanently would let the two
// drift apart.
export const KNOWN_PERMISSION_PROFILES: PermissionProfileOption[] = [
  { key: 'inherit', label: '继承全局权限', summary: '不改动任何权限配置。', detail: '' },
  { key: 'read_only', label: '强制只读', summary: '不写工作区文件。', detail: '' },
  { key: 'workspace_write', label: '可在项目内写入', summary: '可改文件、跑测试、出报告。', detail: '' },
]


// The model picker's label renders at 11px (see .composer-tools .model-select
// .ant-select-selection-item). Measuring with that same font keeps the control's
// inline width honest — counting characters under-read the small label font and
// left ~40px of dead space to the right of the model name.
export const MODEL_LABEL_FONT =
  "11px -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif"


// What the control spends on chrome around the label: 8px of left padding plus
// 28px on the right for the 10px chevron, its 11px inset and a 7px gap. The
// last 2px absorb the subpixel difference between canvas metrics and DOM text
// layout, which otherwise ellipsizes a label that exactly fits.
export const MODEL_PICKER_CHROME = 38


// Stopping a turn is cooperative: the cancel request aborts the in-flight
// model request or child process, but whatever step is already running has to
// unwind before `turn_complete` arrives. These bound how long the UI stays
// quiet about it — first by asking again on the user's behalf, then by saying
// plainly that it has not taken effect.
export const INTERRUPT_RETRY_MS = 5000

export const INTERRUPT_STUCK_MS = 20000


// Half of .conversation-summary's max-height (124px, index.css). The hover
// summary clamps its top position so the card never crosses the chat bounds;
// keep this in sync when the card's max-height changes.
export const CONVERSATION_SUMMARY_HALF_HEIGHT = 62


// One mark per call does not scale: the width grows linearly while the
// information per mark saturates -- nobody reads 90 dots differently from
// 100 -- so past this budget the strip describes the shape of the turn
// instead of indexing it. The exact step count stays in the label next to it,
// which is why the marks are free to become summaries.
export const MAX_TRACE_DOTS = 20


// A summarised mark reports its most severe member, never a sample of them.
// That is the whole point of aggregating by worst case rather than picking
// every n-th step: a single blocked call must not be able to disappear just
// because the turn around it was long. ``done`` is the floor, matching the
// `toolState || 'done'` the marks have always used.
export const TRACE_SEVERITY: Record<ToolState, number> = {
  done: 0,
  running: 1,
  interrupted: 2,
  blocked: 3,
}


// Approval prompts arrive with the tool's internal name; the bar says what the
// user is actually being asked about rather than echoing a code identifier.
export const CONFIRM_TOOL_LABELS: Record<string, string> = {
  shell: '终端命令',
  install_plugin: '安装插件',
  create_tool: '激活用户工具',
  memory_clear: '清空记忆',
}


export const CONFIRM_RISK_LABELS: Record<ConfirmRisk, string> = {
  high: '高风险',
  medium: '中风险',
  low: '低风险',
}


/**
 * Task statuses where "continue or abandon?" is a real question.
 *
 * The working state is written on every turn end with one of
 * `cancelled` / `failed` / `completed` / `in_progress`, so a blocklist of
 * terminal states is not enough: `in_progress` (a turn that produced neither
 * text nor an error) was also advertised as 「任务已中断」. After the user
 * answered the prompt once, the continuation turn wrote exactly such a state,
 * the card came back, and every further click queued another interjection.
 */
export const TASK_INTERRUPTED_STATUSES = new Set(['cancelled', 'interrupted', 'failed'])


// What the session list says a busy session is doing.  Only busy states get a
// word: an idle session is the unremarkable case, and a badge saying "空闲"
// on every row would make the badge on the busy one mean nothing.
export const SESSION_STATUS_LABELS: Record<string, string> = {
  active: '运行中',
  cancelling: '正在停止',
  queued: '排队中',
}


export const TASK_STATUS_LABELS: Record<string, string> = {
  cancelled: '已取消',
  interrupted: '已中断',
  failed: '失败',
}


// Sub-agent progress is the noisiest thing the socket carries: one batch emits
// a start/progress/finish event per agent. Stamping each event into the
// transcript cost roughly 60px apiece — a 36px note row plus the 22–32px
// conversation gap — for lines that mostly repeated each other, so a single
// multi-agent turn could push 600–1800px of telemetry through the scroll area.
// Folding the events into one note keeps that cost constant.
export const SUBAGENT_KIND_LABELS: Record<string, string> = {
  batch_started: '批量启动',
  batch_progress: '批量进度',
  batch_finished: '批量结束',
  agent_started: '开始执行',
  agent_finished: '执行完成',
  agent_failed: '执行失败',
  agent_retry: '重试',
}


/** Kinds that end a batch; everything after them starts a fresh note. */
export const SUBAGENT_TERMINAL_KINDS = new Set(['batch_finished'])


export const WEEKDAY_OPTIONS = [
  { value: 'monday', label: '周一' },
  { value: 'tuesday', label: '周二' },
  { value: 'wednesday', label: '周三' },
  { value: 'thursday', label: '周四' },
  { value: 'friday', label: '周五' },
  { value: 'saturday', label: '周六' },
  { value: 'sunday', label: '周日' },
]


//: How often the automation page asks the server again. A run that is happening
//: is worth watching closely. Nothing else is worth watching that closely --
//: but "not closely" must not mean "never", which is what it used to mean.
export const SCHEDULE_FAST_POLL_MS = 2000

export const SCHEDULE_IDLE_POLL_MS = 15000

//: How close a run has to be before it is worth watching at the fast cadence.
export const SCHEDULE_WATCH_WINDOW_MS = 60_000

//: How far past due a run can be and still count as imminent. Beyond this the
//: scheduler is stuck rather than busy, and the health badge is what says so;
//: holding the page at two seconds would only spend requests on a problem that
//: polling cannot fix.
export const SCHEDULE_OVERDUE_GRACE_MS = 120_000


export const GRAPH_NODE_WIDTH = 178

export const GRAPH_NODE_HEIGHT = 54

export const GRAPH_COLUMN_GAP = 62

export const GRAPH_ROW_GAP = 14

export const GRAPH_PAD = 2
