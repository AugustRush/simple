import { useCallback, useEffect, useMemo, useState } from 'react'
import { MODEL_PICKER_CHROME } from '../constants'
import { effortOptionsFrom, estimateLabelWidth, measureLabelWidth, thinkingEffortOf } from '../lib/format'
import type { FeishuChatInfo } from '../types'
import { Form } from 'antd'
import { useUi } from './useUi'
import { useApiClient } from './useApiClient'
import { useConversations } from './useConversations'

type Deps = ReturnType<typeof useUi> & ReturnType<typeof useApiClient> & ReturnType<typeof useConversations>

export function useSettings(deps: Deps) {
  const { api, currentModel, currentModelRef, messageApi, setCurrentModel, setLoadingView, setToken, token } = deps

  const [feishuChats, setFeishuChats] = useState<FeishuChatInfo[]>([])

  const [feishuChatsLoading, setFeishuChatsLoading] = useState(false)

  // "Have we asked yet" is not the same question as "did we get anything".
  // Inferring the first from `feishuChats.length` treats a *successful* empty
  // list -- a bot that is in no groups yet, which is the normal first-run state
  // -- as "never fetched", and the effect below re-fires every time loading
  // falls back to false. That loop keeps the spinner up forever.
  const [feishuChatsLoaded, setFeishuChatsLoaded] = useState(false)

  const [feishuChatsError, setFeishuChatsError] = useState('')

  const [feishuTesting, setFeishuTesting] = useState(false)

  const [pickingDirectory, setPickingDirectory] = useState(false)

  const [config, setConfig] = useState<any>(null)

  const [configText, setConfigText] = useState<string>('')

  const [currentProvider, setCurrentProvider] = useState('')

  const [settingsDirty, setSettingsDirty] = useState(false)

  // What the settings page offers, as /api/config delivered it. State rather
  // than a constant because it is the backend's list, not ours.
  const [thinkingEffortOptions, setThinkingEffortOptions] = useState(
    () => effortOptionsFrom(null),
  )

  const [form] = Form.useForm()

  const activeProviderName = Form.useWatch('active_provider', form)

  // What the box currently shows.  Separate from ``token`` so that half-typed
  // credentials are not sent as headers before the user has finished: the
  // applied token only moves when they press save.
  const [tokenDraft, setTokenDraft] = useState(token)

  const tokenDirty = tokenDraft.trim() !== token

  useEffect(() => {
    currentModelRef.current = currentModel
  }, [currentModel])

  const loadFeishuChats = useCallback(async () => {
    setFeishuChatsLoading(true)
    setFeishuChatsError('')
    // This is a network round-trip out to Feishu on the user's own app
    // credentials. Unbounded, a hung connection leaves the picker spinning with
    // nothing to click and no way to tell it apart from a slow success -- which
    // is exactly how it was reported. 15s is far past a healthy list call.
    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), 15_000)
    try {
      const resp = await api('/api/feishu/chats', { signal: controller.signal })
      const data = await resp.json()
      setFeishuChats(Array.isArray(data.chats) ? data.chats : [])
    } catch (error) {
      // Keep the reason on the form itself: "no permission" and "no config"
      // read identically as an empty dropdown, and the user cannot fix what
      // they cannot see.
      if (error instanceof Error && error.name === 'AbortError') {
        setFeishuChatsError('获取会话列表超时，请检查网络或飞书配置后点「重新获取」')
      } else {
        setFeishuChatsError(error instanceof Error ? error.message : '会话列表获取失败')
      }
    } finally {
      window.clearTimeout(timer)
      setFeishuChatsLoading(false)
      // Set last: this is what stops the effect from asking again. A failure
      // counts as "asked" too, or a broken config would loop instead of
      // settling on the error message with its 重新获取 button.
      setFeishuChatsLoaded(true)
    }
  }, [api])

  const sendFeishuTest = useCallback(async (chatId: string) => {
    setFeishuTesting(true)
    try {
      await api('/api/feishu/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: chatId }),
      })
      messageApi.success('测试消息已发送，请检查飞书会话')
    } catch {
      // The server message (missing config, wrong scope, chat not found)
      // already surfaced as a toast; there is nothing more specific to say.
    } finally {
      setFeishuTesting(false)
    }
  }, [api, messageApi])

  const pickDirectory = useCallback(async (apply: (path: string) => void) => {
    setPickingDirectory(true)
    try {
      // The native dialog lives as long as the user needs, so this request
      // simply waits; cancelling the dialog is a normal outcome, not an error.
      const resp = await api('/api/fs/pick-directory', { method: 'POST' })
      const data = await resp.json()
      if (!data.cancelled && typeof data.workspace_root === 'string' && data.workspace_root) {
        apply(data.workspace_root)
      }
    } catch {
      // Surfaced by the api helper.
    } finally {
      setPickingDirectory(false)
    }
  }, [api])

  const loadSettings = useCallback(async () => {
    try {
      setLoadingView(true)
      const resp = await api('/api/config')
      const data = await resp.json()
      const cfg = data.config || {}
      setConfig(cfg)
      setConfigText(JSON.stringify(cfg, null, 2))
      setSettingsDirty(false)
      // The levels on offer come from the same response as the config, so
      // the page offers what this backend will validate -- a level added on
      // the server shows up here without a second copy of the list here.
      setThinkingEffortOptions(effortOptionsFrom(data.thinking_efforts))

      const providers = cfg.providers || {}
      const active = providers[cfg.active_provider] || {}
      setCurrentProvider(cfg.active_provider || '')
      // Only seed the composer's model when the user has not picked one:
      // reloading settings (opening the settings view, saving) must not
      // silently revert a per-turn model selection.
      setCurrentModel(prev => prev || active.default_model || '')
      form.setFieldsValue({
        active_provider: cfg.active_provider,
        model: active.default_model,
        max_tokens: active.max_tokens,
        thinking_effort: thinkingEffortOf(active),
        web_enabled: !!(cfg.channels?.web?.enabled),
        feishu_enabled: !!(cfg.channels?.feishu?.enabled),
      })
    } catch {
      // Handled by api helper.
    } finally {
      setLoadingView(false)
    }
  }, [api, form])

  useEffect(() => {
    loadSettings()
  }, [loadSettings])

  /** Fold the form's fields into a config object.
   *
   *  One definition, because two callers need it: saving, and the write-through
   *  that keeps the JSON box showing what saving would send.  When the two were
   *  computed separately the box disagreed with the form, and the form won at
   *  save time -- so an edit made in the box could be discarded without a word.
   */
  const mergeSettingsForm = (base: any, values: any) => {
    const cfg = { ...(base || {}) }
    const provider = values.active_provider
    cfg.active_provider = provider
    if (provider) {
      cfg.providers = { ...(cfg.providers || {}) }
      const providerCfg = { ...(cfg.providers[provider] || {}) }
      providerCfg.default_model = values.model
      providerCfg.max_tokens = values.max_tokens
      // Only ever one provider's thinking block, and only when a level was
      // actually chosen. Clearing the field means "no opinion" -- which is a
      // different request from any level, including 关闭 -- so the key is
      // removed rather than written empty.
      const effort = values.thinking_effort
      if (effort) {
        providerCfg.thinking = { ...(providerCfg.thinking || {}), effort }
      } else if (providerCfg.thinking) {
        const { effort: _cleared, ...rest } = providerCfg.thinking
        if (Object.keys(rest).length) providerCfg.thinking = rest
        else delete providerCfg.thinking
      }
      cfg.providers[provider] = providerCfg
    }
    cfg.channels = { ...(cfg.channels || {}) }
    cfg.channels.web = { ...(cfg.channels.web || {}), enabled: values.web_enabled }
    cfg.channels.feishu = {
      ...(cfg.channels.feishu || {}),
      enabled: values.feishu_enabled,
    }
    return cfg
  }

  const handleSettingsFormChange = (changed: any, all: any) => {
    let values = all
    // A model and a max_tokens belong to one provider, so choosing a different
    // provider has to change them.  Leaving the previous provider's model in
    // the box meant save wrote it onto the newly chosen provider -- a config
    // edit nobody made, in a provider nobody was looking at.
    if (changed && 'active_provider' in changed) {
      const provider = config?.providers?.[changed.active_provider] || {}
      values = {
        ...all,
        model: provider.default_model ?? '',
        max_tokens: provider.max_tokens ?? null,
        thinking_effort: thinkingEffortOf(provider),
      }
      form.setFieldsValue({
        model: values.model,
        max_tokens: values.max_tokens,
        thinking_effort: values.thinking_effort,
      })
    }
    setSettingsDirty(true)
    setConfigText(current => {
      try {
        return JSON.stringify(
          mergeSettingsForm(JSON.parse(current || '{}'), values),
          null,
          2,
        )
      } catch {
        // The box holds something unparseable, so the user is typing in it.
        // Rewriting would destroy that; saving is blocked while it stays
        // invalid, so nothing is lost behind their back either way.
        return current
      }
    })
  }

  const applyToken = () => {
    const next = tokenDraft.trim()
    localStorage.setItem('agent_token', next)
    // State, not a page reload: the headers and every link built from the
    // token are rebuilt from this, and the conversation survives.
    setToken(next)
    setTokenDraft(next)
    messageApi.success(next ? '令牌已保存' : '令牌已清除')
  }

  const saveSettings = async () => {
    try {
      const values = await form.validateFields()
      const cfg = mergeSettingsForm(JSON.parse(configText || '{}'), values)

      await api('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ config: cfg }),
      })
      messageApi.success('设置已保存')
      setSettingsDirty(false)
      loadSettings()
    } catch (error) {
      if (error instanceof SyntaxError) {
        messageApi.error('保存失败：JSON 格式有误')
      } else if (error instanceof Error && error.message) {
        messageApi.error(error.message)
      } else {
        messageApi.error('保存失败，请检查 JSON 格式')
      }
    }
  }

  const resetSettings = () => {
    const next = JSON.stringify(config || {}, null, 2)
    setConfigText(next)
    setSettingsDirty(false)
    // Everything unsaved on this page, including the token box: "discard" that
    // left one field behind would be the same surprise this page already had.
    setTokenDraft(token)
    const providers = config?.providers || {}
    const active = providers[config?.active_provider] || {}
    form.setFieldsValue({
      active_provider: config?.active_provider,
      model: active.default_model,
      max_tokens: active.max_tokens,
      thinking_effort: thinkingEffortOf(active),
      web_enabled: !!config?.channels?.web?.enabled,
      feishu_enabled: !!config?.channels?.feishu?.enabled,
    })
  }

  const jsonStatus = useMemo(() => {
    try {
      JSON.parse(configText || '{}')
      return { valid: true, label: 'JSON 格式有效' }
    } catch {
      return { valid: false, label: 'JSON 格式有误' }
    }
  }, [configText])

  const modelOptions = useMemo(() => {
    // Every configured provider's models are selectable: the backend routes a
    // model id to the provider that owns it, so the list is not limited to the
    // active provider's group. The active provider's group comes first.
    const providers = config?.providers || {}
    const activeName = config?.active_provider
    const groups: { label: string; options: { value: string; label: string }[] }[] = []
    const seen = new Set<string>()
    const push = (providerName: string) => {
      const provider = providers[providerName]
      if (!provider) return
      const models = provider.models?.length
        ? provider.models
        : [provider.default_model].filter(Boolean)
      const options: { value: string; label: string }[] = []
      for (const model of models || []) {
        if (!model || seen.has(model)) continue
        seen.add(model)
        options.push({ value: model, label: model })
      }
      if (options.length) groups.push({ label: providerName, options })
    }
    if (activeName) push(activeName)
    for (const name of Object.keys(providers)) {
      if (name !== activeName) push(name)
    }
    return groups
  }, [config])

  // Placeholder while the config has not loaded yet; once loaded,
  // currentModel holds the active provider's default model id.
  const modelSelectPlaceholder = currentModel ? undefined : '默认模型'

  // Size the model picker to the label it currently shows, not to the longest
  // id in the list: a short model should not reserve a long one's width. The
  // popup is width-independent (popupMatchSelectWidth={false}), so long entries
  // still read in full while open; the closed control ellipsizes past the clamp.
  const modelSelectWidth = useMemo(() => {
    const label = currentModel || modelSelectPlaceholder || ''
    const measured = measureLabelWidth(label) || estimateLabelWidth(label)
    return `${Math.max(88, Math.min(240, Math.ceil(measured) + MODEL_PICKER_CHROME))}px`
  }, [currentModel, modelSelectPlaceholder])

  // Settings page: models of the currently selected provider. The default
  // model is chosen from a dropdown instead of free-text input, so the value
  // always matches a real model id of the active provider.
  const settingsModelOptions = useMemo(() => {
    const provider = config?.providers?.[activeProviderName]
    const models = provider?.models?.length
      ? provider.models
      : [provider?.default_model].filter(Boolean)
    const seen = new Set<string>()
    const list: string[] = []
    for (const model of models || []) {
      if (model && !seen.has(model)) {
        seen.add(model)
        list.push(model)
      }
    }
    return list.map(model => ({ value: model, label: model }))
  }, [config, activeProviderName])

  const handleModelChange = (model: string) => {
    currentModelRef.current = model
    setCurrentModel(model)
  }

  return { activeProviderName, applyToken, config, configText, currentProvider, feishuChats, feishuChatsError, feishuChatsLoaded, feishuChatsLoading, feishuTesting, form, handleModelChange, handleSettingsFormChange, jsonStatus, loadFeishuChats, loadSettings, mergeSettingsForm, modelOptions, modelSelectPlaceholder, modelSelectWidth, pickDirectory, pickingDirectory, resetSettings, saveSettings, sendFeishuTest, setConfig, setConfigText, setCurrentProvider, setFeishuChats, setFeishuChatsError, setFeishuChatsLoaded, setFeishuChatsLoading, setFeishuTesting, setPickingDirectory, setSettingsDirty, setThinkingEffortOptions, setTokenDraft, settingsDirty, settingsModelOptions, thinkingEffortOptions, tokenDirty, tokenDraft } as const
}
