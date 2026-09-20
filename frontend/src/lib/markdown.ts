/** Minimal Markdown-to-HTML renderer used by the transcript. */

import { escapeHtml, markdownMediaHref, replaceMarkdownImages } from './media'


export function markdownToHtml(
  text: string,
  sessionId?: string | null,
  token?: string | null,
): string {
  let t = String(text || '')
  const codeBlocks: string[] = []
  const images: string[] = []

  // Replace fenced code with placeholders so line-level parsing can't corrupt it.
  t = t.replace(
    /```([\w-]*)[ \t]*\n?([\s\S]*?)```/g,
    (_m, lang: string, code: string) => {
      const label = lang ? escapeHtml(lang) : 'code'
      const body = code.replace(/\n$/, '')
      const index = codeBlocks.length
      codeBlocks.push(
        `<div class="code-block"><div class="code-block-head"><span>${label}</span></div>` +
        `<pre><code>${escapeHtml(body)}</code></pre></div>`,
      )
      return `\u0000CODE${index}\u0000`
    },
  )

  t = replaceMarkdownImages(t, (alt, target) => {
    const index = images.length
    const href = markdownMediaHref(target, sessionId, token)
    images.push(
      `<img class="md-img" src="${escapeHtml(href)}" alt="${escapeHtml(alt)}" loading="lazy" />`,
    )
    return `\u0000IMAGE${index}\u0000`
  })

  t = escapeHtml(t)

  t = t.replace(/`([^`]+)`/g, '<code>$1</code>')
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  t = t.replace(/\*([^*]+)\*/g, '<em>$1</em>')
  t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')

  const lines = t.split('\n')
  const output: string[] = []
  let listOpen = false

  for (const line of lines) {
    const listItem = line.match(/^\s*[-*]\s+(.*)$/)
    if (listItem) {
      if (!listOpen) {
        output.push('<ul>')
        listOpen = true
      }
      output.push(`<li>${listItem[1]}</li>`)
      continue
    }

    if (listOpen) {
      output.push('</ul>')
      listOpen = false
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/)
    if (heading) {
      const level = heading[1].length
      output.push(`<h${level}>${heading[2]}</h${level}>`)
    } else {
      output.push(line)
    }
  }

  if (listOpen) output.push('</ul>')
  t = output.join('\n')
  t = t.replace(/\u0000CODE(\d+)\u0000/g, (_m, index: string) => codeBlocks[Number(index)] || '')
  t = t.replace(/\u0000IMAGE(\d+)\u0000/g, (_m, index: string) => images[Number(index)] || '')
  t = t.replace(/\n{2,}/g, '<br /><br />')
  t = t.replace(/\n/g, '<br />')

  return t
}
