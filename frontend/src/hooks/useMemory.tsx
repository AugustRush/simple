/** 记忆页的状态与动作。
 *
 * 这个 hook 只有一个不寻常的约定：**读用裸 fetch，写用 `api`**。
 *
 * - 写（新增/编辑/删除/忘记）是人按下去的，失败要让他知道 —— 这正是 `api`
 *   的 toast 语义。
 * - 读（列表、概览、事实）是页面自己发起的，`api` 的 toast 会让"记忆未启用"
 *   这一个状态在每次进入页面时弹一次；更糟的是它把 503（这个部署没有记忆）
 *   和网络抖动混成同一个错误。所以读的失败回到调用方，由页面分别呈现：
 *   503 是一段说明加一个去设置的链接，其它失败是"重试"。
 *
 * 搜索是 250ms 防抖的：每敲一个字发三个请求，对一个只存着几百条记忆的库来说
 * 不是性能问题，但它会把"正在搜索"的中间态渲染成一串空列表。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { MemoryDraft, MemoryEntry, MemoryFact, MemoryOverview } from '../types'
import { useUi } from './useUi'
import { useApiClient } from './useApiClient'

type Deps = ReturnType<typeof useUi> & ReturnType<typeof useApiClient>

//: 一页多少条。比屏幕高，是为了让"还有 N 条"出现得少一些；比库小，是为了
//: 不让一次几 MB 的 JSON 卡住渲染。
const MEMORY_PAGE_SIZE = 50

const EMPTY_DRAFT: MemoryDraft = { content: '', category: '', entity: '', importance: 0.8 }

export function useMemory(deps: Deps) {
  const { api, apiHeaders, messageApi, setLoadingView, view } = deps

  const [memoryOverview, setMemoryOverview] = useState<MemoryOverview | null>(null)
  const [memoryEntries, setMemoryEntries] = useState<MemoryEntry[]>([])
  const [memoryEntryTotal, setMemoryEntryTotal] = useState(0)
  const [memoryFacts, setMemoryFacts] = useState<MemoryFact[]>([])
  const [memoryQuery, setMemoryQuery] = useState('')
  const [memoryCategory, setMemoryCategory] = useState('')
  const [memoryTab, setMemoryTab] = useState<'entries' | 'facts'>('entries')
  const [memoryBusy, setMemoryBusy] = useState(false)
  //: True when the deployment has no memory service at all (503), which is a
  //: different page than "loading" or "nothing remembered yet".
  const [memoryUnavailable, setMemoryUnavailable] = useState(false)
  const [memoryLimit, setMemoryLimit] = useState(MEMORY_PAGE_SIZE)
  const [memoryEditorOpen, setMemoryEditorOpen] = useState(false)
  //: ``null`` means "adding"; an entry means "editing that one".
  const [memoryEditing, setMemoryEditing] = useState<MemoryEntry | null>(null)
  const [memoryDraft, setMemoryDraft] = useState<MemoryDraft>(EMPTY_DRAFT)

  //: Guards against a slow response landing after a newer one: the last
  //: request to leave decides. Without it, typing "咖" then "咖啡" can render the
  //: results for "咖" after the results for "咖啡".
  const requestSeq = useRef(0)

  type ReadResult = { payload?: unknown; unavailable?: boolean }

  const readJson = useCallback(
    async (path: string): Promise<ReadResult | null> => {
      try {
        const resp = await fetch(path, { headers: apiHeaders() })
        if (resp.status === 503) return { unavailable: true }
        if (!resp.ok) return null
        return { payload: await resp.json() }
      } catch {
        return null
      }
    },
    [apiHeaders],
  )

  const loadMemory = useCallback(
    async (options: { silent?: boolean } = {}) => {
      const seq = ++requestSeq.current
      if (!options.silent) setLoadingView(true)
      const params = new URLSearchParams()
      const query = memoryQuery.trim()
      if (query) params.set('q', query)
      if (memoryCategory) params.set('category', memoryCategory)
      params.set('limit', String(memoryLimit))

      const [overview, entries, facts] = await Promise.all([
        readJson('/api/memory'),
        readJson(`/api/memory/entries?${params.toString()}`),
        readJson('/api/memory/facts'),
      ])
      if (seq !== requestSeq.current) return
      if (!options.silent) setLoadingView(false)

      if (overview?.unavailable || entries?.unavailable || facts?.unavailable) {
        setMemoryUnavailable(true)
        setMemoryBusy(false)
        return
      }
      if (!overview?.payload || !entries?.payload || !facts?.payload) {
        // A transient failure: keep whatever is on screen rather than blanking
        // it, and say so once. A retry is the page's own button, not a loop.
        messageApi.error('读取记忆失败')
        setMemoryBusy(false)
        return
      }
      setMemoryUnavailable(false)
      setMemoryOverview((overview.payload as MemoryOverview) || null)
      const entryPayload = entries.payload as { entries?: MemoryEntry[]; total?: number }
      setMemoryEntries(entryPayload.entries || [])
      setMemoryEntryTotal(entryPayload.total || 0)
      const factPayload = facts.payload as { facts?: MemoryFact[] }
      setMemoryFacts(factPayload.facts || [])
    },
    [memoryCategory, memoryLimit, memoryQuery, messageApi, readJson, setLoadingView],
  )

  // The page owns its own loading: opening it, changing the filter, and typing
  // in the search box all go through this one effect, so there is one place
  // that decides when to ask and one place to debounce.
  useEffect(() => {
    if (view !== 'memory') return
    const timer = window.setTimeout(() => {
      void loadMemory({ silent: true })
    }, 250)
    return () => window.clearTimeout(timer)
  }, [view, loadMemory])

  const updateMemoryDraft = useCallback((patch: Partial<MemoryDraft>) => {
    setMemoryDraft(current => ({ ...current, ...patch }))
  }, [])

  const applyMemorySearch = useCallback((value: string) => {
    setMemoryQuery(value)
    // A new filter resets the window: page 3 of the old result set is not a
    // place the new one has.
    setMemoryLimit(MEMORY_PAGE_SIZE)
  }, [])

  const applyMemoryCategory = useCallback((value: string) => {
    setMemoryCategory(value)
    setMemoryLimit(MEMORY_PAGE_SIZE)
  }, [])

  const loadMoreMemory = useCallback(() => {
    setMemoryLimit(current => current + MEMORY_PAGE_SIZE)
  }, [])

  const openMemoryCreate = useCallback(() => {
    setMemoryEditing(null)
    setMemoryDraft({ ...EMPTY_DRAFT, category: memoryCategory || '' })
    setMemoryEditorOpen(true)
  }, [memoryCategory])

  const openMemoryEdit = useCallback((entry: MemoryEntry) => {
    setMemoryEditing(entry)
    setMemoryDraft({
      content: entry.content,
      category: entry.category,
      entity: entry.entity,
      importance: entry.importance,
    })
    setMemoryEditorOpen(true)
  }, [])

  const closeMemoryEditor = useCallback(() => {
    setMemoryEditorOpen(false)
  }, [])

  const saveMemoryEntry = useCallback(async () => {
    const content = memoryDraft.content.trim()
    if (!content) {
      messageApi.error('记忆内容不能为空')
      return
    }
    setMemoryBusy(true)
    try {
      const body = JSON.stringify({
        content,
        category: memoryDraft.category.trim(),
        entity: memoryDraft.entity.trim(),
        importance: memoryDraft.importance,
      })
      if (memoryEditing) {
        await api(`/api/memory/entries/${encodeURIComponent(memoryEditing.id)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body,
        })
        messageApi.success('已更新这条记忆')
      } else {
        await api('/api/memory/entries', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body,
        })
        messageApi.success('已记住')
      }
      setMemoryEditorOpen(false)
      await loadMemory({ silent: true })
    } catch {
      // `api` already reported it, and the draft stays open so the words are
      // not lost to a failed save.
    } finally {
      setMemoryBusy(false)
    }
  }, [api, loadMemory, memoryDraft, memoryEditing, messageApi])

  /** Entries whose full text is showing.
   *
   * Memory entries are journal dumps -- one of them is forty lines of markdown. All
   * of them expanded turns the page into a wall you cannot scan or tick your way
   * through, which is the opposite of what a list is for. So the list shows a
   * four-line excerpt and expanding one is an explicit act.
   *
   * Kept here rather than in the view because views here are plain render
   * functions: a `useState` inside one runs only while that view is current, and
   * React counts hooks per component -- a varying count is error #300, a blank page.
   */
  const [expandedMemoryIds, setExpandedMemoryIds] = useState<string[]>([])

  const toggleMemoryExpanded = useCallback((entryId: string) => {
    setExpandedMemoryIds(current =>
      current.includes(entryId)
        ? current.filter(id => id !== entryId)
        : [...current, entryId],
    )
  }, [])

  /** The ids the page has ticked. Kept here so the bar and the rows agree. */
  const [selectedMemoryIds, setSelectedMemoryIds] = useState<string[]>([])

  const toggleMemorySelected = useCallback((entryId: string) => {
    setSelectedMemoryIds(current =>
      current.includes(entryId)
        ? current.filter(id => id !== entryId)
        : [...current, entryId],
    )
  }, [])

  const clearMemorySelection = useCallback(() => setSelectedMemoryIds([]), [])

  /** Delete every ticked entry in one request.
   *
   * One request, not one per row: the rows are independent, so a loop can stop
   * halfway -- and a half-cleaned memory is worse than either end state. The
   * server reports what it could not remove, and the selection keeps the stragglers
   * so the next attempt is a retry rather than a re-selection.
   */
  const deleteSelectedMemoryEntries = useCallback(async () => {
    if (selectedMemoryIds.length === 0) return
    setMemoryBusy(true)
    try {
      const resp = await api('/api/memory/entries/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: selectedMemoryIds }),
      })
      const data = await resp.json().catch(() => ({}))
      const removed: string[] = Array.isArray(data.removed) ? data.removed : []
      const missing: string[] = Array.isArray(data.missing) ? data.missing : []
      const gone = new Set(removed)
      setMemoryEntries(current => current.filter(item => !gone.has(item.id)))
      setMemoryEntryTotal(current => Math.max(0, current - removed.length))
      setSelectedMemoryIds(missing)
      if (removed.length) messageApi.success(`已删除 ${removed.length} 条`)
      if (missing.length) {
        messageApi.warning(`${missing.length} 条没能删除，已保留选中`)
      }
      await loadMemory({ silent: true })
    } catch {
      // Reported by `api`; the selection survives so the retry is one click.
    } finally {
      setMemoryBusy(false)
    }
  }, [api, loadMemory, messageApi, selectedMemoryIds])

  /** Erase everything the model can retrieve.
   *
   * Not a longer version of the bulk delete: this also drops facts resolved from
   * the assertion ledger, which is why the server's own `clear` is the only thing
   * that should run it. A page that looped entries would leave every belief.
   */
  const clearAllMemory = useCallback(async () => {
    setMemoryBusy(true)
    try {
      const resp = await api('/api/memory/clear', { method: 'POST' })
      const data = await resp.json().catch(() => ({}))
      if (!resp.ok) {
        messageApi.warning(String(data.error || '清空失败'))
        return
      }
      setSelectedMemoryIds([])
      messageApi.success(`已清空 ${Number(data.deleted || 0)} 项`)
      await loadMemory({ silent: true })
    } catch {
      // Reported by `api`.
    } finally {
      setMemoryBusy(false)
    }
  }, [api, loadMemory, messageApi])

  const deleteMemoryEntry = useCallback(
    async (entry: MemoryEntry) => {
      setMemoryBusy(true)
      try {
        await api(`/api/memory/entries/${encodeURIComponent(entry.id)}`, { method: 'DELETE' })
        // Optimistic removal *after* the server agreed: a row that vanishes
        // and comes back because the delete failed is the worse flicker.
        setMemoryEntries(current => current.filter(item => item.id !== entry.id))
        setMemoryEntryTotal(current => Math.max(0, current - 1))
        messageApi.success('已删除')
        await loadMemory({ silent: true })
      } catch {
        // Reported by `api`.
      } finally {
        setMemoryBusy(false)
      }
    },
    [api, loadMemory, messageApi],
  )

  const toggleMemoryPin = useCallback(
    async (entry: MemoryEntry) => {
      const pinned = !entry.pinned
      setMemoryBusy(true)
      try {
        const resp = await api(
          `/api/memory/entries/${encodeURIComponent(entry.id)}`,
          {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pinned }),
          },
        )
        const payload = await resp.json()
        if (payload?.entry) {
          setMemoryEntries(current =>
            current.map(item => (item.id === entry.id ? (payload.entry as MemoryEntry) : item)),
          )
        }
        messageApi.success(pinned ? '已钉住，不再随时间淡出' : '已取消钉住')
      } catch {
        // Reported by `api`.
      } finally {
        setMemoryBusy(false)
      }
    },
    [api, messageApi],
  )

  const forgetMemoryFact = useCallback(
    async (fact: MemoryFact) => {
      setMemoryBusy(true)
      try {
        await api('/api/memory/facts/forget', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            subject: fact.subject,
            predicate: fact.predicate,
            scope: fact.scope,
          }),
        })
        // The evidence is archived rather than erased, so a re-consolidation
        // that reads the same sentence can resolve the fact again -- which is
        // why this is "忘记" and not "删除".
        setMemoryFacts(current => current.filter(item => item.fact_key !== fact.fact_key))
        messageApi.success('已忘记这条；它不会再出现在对事实的引用里')
        await loadMemory({ silent: true })
      } catch {
        // Reported by `api`.
      } finally {
        setMemoryBusy(false)
      }
    },
    [api, loadMemory, messageApi],
  )

  const memoryCategoryOptions = useMemo(() => {
    const names = new Set<string>()
    for (const category of memoryOverview?.categories || []) names.add(category.name)
    for (const entry of memoryEntries) names.add(entry.category)
    return [...names].sort((left, right) => left.localeCompare(right))
  }, [memoryEntries, memoryOverview])

  return {
    applyMemoryCategory,
    applyMemorySearch,
    closeMemoryEditor,
    clearAllMemory,
    clearMemorySelection,
    expandedMemoryIds,
    toggleMemoryExpanded,
    deleteMemoryEntry,
    deleteSelectedMemoryEntries,
    selectedMemoryIds,
    toggleMemorySelected,
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
    memoryLimit,
    memoryOverview,
    memoryQuery,
    memoryTab,
    memoryUnavailable,
    openMemoryCreate,
    openMemoryEdit,
    saveMemoryEntry,
    setMemoryTab,
    toggleMemoryPin,
    updateMemoryDraft,
  } as const
}
