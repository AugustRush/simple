/** Domain types shared by the web UI. Nothing here has a runtime value. */



export type MessageRole = 'user' | 'assistant' | 'tool' | 'command' | 'error' | 'subagent'

export type ToolState = 'running' | 'done' | 'blocked' | 'interrupted'

export type ConfirmDecision = 'allow_once' | 'allow_session' | 'deny'

export type ConfirmRisk = 'high' | 'medium' | 'low'


/**
 * Rolling state for one batch of sub-agent activity.
 *
 * Sub-agent progress is live telemetry, not conversation: a single batch emits
 * a start/progress/finish event per agent, so appending a chat row per event
 * buried the transcript under dozens of near-identical lines. The note is
 * updated in place instead and rendered as one thin status strip.
 */
export interface SubAgentNote {
  /** Ordered event log, kept whole for the expanded view. */
  logs: string[]
  /** Distinct agent roles seen so far. */
  roles: string[]
  /** Roles that reached a terminal state (finished or failed). */
  doneRoles: string[]
  completed: number
  total: number
  failed: number
  running: boolean
  finished: boolean
  open?: boolean
  /** Local epoch millis of the first and last event, for the duration readout. */
  startedAt: number
  endedAt: number
}


/**
 * A pending tool-approval prompt pushed by the server. `allow_session` is only
 * true when the server holds a redeemable pending record for the command, so
 * the "总是允许" option is never offered where it could not be honoured.
 */
export interface ConfirmRequest {
  name?: string
  command?: string
  risk_level?: string
  reason?: string
  confirmation_token?: string
  allow_session?: boolean
  timeout_seconds?: number
}


export interface SessionInfo {
  session_id: string
  title?: string
  live?: boolean
  turn_count?: number
  last_activity?: string
  /**
   * What the session is doing right now, as the server sees it: idle,
   * active/cancelling (a turn is executing, or being interrupted), or
   * queued (an idle session still holding messages in its restart queue).
   * Absent means an older server that never sent one.
   */
  status?: 'idle' | 'active' | 'cancelling' | 'queued' | string
}


export interface Message {
  id: string
  role: MessageRole
  content: string
  streaming?: boolean
  queued?: boolean
  link?: string
  tool?: string
  toolState?: ToolState
  attachments?: AttachmentInfo[]
  subagent?: SubAgentNote
  /**
   * The model's thinking for this turn, streamed on its own channel.
   *
   * Deliberately not folded into `content`: thinking is not what the turn
   * said, so it must stay out of the copy button and out of the message the
   * turn is reported as having produced. Rendered as a collapsed note above
   * the reply, and only while the page is open — the transcript the server
   * replays carries replies, not thinking.
   */
  reasoning?: string
  /** Set by the reader, not the server: the note starts collapsed. */
  reasoningOpen?: boolean
}


export interface AttachmentInfo {
  id: string
  filename: string
  mime_type: string
  kind: string
  path: string
  size_bytes?: number
}


export interface PluginInfo {
  name: string
  version?: string
  description?: string
  source?: string
  enabled?: boolean
}


export interface SkillInfo {
  id: string
  name?: string
  description?: string
  source?: string
  user_invocable?: boolean
  /** False when the skill is switched off in config.json. */
  enabled?: boolean
}


export interface CommandInfo {
  name: string
  aliases?: string[]
  usage?: string
  description?: string
  kind?: 'command' | 'skill'
}


export interface PermissionProfileOption {
  key: string
  label: string
  summary: string
  detail: string
}


export type PermissionProfileKey = 'inherit' | 'read_only' | 'workspace_write'


export interface ScheduleInfo {
  id: string
  name: string
  kind: string
  enabled?: boolean
  trigger_type?: string
  trigger?: Record<string, any>
  payload?: Record<string, any>
  next_run_at?: string
  last_run_at?: string
  last_success_at?: string
  delivery_mode?: string
  delivery_target?: { target_type?: string; payload?: Record<string, any> }
  active_run_id?: string | null
  //: Whether a run is queued or running. The backend computes it because a
  //: run woken by a signal is written down as `queued` and has no
  //: active_run_id until a later tick claims it, so a client deriving this
  //: from the two fields below answers "no" during the one window that
  //: matters -- the one somebody watching a workflow is looking at.
  in_flight?: boolean
  latest_run?: ScheduleRun | null
  model_override?: string | null
  workspace_root?: string
  context_policy?: 'stateless' | 'task_history' | 'shared_memory'
  timeout_seconds?: number
  retry_policy?: { max_attempts?: number; backoff_seconds?: number }
  selected_skills?: string[]
  permission_profile?: PermissionProfileKey
  //: What this task's runs are judged by. Always present, possibly empty.
  acceptance?: AcceptanceInfo
  unseen_attention?: number
  // Empty for a standalone task, which most are. A step of a workflow carries
  // both, so a task row can say where it belongs.
  workflow_id?: string
  step_key?: string
  //: The words that asked for this task, quoted from whoever asked. Empty
  //: means no sentence was recorded -- a row that predates the column, or a
  //: task made by filling in the form -- never "nobody asked".
  request_quote?: string
}


