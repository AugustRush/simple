/** The capability lists: plugins and skills, as a card on 设置.
 *
 * Why not a page
 * --------------
 * 扩展 was a destination of its own, and it was the wrong shape for what it
 * holds. A page answers "where am I"; these two lists answer "what can it do",
 * which is a question you have while changing a setting, not one you go
 * somewhere to ask. Everything on this screen is a switch or a delete -- the
 * same verbs as the settings page it now lives on. The old page also had to
 * carry its own title bar, its own lead sentence and a tab strip, three
 * affordances around two lists that each fit on a card.
 */

import type { AppCtx } from '../app/AppCtx'
import {
  ApiOutlined,
  CopyOutlined,
  DeleteOutlined,
  SearchOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { Button, Card, Col, Empty, Input, Row, Skeleton, Switch, Tag } from 'antd'

export function renderCapabilities(ctx: AppCtx) {
  const { copyMessage, deletePlugin, deleteSkill, extensionsTab, filteredPlugins, filteredSkills, loadingView, pluginSearch, setExtensionsTab, setPluginSearch, setSkillFilter, setSkillSearch, skillFilter, skills, skillSearch, togglePlugin, toggleSkill } = ctx


  // The two bodies behind the merged 扩展 page.  Each keeps its own toolbar
  // and its own empty state; what they lose is their separate page shells,
  // which the tabs below the page head replace.
  const renderPluginsBody = () => (
    <>
      <div className="list-toolbar">
        <Input
          prefix={<SearchOutlined />}
          placeholder="搜索插件"
          value={pluginSearch}
          onChange={event => setPluginSearch(event.target.value)}
          allowClear
          className="workspace-search"
        />
      </div>

      {loadingView ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : filteredPlugins.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          // An empty list that only says "empty" leaves the reader to guess how it
          // fills. Plugins arrive one of two ways, and both are one sentence away.
          description="还没有插件。可以让 agent 在对话里装一个（它知道从哪里找），或者把插件目录放进 ~/.agent/plugins/。"
          className="page-empty"
        />
      ) : (
        <Row gutter={[16, 16]}>
          {filteredPlugins.map(item => (
            <Col xs={24} md={12} xl={8} key={item.name}>
              <Card className="entity-card">
                <div className="entity-card-head">
                  <span className="entity-icon">
                    <ThunderboltOutlined />
                  </span>
                  <div className="entity-title">
                    <strong>{item.name}</strong>
                    <small>v{item.version || '—'}</small>
                  </div>
                  <Switch
                    size="small"
                    checked={item.enabled}
                    onChange={checked => togglePlugin(item, checked)}
                  />
                  {item.source === 'user' && <Button type="text" danger size="small" icon={<DeleteOutlined />} onClick={() => deletePlugin(item)} />}
                </div>
                <p className="entity-description">
                  {item.description || '暂无描述'}
                </p>
                <div className="entity-meta">
                  <span>来源</span>
                  <code>{item.source || '-'}</code>
                </div>
              </Card>
            </Col>
          ))}
        </Row>
      )}
    </>
  )

  const renderSkillsBody = () => (
    <>
      <div className="skills-summary">
        <span><strong>{skills.length}</strong> 全部</span>
        <span><strong>{skills.filter(item => item.enabled !== false).length}</strong> 已启用</span>
        <span><strong>{skills.filter(item => item.enabled === false).length}</strong> 已停用</span>
        <span><strong>{skills.filter(item => item.user_invocable).length}</strong> 可调用</span>
        <span><strong>{skills.filter(item => !item.user_invocable).length}</strong> 内部</span>
      </div>

      <div className="list-toolbar skills-toolbar">
        <Input
          prefix={<SearchOutlined />}
          placeholder="搜索技能"
          value={skillSearch}
          onChange={event => setSkillSearch(event.target.value)}
          allowClear
          className="workspace-search skills-search"
        />
        <div className="segmented-control" role="tablist" aria-label="技能筛选">
          {[
            ['all', '全部'],
            ['callable', '可调用'],
            ['internal', '内部'],
          ].map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="tab"
              aria-selected={skillFilter === value}
              className={skillFilter === value ? 'active' : ''}
              onClick={() => setSkillFilter(value as 'all' | 'callable' | 'internal')}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {loadingView ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : filteredSkills.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="还没有技能。在对话里说「帮我做一个能……的技能」，agent 会建好并立刻可用。"
          className="page-empty"
        />
      ) : (
        <div className="skills-list">
          {filteredSkills.map(item => {
            // A skill that is off stays in this list on purpose: it is the
            // only place the switch can be found again, and the only place
            // that says why the skill the model was asked about is missing
            // from the turn it just took.
            const enabled = item.enabled !== false
            return (
            <div className={`skill-row ${enabled ? '' : 'skill-row-disabled'}`} key={item.id}>
              <span className="skill-row-icon"><ApiOutlined /></span>
              <div className="skill-row-main">
                <div className="skill-row-title">
                  <strong>{item.name || item.id}</strong>
                  <Tag>{item.user_invocable ? '可调用' : '内部'}</Tag>
                  {!enabled && <Tag color="default">已停用</Tag>}
                </div>
                <div className="skill-row-id">{item.id}</div>
                <p>{item.description || '暂无描述'}</p>
              </div>
              <div className="skill-row-side">
                <span className="skill-source">{item.source || '未知来源'}</span>
                <Switch
                  size="small"
                  checked={enabled}
                  aria-label={`${enabled ? '停用' : '启用'} ${item.name || item.id}`}
                  onChange={value => toggleSkill(item, value)}
                />
                <Button
                  type="text"
                  size="small"
                  icon={<CopyOutlined />}
                  onClick={() => copyMessage(item.id)}
                >复制 ID</Button>
                {item.source === 'user' && <Button type="text" danger size="small" icon={<DeleteOutlined />} onClick={() => deleteSkill(item)} />}
              </div>
            </div>
            )
          })}
        </div>
      )}
    </>
  )

  return (
    <Card
      className="settings-card settings-card-wide capabilities-card"
      title="能力"
      extra={<span className="card-kicker">即时生效</span>}
    >
      <p className="settings-hint capabilities-hint">
        插件给 Agent 增加新的能力入口，技能决定这些能力怎么被调用。这一页的开关不用保存，拨完就是结果。
      </p>
      {/* The same tab idiom as the 自动化 page, so the two multi-list screens
          read as one family. Counts on the tabs because that is where the
          reader decides which list to look at. */}
      <div className="schedule-tabs extensions-tabs" role="tablist" aria-label="能力">
        <button
          type="button"
          role="tab"
          aria-selected={extensionsTab === 'plugins'}
          className={extensionsTab === 'plugins' ? 'active' : ''}
          onClick={() => setExtensionsTab('plugins')}
        >
          插件<span className="tab-count">{filteredPlugins.length}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={extensionsTab === 'skills'}
          className={extensionsTab === 'skills' ? 'active' : ''}
          onClick={() => setExtensionsTab('skills')}
        >
          技能<span className="tab-count">{skills.length}</span>
        </button>
      </div>
      {extensionsTab === 'plugins' ? renderPluginsBody() : renderSkillsBody()}
    </Card>
  )
}
