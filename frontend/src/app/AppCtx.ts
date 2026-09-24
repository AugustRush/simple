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
import type { AttachmentInfo, AttentionRun, CommandInfo, ConfirmDecision, ConfirmRequest, FeishuChatInfo, ProviderFieldSpec, Message, PermissionProfileOption, PluginInfo, ScheduleArtifact, ScheduleDraft, ScheduleInfo, SchedulerHealth, ScheduleRun, ScheduleRunOutput, SessionInfo, SessionState, SignalInfo, SkillInfo, WorkflowDraft, WorkflowGraphCheck, WorkflowInfo, WorkflowStepDraft, WorkflowStepInfo } from '../types'

export interface AppCtx {
  updateMessage: (id: string, patch: Partial<Message>) => void
  activeSession: string | null
  token: string
  turnRefs: MutableRefObject<Record<string, HTMLDivElement | null>>
  copyMessage: (content: string) => void
  expandedTraces: Record<string, boolean>
  toggleTraceExpanded: (id: string) => void
  messages: Message[]
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
  approvalCommandRef: MutableRefObject<HTMLDivElement | null>
  setConfirmDetailOpen: Dispatch<SetStateAction<boolean>>
  sendConfirm: (decision: ConfirmDecision) => void
  queueView: { id: string; text: string; withdrawable: boolean; }[]
  withdrawQueuedMessages: (messageIds: string[]) => Promise<void>
  sessionState: SessionState | null
  resumingTaskId: string | null
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
  createSession: () => Promise<void>
  sendMessage: (overrideText?: string | undefined) => Promise<void>
  resolvedComposerText: string
  pageMeta: Record<string, { title: string; subtitle: string; }>
  // Every session, unfiltered -- so an empty page can tell "none yet" from
  // "none match the search".
  sessions: SessionInfo[]
  filteredSessions: SessionInfo[]
  allFilteredSessionsSelected: boolean
  selectedSessionIds: string[]
  setSelectedSessionIds: Dispatch<SetStateAction<string[]>>
  deleteSelectedSessions: () => void
  sessionSearch: string
  setSessionSearch: Dispatch<SetStateAction<string>>
  loadingSessions: boolean
  handleSessionContainerClick: (event: ReactMouseEvent<HTMLElement, MouseEvent> | ReactKeyboardEvent<HTMLElement>, sid: string) => void
  pendingDeleteSessionId: string | null
  deleteSession: (item: SessionInfo) => Promise<void>
  setPendingDeleteSessionId: Dispatch<SetStateAction<string | null>>
  revealSession: (item: SessionInfo) => Promise<void>
  // `${place}:${session_id}` of the title being edited in place, if any.
  renamingSession: string | null
  setRenamingSession: Dispatch<SetStateAction<string | null>>
  commitSessionTitle: (item: SessionInfo, title: string) => Promise<void>
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
  patchWorkflowStep: (index: number, patch: Partial<WorkflowStepDraft>) => void
  signals: SignalInfo[]
  workflowModalOpen: boolean
  editingWorkflowId: string | null
  workflowSaving: boolean
  // Workflows whose "run now" request has not come back yet.
  startingWorkflowIds: string[]
  workflowGraph: WorkflowGraphCheck
  setWorkflowModalOpen: Dispatch<SetStateAction<boolean>>
  setEditingWorkflowId: Dispatch<SetStateAction<string | null>>
  saveWorkflow: () => Promise<void>
  workflowDraft: WorkflowDraft
  setWorkflowDraft: Dispatch<SetStateAction<WorkflowDraft>>
  workflowOrderDiffersFromArray: boolean
  workflowKeyRewrite: { at: number; key: string; } | null
  editingWorkflow: WorkflowInfo | null
  setWorkflowKeyRewrite: Dispatch<SetStateAction<{ at: number; key: string; } | null>>
  insertWorkflowStepAfter: (index: number) => void
  moveWorkflowStep: (index: number, direction: "earlier" | "later") => void
  removeWorkflowStep: (index: number) => void
  changeWorkflowStepUpstreams: (index: number, upstreams: string[]) => void
  pickingDirectory: boolean
  pickDirectory: (apply: (path: string) => void) => Promise<void>
  openEditSchedule: (task: ScheduleInfo) => void
  addWorkflowStep: () => void
  workflows: WorkflowInfo[]
  filteredWorkflows: WorkflowInfo[]
  toggleWorkflow: (info: WorkflowInfo, enabled: boolean) => Promise<void>
  schedulerHealth: SchedulerHealth
  runWorkflowNow: (info: WorkflowInfo) => Promise<void>
  openEditWorkflow: (info: WorkflowInfo) => void
  deleteWorkflow: (info: WorkflowInfo) => void
  openStepDetails: (step: WorkflowStepInfo) => void
  schedulerRefreshedAt: number | null
  schedulerStale: boolean
  automationTab: "tasks" | "workflows" | "attention"
  loadSchedules: (silent?: boolean) => Promise<void>
  loadWorkflows: (silent?: boolean) => Promise<void>
  openCreateSchedule: () => void
  openCreateWorkflow: () => void
  unseenFailures: number
  clearScheduleAttention: (taskId?: string | undefined) => Promise<void>
  setAutomationTab: Dispatch<SetStateAction<"tasks" | "workflows" | "attention">>
  workflowAttention: number
  scheduleQuery: string
  setScheduleQuery: Dispatch<SetStateAction<string>>
  workflowQuery: string
  setWorkflowQuery: Dispatch<SetStateAction<string>>
  scheduleStatusFilter: string
  setScheduleStatusFilter: Dispatch<SetStateAction<string>>
  selectedScheduleIds: string[]
  bulkScheduleAction: (action: "enable" | "disable" | "delete") => Promise<void>
  scheduleModalOpen: boolean
  editingScheduleId: string | null
  scheduleSaving: boolean
  setScheduleModalOpen: Dispatch<SetStateAction<boolean>>
  setEditingScheduleId: Dispatch<SetStateAction<string | null>>
  saveSchedule: () => Promise<void>
  scheduleDraft: ScheduleDraft
  setScheduleDraft: Dispatch<SetStateAction<ScheduleDraft>>
  recentWorkspaceRoots: string[]
  permissionProfileOptions: PermissionProfileOption[]
  activePermissionProfile: PermissionProfileOption | undefined
  editingStep: { flow: WorkflowInfo | undefined; step: WorkflowStepInfo; followsUpstreams: boolean; } | null
  signalsWaiting: { name: string; subscriber_count: number; }[]
  feishuChatsLoading: boolean
  feishuChats: FeishuChatInfo[]
  feishuTesting: boolean
  sendFeishuTest: (chatId: string) => Promise<void>
  feishuChatsError: string
  loadFeishuChats: () => Promise<void>
  feishuChatsLoaded: boolean
  schedulePreview: string[]
  schedulePreviewError: string
  scheduleDetailOpen: boolean
  selectedSchedule: ScheduleInfo | null
  setScheduleDetailOpen: Dispatch<SetStateAction<boolean>>
  runScheduleNow: (task: ScheduleInfo) => Promise<void>
  duplicateSchedule: (task: ScheduleInfo) => void
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
  workflowsLoaded: boolean
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
}
