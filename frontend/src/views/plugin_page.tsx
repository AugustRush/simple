/** A page a plugin contributed, shown inside a sandboxed frame.
 *
 * Why a frame and not our components
 * ----------------------------------
 * A plugin's page is *its* HTML. The alternative -- the plugin hands us a schema
 * and we render it with antd -- makes safety a property of every field mapping we
 * write, and one that lets a `javascript:` URL through is a cross-site script. In
 * a frame, safety is a property of the browser: `sandbox` without
 * `allow-same-origin` puts the document in an opaque origin, and it cannot read
 * this app's cookies, storage or DOM.
 *
 * `allow-same-origin` must never be added. With it plus `allow-scripts` the page
 * can reach into our DOM and remove its own sandbox attribute -- that pair is
 * not a weaker sandbox, it is no sandbox. There is a test pinning it, because
 * "just for a moment, so it can read localStorage" is exactly how it would come
 * back.
 *
 * Why the HTML arrives via `srcDoc` and not `src`
 * ----------------------------------------------
 * A frame navigates with the browser's own request machinery, which cannot set
 * an `Authorization` header. With `src="/api/pages/…"` every contributed
 * page was a 401 the moment somebody configured `auth_token` -- the list rendered,
 * the page inside it did not. Fetching here instead means the request goes
 * through the same client (and the same header) as every other call, and the
 * document is handed to the frame as text. Nothing about the sandbox changes:
 * `srcDoc` documents are still opaque-origin.
 *
 * The shell is ours: the heading, the padding, the background. The interior is
 * the plugin's world, and it will not look like ours -- that is the price, and it
 * was chosen deliberately over the alternative above.
 */

import { useEffect, useState } from 'react'
import { Alert, Button, Skeleton } from 'antd'
import { ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons'

function PluginFrame(props: {
  pageId: string
  theme: string
  title: string
  api: (path: string, options?: RequestInit) => Promise<Response>
}) {
  const { pageId, theme, title, api } = props
  const [html, setHtml] = useState<string | null>(null)
  const [error, setError] = useState('')
  //: Bumped by the retry button. An effect that only ran on mount would leave a
  //: page that failed once failed until the person navigated away.
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let cancelled = false
    setHtml(null)
    setError('')
    api(`/api/pages/${encodeURIComponent(pageId)}?theme=${encodeURIComponent(theme)}`)
      .then(async response => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return response.text()
      })
      .then(text => {
        if (!cancelled) setHtml(text)
      })
      .catch(reason => {
        if (!cancelled) setError(String(reason?.message || reason))
      })
    return () => {
      cancelled = true
    }
  }, [api, pageId, theme, attempt])

  if (error) {
    return (
      <Alert
        type="error"
        showIcon
        message="这个插件的页面打不开"
        description={error}
        action={
          <Button size="small" icon={<ReloadOutlined />} onClick={() => setAttempt(n => n + 1)}>
            重试
          </Button>
        }
      />
    )
  }
  if (html === null) return <Skeleton active paragraph={{ rows: 8 }} />
  return (
    <iframe
      className="plugin-page-frame"
      title={title}
      // NOTE: no `allow-same-origin`. See the docstring above; a test pins it.
      sandbox="allow-scripts allow-forms"
      srcDoc={html}
    />
  )
}

export function renderPluginPage(ctx: any) {
  const { api, pluginPage, openPage, resolvedTheme, pluginPages } = ctx
  const meta = (pluginPages || []).find((item: any) => item.id === pluginPage)
  const title = meta?.title || pluginPage || '插件页面'

  const back = () => openPage(null)

  return (
    <div className="plugin-page-view">
      <div className="plugin-page-head">
        <Button
          type="text"
          className="plugin-page-back"
          icon={<ArrowLeftOutlined />}
          onClick={back}
        >
          返回
        </Button>
        <div className="plugin-page-title">
          <strong>{title}</strong>
          {meta?.description && <span>{meta.description}</span>}
        </div>
      </div>
      <PluginFrame
        pageId={String(pluginPage)}
        theme={resolvedTheme}
        title={title}
        api={api}
      />
    </div>
  )
}