export interface WorkflowStepInfo {
  key: string
  name: string
  kind: string
  payload?: Record<string, any>
  depends_on: string[]
  trigger_type?: string
  trigger?: Record<string, any>
  workspace_root?: string
  permission_profile?: PermissionProfileKey
  acceptance?: AcceptanceInfo
  timeout_seconds?: number
  task_id: string
  enabled?: boolean
  unseen_attention?: number
  //: See ScheduleInfo.in_flight: the graph reads a step's liveness from this,
  //: not from latest_run.status, or a queued step would draw as idle.
  in_flight?: boolean
  latest_run?: ScheduleRun | null
}


export interface WorkflowInfo {
  id: string
  name: string
  description?: string
  enabled?: boolean
  //: The sentence that asked for this chain; its steps inherit it.
  request_quote?: string
  created_at?: string
  updated_at?: string
  steps: WorkflowStepInfo[]
  unseen_attention?: number
}


export interface WorkflowStepDraft {
  key: string
  name: string
  kind: 'agent_prompt' | 'message'
  content: string
  depends_on: string[]
  workspace_root: string
  //: The parts of a payload that are not the content -- kept so that editing a
  //: step's text cannot drop a key some other tool put there.
  payload: Record<string, any>
  //: How a step with no upstreams starts. `null` means "leave it as it is",
  //: which is what an existing entry step sends: the graph editor does not
  //: show a schedule, so it must not be able to overwrite one. A brand new
  //: entry step has nothing to leave alone, so it must bring one.
  trigger: WorkflowTriggerDraft | null
  //: A step whose schedule is moving onto this one, named rather than copied.
  //:
  //: A schedule belongs to the chain, not to the step holding it: whoever runs
  //: first is the task that fires. So reordering a chain has to hand the
  //: schedule over -- and the editor must not do that by reading it out into a
  //: form and writing it back, because a weekly clock rendered as a sentence
  //: cannot be rendered back into a weekly clock. It names the step instead,
  //: and the server copies the spec that is actually stored.
  triggerFrom: string
}


export interface WorkflowTriggerDraft {
  trigger_type: 'once' | 'daily' | 'weekly' | 'weekdays' | 'monthly' | 'interval' | 'signal'
  at: string
  every: number
  unit: string
  anchor_at: string
  time_of_day: string
  day_of_week: string
  day_of_month: number
  signal_name: string
}


export interface WorkflowDraft {
  name: string
  description: string
  steps: WorkflowStepDraft[]
}


export interface SignalInfo {
  name: string
  source: 'task' | 'custom'
  task_id: string
  task_name: string
  status: string
  last_emitted_at?: string | null
  emission_count: number
  subscriber_count: number
}


/** What a task's runs are judged by. */
export interface AcceptanceInfo {
  criteria: string[]
  //: Empty when nothing mechanical decides it, which is the ordinary case.
  verify_command: string
}


/** The acceptance check's own result, when one ran. */
export interface VerificationInfo {
  //: One of the six verification statuses. Only `passed` and `failed` are
  //: statements about the work; the rest are statements about the check.
  status: string
  exit_code?: number | null
  stdout_tail?: string
  stderr_tail?: string
  error?: string | null
}


export interface ScheduleRun {
  id: string
  task_id: string
  status: string
  scheduled_for?: string
  started_at?: string
  finished_at?: string
  duration_ms?: number | null
  summary?: string
  error?: string
  delivery_status?: string
  //: Whether what the run produced met the bar the task was given. A
  //: different question from `status`, which only says whether the run
  //: happened; see scheduleRunVerdictLabel.
  verdict?: string
  verification?: VerificationInfo | null
  output_available?: boolean
  output_url?: string
  trigger_source?: string
  attempt?: number
  cancel_requested_at?: string | null
  retry_of_run_id?: string
  missed_count?: number
  acknowledged_at?: string | null
  needs_attention?: boolean
  config_snapshot?: Record<string, any>
}


