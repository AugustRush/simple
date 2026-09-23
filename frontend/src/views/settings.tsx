/** The settings view. Rendered by the App container from its context object. */

import { useState } from 'react'
import type { AppCtx } from '../app/AppCtx'
import type { ProviderFieldSpec } from '../types'
import { thinkingEffortOf } from '../lib/format'
import { ApiOutlined, CheckCircleFilled, MessageOutlined, ReloadOutlined } from '@ant-design/icons'
import {
  Button,
  Card,
  Col,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Row,
  Select,
  Skeleton,
  Space,
  Switch,
  Tag,
} from 'antd'

const { TextArea } = Input

//: The mask the backend sends instead of a stored credential.  Typed back
//: unchanged, it means "keep what is on disk" -- which is what lets a form
//: round-trip a key it is not allowed to show.
const MASK = '******'

/** One control per field kind, chosen from what the backend declared.
 *
 * No field name appears in this file: the vocabulary arrives in
 * `provider_fields`, so a field added on the server renders here without an
 * edit -- and a field removed there stops being offered.  That is the whole
 * point of the table, and the reason this is a switch on `kind` rather than a
 * form spelled out for the providers we happen to ship.
 */
function providerFieldControl(
  spec: ProviderFieldSpec,
  value: unknown,
  onChange: (next: unknown) => void,
  //: The id its <label htmlFor> points at.  Passed in rather than derived from
  //: the key, because the same field is on screen twice when the editor and
  //: the "add" form are both open, and two elements sharing an id is the kind
  //: of thing that only shows up as a mislabelled field later.
  controlId: string,
) {
  switch (spec.kind) {
    case 'bool':
      return (
        <Switch
          id={controlId}
          checked={Boolean(value)}
          onChange={checked => onChange(checked)}
        />
      )
    case 'int':
      return (
        <InputNumber
          id={controlId}
          style={{ width: '100%' }}
          value={typeof value === 'number' ? value : undefined}
          onChange={next => onChange(next ?? null)}
        />
      )
    case 'choice':
      return (
        <Select
          id={controlId}
          value={typeof value === 'string' && value ? value : undefined}
          onChange={next => onChange(next)}
          options={spec.choices.map(choice => ({ value: choice, label: choice }))}
        />
      )
    case 'secret':
      return (
        <Input.Password
          id={controlId}
          value={typeof value === 'string' ? value : ''}
          placeholder={value === MASK ? '已保存（留空表示不改）' : ''}
          onChange={event => onChange(event.target.value)}
        />
      )
    case 'string_list':
      return (
        <Select
          id={controlId}
          mode="tags"
          style={{ width: '100%' }}
          value={Array.isArray(value) ? (value as string[]) : []}
          onChange={next => onChange(next)}
          placeholder="输入后回车添加"
          tokenSeparators={[',', ' ']}
        />
      )
    case 'string_map': {
      // Rows, not a map: an empty map has no rows, and "add a row" that
      // immediately discards the blank row it just made is a button that does
      // nothing -- which is exactly what adding the *first* header looked
      // like.  Blanks are dropped when the value is stored, not while it is
      // being typed.
      const pairs: [string, string][] = Array.isArray(value)
        ? (value as [string, string][])
        : Object.entries((value && typeof value === 'object' ? value : {}) as Record<string, string>)
            .map(([key, item]) => [key, String(item ?? '')])
      const commit = (next: [string, string][]) => onChange(next)
      return (
        <div className="provider-map">
          {pairs.map(([key, item], index) => (
            <Space.Compact key={`${key}-${index}`} style={{ width: '100%', marginBottom: 6 }}>
              <Input
                aria-label={`${spec.label} 名称 ${index + 1}`}
                style={{ width: '40%' }}
                value={key}
                placeholder="名称"
                onChange={event => {
                  const next = [...pairs]
                  next[index] = [event.target.value, item]
                  commit(next)
                }}
              />
              <Input
                aria-label={`${spec.label} 值 ${index + 1}`}
                style={{ width: '60%' }}
                value={item}
                placeholder="值"
                onChange={event => {
                  const next = [...pairs]
                  next[index] = [key, event.target.value]
                  commit(next)
                }}
              />
              <Button
                danger
                type="text"
                onClick={() => commit(pairs.filter((_, i) => i !== index))}
              >
                删除
              </Button>
            </Space.Compact>
          ))}
          <Button size="small" onClick={() => commit([...pairs, ['', '']])}>
            添加一行
          </Button>
        </div>
      )
    }
    default:
      return (
        <Input
          id={controlId}
          value={typeof value === 'string' ? value : ''}
          onChange={event => onChange(event.target.value)}
        />
      )
  }
}

