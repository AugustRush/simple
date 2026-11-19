/** A page a plugin contributed, shown inside a sandboxed frame.
 *
 * Why a frame and not our components
 * ----------------------------------
 * A plugin's page is *its* HTML. The alternative -- the plugin hands us a schema
 * and we render it with antd -- makes safety a property of every field mapping
 * we write, and one that lets a `javascript:` URL through is a cross-site
 * script. In a frame, safety is a property of the browser: `sandbox` without
 * `allow-same-origin` puts the document in an opaque origin, and it cannot read
 * this app's cookies, storage or DOM.
 *
 * `allow-same-origin` must never be added. With it plus `allow-scripts` the page
 * can reach into our DOM and remove its own sandbox attribute -- that pair is
 * not a weaker sandbox, it is no sandbox. There is a test pinning it, because
 * "just for a moment, so it can read localStorage" is exactly how it would come
 * back.
 *
 * The shell is ours: the heading, the padding, the background. The interior is
 * the plugin's world, and it will not look like ours -- that is the price, and it
 * was chosen deliberately over the alternative above.
 */

import { Button } from 'antd'
import { ArrowLeftOutlined } from '@ant-design/icons'

export function renderPluginPage(ctx: any) {
  const { pluginPage, openPage, resolvedTheme, pluginPages } = ctx
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
      {/* The frame is rendered whether or not the page is in the listing. The
          listing is for the *name* in the bar; whether a page exists is the
          server's answer, and it gives one -- a 404 in the frame, or the page.
          Gating on the listing made a deep link into a 404 until some other
          request happened to refresh the plugin list. */}
      <iframe
        className="plugin-page-frame"
        title={title}
        // NOTE: no `allow-same-origin`. See the docstring above; a test pins it.
        sandbox="allow-scripts allow-forms"
        // The theme travels in the URL, not over postMessage: the page only
        // needs to *read* which theme is on, and a two-way channel is a second
        // API that nothing in this step needs.
        src={`/api/plugin/pages/${encodeURIComponent(String(pluginPage))}?theme=${resolvedTheme}`}
      />
    </div>
  )
}
