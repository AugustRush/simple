/**
 * The slice of the App container that the view modules render from.
 *
 * App owns every piece of state and every action; the views are pure
 * renderers. Handing them one typed object keeps that boundary explicit and
 * keeps the views from growing a second source of truth.
 */
import type { ChangeEvent, ClipboardEvent, Dispatch, DragEvent, MutableRefObject, KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent, SetStateAction } from 'react'
import type { FormInstance } from 'antd'
import type { TextAreaRef } from 'antd/es/input/TextArea'
import type { MessageInstance } from 'antd/es/message/interface'
import type { MessageType } from 'antd/es/message/interface'
import type { AttachmentInfo, AttentionRun, AutomationRow, AutonomyFunnel, HomeOverview, ProposalRow, CommandInfo, ConfirmDecision, ConfirmRequest, FeishuChatInfo, ProviderFieldSpec, Message, PluginInfo, ScheduleArtifact, ScheduleInfo, SchedulerHealth, ScheduleRun, ScheduleRunOutput, SessionInfo, SessionState, SkillInfo, WorkflowInfo, MemoryDraft, MemoryEntry, MemoryFact, MemoryOverview, SessionUsage } from '../types'

export interface AppCtx {
  updateMessage: (id: string, patch: Partial<Message>) => void
  activeSession: string | null
  token: string
  turnRefs: MutableRefObject<Record<string, HTMLDivElement | null>>
  copyMessage: (content: string) => void
  /** Whether the newest turn may be cut (idle, not streaming). */
  canRewind: boolean
  /** Rewind the newest turn and put its text back in the composer. */
  editLastTurn: () => void
  /** Rewind the newest turn and send the same text again. */
  regenerateLastTurn: () => void
  expandedTraces: Record<string, boolean>
  toggleTraceExpanded: (id: string) => void
  messages: Message[]
  /**
   * True when the session is longer than the loaded window. The transcript says
   * so and offers to page back rather than letting the reader assume they are
   * looking at all of it.
   */
  hasMoreHistory: boolean
  /** True while a page of earlier messages is being fetched. */
  loadingHistory: boolean
  /** Fetch the page of transcript before what is on screen, and prepend it. */
  loadEarlierMessages: () => Promise<void>
  conversationTurns: Message[]
  conversationRailRef: MutableRefObject<HTMLDivElement | null>
  keepTurnSummary: () => void
  scheduleHideTurnSummary: () => void
  conversationGap: number
  handleRailMouseMove: (event: ReactMouseEvent<HTMLDivElement, MouseEvent>) => void
  conversationMarkerRefs: MutableRefObject<Record<string, HTMLButtonElement | null>>
  hoveredTurn: { id: string; top: number; } | null
  hoveredTurnIndex: number | null
  activateTurnIndex: (index: number) => void
  chatScrollRef: MutableRefObject<HTMLDivElement | null>
  handleChatScroll: () => void
  awayFromLatest: boolean
  jumpToLatest: () => void
  composerRef: MutableRefObject<TextAreaRef | null>
  focusComposer: () => void
  handleComposerPaste: (event: ClipboardEvent<HTMLTextAreaElement>) => void
  fileDragActive: boolean
  handleChatDragOver: (event: DragEvent<HTMLDivElement>) => void
  handleChatDragLeave: (event: DragEvent<HTMLDivElement>) => void
  handleChatDrop: (event: DragEvent<HTMLDivElement>) => void
  setInput: (value: string) => void
  isStreaming: boolean
  confirmReq: ConfirmRequest | null
  confirmRemaining: number
  confirmOverflowing: boolean
  confirmDetailOpen: boolean
  setConfirmDetailOpen: Dispatch<SetStateAction<boolean>>
  sendConfirm: (decision: ConfirmDecision) => void
  dismissConfirm: () => void
  attachApprovalCommand: (element: HTMLDivElement | null) => void
  queueView: { id: string; text: string; withdrawable: boolean; }[]
  withdrawQueuedMessages: (messageIds: string[]) => Promise<void>
  sessionState: SessionState | null
  resumingTaskId: string | null
  /**
   * Whether the task-guidance card may be shown. The whole rule lives with the
   * state it reads -- the recorded task status, the server's snapshot, the
   * frames narrated since, and whether this same prompt was already answered --
   * so the view cannot render a second, drifting copy of it.
   */
  taskGuidanceVisible: boolean
  continueTask: () => Promise<void>
  dismissTaskGuidance: () => Promise<void>
  activity: string
  interrupting: boolean
  pendingAttachments: AttachmentInfo[]
  setPendingAttachments: Dispatch<SetStateAction<AttachmentInfo[]>>
  inlineCommandOpen: boolean
  inlineCommandEmpty: boolean
  sendShortcutLabel: "Ctrl/Cmd + Enter" | "Enter"
  filteredCommands: CommandInfo[]
  commandItemRefs: MutableRefObject<Record<string, HTMLButtonElement | null>>
  commandIndex: number
  setCommandIndex: Dispatch<SetStateAction<number>>
  setCommandIndexPinned: Dispatch<SetStateAction<boolean>>
  input: string
  setCommandDismissed: Dispatch<SetStateAction<boolean>>
  handleComposerKeyDown: (event: ReactKeyboardEvent<HTMLTextAreaElement>) => void
  fileInputRef: MutableRefObject<HTMLInputElement | null>
  handleFilesSelected: (event: ChangeEvent<HTMLInputElement>) => Promise<void>
  pickWorkspace: () => Promise<void>
  permissionLevel: string
  updateSessionPermissions: (patch: { level?: string | undefined; sandbox?: string | undefined; }) => Promise<void>
  sandboxMode: string
  permissionLabel: "完全访问" | "高权限" | "中权限" | "需确认"
  currentModel: string
  modelSelectPlaceholder: "默认模型" | undefined
  handleModelChange: (model: string) => void
  modelOptions: { label: string; options: { value: string; label: string; }[]; }[]
  modelSelectWidth: string
  stopStreaming: () => void
  composerSendable: boolean
  creatingSession: boolean
  //: Open the one conversation, optionally with a sentence already in the box.
  openConversation: (seed?: string) => Promise<void>
  sendMessage: (overrideText?: string | undefined) => Promise<void>
  resolvedComposerText: string
  pageMeta: Record<string, { title: string; subtitle: string; }>
  // The conversation's own title, for the bar. There is exactly one session, so
  // there is no list here any more: 线程 was a list of things to switch between,
  // and switching is what this product stopped having.
  sessions: SessionInfo[]
  pluginSearch: string
  setPluginSearch: Dispatch<SetStateAction<string>>
  loadingView: boolean
  filteredPlugins: PluginInfo[]
  togglePlugin: (plugin: PluginInfo, enabled: boolean) => Promise<void>
  deletePlugin: (plugin: PluginInfo) => MessageType | undefined
  skills: SkillInfo[]
  skillFilter: "all" | "callable" | "internal"
  setSkillFilter: Dispatch<SetStateAction<"all" | "callable" | "internal">>
  skillSearch: string
  setSkillSearch: Dispatch<SetStateAction<string>>
  filteredSkills: SkillInfo[]
  toggleSkill: (skill: SkillInfo, enabled: boolean) => Promise<void>
  deleteSkill: (skill: SkillInfo) => MessageType | undefined
  extensionsTab: "plugins" | "skills"
  setExtensionsTab: Dispatch<SetStateAction<"plugins" | "skills">>
  schedules: ScheduleInfo[]
  attentionRuns: AttentionRun[]
  clock: number
  openScheduleDetails: (task: ScheduleInfo, runId?: string | undefined) => void
  acknowledgeScheduleRun: (taskId: string, runId: string) => Promise<void>
  editingWorkflowId: string | null
  // Workflows whose "run now" request has not come back yet.
  startingWorkflowIds: string[]
  setEditingWorkflowId: Dispatch<SetStateAction<string | null>>
  editingWorkflow: WorkflowInfo | null
  pickingDirectory: boolean
  pickDirectory: (apply: (path: string) => void) => Promise<void>
  workflows: WorkflowInfo[]
  filteredWorkflows: WorkflowInfo[]
  toggleWorkflow: (info: WorkflowInfo, enabled: boolean) => Promise<void>
  schedulerHealth: SchedulerHealth
  runWorkflowNow: (info: WorkflowInfo) => Promise<void>
  deleteWorkflow: (info: WorkflowInfo) => void
  schedulerRefreshedAt: number | null
  schedulerStale: boolean
  //: Two views: what needs a person, and everything they manage. "Task or workflow"
  //: is a filter inside the second one -- see agent/scheduler/automations.py.
  automationTab: "automation" | "attention"
  automationKind: "all" | "task" | "workflow"
  automationQuery: string
  automations: AutomationRow[]
  automationsLoaded: boolean
  answeredProposals: ProposalRow[]
  boardOpen: boolean
  setBoardOpen: (next: boolean) => void
  home: HomeOverview | null
  homeLoaded: boolean
  loadHome: () => Promise<void>
  funnel: AutonomyFunnel | null
  loadAutonomy: () => Promise<void>
  decideProposal: (id: string, state: "accepted" | "declined") => Promise<void>
  loadAutomations: (silent?: boolean) => Promise<void>
  loadSchedules: (silent?: boolean) => Promise<void>
  loadWorkflows: (silent?: boolean) => Promise<void>
  unseenFailures: number
  clearScheduleAttention: (taskId?: string | undefined) => Promise<void>
  setAutomationTab: Dispatch<SetStateAction<"automation" | "attention">>
  setAutomationKind: Dispatch<SetStateAction<"all" | "task" | "workflow">>
  setAutomationQuery: Dispatch<SetStateAction<string>>
  scheduleQuery: string
  setScheduleQuery: Dispatch<SetStateAction<string>>
  workflowQuery: string
  setWorkflowQuery: Dispatch<SetStateAction<string>>
  scheduleStatusFilter: string
  setScheduleStatusFilter: Dispatch<SetStateAction<string>>
  selectedScheduleIds: string[]
  bulkScheduleAction: (action: "enable" | "disable" | "delete") => Promise<void>
  feishuChatsLoading: boolean
  feishuChats: FeishuChatInfo[]
  feishuTesting: boolean
  sendFeishuTest: (chatId: string) => Promise<void>
  feishuChatsError: string
  loadFeishuChats: () => Promise<void>
  feishuChatsLoaded: boolean
  scheduleDetailOpen: boolean
  selectedSchedule: ScheduleInfo | null
  setScheduleDetailOpen: Dispatch<SetStateAction<boolean>>
  runScheduleNow: (task: ScheduleInfo) => Promise<void>
  deleteSchedule: (task: ScheduleInfo) => void
  scheduleRunsLoading: boolean
  loadScheduleRuns: (taskId: string, selectLatest?: boolean, silent?: boolean, focusRunId?: string | undefined) => Promise<void>
  permissionProfileLabel: (key?: string | undefined) => string
  scheduleRuns: ScheduleRun[]
  selectedScheduleRunId: string | null
  setSelectedScheduleRunId: Dispatch<SetStateAction<string | null>>
  selectedScheduleRun: ScheduleRun | null
  cancelScheduleRun: (task: ScheduleInfo, run: ScheduleRun) => Promise<void>
  retryScheduleRun: (task: ScheduleInfo, run: ScheduleRun, useLatest: boolean) => Promise<void>
  scheduleRunOutput: ScheduleRunOutput | null
  selectedScheduleRunTask: ScheduleInfo | null
  scheduleOutputLoading: boolean
  scheduleArtifacts: ScheduleArtifact[]
  filteredSchedules: ScheduleInfo[]
  attentionByTask: Map<string, string>
  setSelectedScheduleIds: Dispatch<SetStateAction<string[]>>
  toggleSchedule: (task: ScheduleInfo, enabled: boolean) => Promise<void>
  settingsDirty: boolean
  tokenDirty: boolean
  jsonStatus: { valid: boolean; label: string; }
  resetSettings: () => void
  saveSettings: () => Promise<void>
  tokenDraft: string
  setTokenDraft: Dispatch<SetStateAction<string>>
  applyToken: () => void
  sendShortcut: "enter" | "ctrl-enter"
  setSendShortcut: Dispatch<SetStateAction<"enter" | "ctrl-enter">>
  messageApi: MessageInstance
  form: FormInstance<any>
  handleSettingsFormChange: (changed: any, all: any) => void
  config: any
  providerFields: ProviderFieldSpec[]
  providerBusy: string
  saveProvider: (name: string, fields: Record<string, unknown>) => Promise<boolean>
  deleteProvider: (name: string) => Promise<boolean>
  activateProvider: (name: string) => Promise<boolean>
  testProvider: (name: string) => Promise<boolean>
  settingsModelOptions: { value: string; label: string; }[]
  thinkingEffortOptions: { value: string; label: string; }[]
  configText: string
  setConfigText: Dispatch<SetStateAction<string>>
  setSettingsDirty: Dispatch<SetStateAction<boolean>>
  // Movement between pages, so a view can hand the reader somewhere else
  // without reaching into the shell.  `navigateTo` keeps the unsaved-settings
  // guard; `selectSession` alone is what a link to a source
  // conversation is made of.
  navigateTo: (next: string) => void
  selectSession: (sid: string) => void

