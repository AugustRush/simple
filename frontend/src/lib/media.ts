/** Media and file URL helpers: turning a path the model emitted into something the browser can load. */

import type { MediaKind } from '../types'


export function escapeHtml(s: string): string {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[c]!))
}


export const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'avif', 'ico'])

export const AUDIO_EXT = new Set(['mp3', 'wav', 'm4a', 'ogg', 'flac', 'aac', 'opus'])

export const VIDEO_EXT = new Set(['mp4', 'webm', 'mov', 'm4v', 'avi', 'mkv', 'ogv'])


export function mediaKindForUrl(url: string): MediaKind {
  if (!url) return 'file'
  let s = String(url)
  try {
    // /api/files?path=... — read the underlying path so the extension is true.
    const u = new URL(s, location.origin)
    const p = u.searchParams.get('path')
    if (p && !/^https?:/i.test(p)) return mediaKindForUrl(p)
  } catch {
    // not a parseable URL — fall through to extension sniffing
  }
  const clean = s.split('?')[0].split('#')[0].toLowerCase()
  const ext = (clean.split('.').pop() || '').trim()
  if (IMAGE_EXT.has(ext)) return 'image'
  if (AUDIO_EXT.has(ext)) return 'audio'
  if (VIDEO_EXT.has(ext)) return 'video'
  return 'file'
}


/** Build a /api/files link that names the session owning the file.
 *
 * The gateway treats a file URL as a read capability and only serves it to
 * the session that owns the file, so the session id is part of the link. */
export function fileHref(path: string, sessionId?: string | null, token?: string | null): string {
  const params = new URLSearchParams()
  params.set('path', path)
  if (sessionId) params.set('session_id', sessionId)
  if (token) params.set('token', token)
  return `/api/files?${params.toString()}`
}


/** Attach the owning session (and token) to a backend-supplied /api/files link. */
export function withFileSession(link: string, sessionId?: string | null, token?: string | null): string {
  if (!link) return ''
  let url: URL
  try {
    url = new URL(link, location.origin)
  } catch {
    return link
  }
  if (url.pathname !== '/api/files') return link
  if (sessionId && !url.searchParams.get('session_id')) url.searchParams.set('session_id', sessionId)
  if (token && !url.searchParams.get('token')) url.searchParams.set('token', token)
  return `${url.pathname}?${url.searchParams.toString()}`
}


/** Resolve an image target emitted by the model into something the browser can load. */
export function markdownMediaHref(
  rawTarget: string,
  sessionId?: string | null,
  token?: string | null,
): string {
  let target = rawTarget.trim()
  if (target.startsWith('<') && target.endsWith('>')) {
    target = target.slice(1, -1).trim()
  }
  // Markdown commonly escapes parentheses in filenames.
  target = target.replace(/\\([\\() ])/g, '$1')
  if (!target) return ''

  if (/^(?:https?:|data:|blob:)/i.test(target) || target.startsWith('//')) return target
  if (target.startsWith('/api/files?')) return withFileSession(target, sessionId, token)
  // Scheduled-run Markdown has a different task_id/run_id ownership model.
  // Without a session owner, preserve the target instead of manufacturing a
  // file URL that the gateway must correctly reject.
  if (!sessionId) return target

  if (/^file:/i.test(target)) {
    try {
      const url = new URL(target)
      target = decodeURIComponent(url.pathname)
      // file:///C:/path becomes /C:/path in URL.pathname.
      if (/^\/[A-Za-z]:\//.test(target)) target = target.slice(1)
    } catch {
      return target
    }
  }

  const isAbsoluteLocalPath = target.startsWith('/') || /^[A-Za-z]:[\\/]/.test(target)
  return isAbsoluteLocalPath ? fileHref(target, sessionId, token) : target
}


/**
 * Replace Markdown images while balancing parentheses in the destination.
 * A regex ending at the first `)` corrupts common names such as `result (1).png`.
 */
export function replaceMarkdownImages(
  text: string,
  render: (alt: string, target: string) => string,
): string {
  let output = ''
  let cursor = 0

  while (cursor < text.length) {
    const start = text.indexOf('![', cursor)
    if (start < 0) {
      output += text.slice(cursor)
      break
    }
    output += text.slice(cursor, start)

    let altEnd = start + 2
    while (altEnd < text.length) {
      if (text[altEnd] === ']' && text[altEnd - 1] !== '\\') break
      altEnd += 1
    }
    if (altEnd >= text.length || text[altEnd + 1] !== '(') {
      output += text[start]
      cursor = start + 1
      continue
    }

    let depth = 1
    let targetEnd = altEnd + 2
    while (targetEnd < text.length && depth > 0) {
      const char = text[targetEnd]
      const escaped = text[targetEnd - 1] === '\\'
      if (!escaped && char === '(') depth += 1
      if (!escaped && char === ')') depth -= 1
      targetEnd += 1
    }
    if (depth !== 0) {
      output += text[start]
      cursor = start + 1
      continue
    }

    const alt = text.slice(start + 2, altEnd).replace(/\\([\\\]])/g, '$1')
    const target = text.slice(altEnd + 2, targetEnd - 1)
    output += render(alt, target)
    cursor = targetEnd
  }

  return output
}
