/** Formatting helpers: dates, durations, sizes, paths and control labels. */

import { DEFAULT_THINKING_EFFORTS, MODEL_LABEL_FONT, THINKING_EFFORT_LABELS } from '../constants'
import dayjs from 'dayjs'


export const effortOptionsFrom = (efforts: unknown) => {
  const words = Array.isArray(efforts) && efforts.length > 0
    ? efforts.map(word => String(word))
    : DEFAULT_THINKING_EFFORTS
  return [
    { value: '', label: '默认（不干预）' },
    ...words.map(word => ({
      value: word,
      // A level the labels table has not met still has to be offered — the
      // server just said it accepts the word — so it shows as itself.
      label: THINKING_EFFORT_LABELS[word] || word,
    })),
  ]
}


/** The effort a provider's config already carries, or '' when it carries none. */
export const thinkingEffortOf = (provider: any): string => {
  const thinking = provider?.thinking
  if (typeof thinking === 'string') return thinking
  const effort = thinking?.effort
  return typeof effort === 'string' ? effort : ''
}


export let labelMeasureContext: CanvasRenderingContext2D | null | undefined


export function measureLabelWidth(text: string): number {
  if (!text || typeof document === 'undefined') return 0
  try {
    if (labelMeasureContext === undefined) {
      labelMeasureContext = document.createElement('canvas').getContext('2d')
    }
    if (!labelMeasureContext) return 0
    labelMeasureContext.font = MODEL_LABEL_FONT
    return labelMeasureContext.measureText(text).width
  } catch {
    return 0
  }
}


// Fallback for when canvas measurement is unavailable: at this size CJK is
// roughly twice as wide as Latin. It only ever over-estimates, so the control
// cannot end up too narrow to read its own label.
export function estimateLabelWidth(text: string): number {
  return [...text].reduce(
    (sum, ch) => sum + (ch.charCodeAt(0) > 0x2e7f ? 13.4 : 5.9),
    0,
  )
}


export function truncate(value: string, length = 64): string {
  return value.length > length ? `${value.slice(0, length)}…` : value
}


export function compactWorkspacePath(value: string, maxLength = 36): string {
  const path = String(value || '').trim()
  if (!path) return path
  const parts = path.split(/[\\/]+/).filter(Boolean)
  if (parts.length < 3) return truncate(path, maxLength)
  const tail = parts.slice(-2).join('/')
  const compact = `…/${tail}`
  return compact.length <= maxLength ? compact : truncate(compact, maxLength)
}


export function relativeTime(value?: string): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const diff = Date.now() - date.getTime()
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  return `${Math.floor(diff / 86_400_000)} 天前`
}


/**
 * Every absolute timestamp the user reads.
 *
 * These went through `new Date(x).toLocaleString()`, which follows the
 * *browser's* locale rather than the app's -- so an English browser rendered
 * `9/16/2026, 3:30:00 PM` inside an otherwise all-Chinese screen, on the same
 * card as a trigger reading `15:30`. Two clocks on one line, and the format
 * changed with the browser rather than with the app. dayjs is already imported
 * and already pinned to zh-cn, so these go through it and the format becomes
 * the app's decision.
 *
 * `relativeTime` above stays as it is: "3 分钟前" answers a different question
 * from "when exactly", and it does not depend on the locale.
 */
export function formatDateTime(value?: string | null, withSeconds = false): string {
  if (!value) return '—'
  const at = dayjs(value)
  if (!at.isValid()) return String(value)
  return at.format(withSeconds ? 'YYYY-MM-DD HH:mm:ss' : 'YYYY-MM-DD HH:mm')
}


export function formatScheduleDuration(durationMs?: number | null): string {
  if (durationMs === null || durationMs === undefined) return '—'
  if (durationMs < 1000) return `${durationMs} 毫秒`
  const seconds = Math.round(durationMs / 1000)
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`
}


export function formatFileSize(sizeBytes: number): string {
  if (sizeBytes < 1024) return `${sizeBytes} B`
  if (sizeBytes < 1024 * 1024) return `${Math.max(1, Math.round(sizeBytes / 1024))} KB`
  return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`
}
