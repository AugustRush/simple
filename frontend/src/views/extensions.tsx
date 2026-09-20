/** The extensions view. Rendered by the App container from its context object. */

import type { AppCtx } from '../app/AppCtx'
import {
  ApiOutlined,
  CopyOutlined,
  DeleteOutlined,
  SearchOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { Button, Card, Col, Empty, Input, Row, Skeleton, Switch, Tag } from 'antd'

export function createExtensionsView(ctx: AppCtx) {
  const { copyMessage, deletePlugin, deleteSkill, extensionsTab, filteredPlugins, filteredSkills, loadingView, pageMeta, pluginSearch, setExtensionsTab, setPluginSearch, setSkillFilter, setSkillSearch, skillFilter, skills, skillSearch, togglePlugin, toggleSkill } = ctx


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
        <Empty description="暂无插件" className="page-empty" />
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

      <div className="skills-toolbar">
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
        <Input
          prefix={<SearchOutlined />}
          placeholder="按名称、ID 或来源搜索"
          value={skillSearch}
          onChange={event => setSkillSearch(event.target.value)}
          allowClear
          className="workspace-search skills-search"
        />
      </div>

      {loadingView ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : filteredSkills.length === 0 ? (
        <Empty description="暂无技能" className="page-empty" />
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

  const renderExtensions = () => (
    <div className="page-view extensions-view">
      <div className="page-head">
        <div>
          <div className="eyebrow">CAPABILITIES</div>
          <h2>{pageMeta.extensions.title}</h2>
          <p>插件给 Agent 增加新的能力入口，技能决定这些能力怎么被调用。</p>
        </div>
      </div>
      {/* The same tab idiom as the 自动化 page, so the two multi-list pages
          read as one family.  Counts sit on the tabs because that is where
          the visitor decides which list they are about to see. */}
      <div className="schedule-tabs extensions-tabs" role="tablist" aria-label="扩展视图">
        <button
          type="button"
          role="tab"
          aria-selected={extensionsTab === 'plugins'}
          className={extensionsTab === 'plugins' ? 'active' : ''}
          onClick={() => setExtensionsTab('plugins')}
        >
          插件
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={extensionsTab === 'skills'}
          className={extensionsTab === 'skills' ? 'active' : ''}
          onClick={() => setExtensionsTab('skills')}
        >
          技能
        </button>
      </div>
      {extensionsTab === 'plugins' ? renderPluginsBody() : renderSkillsBody()}
    </div>
  )
  return { renderExtensions }
}