/** The provider list and per-provider editor.
 *
 * Defined at module scope on purpose: `createSettingsView` runs on every App
 * render, so a component created inside it would be a new type each time and
 * React would remount it -- losing the draft the user is typing.
 */
function ProvidersCard({
  config,
  fields,
  busy,
  notify,
  onSave,
  onDelete,
  onActivate,
  onTest,
}: {
  config: any
  fields: ProviderFieldSpec[]
  busy: string
  //: The app's message API rather than antd's static one: the static call
  //: cannot see the app's ConfigProvider, so it renders with the default theme
  //: and locale -- a different-looking toast from every other toast here.
  notify: { error: (text: string) => void }
  onSave: (name: string, values: Record<string, unknown>) => Promise<boolean>
  onDelete: (name: string) => Promise<boolean>
  onActivate: (name: string) => Promise<boolean>
  onTest: (name: string) => Promise<boolean>
}) {
  const [editing, setEditing] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [newName, setNewName] = useState('')
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [saving, setSaving] = useState(false)

  const providers: Record<string, any> = config?.providers || {}
  const names = Object.keys(providers).filter(name => !name.startsWith('_'))
  const activeName = String(config?.active_provider || '')

  //: ``string_map`` fields are edited as rows and stored as a map.
  const toEditor = (spec: ProviderFieldSpec, raw: unknown): unknown => {
    if (spec.kind !== 'string_map') return raw
    if (Array.isArray(raw)) return raw
    return Object.entries((raw && typeof raw === 'object' ? raw : {}) as Record<string, string>)
      .map(([key, item]) => [key, String(item ?? '')] as [string, string])
  }

  const toStored = (spec: ProviderFieldSpec, raw: unknown): unknown => {
    if (spec.kind !== 'string_map') return raw
    if (!Array.isArray(raw)) return raw
    const built: Record<string, string> = {}
    for (const [key, item] of raw as [string, string][]) {
      const name = String(key ?? '').trim()
      if (name) built[name] = String(item ?? '')
    }
    return built
  }

  //: What the agent will actually use for a field: what the config says, or
  //: the vocabulary's default when it says nothing.  The form shows this
  //: rather than a bare "off" for an absent switch -- an unconfigured provider
  //: streams usage and does not support vision, and a form that claims
  //: otherwise is describing something the runtime will not do.
  const effective = (spec: ProviderFieldSpec, stored: unknown): unknown =>
    stored !== undefined ? stored : spec.default

  //: Stored shape, so a draft (rows for a string_map) can be compared with it.
  const normalize = (spec: ProviderFieldSpec, raw: unknown): unknown =>
    toStored(spec, toEditor(spec, raw))

  const openEditor = (name: string) => {
    const stored = providers[name] || {}
    const seeded: Record<string, unknown> = {}
    for (const spec of fields) {
      const value = effective(spec, stored[spec.key])
      if (value !== undefined) seeded[spec.key] = toEditor(spec, value)
    }
    setAdding(false)
    setEditing(name)
    setDraft(seeded)
  }

  const openNew = () => {
    const seeded: Record<string, unknown> = {}
    for (const spec of fields) {
      if (spec.default !== null && spec.default !== undefined) seeded[spec.key] = spec.default
    }
    setEditing(null)
    setAdding(true)
    setNewName('')
    setDraft(seeded)
  }

  // Only what the user actually changed.  Sending the whole block back would
  // assert values for fields nobody looked at, and two open tabs would
  // overwrite each other; the backend merges a patch.
  const changedFields = (name: string | null) => {
    const stored = name ? providers[name] || {} : {}
    const patch: Record<string, unknown> = {}
    for (const spec of fields) {
      if (!(spec.key in draft)) continue
      const normalized = normalize(spec, draft[spec.key])
      const current = normalize(spec, effective(spec, stored[spec.key]))
      // Compared against the *effective* current value, not the stored one:
      // the draft was seeded with the default for a field the config omits, and
      // that seed is not a change the user made.  Otherwise opening a provider
      // and pressing save would write every default into the file.
      if (JSON.stringify(current ?? null) !== JSON.stringify(normalized ?? null)) {
        patch[spec.key] = normalized
      }
    }
    return patch
  }

  const submit = async () => {
    const name = adding ? newName.trim() : String(editing || '')
    if (!name) return
    for (const spec of fields) {
      if (spec.required && !String(draft[spec.key] ?? '').trim()) {
        notify.error(`${spec.label} 不能为空`)
        return
      }
    }
    // A half-typed row (a name with no value, or the reverse) is dropped
    // rather than sent: the backend would store a header the SDK then cannot
    // send.  Blank rows are how the editor holds space, not data.
    for (const spec of fields) {
      if (spec.kind !== 'string_map' || !Array.isArray(draft[spec.key])) continue
      const rows = draft[spec.key] as [string, string][]
      const half = rows.find(([key, item]) => Boolean(String(key || '').trim()) !== Boolean(String(item || '').trim()))
      if (half) {
        notify.error(`${spec.label}：「${half[0] || half[1]}」这一行只填了一半，请补全或删除`)
        return
      }
    }
    const patch = changedFields(adding ? null : name)
    if (adding) {
      // A new provider has nothing to differ from: everything the form holds is
      // what the user is asking to be configured, defaults included.
      for (const spec of fields) {
        if (draft[spec.key] !== undefined) patch[spec.key] = normalize(spec, draft[spec.key])
      }
    }
    setSaving(true)
    const ok = await onSave(name, patch)
    setSaving(false)
    if (ok) {
      setEditing(null)
      setAdding(false)
      setDraft({})
    }
  }

  return (
    <Card
      className="settings-card settings-card-wide"
      title="Providers"
      extra={<span className="card-kicker">ENDPOINTS</span>}
    >
      <p className="settings-hint">
        每个 Provider 是一个接口地址、一个密钥和一组模型。改动只作用于这一个 Provider，
        不会影响其它；「高级 JSON」里手写的同一段配置也会即时反映到这里。
      </p>
      <Space direction="vertical" style={{ width: '100%' }} size="small">
        {names.map(name => {
          const provider = providers[name] || {}
          const isActive = name === activeName
          const open = editing === name
          return (
            <Card key={name} size="small" className="provider-row">
              <Space style={{ width: '100%', justifyContent: 'space-between' }} wrap>
                <Space wrap>
                  <strong>{name}</strong>
                  {isActive && <Tag color="green">使用中</Tag>}
                  <span className="settings-hint">
                    {String(provider.api_format || '?')} · {String(provider.base_url || '默认地址')} ·{' '}
                    {(provider.models || []).length || 1} 个模型
                  </span>
                </Space>
                <Space wrap>
                  <Button
                    size="small"
                    loading={busy === name}
                    onClick={() => onTest(name)}
                  >
                    测试
                  </Button>
                  <Button
                    size="small"
                    disabled={isActive}
                    onClick={() => onActivate(name)}
                  >
                    {isActive ? '当前使用' : '切换到此'}
                  </Button>
                  <Button size="small" onClick={() => (open ? setEditing(null) : openEditor(name))}>
                    {open ? '收起' : '编辑'}
                  </Button>
                  <Popconfirm
                    title={`删除 Provider「${name}」？`}
                    onConfirm={() => onDelete(name)}
                  >
                    <Button size="small" danger disabled={isActive}>
                      删除
                    </Button>
                  </Popconfirm>
                </Space>
              </Space>
              {open && (
                <div className="provider-editor">
                  {fields.map(spec => (
                    <div key={spec.key} className="provider-field">
                      <label className="settings-field-label" htmlFor={`${name}-${spec.key}`}>
                        {spec.label}
                        {spec.required && <span style={{ color: '#ff4d4f' }}> *</span>}
                      </label>
                      {providerFieldControl(
                        spec,
                        draft[spec.key],
                        next => setDraft(prev => ({ ...prev, [spec.key]: next })),
                        `${name}-${spec.key}`,
                      )}
                      {spec.help && <div className="settings-hint">{spec.help}</div>}
                    </div>
                  ))}
                  <Space>
                    <Button type="primary" loading={saving} onClick={submit}>
                      保存这个 Provider
                    </Button>
                    <Button onClick={() => setEditing(null)}>取消</Button>
                  </Space>
                </div>
              )}
            </Card>
          )
        })}
      </Space>
      {adding ? (
        <Card size="small" className="provider-row" style={{ marginTop: 12 }}>
          <label className="settings-field-label" htmlFor="new-provider-name">
            名称
          </label>
          <Input
            id="new-provider-name"
            value={newName}
            placeholder="例如 opencode-go"
            onChange={event => setNewName(event.target.value)}
          />
          <div className="provider-editor">
            {fields.map(spec => (
              <div key={spec.key} className="provider-field">
                <label className="settings-field-label" htmlFor={`new-${spec.key}`}>
                  {spec.label}
                  {spec.required && <span style={{ color: '#ff4d4f' }}> *</span>}
                </label>
                {providerFieldControl(
                  spec,
                  draft[spec.key],
                  next => setDraft(prev => ({ ...prev, [spec.key]: next })),
                  `new-${spec.key}`,
                )}
                {spec.help && <div className="settings-hint">{spec.help}</div>}
              </div>
            ))}
          </div>
          <Space>
            <Button type="primary" loading={saving} onClick={submit}>
              添加
            </Button>
            <Button onClick={() => setAdding(false)}>取消</Button>
          </Space>
        </Card>
      ) : (
        <Button style={{ marginTop: 12 }} onClick={openNew}>
          添加 Provider
        </Button>
      )}
    </Card>
  )
}

