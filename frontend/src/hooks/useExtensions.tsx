import { useCallback, useMemo, useState } from 'react'
import type { PluginInfo, SkillInfo } from '../types'
import { useUi } from './useUi'
import { useApiClient } from './useApiClient'
import { useConfirm } from './useConfirm'

type Deps = ReturnType<typeof useUi> & ReturnType<typeof useApiClient> & ReturnType<typeof useConfirm>

export function useExtensions(deps: Deps) {
  const { api, confirmResourceDeletion, messageApi, setLoadingView } = deps

  const [plugins, setPlugins] = useState<PluginInfo[]>([])

  const [skills, setSkills] = useState<SkillInfo[]>([])

  // The merged page keeps two lists behind one navigation entry.  The tab is
  // remembered across visits for the same reason the search strings are:
  // someone who toggles a plugin off and comes back later is coming back for
  // the list they left, not for a default.
  const [extensionsTab, setExtensionsTab] = useState<'plugins' | 'skills'>('plugins')

  const [pluginSearch, setPluginSearch] = useState('')

  const [skillSearch, setSkillSearch] = useState('')

  const [skillFilter, setSkillFilter] = useState<'all' | 'callable' | 'internal'>('all')

  const loadPlugins = useCallback(async () => {
    try {
      setLoadingView(true)
      const resp = await api('/api/plugins')
      const data = await resp.json()
      setPlugins(data.plugins || [])
    } catch {
      // Handled by api helper.
    } finally {
      setLoadingView(false)
    }
  }, [api])

  const loadSkills = useCallback(async (silent = false) => {
    try {
      if (!silent) setLoadingView(true)
      const resp = await api('/api/skills')
      const data = await resp.json()
      setSkills(data.skills || [])
    } catch {
      // Handled by api helper.
    } finally {
      if (!silent) setLoadingView(false)
    }
  }, [api])

  const togglePlugin = async (plugin: PluginInfo, enabled: boolean) => {
    setPlugins(prev =>
      prev.map(item => (item.name === plugin.name ? { ...item, enabled } : item)),
    )
    try {
      await api(`/api/plugins/${encodeURIComponent(plugin.name)}/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      })
      messageApi.success(enabled ? '插件已启用' : '插件已停用')
    } catch {
      setPlugins(prev =>
        prev.map(item =>
          item.name === plugin.name
            ? { ...item, enabled: !enabled }
            : item,
        ),
      )
    }
  }

  const deletePlugin = (plugin: PluginInfo) => {
    if (plugin.source !== 'user') return messageApi.info('内置插件不能删除')
    confirmResourceDeletion('插件', plugin.name, async () => {
      await api(`/api/plugins/${encodeURIComponent(plugin.name)}`, { method: 'DELETE' })
      setPlugins(prev => prev.filter(item => item.name !== plugin.name))
      messageApi.success('插件已删除')
    })
  }

  const deleteSkill = (skill: SkillInfo) => {
    if (skill.source !== 'user') return messageApi.info('内置技能不能删除')
    confirmResourceDeletion('技能', skill.name || skill.id, async () => {
      await api(`/api/skills/${encodeURIComponent(skill.id)}`, { method: 'DELETE' })
      setSkills(prev => prev.filter(item => item.id !== skill.id))
      messageApi.success('技能已删除')
    })
  }

  const toggleSkill = async (skill: SkillInfo, enabled: boolean) => {
    setSkills(prev =>
      prev.map(item => (item.id === skill.id ? { ...item, enabled } : item)),
    )
    try {
      await api(`/api/skills/${encodeURIComponent(skill.id)}/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      })
      messageApi.success(enabled ? '技能已启用' : '技能已停用')
    } catch {
      setSkills(prev =>
        prev.map(item => (item.id === skill.id ? { ...item, enabled: !enabled } : item)),
      )
    }
  }

  const filteredPlugins = useMemo(() => {
    const query = pluginSearch.trim().toLowerCase()
    if (!query) return plugins
    return plugins.filter(item =>
      [item.name, item.description, item.source].join(' ').toLowerCase().includes(query),
    )
  }, [plugins, pluginSearch])

  const filteredSkills = useMemo(() => {
    const query = skillSearch.trim().toLowerCase()
    return skills.filter(item => {
      const matchesFilter =
        skillFilter === 'all' ||
        (skillFilter === 'callable' && item.user_invocable) ||
        (skillFilter === 'internal' && !item.user_invocable)
      return matchesFilter && (!query ||
        [item.id, item.name, item.description, item.source].join(' ').toLowerCase().includes(query))
    })
  }, [skills, skillSearch, skillFilter])

  return { deletePlugin, deleteSkill, extensionsTab, filteredPlugins, filteredSkills, loadPlugins, loadSkills, pluginSearch, plugins, setExtensionsTab, setPluginSearch, setPlugins, setSkillFilter, setSkillSearch, setSkills, skillFilter, skillSearch, skills, togglePlugin, toggleSkill } as const
}
