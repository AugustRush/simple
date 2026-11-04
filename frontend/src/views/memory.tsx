/** 记忆页：它记得的东西，以及你能对它做的两件事。
 *
 * 为什么是"两件事"而不是"编辑/删除"：这个库里有两种东西，它们的可改程度不同。
 *
 * - **记忆条目**是散文，是这个库直接保存的内容 —— 可以改、可以删、可以钉住。
 * - **事实**是推断，落在一条只追加的证据账本上 —— 不能直接改（下一次合并会把它
 *   覆盖回去），也不删那一行（证据还在，会在下次重新得出结论）。所以那一栏只有
 *   "忘记"：归档证据、丢掉结论。拒绝一个判断和修改一条笔记是两种动作，所以是两个
 *   动词。
 *
 * 这个区别对读者不是理论：一条记错的偏好，改掉就结束了；一条错误的事实，得让
 * 它不再被相信。把它们做成同一个按钮，第二种情况会静默地不生效。
 */

import type { AppCtx } from '../app/AppCtx'
import type { MemoryEntry, MemoryFact } from '../types'
import { relativeTime } from '../lib/format'
import { markdownToHtml } from '../lib/markdown'
import {
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
  StarFilled,
  StarOutlined,
} from '@ant-design/icons'
import {
  AutoComplete,
  Button,
  Checkbox,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Select,
  Slider,
  Tag,
  Tooltip,
} from 'antd'

//: 分类的候选值。库允许任意分类（会被规范化并受上限约束），所以这是
//: "常见的那几个"，不是白名单 —— AutoComplete 里仍然可以自己写一个。
const BUILTIN_CATEGORIES = [
  { value: 'identity', label: 'identity · 身份' },
  { value: 'preferences', label: 'preferences · 偏好' },
  { value: 'projects', label: 'projects · 项目' },
  { value: 'people', label: 'people · 人' },
  { value: 'concepts', label: 'concepts · 概念' },
  { value: 'episodes', label: 'episodes · 经历' },
  { value: 'notes', label: 'notes · 笔记' },
]

//: 事实来源的说法。`source_kind` 是账本里的机器词汇，直接印出来对读者没有
//: 意义；测试里断言的是这些词而不是原来的值，因为读者看到的就是它们。
const FACT_KIND_LABELS: Record<string, string> = {
  manual_write: '你写的',
  consolidation: '对话里总结的',
  inference: '推断的',
  bootstrap: '启动时写下的',
}

//: The preview's source: Markdown with blank lines collapsed outside fenced code.
//:
//: The transcript renderer turns a newline into `<br>` rather than closing a
//: paragraph, so the blank line after a `#` heading arrives as `<br><br>` -- two
//: empty lines that ate the whole four-line clamp and left every entry looking
//: blank under its title. Inside a fence those newlines are content, so they are
//: left alone.
function previewSource(content: string): string {
  return String(content || '')
    .split(/(```[\s\S]*?```)/g)
    .map((chunk, index) => (index % 2 === 1 ? chunk : chunk.replace(/\n{2,}/g, '\n')))
    .join('')
}

function sourceLabel(sourceSession: string, sessions: AppCtx['sessions']): string {
  const clean = String(sourceSession || '').trim()
  if (!clean) return '来源未知'
  if (clean === 'manual') return '你手写的'
  const match = sessions.find(item => item.session_id === clean)
  return match?.title ? `来自会话「${match.title}」` : `来自会话 ${clean.slice(0, 8)}`
}

function formatFactValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

