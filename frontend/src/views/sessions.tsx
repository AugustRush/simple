/** The sessions view. Rendered by the App container from its context object. */

import type { AppCtx } from '../app/AppCtx'
import { relativeTime } from '../lib/format'
import {
  ClockCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  FolderOpenOutlined,
  MessageOutlined,
  MoreOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import {
  Badge,
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

export function renderSessions(ctx: AppCtx) {
  const { activeSession, allFilteredSessionsSelected, deleteSelectedSessions, deleteSession, filteredSessions, handleSessionContainerClick, loadingSessions, pageMeta, pendingDeleteSessionId, renameSession, revealSession, selectedSessionIds, sessionSearch, setPendingDeleteSessionId, setSelectedSessionIds, setSessionSearch } = ctx


  return (
    <div className="page-view">
      <div className="page-head">
        <div>
          <h2>{pageMeta.sessions.title}</h2>
          <p>{pageMeta.sessions.subtitle}</p>
        </div>
        <Space>
          {filteredSessions.length > 0 && (
            <Checkbox
              checked={allFilteredSessionsSelected}
              indeterminate={selectedSessionIds.length > 0 && !allFilteredSessionsSelected}
              onChange={event => {
                setSelectedSessionIds(event.target.checked
                  ? Array.from(new Set([...selectedSessionIds, ...filteredSessions.map(item => item.session_id)]))
                  : selectedSessionIds.filter(id => !filteredSessions.some(item => item.session_id === id)))
              }}
            >
              全选
            </Checkbox>
          )}
          {selectedSessionIds.length > 0 && (
            <Button danger icon={<DeleteOutlined />} onClick={deleteSelectedSessions}>
              删除选中 ({selectedSessionIds.length})
            </Button>
          )}
          <Input
            prefix={<SearchOutlined />}
            placeholder="搜索会话"
            value={sessionSearch}
            onChange={event => setSessionSearch(event.target.value)}
            allowClear
            style={{ width: 240 }}
          />
        </Space>
      </div>

      {loadingSessions ? (
        <Skeleton active paragraph={{ rows: 8 }} />
      ) : filteredSessions.length === 0 ? (
        <Empty description="没有匹配的会话" className="page-empty" />
      ) : (
        <Row gutter={[16, 16]}>
          {filteredSessions.map(item => (
            <Col xs={24} sm={12} xl={8} key={item.session_id}>
              <Card
                className={`session-card ${item.session_id === activeSession ? 'session-card-active' : ''}`}
                hoverable
                role="button"
                tabIndex={0}
                aria-label={`打开会话 ${item.title || '未命名会话'}`}
                aria-current={item.session_id === activeSession ? 'true' : undefined}
                onClick={event => handleSessionContainerClick(event, item.session_id)}
                onKeyDown={event => {
                  // The card holds its own checkbox and delete buttons; without
                  // this, activating one of those with the keyboard would also
                  // open the session behind it.
                  if (event.target !== event.currentTarget) return
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    handleSessionContainerClick(event, item.session_id)
                  }
                }}
              >
                <div className="session-card-head">
                  <div className="session-card-title">
                    <Checkbox
                      checked={selectedSessionIds.includes(item.session_id)}
                      onClick={event => event.stopPropagation()}
                      onChange={event => {
                        setSelectedSessionIds(current => event.target.checked
                          ? [...current, item.session_id]
                          : current.filter(id => id !== item.session_id))
                      }}
                    />
                    <span>{item.title || '未命名会话'}</span>
                    {item.live && <Badge status="processing" />}
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
                            onClick: () => renameSession(item),
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
                        onClick={event => event.stopPropagation()}
                      />
                    </Dropdown>
                  )}
                </div>
                <div className="session-card-id">
                  <Typography.Text code>{item.session_id.slice(0, 18)}</Typography.Text>
                </div>
                <div className="session-card-footer">
                  <Tag>
                    {item.live ? '动态会话' : '持久会话'}
                  </Tag>
                  <span>
                    <MessageOutlined /> {item.turn_count || 0} 轮
                  </span>
                  <span>
                    <ClockCircleOutlined /> {relativeTime(item.last_activity)}
                  </span>
                </div>
              </Card>
            </Col>
          ))}
        </Row>
      )}
    </div>
  )
}
