/** The sessions view. Rendered by the App container from its context object. */

import type { MouseEvent as ReactMouseEvent } from 'react'
import type { AppCtx } from '../app/AppCtx'
import type { SessionInfo } from '../types'
import { formatDateTime, relativeTime } from '../lib/format'
import { isSessionBusy, sessionStatusOf } from '../lib/tools'
import { SessionTitle } from '../components/SessionTitle'
import {
  ClockCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  FolderOpenOutlined,
  MessageOutlined,
  MoreOutlined,
  PlusOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import {
  Button,
  Card,
  Checkbox,
  Col,
  Dropdown,
  Empty,
  Input,
  Row,
  Skeleton,
  Space,
  Tag,
  Typography,
} from 'antd'

// Anything inside a card that is its own control. In selection mode a click
// on the card toggles it, and these must keep doing their own thing instead.
const CARD_CONTROLS = '.ant-checkbox-wrapper, .ant-btn, .ant-dropdown, .ant-dropdown-trigger, .ant-typography-copy, .session-item-delete-actions, input'

export function renderSessions(ctx: AppCtx) {
  const { activeSession, allFilteredSessionsSelected, commitSessionTitle, createSession, creatingSession, deleteSelectedSessions, deleteSession, filteredSessions, handleSessionContainerClick, loadingSessions, pageMeta, pendingDeleteSessionId, renamingSession, revealSession, selectedSessionIds, sessionSearch, sessions, setPendingDeleteSessionId, setRenamingSession, setSelectedSessionIds, setSessionSearch } = ctx

  // A selected id can outlive its session (deleted from the sidebar, or by
  // another tab); counting those would put a number on the delete button
  // that the confirm dialog then contradicts.
  const existing = new Set(sessions.map(item => item.session_id))
  const selectedCount = selectedSessionIds.filter(id => existing.has(id)).length
  const selecting = selectedCount > 0
  const query = sessionSearch.trim()

  const toggleSelected = (sid: string) => {
    setSelectedSessionIds(current => current.includes(sid)
      ? current.filter(id => id !== sid)
      : [...current, sid])
  }

  const activateCard = (event: ReactMouseEvent<HTMLElement>, sid: string) => {
    if (!selecting) {
      handleSessionContainerClick(event, sid)
      return
    }
    // Once something is selected, a click on a card means "this one too" --
    // opening a session would throw the selection away with the page.
    if ((event.target as HTMLElement | null)?.closest(CARD_CONTROLS)) return
    toggleSelected(sid)
  }

  // `filteredSessions` is already ordered busy, then live, then by recency,
  // so each group below is a contiguous run of it and keeps that order.
  const groups = [
    { key: 'busy', label: '进行中', items: filteredSessions.filter(item => isSessionBusy(item)) },
    { key: 'live', label: '动态会话', hint: '已加载到运行时，可直接继续', items: filteredSessions.filter(item => !isSessionBusy(item) && item.live) },
    { key: 'history', label: '历史会话', hint: '打开时从记录中恢复', items: filteredSessions.filter(item => !isSessionBusy(item) && !item.live) },
  ].filter(group => group.items.length > 0)

  const renderCard = (item: SessionInfo) => {
    const selected = selectedSessionIds.includes(item.session_id)
    const cardKey = `card:${item.session_id}`
    const name = item.title || '未命名会话'
    const status = sessionStatusOf(item)
    return (
      <Col xs={24} sm={12} xl={8} key={item.session_id}>
        <Card
          className={[
            'session-card',
            item.session_id === activeSession ? 'session-card-active' : '',
            selected ? 'session-card-selected' : '',
          ].filter(Boolean).join(' ')}
          hoverable
          role="button"
          tabIndex={0}
          aria-label={selecting ? `${selected ? '取消选择' : '选择'}会话 ${name}` : `打开会话 ${name}`}
          aria-pressed={selecting ? selected : undefined}
          aria-current={item.session_id === activeSession ? 'true' : undefined}
          onClick={event => activateCard(event, item.session_id)}
          onKeyDown={event => {
            // The card holds its own checkbox and delete buttons; without
            // this, activating one of those with the keyboard would also
            // open the session behind it.
            if (event.target !== event.currentTarget) return
            if (event.key === 'F2') {
              event.preventDefault()
              setRenamingSession(cardKey)
              return
            }
            if (event.key === 'Escape' && selecting) {
              event.preventDefault()
              setSelectedSessionIds([])
              return
            }
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault()
              if (selecting) toggleSelected(item.session_id)
              else handleSessionContainerClick(event, item.session_id)
            }
          }}
        >
          <div className="session-card-head">
            <div className="session-card-title">
              <Checkbox
                checked={selected}
                aria-label={`选择 ${name}`}
                onClick={event => event.stopPropagation()}
                onChange={() => toggleSelected(item.session_id)}
              />
              <SessionTitle
                item={item}
                className="session-card-title-text"
                doubleClickToEdit={false}
                editing={renamingSession === cardKey}
                onStartEdit={() => setRenamingSession(cardKey)}
                onCommit={title => void commitSessionTitle(item, title)}
                onCancel={() => setRenamingSession(null)}
              />
              {renamingSession !== cardKey && (
                // A click on the card opens the session, so renaming
                // here needs its own target rather than a double-click.
                <Button
                  type="text"
                  size="small"
                  className="session-card-rename"
                  icon={<EditOutlined />}
                  aria-label={`重命名 ${name}`}
                  onClick={event => {
                    event.stopPropagation()
                    setRenamingSession(cardKey)
                  }}
                />
              )}
            </div>
            {pendingDeleteSessionId === item.session_id ? (
              <div
                className="session-item-delete-actions session-card-delete-actions"
                onClick={event => event.stopPropagation()}
              >
                <Button
                  type="text"
                  danger
                  size="small"
                  onClick={() => deleteSession(item)}
                >
                  删除
                </Button>
                <Button
                  type="text"
                  size="small"
                  onClick={() => setPendingDeleteSessionId(null)}
                >
                  取消
                </Button>
              </div>
            ) : (
              <Dropdown
                trigger={['click']}
                menu={{
                  onClick: ({ domEvent }) => domEvent.stopPropagation(),
                  items: [
                    {
                      key: 'reveal',
                      label: '在 Finder 中显示',
                      icon: <FolderOpenOutlined />,
                      onClick: () => revealSession(item),
                    },
                    {
                      key: 'rename',
                      label: '重命名',
                      icon: <EditOutlined />,
                      onClick: () => setRenamingSession(cardKey),
                    },
                    {
                      key: 'delete',
                      label: '删除',
                      icon: <DeleteOutlined />,
                      danger: true,
                      onClick: () => setPendingDeleteSessionId(item.session_id),
                    },
                  ],
                }}
              >
                <Button
                  type="text"
                  size="small"
                  icon={<MoreOutlined />}
                  aria-label={`更多操作 ${name}`}
                  onClick={event => event.stopPropagation()}
                />
              </Dropdown>
            )}
          </div>
          <div className="session-card-id">
            {/* Only the head of the id fits, so copy hands over all of it --
                a truncated id is useless in a log search or a CLI flag. */}
            <Typography.Text
              code
              title={item.session_id}
              copyable={{ text: item.session_id, tooltips: ['复制会话 ID', '已复制'] }}
            >
              {item.session_id.slice(0, 18)}
            </Typography.Text>
          </div>
          <div className="session-card-footer">
            {status && (
              <Tag color={item.status === 'queued' ? 'gold' : 'processing'}>
                {status}
              </Tag>
            )}
            <span>
              <MessageOutlined /> {item.turn_count || 0} 轮
            </span>
            <span title={formatDateTime(item.last_activity, true)}>
              <ClockCircleOutlined /> {relativeTime(item.last_activity)}
            </span>
          </div>
        </Card>
      </Col>
    )
  }

  const renderEmpty = () => {
    if (sessions.length === 0) {
      return (
        <Empty description="还没有会话" className="page-empty">
          <Button type="primary" icon={<PlusOutlined />} loading={creatingSession} onClick={() => void createSession()}>
            新建会话
          </Button>
        </Empty>
      )
    }
    return (
      <Empty description={query ? `没有匹配「${query}」的会话` : '没有匹配的会话'} className="page-empty">
        {query && <Button onClick={() => setSessionSearch('')}>清除搜索</Button>}
      </Empty>
    )
  }

  return (
    <div className="page-view">
      <div className="page-head">
        <div>
          <h2>{pageMeta.sessions.title}</h2>
          <p>{pageMeta.sessions.subtitle}</p>
        </div>
        <Space wrap>
          {filteredSessions.length > 0 && (
            <Checkbox
              checked={allFilteredSessionsSelected}
              indeterminate={selecting && !allFilteredSessionsSelected}
              onChange={event => {
                setSelectedSessionIds(event.target.checked
                  ? Array.from(new Set([...selectedSessionIds, ...filteredSessions.map(item => item.session_id)]))
                  : selectedSessionIds.filter(id => !filteredSessions.some(item => item.session_id === id)))
              }}
            >
              全选
            </Checkbox>
          )}
          <Input
            prefix={<SearchOutlined />}
            placeholder="搜索会话"
            value={sessionSearch}
            onChange={event => setSessionSearch(event.target.value)}
            allowClear
            className="sessions-search"
          />
        </Space>
      </div>

      {selecting && (
        <div className="sessions-selection-bar" role="toolbar" aria-label="批量操作">
          <span className="sessions-selection-count">已选 {selectedCount} 个会话</span>
          <span className="sessions-selection-hint">点击卡片可继续选择</span>
          <Space size={8} className="sessions-selection-actions">
            <Button size="small" onClick={() => setSelectedSessionIds([])}>
              取消选择
            </Button>
            <Button size="small" danger icon={<DeleteOutlined />} onClick={deleteSelectedSessions}>
              删除选中
            </Button>
          </Space>
        </div>
      )}

      {loadingSessions ? (
        <Skeleton active paragraph={{ rows: 8 }} />
      ) : filteredSessions.length === 0 ? (
        renderEmpty()
      ) : (
        groups.map(group => (
          <section className="session-group" key={group.key} aria-label={group.label}>
            <div className="session-group-head">
              <span className="session-group-label">{group.label}</span>
              <span className="session-group-count">{group.items.length}</span>
              {group.hint && <span className="session-group-hint">{group.hint}</span>}
            </div>
            <Row gutter={[16, 16]}>
              {group.items.map(renderCard)}
            </Row>
          </section>
        ))
      )}
    </div>
  )
}