/**
 * One run that finished badly while nobody was looking.
 *
 * The badge on the navigation says a number; this is the row behind it. They
 * are sent in the same payload for exactly that reason -- a count that has to
 * be reconciled against a list fetched from somewhere else is a count nobody
 * can check, which was the complaint.
 *
 * Only what a row needs to be recognised and reopened is carried. The task
 * name and, when the run belongs to a step, the workflow and step are here
 * because "which task" is the whole question: a failure whose workflow was
 * deleted is otherwise a row belonging to nothing.
 */
export interface AttentionRun {
  run_id: string
  task_id: string
  task_name: string
  workflow_id: string
  workflow_name: string
  workflow_deleted: boolean
  step_key: string
  status: string
  missed_count: number
  error: string
  started_at?: string | null
  finished_at?: string | null
}


export interface SchedulerHealth {
  status: 'online' | 'offline' | string
  last_heartbeat?: string
  active_runs?: number
  max_concurrent_runs?: number
}


export interface ScheduleRunOutput {
  run_id: string
  available: boolean
  content: string
  truncated?: boolean
  output_url?: string
}


export interface ScheduleArtifact {
  path: string
  name: string
  mime_type: string
  size_bytes: number
  url: string
}


export interface ScheduleDraft {
  name: string
  action_type: 'agent_task' | 'message'
  trigger_type: 'once' | 'interval' | 'daily' | 'weekly' | 'weekdays' | 'monthly' | 'signal'
  at: string
  every: number
  unit: 'minutes' | 'hours' | 'days' | 'weeks'
  anchor_at: string
  time_of_day: string
  day_of_week: string
  day_of_month: number
  signal_name: string
  prompt: string
  message_text: string
  workspace_root: string
  context_policy: 'stateless' | 'task_history' | 'shared_memory'
  model_override: string
  timeout_seconds: number
  max_attempts: number
  backoff_seconds: number
  selected_skills: string[]
  permission_profile: PermissionProfileKey
  //: Where a run's output goes. `channel` today means "发到飞书" -- the one
  //: channel the runtime delivers to -- and the chat fields say which of the
  //: bot's conversations. The name is kept only for display, so an edit still
  //: reads back a chat whose list has not been fetched yet.
  delivery_mode: 'standalone' | 'channel'
  delivery_chat_id: string
  delivery_chat_name: string
}


//: One row of the provider vocabulary, as /api/config publishes it.  The
//: Providers card renders whatever these say instead of naming fields itself,
//: so a field added on the server shows up here with no change to this file.
export interface ProviderFieldSpec {
  key: string
  kind: 'string' | 'secret' | 'int' | 'bool' | 'choice' | 'string_list' | 'string_map'
  label: string
  help: string
  default?: unknown
  required: boolean
  choices: string[]
  secret: boolean
  secret_values: boolean
}

export interface FeishuChatInfo {
  chat_id: string
  name: string
  description?: string
  external?: boolean
}


export interface SessionTaskGuidance {
  task_id?: string
  active_goal?: string
  status?: string
  progress?: string
  next_action?: string
  last_error?: string
  artifacts?: string[]
}


export interface QueuedQueueItem {
  id: string
  text: string
  kind?: string
  urgency?: string
  arrived_at?: number
}


export interface SessionState {
  session_id: string
  live?: boolean
  operation_state?: string
  queue?: {
    pending?: number
    interjections?: number
    restarts?: number
    // The entries themselves. The count alone cannot say whether the message
    // the reader is looking at is still really waiting, so it cannot decide
    // whether taking it back is still possible.
    items?: QueuedQueueItem[]
  }
  task?: SessionTaskGuidance | null
  workspace_root?: string
  workspace_status?: 'ready' | 'missing' | 'unset' | string
  workspace_exists?: boolean
  workspace_read?: boolean
  workspace_write?: boolean
}


export interface QueuedMessage {
  id: string
  text: string
  model: string
}


export type MediaKind = 'image' | 'audio' | 'video' | 'file'


export interface ToolDotSummary {
  state: ToolState
  count: number
}


export interface WorkflowGraphCheck {
  order: string[]
  problems: string[]
}


export interface WorkflowStepMovePlan {
  //: The two steps that trade places, the earlier one first.
  pair: [string, string] | null
  //: Why this move cannot be made, in a sentence, or `` when it can.
  problem: string
}


/** A step of a workflow, as the picture of it needs the step. */
export interface WorkflowGraphNode {
  key: string
  name: string
  kind: string
  depends_on: string[]
  status?: string
  taskId?: string
}
