/** The settings view. Rendered by the App container from its context object. */

import type { AppCtx } from '../app/AppCtx'
import { thinkingEffortOf } from '../lib/format'
import { ApiOutlined, CheckCircleFilled, MessageOutlined, ReloadOutlined } from '@ant-design/icons'
import {
  Button,
  Card,
  Col,
  Form,
  Input,
  InputNumber,
  Row,
  Select,
  Skeleton,
  Space,
  Switch,
} from 'antd'

const { TextArea } = Input

export function createSettingsView(ctx: AppCtx) {
  const { applyToken, config, configText, form, handleSettingsFormChange, jsonStatus, loadingView, messageApi, pageMeta, resetSettings, saveSettings, sendShortcut, setConfigText, setSendShortcut, setSettingsDirty, settingsDirty, settingsModelOptions, setTokenDraft, thinkingEffortOptions, tokenDirty, tokenDraft } = ctx


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
