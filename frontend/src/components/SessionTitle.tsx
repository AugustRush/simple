/** A session's title that turns into an input where it stands. */

import type { SessionInfo } from '../types'
import { useEffect, useRef, useState } from 'react'

type Props = {
  item: SessionInfo
  editing: boolean
  className?: string
  // Off where a single click on the row already leaves the page (the session
  // cards): the second click of a double-click would never arrive there, and a
  // tooltip promising it would be a lie.
  doubleClickToEdit?: boolean
  onStartEdit: () => void
  onCommit: (title: string) => void
  onCancel: () => void
}

export function SessionTitle({ item, editing, className, doubleClickToEdit = true, onStartEdit, onCommit, onCancel }: Props) {
  const label = item.title || '未命名会话'

  if (editing) {
    return <TitleInput initial={item.title || ''} onCommit={onCommit} onCancel={onCancel} />
  }
  if (!doubleClickToEdit) {
    return <span className={className} title={label}>{label}</span>
  }
  return (
    <span
      className={className}
      title={`${label}（双击重命名）`}
      onDoubleClick={event => {
        event.stopPropagation()
        onStartEdit()
      }}
    >
      {label}
    </span>
  )
}

// Mounted per edit, so the draft is seeded once from the title as it was when
// the edit began: the list refreshes underneath while a turn runs, and that
// must not wipe what is being typed.
function TitleInput({ initial, onCommit, onCancel }: { initial: string, onCommit: (title: string) => void, onCancel: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [draft, setDraft] = useState(initial)
  // Enter commits and unmounts the input, and some browsers then fire a blur
  // on the way out; without this the one edit would be sent twice.
  const settledRef = useRef(false)

  useEffect(() => {
    // A frame later rather than autoFocus: opened from the row's menu, the
    // menu is still closing on this frame and can take focus back with it,
    // and an input that lost focus at once would read that as "done".
    const frame = requestAnimationFrame(() => {
      inputRef.current?.focus()
      inputRef.current?.select()
    })
    return () => cancelAnimationFrame(frame)
  }, [])

  const settle = (commit: boolean) => {
    if (settledRef.current) return
    settledRef.current = true
    if (commit) onCommit(draft)
    else onCancel()
  }

  // Enter/Escape unmount the focused input, which would drop focus to <body>
  // and lose a keyboard user's place in the list. It goes back to the row (or
  // card) the rename was started from. A blur is left alone: focus is already
  // somewhere the person chose.
  const settleFromKeyboard = (commit: boolean) => {
    const home = inputRef.current?.parentElement?.closest<HTMLElement>('[tabindex="0"]')
    settle(commit)
    home?.focus()
  }

  return (
    <input
      ref={inputRef}
      className="session-title-input"
      value={draft}
      maxLength={120}
      placeholder="会话标题"
      aria-label="会话标题"
      // The row around this is itself a button: a click in here would open
      // the session, and Enter/Space would be read as activating the row.
      onClick={event => event.stopPropagation()}
      onDoubleClick={event => event.stopPropagation()}
      onMouseDown={event => event.stopPropagation()}
      onChange={event => setDraft(event.target.value)}
      onKeyDown={event => {
        event.stopPropagation()
        // Enter while an IME is composing picks a candidate; it is not "save".
        if (event.nativeEvent.isComposing || event.keyCode === 229) return
        if (event.key === 'Enter') {
          event.preventDefault()
          settleFromKeyboard(true)
        } else if (event.key === 'Escape') {
          event.preventDefault()
          settleFromKeyboard(false)
        }
      }}
      onBlur={() => settle(true)}
    />
  )
}