export function renderMemory(ctx: AppCtx) {
  const {
    applyMemoryCategory,
    applyMemorySearch,
    closeMemoryEditor,
    deleteMemoryEntry,
    forgetMemoryFact,
    loadMemory,
    loadMoreMemory,
    memoryBusy,
    memoryCategory,
    memoryCategoryOptions,
    memoryDraft,
    memoryEditing,
    memoryEditorOpen,
    memoryEntries,
    memoryEntryTotal,
    memoryFacts,
    memoryQuery,
    memoryTab,
    memoryUnavailable,
    navigateTo,
    openMemoryCreate,
    openMemoryEdit,
    saveMemoryEntry,
    selectSession,
    sessions,
    setMemoryTab,
    toggleMemoryPin,
    updateMemoryDraft, clearAllMemory, clearMemorySelection, deleteSelectedMemoryEntries, selectedMemoryIds, toggleMemorySelected, expandedMemoryIds, toggleMemoryExpanded } = ctx

  const categoryOptions = [
    { value: '', label: '全部分类' },
    ...memoryCategoryOptions.map(name => ({ value: name, label: name })),
  ]
  const draftCategoryOptions = [
    ...BUILTIN_CATEGORIES,
    ...memoryCategoryOptions
      .filter(name => !BUILTIN_CATEGORIES.some(item => item.value === name))
      .map(name => ({ value: name, label: name })),
  ]

  const openSourceSession = (sourceSession: string) => {
    const clean = String(sourceSession || '').trim()
    if (!clean || clean === 'manual') return
    if (!sessions.some(item => item.session_id === clean)) return
    selectSession(clean)
    navigateTo('chat')
  }

  const unavailable = memoryUnavailable ? (
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={
        <span>
          这个部署没有启用记忆。
          <br />
          打开记忆插件之后，这里会列出它记得的东西。
        </span>
      }
    >
      <Button size="small" onClick={() => navigateTo('settings')}>
        去设置
      </Button>
    </Empty>
  ) : null

  const selectionBar = selectedMemoryIds.length > 0 ? (
    <div className="memory-selection-bar">
      <span>已选 {selectedMemoryIds.length} 条</span>
      <div className="memory-selection-actions">
        <Button size="small" onClick={clearMemorySelection}>
          取消选择
        </Button>
        <Popconfirm
          title={`删除选中的 ${selectedMemoryIds.length} 条记忆？`}
          description="它们会从库里消失，之后不会再被检索到。"
          okText="删除"
          cancelText="取消"
          okButtonProps={{ danger: true }}
          onConfirm={() => void deleteSelectedMemoryEntries()}
        >
          <Button size="small" danger loading={memoryBusy}>
            删除所选
          </Button>
        </Popconfirm>
      </div>
    </div>
  ) : null

  const entryList = memoryEntries.length ? (
    <div className="memory-list">
      {selectionBar}
      {memoryEntries.map((entry: MemoryEntry) => {
        const pinned = Boolean(entry.pinned)
        const source = sourceLabel(entry.source_session, sessions)
        const canOpenSource =
          entry.source_session &&
          entry.source_session !== 'manual' &&
          sessions.some(item => item.session_id === entry.source_session)
        return (
          <div
            className={`memory-row${pinned ? ' is-pinned' : ''}${
              selectedMemoryIds.includes(entry.id) ? ' is-selected' : ''
            }`}
            key={entry.id}
          >
            <Checkbox
              className="memory-row-check"
              checked={selectedMemoryIds.includes(entry.id)}
              onChange={() => toggleMemorySelected(entry.id)}
              aria-label={`选择记忆 ${entry.content.slice(0, 16)}`}
            />
            <div className="memory-row-main">
              {/* Rendered, not printed. These entries are Markdown journals, and
                  showing the source made every row begin with a literal "#" and
                  wear its backticks as text -- a page about what the agent
                  remembers read like a log file of what it wrote. */}
              <div
                className={`memory-row-text markdown${expandedMemoryIds.includes(entry.id) ? ' is-expanded' : ''}`}
                dangerouslySetInnerHTML={{ __html: markdownToHtml(previewSource(entry.content)) }}
              />
              {entry.content.length > 160 && (
                <button
                  type="button"
                  className="memory-row-expand"
                  onClick={() => toggleMemoryExpanded(entry.id)}
                >
                  {expandedMemoryIds.includes(entry.id) ? '收起' : '展开全文'}
                </button>
              )}
              <div className="memory-row-meta">
                <Tag className="memory-tag">{entry.category}</Tag>
                {entry.entity && <Tag className="memory-tag is-quiet">{entry.entity}</Tag>}
                {canOpenSource ? (
                  <button
                    type="button"
                    className="memory-meta-link"
                    onClick={() => openSourceSession(entry.source_session)}
                  >
                    {source}
                  </button>
                ) : (
                  <span className="memory-meta-plain">{source}</span>
                )}
                <span className="memory-meta-plain">{relativeTime(entry.updated_at)}</span>
                {pinned && <span className="memory-pin-flag">已钉住</span>}
                {/* The importance number, spelled out. It used to be a 44x4px
                    bar with no label, sitting beside the row's buttons: at that
                    size it read as a stray divider rather than as a meter, and
                    a value nobody can name is a value nobody uses. */}
                <span className="memory-meta-plain">
                  重要度 {Math.round(entry.importance * 100)}%
                </span>
              </div>
            </div>
            <div className="memory-row-side">
              <Tooltip title={pinned ? '取消钉住' : '钉住：不再随时间淡出'}>
                <Button
                  type="text"
                  size="small"
                  aria-label={pinned ? '取消钉住' : '钉住'}
                  icon={pinned ? <StarFilled /> : <StarOutlined />}
                  onClick={() => void toggleMemoryPin(entry)}
                  disabled={memoryBusy}
                />
              </Tooltip>
              <Tooltip title="编辑">
                <Button
                  type="text"
                  size="small"
                  aria-label="编辑"
                  icon={<EditOutlined />}
                  onClick={() => openMemoryEdit(entry)}
                />
              </Tooltip>
              <Popconfirm
                title="删除这条记忆？"
                description="它会从库里消失，之后不会再被检索到。"
                okText="删除"
                cancelText="取消"
                okButtonProps={{ danger: true }}
                onConfirm={() => void deleteMemoryEntry(entry)}
              >
                <Tooltip title="删除">
                  <Button
                    type="text"
                    size="small"
                    danger
                    aria-label="删除"
                    icon={<DeleteOutlined />}
                    disabled={memoryBusy}
                  />
                </Tooltip>
              </Popconfirm>
            </div>
          </div>
        )
      })}
      {memoryEntryTotal > memoryEntries.length && (
        <div className="memory-more">
          <Button size="small" onClick={loadMoreMemory}>
            还有 {memoryEntryTotal - memoryEntries.length} 条
          </Button>
        </div>
      )}
    </div>
  ) : (
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={
        memoryQuery.trim() || memoryCategory
          ? '没有匹配的记忆'
          // One entry, not two: the toolbar already has 新增记忆, and a second
          // primary button here was the same action in a louder place. The
          // sentence points at both ways in, which is what an empty state is for.
          : '还什么都没记住。可以在对话里说「记住……」，也可以用右上角的「新增记忆」手写一条。'
      }
    />
  )

  const factList = memoryFacts.length ? (
    <div className="memory-list">
      {memoryFacts.map((fact: MemoryFact) => {
        const kind = FACT_KIND_LABELS[fact.source_kind] || fact.source_kind || '来源未知'
        const source = sourceLabel(fact.source_session, sessions)
        return (
          <div className="memory-row" key={fact.fact_key}>
            <div className="memory-row-main">
              <div className="memory-fact-line">
                <span className="memory-fact-subject">{fact.subject}</span>
                <span className="memory-fact-op">·</span>
                <span className="memory-fact-predicate">{fact.predicate}</span>
                <span className="memory-fact-op">=</span>
                <strong className="memory-fact-value">{formatFactValue(fact.value)}</strong>
              </div>
              <div className="memory-row-meta">
                <Tag className="memory-tag">{kind}</Tag>
                {/* 手写的事实：来源那半句和标签是同一件事，只说一次。 */}
                {fact.source_session !== 'manual' && source !== kind && (
                  <span className="memory-meta-plain">{source}</span>
                )}
                <span className="memory-meta-plain">
                  置信度 {Math.round((fact.confidence || 0) * 100)}%
                </span>
                <span className="memory-meta-plain">{relativeTime(fact.updated_at)}</span>
              </div>
            </div>
            <div className="memory-row-side">
              <Popconfirm
                title="忘记这条事实？"
                description="它的证据会被归档，这条结论不再被引用；同样的证据再次出现时，它可能重新得出结论。"
                okText="忘记"
                cancelText="保留"
                onConfirm={() => void forgetMemoryFact(fact)}
              >
                <Button type="text" size="small" danger disabled={memoryBusy}>
                  忘记
                </Button>
              </Popconfirm>
            </div>
          </div>
        )
      })}
    </div>
  ) : (
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description="还没有形成任何结论。它会在对话里慢慢总结出关于你的事实。"
    />
  )

  // 工具条属于它下面那张列表：搜索和分类筛的是条目，不是事实。把它们放在
  // 标签页上方会让"事实"那一栏看起来也能搜 —— 它不能，而一个默默不生效的
  // 搜索框比没有搜索框更糟。
  const entryToolbar = (
    <div className="list-toolbar">
      <Input
        className="workspace-search"
        allowClear
        prefix={<SearchOutlined />}
        placeholder="搜索内容、实体或分类"
        value={memoryQuery}
        onChange={event => applyMemorySearch(event.target.value)}
      />
      <Select
        className="memory-category-filter"
        value={memoryCategory}
        options={categoryOptions}
        onChange={applyMemoryCategory}
      />
      <div className="list-toolbar-side">
        <Button icon={<ReloadOutlined />} onClick={() => void loadMemory()} size="small">
          刷新
        </Button>
        <Button type="primary" icon={<PlusOutlined />} onClick={openMemoryCreate}>
          新增记忆
        </Button>
        {/* The one irreversible button on this page, so it is the one that asks a
            second time. Deleting entries one at a time can be undone by saying the
            thing again; this removes the facts derived from them too. */}
        <Popconfirm
          title="清空全部记忆？"
          description={
            <span>
              笔记和从它们推出来的事实都会被删除，之后它不再记得任何事。
              <br />
              这个操作不能撤销。
            </span>
          }
          okText="清空"
          cancelText="取消"
          okButtonProps={{ danger: true }}
          onConfirm={() => void clearAllMemory()}
        >
          <Button size="small" danger icon={<DeleteOutlined />} disabled={memoryBusy}>
            清空
          </Button>
        </Popconfirm>
      </div>
    </div>
  )

  const factsToolbar = (
    <div className="list-toolbar">
      <div className="list-toolbar-side">
        <Button icon={<ReloadOutlined />} onClick={() => void loadMemory()} size="small">
          刷新
        </Button>
      </div>
    </div>
  )

  return (
    <div className="page-view memory-view">
      <p className="page-lead">
        它记得的关于你的事。改一条，就是改它下一轮的认知；删一条，就是让它忘掉。
      </p>

      {memoryUnavailable ? (
        unavailable
      ) : (
        <>
          {/* The same tab idiom as 扩展 / 自动化: the two multi-list pages of
              this app already read as one family, and a third tab strip that
              looked like antd's default would be a fourth visual language. */}
          <div className="schedule-tabs memory-tabs" role="tablist" aria-label="记忆视图">
            <button
              type="button"
              role="tab"
              aria-selected={memoryTab === 'entries'}
              className={memoryTab === 'entries' ? 'active' : ''}
              onClick={() => setMemoryTab('entries')}
            >
              记忆条目 · {memoryEntryTotal}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={memoryTab === 'facts'}
              className={memoryTab === 'facts' ? 'active' : ''}
              onClick={() => setMemoryTab('facts')}
            >
              事实 · {memoryFacts.length}
            </button>
          </div>

          {memoryTab === 'entries' ? (
            <>
              {entryToolbar}
              {entryList}
            </>
          ) : (
            <>
              {factsToolbar}
              {factList}
            </>
          )}
        </>
      )}

      <Modal
        open={memoryEditorOpen}
        title={memoryEditing ? '编辑这条记忆' : '手写一条记忆'}
        okText="保存"
        cancelText="取消"
        confirmLoading={memoryBusy}
        onOk={() => void saveMemoryEntry()}
        onCancel={closeMemoryEditor}
        destroyOnClose
      >
        <div className="memory-editor">
          <Input.TextArea
            autoFocus
            rows={5}
            maxLength={4000}
            showCount
            value={memoryDraft.content}
            placeholder="例如：我只喝手冲，不加糖。"
            onChange={event => updateMemoryDraft({ content: event.target.value })}
          />
          <div className="memory-editor-row">
            <AutoComplete
              className="memory-editor-category"
              value={memoryDraft.category}
              options={draftCategoryOptions}
              placeholder="分类"
              onChange={value => updateMemoryDraft({ category: value })}
            />
            <Input
              className="memory-editor-entity"
              value={memoryDraft.entity}
              placeholder="实体（可留空）"
              onChange={event => updateMemoryDraft({ entity: event.target.value })}
            />
          </div>
          <div className="memory-editor-row memory-editor-importance">
            <span>重要度</span>
            <Slider
              min={0}
              max={1}
              step={0.05}
              value={memoryDraft.importance}
              onChange={value => updateMemoryDraft({ importance: value as number })}
              tooltip={{ formatter: value => `${Math.round((value || 0) * 100)}%` }}
            />
            <span className="memory-editor-importance-value">
              {Math.round(memoryDraft.importance * 100)}%
            </span>
          </div>
        </div>
      </Modal>
    </div>
  )
}