  /* ── 记忆页 ──────────────────────────────────────────────────────────── */
  memoryOverview: MemoryOverview | null
  memoryEntries: MemoryEntry[]
  memoryEntryTotal: number
  memoryFacts: MemoryFact[]
  memoryQuery: string
  memoryCategory: string
  memoryCategoryOptions: string[]
  /** 这个会话花了多少 token、离上限多远；null = 还没有会话或还没读到。 */
  sessionUsage: SessionUsage | null
  loadUsage: () => Promise<void>
  memoryTab: 'entries' | 'facts'
  memoryBusy: boolean
  /** 这个部署没有记忆服务（503），和"读失败"是两种页面。 */
  memoryUnavailable: boolean
  memoryLimit: number
  memoryEditorOpen: boolean
  memoryEditing: MemoryEntry | null
  memoryDraft: MemoryDraft
  applyMemorySearch: (value: string) => void
  applyMemoryCategory: (value: string) => void
  loadMemory: (options?: { silent?: boolean }) => Promise<void>
  loadMoreMemory: () => void
  openMemoryCreate: () => void
  openMemoryEdit: (entry: MemoryEntry) => void
  closeMemoryEditor: () => void
  updateMemoryDraft: (patch: Partial<MemoryDraft>) => void
  saveMemoryEntry: () => Promise<void>
  clearAllMemory: () => Promise<void>
  expandedMemoryIds: string[]
  toggleMemoryExpanded: (entryId: string) => void
  clearMemorySelection: () => void
  deleteSelectedMemoryEntries: () => Promise<void>
  selectedMemoryIds: string[]
  toggleMemorySelected: (entryId: string) => void
  deleteMemoryEntry: (entry: MemoryEntry) => Promise<void>
  toggleMemoryPin: (entry: MemoryEntry) => Promise<void>
  forgetMemoryFact: (fact: MemoryFact) => Promise<void>
  setMemoryTab: Dispatch<SetStateAction<'entries' | 'facts'>>
}