export function createSettingsView(ctx: AppCtx) {
  const { activateProvider, applyToken, config, configText, deleteProvider, form, handleSettingsFormChange, jsonStatus, loadingView, messageApi, pageMeta, providerBusy, providerFields, resetSettings, saveProvider, saveSettings, sendShortcut, setConfigText, setSendShortcut, setSettingsDirty, settingsDirty, settingsModelOptions, setTokenDraft, testProvider, thinkingEffortOptions, tokenDirty, tokenDraft } = ctx


  const renderSettings = () => {
    // "Unsaved" has to mean everything pending on this page, or the badge
    // reassures the user about a token they have not applied yet.
    const unsaved = settingsDirty || tokenDirty
    const blocked = settingsDirty && !jsonStatus.valid
    return (
    <div className="page-view settings-view">
      <div className="page-head settings-page-head">
        <div>
          <div className="eyebrow">WORKSPACE</div>
          <h2>{pageMeta.settings.title}</h2>
          <p>配置访问权限、模型偏好与消息频道。</p>
        </div>
        <Space>
          <span className={`save-state ${unsaved ? 'dirty' : ''} ${blocked ? 'blocked' : ''}`}>
            <span className="save-state-dot" />
            {blocked ? 'JSON 格式有误，无法保存' : unsaved ? '有未保存更改' : '已同步'}
          </span>
          <Button
            icon={<ReloadOutlined />}
            onClick={resetSettings}
            disabled={!unsaved}
          >
            放弃更改
          </Button>
          <Button
            className="settings-save-button"
            type="primary"
            icon={<CheckCircleFilled />}
            onClick={saveSettings}
            disabled={!settingsDirty || !jsonStatus.valid}
          >
            保存设置
          </Button>
        </Space>
      </div>

      {loadingView ? (
        <Skeleton active paragraph={{ rows: 10 }} />
      ) : (
        <div className="settings-grid">
          <Card
            className="settings-card"
            title="访问令牌"
            extra={
              <span className="card-kicker">
                SECURITY<span className="settings-instant-tag">即时生效</span>
              </span>
            }
          >
            <p className="settings-hint">
              Web 频道默认只绑定本地地址，因此令牌通常可以为空。对外暴露端口时请填写鉴权令牌。
            </p>
            <Space.Compact style={{ width: '100%' }}>
              <Input.Password
                value={tokenDraft}
                onChange={event => setTokenDraft(event.target.value)}
                onPressEnter={applyToken}
                placeholder="auth_token（可选）"
              />
              <Button
                type="primary"
                className="settings-inline-save"
                disabled={!tokenDirty}
                onClick={applyToken}
              >
                {tokenDirty ? '保存令牌' : '已保存'}
              </Button>
            </Space.Compact>
          </Card>

          <Card
            className="settings-card"
            title="发送偏好"
            extra={
              <span className="card-kicker">
                UX<span className="settings-instant-tag">即时生效</span>
              </span>
            }
          >
            <p className="settings-hint">
              修改发送快捷键。中文输入法用 Enter 上屏，切换成 Ctrl/Cmd + Enter 可避免误发送。
            </p>
            <label className="settings-field-label">发送快捷键</label>
            <Select
              value={sendShortcut}
              onChange={value => {
                setSendShortcut(value)
                localStorage.setItem('send_shortcut', value)
                messageApi.success(`发送快捷键已改为：${value === 'ctrl-enter' ? 'Ctrl/Cmd + Enter' : 'Enter'}`)
              }}
              options={[
                { value: 'enter', label: 'Enter' },
                { value: 'ctrl-enter', label: 'Ctrl/Cmd + Enter' },
              ]}
              style={{ width: '100%' }}
            />
          </Card>

          <ProvidersCard
            config={config}
            fields={providerFields}
            busy={providerBusy}
            notify={messageApi}
            onSave={saveProvider}
            onDelete={deleteProvider}
            onActivate={activateProvider}
            onTest={testProvider}
          />

          <Card className="settings-card settings-card-wide" title="模型与频道" extra={<span className="card-kicker">RUNTIME</span>}>
            <Form form={form} layout="vertical" onValuesChange={handleSettingsFormChange}>
              <Row gutter={16}>
                <Col xs={24} md={6}>
                  <Form.Item
                    name="active_provider"
                    label="Provider"
                    rules={[{ required: true, message: '请选择 Provider' }]}
                  >
                    <Select
                      options={Object.keys(config?.providers || {}).map(key => ({
                        value: key,
                        label: key,
                      }))}
                    />
                  </Form.Item>
                </Col>
                <Col xs={24} md={6}>
                  <Form.Item name="model" label="默认模型">
                    <Select
                      options={settingsModelOptions}
                      placeholder="选择模型"
                      showSearch
                      optionFilterProp="label"
                    />
                  </Form.Item>
                </Col>
                {/* Thinking effort is a property of the provider, not of the
                    model: the level is the provider's own word for it, so the
                    same value means different requests to different groups. It
                    sits inside this Row because this Row is what the form
                    writes onto the selected provider. */}
                <Col xs={24} md={6}>
                  <Form.Item
                    name="thinking_effort"
                    label="思考强度"
                    tooltip={
                      '这个 Provider 的模型想多深。「默认（不干预）」不发送任何思考参数，'
                      + '由服务商自己决定；「关闭」会明确要求不要思考；低/中/高逐级加深。'
                      + '修改后随「保存设置」写入，对之后的新对话生效。'
                    }
                  >
                    <Select options={thinkingEffortOptions} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={6}>
                  <Form.Item name="max_tokens" label="Max tokens">
                    <InputNumber min={1} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>

              {/* A channel is a boolean, not a value you type, so it is not laid
                  out like a form field. Two `md=8` columns put the name on one
                  line and a 44px switch alone on the next, with the rest of the
                  column empty; the switch floated in a control-height slot that
                  nothing else was in. One row per channel instead: what it is on
                  the left, whether it is on, on the right. */}
              <section className="settings-channel-block">
                <header className="settings-channel-block-head">
                  <span className="settings-field-label">消息频道</span>
                  <small>各自独立开关，随「保存设置」一起写入。</small>
                </header>
                <div className="settings-channels">
                  {[
                    {
                      key: 'web_enabled',
                      label: 'Web 频道',
                      desc: '本页所在的 Web 服务，默认只绑本机',
                      icon: <ApiOutlined />,
                    },
                    {
                      key: 'feishu_enabled',
                      label: '飞书频道',
                      desc: '在飞书里收发消息，凭据在下方 JSON 里',
                      icon: <MessageOutlined />,
                    },
                  ].map(channel => (
                    <div className="settings-channel" key={channel.key}>
                      {/* Everything except the switch is a `<label for>` onto
                          it, so the whole row turns the channel on rather than
                          just the 44px pill -- and the switch stays outside the
                          label, so one click is one toggle. The switch keeps an
                          `aria-label` so it is named even if that association is
                          ever lost. */}
                      <label className="settings-channel-hit" htmlFor={channel.key}>
                        <span className="settings-channel-icon">{channel.icon}</span>
                        <span className="settings-channel-copy">
                          <span className="settings-channel-name">{channel.label}</span>
                          <span className="settings-channel-desc">{channel.desc}</span>
                        </span>
                      </label>
                      <Form.Item name={channel.key} valuePropName="checked" noStyle>
                        <Switch aria-label={channel.label} />
                      </Form.Item>
                    </div>
                  ))}
                </div>
              </section>
            </Form>
          </Card>

          <Card
            className="settings-card settings-card-wide settings-json-card"
            title="高级 JSON"
            extra={<span className="card-kicker">RAW</span>}
          >
            <p className="settings-hint">
              表单里的修改会同步写进这里，这里能识别出的字段也会同步回表单，保存发送的就是这段文本。
            </p>
            <div className={`json-status ${jsonStatus.valid ? 'valid' : 'invalid'}`}>
              <span className="json-status-dot" /> {jsonStatus.label}
            </div>
            <TextArea
              value={configText}
              onChange={event => {
                const next = event.target.value
                setConfigText(next)
                setSettingsDirty(true)
                // Keep the form showing the same config.  Without this the two
                // halves of the page disagreed, and only one of them was sent.
                try {
                  const parsed = JSON.parse(next || '{}')
                  const active = (parsed.providers || {})[parsed.active_provider] || {}
                  form.setFieldsValue({
                    active_provider: parsed.active_provider,
                    model: active.default_model,
                    max_tokens: active.max_tokens,
                    thinking_effort: thinkingEffortOf(active),
                    web_enabled: !!parsed.channels?.web?.enabled,
                    feishu_enabled: !!parsed.channels?.feishu?.enabled,
                  })
                } catch {
                  // Unparseable, so the user is mid-edit: the box is the source
                  // of truth until it parses, and saving stays blocked.
                }
              }}
              className="settings-json"
              spellCheck={false}
            />
          </Card>
        </div>
      )}
    </div>
    )
  }
  return { renderSettings }
}
