/** Domain types shared by the web UI. Nothing here has a runtime value. */



//: `report` is the agent speaking unprompted: a scheduled run's result, said in
//: the conversation instead of waiting to be found in a status panel.
export type MessageRole =
  | 'user'
  | 'assistant'
  | 'tool'
  | 'command'
  | 'error'
  | 'subagent'
  //: The agent speaking first: a finished run's result, or an idea it wants a
  //: decision on. Both arrive unprompted, which is why they are roles rather
  //: than parts of a reply.
  | 'report'
  | 'proposal'

export type ToolState = 'running' | 'done' | 'failed' | 'blocked' | 'interrupted'

export type ConfirmDecision =
  | 'allow_once'
  | 'allow_session'
  | 'allow_project'
  | 'deny'

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
 * the "总是允许" option is never offered where it could not be honoured;
 * `allow_project` is stricter still, because it needs the question to have said
 * which rule and which project a durable answer would cover. When it is true,
 * `project_rule_label` is the sentence to show on the button.
 */
export interface ConfirmRequest {
  name?: string
  command?: string
  risk_level?: string
  reason?: string
  confirmation_token?: string
  allow_session?: boolean
  allow_project?: boolean
  /** The rule as the server spells it, e.g. `program:pip`. */
  project_rule?: string
  /** The same rule as a sentence, ready to display. */
  project_rule_label?: string
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
  /**
   * The record's own id (`message_id` from the server), when it has one.
   *
   * Distinct from `id`, which is this client's handle for the row. The record
   * id is what lets an optimistic row be recognised once the transcript
   * confirms it -- the same sentence sent twice in one turn is two messages,
   * and only the id tells them apart.
   */
  messageId?: string
  /** When the message was said, as the server recorded it. */
  createdAt?: string
  /**
   * A run report's own facts: which task spoke, and how the run ended.
   *
   * Separate fields rather than a sentence baked into `content`, because the row
   * shows the name and the status as chips -- a reader scanning the conversation
   * should be able to see "失败" without reading the text.
   */
  title?: string
  status?: string
  taskId?: string
  runId?: string
  /** A proposal message's row id and reversibility class. */
  proposalId?: string
  reversibility?: string
  /**
   * The row's position in the session's transcript, assigned by the server.
   *
   * Unique and stable, which is what makes it usable as this client's row key:
   * a row keeps its identity across reloads, and an older page of the
   * transcript is known to be older rather than guessed at.
   */
  seq?: number
  /**
   * Which tool call this row is, when the server said so.
   *
   * Two calls to the same tool can run at once, so a result is matched to its
   * start by this rather than by name.
   */
  operationId?: string
  /**
   * A message the user sent while a turn was already running.
   *
   * Set by the server on the recorded row (the message was folded into the
   * turn that answered it), and known locally via `queued` while that turn is
   * still in flight.  Either way it is *not* a turn boundary: it renders
   * inside the turn, marked as an aside, instead of starting a new exchange.
   */
  interjection?: boolean
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


//: One row of `GET /api/autonomy`: a proposal the patrol raised.
//:
//: `admission` is the verdict -- "auto" (would have run), "prepare" (would have
//: waited for you), or null when the row predates the verdict. This delivery
//: never executes, so even "auto" is a statement about what *would* happen.
//: The patrol's own numbers over a window: how much was looked at, how much came
//: out. Read with the proposals so "nothing proposed" cannot be mistaken for
//: "nothing to do".
export interface AutonomyFunnel {
  passes: number
  examined: number
  eligible: number
  skipped_warm: number
  skipped_stale: number
  proposed: number
  dropped: number
  duplicates: number
  expired: number
  //: How many proposals this window's passes ran without asking. The one number
  //: that says whether the loop is doing anything at all.
  executed: number
}

//: One item of the home page's timeline: a run, or a conversation.
//:
//: `kind` decides which fields exist -- a run has a status and a summary, a thread
//: has a turn count. Neither is padded into the other's shape, because "how did it
//: end" and "how much was said" are different facts.
export interface HomeActivity {
  kind: 'run' | 'thread'
  at: string
  title: string
  //: `autonomy` when the run was the patrol carrying out one of its own
  //: proposals. The ledger marks those as the agent acting, rather than as a
  //: task the person set up.
  origin?: string
  status?: string
  summary?: string
  error?: string
  task_id?: string
  run_id?: string
  session_id?: string
  turn_count?: number
}

//: One card on the board. `at_kind` says what `at` means, because the three
//: columns use it for three different facts (when it started / when it failed /
//: when it will fire) and a single unlabelled timestamp would be ambiguous.
//: One goal being worked on: the long-running unit of work.
export interface WorkingRow {
  id: string
  goal: string
  iteration: number
  max_iterations: number
  at: string
}

export interface BoardCard {
  task_id: string
  //: Set only for proposals: a candidate task has no task id yet, and the board
  //: still has to be able to act on it.
  id?: string
  name: string
  kind: string
  status: string
  at: string
  at_kind: 'started' | 'finished' | 'next' | 'created'
  trigger_type: string
  detail: string
  session_id: string
  reversibility?: string
  //: For a proposal about a task: which task, so the card can open it.
  target_task_id?: string
  //: How many unread failures this card stands for. The sidebar badge counts
  //: *runs*; a card per task would disagree with it unless the card says how
  //: many it holds.
  unread: number
}

//: `GET /api/home`: the landing page's one request.
//:
//: Three columns and a timeline. "Done" is deliberately not a column: finished
//: work that needs nobody belongs in `activity`, and a board with a done column
//: is a log with worse scanning.
export interface HomeOverview {
  needs: {
    proposals: number
    failures: number
    newest_failure: { run_id: string; task_id: string; status: string } | null
  }
  board: {
    running: BoardCard[]
    waiting: BoardCard[]
    scheduled: BoardCard[]
    scheduled_total: number
    //: Goals it is in the middle of (a Ralph task), which have no run row of
    //: their own. Not cards: there is nothing to act on, and no page to open.
    working?: WorkingRow[]
  }
  activity: HomeActivity[]
  //: Cursor for the next page of the timeline. Empty when the first page came
  //: back short -- which is how the page knows there is nothing older.
  activity_next: string
  at: string
}

export interface ProposalRow {
  id: string
  source: string
  reason: string
  action: string
  tool: string
  reversibility: 'reversible' | 'workspace' | 'irreversible'
  confidence: number
  state: 'proposed' | 'accepted' | 'declined' | 'expired' | 'acted'
  created_at: string
  expires_at: string
  decided_at: string
  decided_by: string
  admission: { answer: string; reason: string; prepared?: Record<string, any> } | null
  rule: string
  //: What the run did, once one was started for this proposal. `status` "queued"
  //: means a task exists and nothing has run it yet -- a placement, not a result.
  outcome: {
    task_id?: string
    run_id?: string
    status?: string
    summary?: string
    output_path?: string
    runner?: string
  } | null
}

//: One row of `GET /api/automations`: a plain task and a chain in the same shape.
//: `kind` is the only thing that differs, and `step_count` is 1 for a task -- so a
//: list column never has to branch on which endpoint the row came from.
export interface AutomationRow {
  id: string
  kind: 'task' | 'chain'
  name: string
  enabled: boolean
  step_count: number
  trigger_type: string
  trigger: Record<string, any>
  next_run_at?: string | null
  last_run_at?: string | null
  last_status: string
  attention: number
  //: The id an existing task endpoint is keyed by (the entry step's, for a chain).
  task_id: string
  //: Empty for a plain task; the chain's id otherwise.
  workflow_id: string
  description?: string
  delivery_mode?: string
  permission_profile?: string
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


export interface FailureReason {
  label: string
  hint: string
  transient: boolean
  raw: string
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
  //: The same failure in a person's terms: a short label, what it means, and
  //: whether a retry could end differently. `error` stays as the original text.
  error_reason?: FailureReason | null
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


//: One row of a scheduled run's own record: the prompt, a tool call, the reply.
export interface RunTranscriptRow {
  role: string
  content: string
  tool?: string
  toolState?: string
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


/* ── the memory page ────────────────────────────────────────────────────── */

export interface MemoryCategory {
  name: string
  entry_count: number
  avg_importance: number
  last_updated: string
}

//: What the store says it holds, in the same read the lists come from.  The
//: counts are the server's, not the page's: a header that counted what it had
//: already rendered would say "0 条" while the first page is in flight.
export interface MemoryOverview {
  enabled: boolean
  entry_count: number
  fact_count: number
  pinned_count: number
  categories: MemoryCategory[]
}

//: One long-term memory, as the page edits it.  `pinned` is a boolean here and
//: 0/1 in the database -- the store's column is a flag, the page's answer is a
//: question ("is this pinned?"), and the endpoint does the translation.
export interface MemoryEntry {
  id: string
  content: string
  importance: number
  category: string
  entity: string
  memory_type: string
  scope: string
  status: string
  source_session: string
  confidence: number
  pinned: boolean
  created_at: string
  updated_at: string
}

//: A belief, with the evidence that produced it.  `source_kind` and
//: `source_session` are what let the page say where it came from; both are
//: empty when the store cannot join, and the page says "来源未知" rather than
//: guessing.
export interface MemoryFact {
  fact_key: string
  subject: string
  predicate: string
  value: unknown
  value_type: string
  scope: string
  confidence: number
  source_kind: string
  source_session: string
  channel: string
  asserted_at: string
  updated_at: string
}

//: The editor's form.  One shape for "add" and "edit": the same four fields,
//: and `entry` on the editor state is what tells them apart.
export interface MemoryDraft {
  content: string
  category: string
  entity: string
  importance: number
}


/* ── what a session has spent ───────────────────────────────────────────── */

//: The ceiling the spend guard is measuring against.  ``limit === 0`` is "no
//: ceiling", which is the default and must render as "not configured" rather
//: than "0 of 0 left".
export interface SessionUsageBudget {
  limit: number
  used: number
  remaining: number
  exhausted: boolean
}

export interface SessionUsage {
  session_id: string
  calls: number
  input_tokens: number
  output_tokens: number
  cached_input_tokens: number
  budget: SessionUsageBudget
}
