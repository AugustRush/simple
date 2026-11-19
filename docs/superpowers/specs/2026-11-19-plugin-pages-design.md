# 插件自己的页面——设计

**Goal:** 让插件能贡献一个界面，而不只是端点和工具。这是插件化剩下的最后一块：
工具、技能、频道、命令、agent 角色、MCP、投递目标、系统任务、HTTP 端点都已经是能力
服务了，**只有 UI 不是**——5 个视图 4045 行，全是硬编码。

---

## 1. 现在的死路

插件能挂 `GET /api/plugin/screen`，但用户看不到它：没有任何地方能显示一个页面。所以
"选股插件"目前只能长成对话里的表格，或一份 HTML 附件——都不是"一个页面"。

## 2. 两条路，和为什么先走第二条

| | 声明式（插件给 schema，前端用既有组件渲染） | 沙箱 HTML（插件给自己的 HTML，iframe 嵌） |
|---|---|---|
| 安全边界 | **靠前端代码正确**：每个字段渲染成一个 antd 组件，组件有问题就是洞 | **靠浏览器**：`sandbox` 属性是引擎实现的，不是我们写的 |
| 风格一致 | 完全一致 | 插件自己的，可能难看 |
| 表达力 | 表格、表单、开关、图表——就这些 | 任意 |
| 插件作者 | 要学我们的 schema 词汇 | 会写 HTML 就会 |
| 主题、暗色 | 自动 | 要传进去，插件得配合 |
| 实现成本 | **要改前端构建**（新组件 + 新路由 + 每次改插件都要重新构建？不，是每次改*渲染器*才构建） | 后端一个端点 + 一个 iframe，前端一处改动 |

**先做沙箱 HTML，理由不是它更好，是它更诚实**：

1. **边界由浏览器执行**。声明式方案的"安全"取决于我们把每个 schema 字段都映射到
   一个安全的组件上；漏一个（比如允许 `href` 是 `javascript:`）就是 XSS。iframe 的
   `sandbox="allow-scripts"`（不含 `allow-same-origin`）让插件跑在一个**不透明源**里，
   读不到父页面的任何东西——这是浏览器保证的，不是我们保证的。
2. **不需要改前端构建就能上线**。声明式要在 `frontend/src` 里加渲染器，而产物是从
   `frontend/` 构建的；沙箱方案只需要前端加**一个**通用视图，之后插件随便发版。
3. **插件作者的认知成本是零**。"给我一个 HTML"是所有开发者都懂的话。

代价我认：**风格会不统一**。所以给它一层外壳（我们的页面头 + 卡片边），里面是插件
自己的世界。这一条写在这里，而不是等它变成投诉。

## 3. 形状

### 插件侧

```python
class ScreenerPlugin:
    name = "stock-screener"
    inject = ("web_routes", "web_pages")

    def setup(self, ctx):
        # 一个页面，就是一个 HTML 端点 + 一条声明
        ctx.web_pages.add(
            path="screener",                  # → #/p/screener
            title="选股",
            description="按条件筛股",
            html=lambda: SCREENER_HTML,       # str 或 (request) -> str
        )
```

`html` 给字符串就是静态页面；给可调用就是每次请求现生成（插件可以注入数据）。

### 服务侧（`agent/channels/web_pages.py`）

跟 `web_routes` 同一个形状（`ScopedContribution` + 协议 + 核心插件），两个区别：

- **只允许 GET，只有一条路径**：`/api/plugin/pages/{id}`。页面不是 REST 资源。
- **响应头固定**：`Content-Type: text/html; charset=utf-8`，外加
  `Content-Security-Policy: sandbox allow-scripts allow-forms`。CSP 是给"有人在
  iframe 之外直接打开这个 URL"准备的——那样它也是一个沙箱，而不是一个能读 cookie 的
  同源页面。

### 前端

- 新视图 `#/p/<id>`，一个 iframe：`<iframe sandbox="allow-scripts allow-forms"
  src="/api/plugin/pages/<id>">`。
- **不加 `allow-same-origin`**：加了它就等于没沙箱（同源 + allow-scripts 可以自己
  摘掉 sandbox 属性）。这是整个方案唯一的硬边界，不能为了"省事读个 localStorage"松掉。
- 主题通过**查询参数**传进去（`?theme=dark`），插件自己决定用不用。不用 postMessage：
  双向通道是第二套 API，而第一步只需要一个方向。
- ~~插件页面自己的数据走 `fetch('/api/plugin/...')`——同源，cookie 在~~

  **这条设计是错的，实现时被浏览器否掉了。** 不含 `allow-same-origin` 的 iframe 是
  不透明源，`fetch` 一个相对 URL 会被当作跨源请求、`Origin: null`，服务端不认，浏览器
  直接 `TypeError: Failed to fetch`。实测确认（cookie / parent / localStorage 都被
  正确拒绝的同时，fetch 也拒绝了）。

  **正确做法：数据在生成 HTML 时内联。** `html` 可以是 `(request) -> str`，服务端把
  数据渲染进去。交互用导航（`<a href="?max_pe=15">`、`<form method=get>`），服务端
  重新渲染整页。这保住了沙箱的完整性，代价是页面不能做局部刷新——第一步够用。

  要客户端取数据，正确的路是给页面一个短期 token、服务端接受 `Origin: null` 但校验
  token。那是另一个机制，不混进来。

### 导航

`#/p/<id>` 不从 `VIEW_NAMES` 走（那是四个固定页面），而是路由里新增一个前缀。设置页
「能力」卡里每个有页面的插件多一个「打开」链接；同时在 `⋯` 菜单里列出（插件多了才
折叠）。

## 4. 明确不做

1. **不做插件页面的设置区**（"贡献一块表单到设置页"）。那是声明式的地盘，混进来就
   等于在沙箱旁边再开一条要信任的路径。
2. **不做 postMessage 双向通信**。第一步只需要"显示"。要交互，插件页面自己 fetch。
3. **不让插件改我们的页面**（注入到对话、看板、设置）。三个真实页面各有各的语义，
   插件往里面塞东西会让"这个按钮是谁的"变成无法回答的问题。
4. **不做插件自己发前端产物**。那要求我们的构建链路接受第三方 bundle，是另一个
   量级的事。

## 5. 验收

- [ ] 插件声明一个页面 → 设置页出现「打开」→ 点开看到它自己的 HTML
- [ ] iframe 里 `document.cookie`、`parent.document` 都读不到（**沙箱真的生效**）
- [ ] 插件页面能 `fetch('/api/plugin/…')` 拿到自己的数据（`web_routes` + 鉴权）
- [ ] 卸载插件 → 页面 404，导航项消失
- [ ] 没有 `allow-same-origin`（有测试钉住，因为加回来"就一会儿"是最可能的退化）
- [ ] 页面标题进顶栏，主题参数传进去
- [ ] 两个插件不能抢同一个 id（同 `web_routes` 的去重规则）

## 6. 风险

| 风险 | 处理 |
|---|---|
| 插件页面很难看，让 app 显得廉价 | 外壳是我们的（页面头、留白）；里面是它的。文档里说清楚"里面你负责" |
| 有人为了"方便"加 `allow-same-origin` | 测试钉住，且注释写明"加了等于没沙箱" |
| iframe 高度问题（滚动条套滚动条） | 固定视口高度 + 插件自己滚；不做自动测高（postMessage 才能做，明确不做） |
| 插件页面里放外链、追踪像素 | 它已经是一个独立源；CSP 里不加 `img-src` 限制会放行。**第一步放行**，写在这里 |
