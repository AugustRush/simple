# 计划：每 provider 自定义请求头 + 通用 provider 配置入口

> **给执行者：** 分两部分，各自可独立发布。每步先写会失败的测试再改代码。

**总目标：** ①让「配一个需要额外 HTTP 头的 OpenAI 兼容网关」变成一行配置，以
opencode go（`https://opencode.ai/zen/go/v1`）为第一个真实用例；②把 provider 配置
从「手改 JSON」变成设置在页面里就能完成的结构化入口——**并且两者都必须是通用的：
加一个 provider、加一个 provider 字段，都不应该需要改代码逻辑或前端**。

**顺序：** 第一部分（请求头）小而独立，先做，它解锁 opencode go。第二部分
（配置入口）依赖第一部分的字段词表，且明显更大（后端 CRUD + 前端表单），单独发布。

**技术栈：** Python 3.11+，openai 2.16 / anthropic 0.76，starlette（web 通道），
React + antd + vite（`frontend/`），pytest。前端只有 `typecheck`/`build` 两个脚本，
没有测试框架（现状如此，第二部分靠类型检查 + 构建 + 手工过一遍）。

---

# 第一部分：每 provider 自定义请求头

## 1.0 需求与事实（已在本机/文档确认，不要重新推理）

用户给出的调用形状：

```bash
curl https://opencode.ai/zen/go/v1/chat/completions \
  -H "Authorization: Bearer $OPENCODE_GO_API_KEY" \
  -H "User-Agent: my-coding-agent/1.0" \
  -H "x-opencode-session: $SESSION_ID" \
  -d '{"model": "kimi-k2.7-code", "messages": [{"role": "user", "content": "..."}]}'
```

**官方订阅说明：`x-opencode-session` 是「一个会话一个稳定 ID」**（用户确认）。
机制因此必须**按请求、按会话**取值，而不是进程级常量。

| 需要什么 | 现状 |
|---|---|
| OpenAI 兼容格式 / 自定义 base_url / Bearer（`$ENV`） | ✅ `api_format` / `base_url` / `api_key` |
| 模型名 `kimi-k2.7-code` | ✅ `models[]` + `default_model`（`routing_table`，`transport.py:1185`） |
| usage 与缓存命中记账 | ✅ `extract_provider_usage` 已读 `prompt_tokens_details.cached_tokens`（`agent/usage.py:53`） |
| 网关若拒绝 `stream_options` | ✅ `stream_usage: false`（`transport.py:1135`） |
| **额外请求头（含每会话一个值）** | ❌ **无机制** |

本机实测（httpx MockTransport 抓真实请求头）：

- 按请求的 `extra_headers=` 两种 SDK 都支持，**且与 client 级 `default_headers` 共存**。
- 全仓库只有 6 个真实 provider 请求点，都在 `transport.py`：OpenAI 的
  `_create_kwargs`（同时服务 create 与 stream，`:502`）与 `simple_chat`；Anthropic 的
  `create`/`stream`/`simple_chat`（`:346`/`:360`/`:384`）。
- **会话 id 每个入口都已经有**：`ctx.metadata["session_id"]` 由
  `runtime/contracts.py:608` 从 `TurnInput.session_id` 写入；交互/web 是会话 id，定时
  运行是 `scheduler:{task_id}:{run_id}`（cli.py:828）——**每次运行一个稳定 id**，正好是
  「一个会话一个 ID」。CLI、web、调度器、子代理四条路径都经过 `send_message`
  （agent.py:2929；子代理 agent.py:3976）。

## 1.1 设计

配置声明模板，`{session}` 在**请求发生时**解析；client 保持「一个 provider 配置一个」，
构造参数、缓存键、连接池不变量全部不变。

```json
"opencode-go": {
  "api_format": "openai",
  "base_url": "https://opencode.ai/zen/go/v1",
  "api_key": "$OPENCODE_GO_API_KEY",
  "default_model": "kimi-k2.7-code",
  "models": ["kimi-k2.7-code"],
  "max_tokens": 32000,
  "context_window": 256000,
  "headers": { "User-Agent": "zcode/1.0", "x-opencode-session": "{session}" }
}
```

取值规则两条，都是现有约定的延伸：

1. 整个值是 `$NAME` → 读环境变量，缺失时以与 `api_key` 相同的措辞报错（不静默发空头）。
2. 值里出现 `{session}` → 每个请求替换为当前会话 id；无会话上下文（后台整合等不经过
   turn 的调用）回落到**进程内稳定**的实例 id（`uuid4()` 一次、模块级缓存）。

**词表只有 `{session}`**，校验器对未识别的 `{...}` 发警告——词表越小越不会说谎。

### 通用性怎么体现（这是本部分的设计验收标准）

- 机制按**能力**命名（`headers`），不按 provider 命名：任何 OpenAI / Anthropic 兼容
  网关都能用，代码里不允许出现 `if provider == "opencode"` 这类分支。
- 两种 `api_format` 都生效（不是只服务 openai 格式）。
- **active 与非 active 分组都生效**：子代理用外部模型时会路由到别的分组，头必须跟着
  走（见 `build_routing_transport` 的分组循环）。
- 字段是配置里的普通键，所以第二部分做出来之后，设置页会自动出现这个字段——不需要
  为它写任何前端代码。

## 1.2 改动清单（client 构造与缓存键不动）

| 文件 | 改动 |
|---|---|
| `agent/shared.py` | `instance_id()`（进程内稳定）；`_active_session_id` ContextVar + `current_session_id()`；`resolve_provider_headers(provider_cfg)`（`$ENV` 展开、`{session}` 原样保留待请求时解析） |
| `agent/core/transport.py` | `ModelTransport.__init__` 增可选 `headers`（默认 `None`，既有调用点不变）；`_headers_kwarg()` 返回 `{}` 或 `{"extra_headers": {...}}`，请求时替换 `{session}`；`build_transport(..., headers=...)` 透传；OpenAI 的 `_create_kwargs` + `simple_chat`、Anthropic 的 `create`/`stream`/`simple_chat` 各加 `**self._headers_kwarg()` |
| `agent/core/transport.py:1262` | `build_routing_transport` 每个分组构造 transport 时传入该 provider 的头部模板 |
| `agent/core/agent.py:2929` | `send_message` 设置/重置 `shared._active_session_id`（照 `_active_cancel_token` 的写法） |
| `agent/config.py:447` | `_check_providers` 校验 `headers`（本部分只加这一条；第二部分把它并进字段词表） |
| `config.example.json` | 加 `opencode-go` 块 + `_headers_readme` |

## 1.3 兼容性论证（「不影响当前配置」）

1. 无 `headers` 键 ⇒ 请求逐字节不变：`_headers_kwarg()` 空 dict 展开后不出现
   `extra_headers`。用测试钉住。
2. **client 构造零改动**：`config.py:216`、`bootstrap.py:105` 两个构造点不动，
   `provider_client_cache_key`（`transport.py:1228`）不动，因此不引入「每会话一个连接池」
   ——那是那段注释明令禁止的。
3. 解析与占位符只作用于新键，不触碰 `api_key`/`base_url` 既有路径。
4. 头部模板与 `thinking_effort`/`stream_usage` 一样在构造 transport 时捕获：web session
   每次重建重读配置（编辑即时生效），进程级 transport 需重启——与现有 provider 选项
   语义一致，不引入新的失效语义。
5. 校验是新增警告，不改任何既有告警的触发条件。

## 1.4 测试计划（新文件 `tests/test_provider_headers.py`）

- [ ] `resolve_provider_headers`：无 `headers` → `{}`；`$ENV` 展开；`{session}` 原样保留；
      `$ENV` 未设置 → 报错且消息含变量名。
- [ ] `current_session_id()`：有会话 → 会话 id；无 → 进程内稳定实例 id（两次相同）。
- [ ] **核心 pin**：同一 provider、同一 client，两个会话各跑一次 → 线上抓到两个不同的
      `x-opencode-session`。这条既是需求本身，也证明「一 client 服务多会话」。
- [ ] **回归 pin**：不带 `headers` 的 provider，请求 kwargs 里没有 `extra_headers`；
      两个 client 构造点没有 `default_headers`。
- [ ] 静态头与 `$ENV` 头出现在 wire 上；`User-Agent` 被配置值覆盖。
- [ ] Anthropic 格式配 headers → `messages.create` 与流式两条路径都带 `extra_headers`。
- [ ] `build_routing_transport`：**非 active** 分组也带自己的头。
- [ ] 端到端（本地 `ThreadingHTTPServer`，`test_model_routing.py` 已有套路）：配 headers 的
      provider 真发一次请求并断言请求头；对照组抓不到。
- [ ] `_validate_config`：`headers` 非字典 / 值非字符串 / 头名非法 / 未知占位符 → 各自警告。
- [ ] `send_message` 之后 ContextVar 被重置；`asyncio.gather` 两个并发会话互不串值。

## 1.5 真机验证（需要用户的 key）

1. `export OPENCODE_GO_API_KEY=<key>`，`active_provider` 指到 `opencode-go`，单轮对话有回复。
2. 多轮对话流式正常；查 `usage_events` 是否有该次调用（`scripts/analyze_cache_hits.py`
   只读查看）。为空 → 网关大概拒绝 `stream_options`，见 1.6。
3. 让它调一次工具，确认 `tools` 被接受、`tool_calls` 正常回来。
4. 长回答确认 `max_tokens` 被接受（400 → 1.6 第 2 条）。
5. 同一会话连续多轮应是同一个 `x-opencode-session`，新会话应是另一个。
6. `context_window` / `max_tokens` 按官方文档核对后写进配置（示例值是占位）。

## 1.6 应急项（真机若失败，都不需要改架构）

1. 网关拒绝 `stream_options` → `"stream_usage": false`（既有开关）。
2. 只认 `max_completion_tokens` → 加每 provider 的 `max_tokens_param`（默认不变），
   在 `_create_kwargs` 改名。**只有真机 400 才做**。
3. 拒绝 `reasoning_effort` → 不写 `thinking`（默认不发送）。
4. 需要比「会话」更细的粒度 → 先讨论，不动本机制。

---

# 第二部分：通用 provider 配置入口

## 2.0 现状（这是要替换掉的东西）

- **设置页**（`frontend/src/views/settings.tsx`，272 行）：表单只有
  `active_provider` / `model` / `thinking_effort` / `max_tokens` + 频道开关；底下是
  「高级 JSON」文本框，**保存发的就是这段文本**（表单改动同步进 JSON）。
  → 想加一个 provider、改一个 base_url、给某个网关加 headers，只能手改 JSON。
- **后端**（`agent/channels/web.py:1197` `_config_get` / `:1214` `_config_save`）：GET 返回
  整份配置（`_mask_api_keys` 只遮 `api_key`）并附带 `thinking_efforts`；POST 存整份配置。
  其中「后端公布可选值、页面不自己抄一份」是有先例的：
  > the page renders what this backend accepts instead of keeping its own copy of the set
  > -- the two would drift the moment a level is added.

  这正是本部分要放大的做法。
- **CLI**：`agent config list|models|get` 只读；首次运行向导 `_first_run_setup`
  （`config.py:628`）能选 provider、填 key/base_url，但不可复用为日常编辑入口。
- `save_config`（`config.py:613`）是纯原子写，不做校验；`load_config` 会补结构性默认段。

## 2.1 设计：一处词表，三处消费

**核心是给 provider 的字段建立唯一词表**（`agent/config.py`）：

```python
@dataclass(frozen=True)
class ProviderField:
    key: str            # "base_url"
    kind: str           # "string" | "secret" | "int" | "bool" | "string_list" | "string_map" | "choice"
    label: str
    help: str
    default: Any = None
    required: bool = False
    choices: tuple[str, ...] = ()
    secret_values: bool = False   # string_map 里「像密钥的名字」要不要遮

PROVIDER_FIELDS: tuple[ProviderField, ...] = (
    ProviderField("api_format", "choice", "接口格式", "...", choices=("openai", "anthropic"), required=True),
    ProviderField("api_key", "secret", "API Key", "...", required=True),
    ProviderField("base_url", "string", "Base URL", "..."),
    ProviderField("default_model", "string", "默认模型", "...", required=True),
    ProviderField("models", "string_list", "可选模型", "..."),
    ProviderField("max_tokens", "int", "输出上限", "..."),
    ProviderField("context_window", "int", "上下文窗口", "..."),
    ProviderField("output_reserve", "int", "输出预留", "..."),
    ProviderField("supports_vision", "bool", "支持图片", "..."),
    ProviderField("stream_usage", "bool", "流式上报用量", "..."),
    ProviderField("thinking", "string_map", "思考强度", "..."),
    ProviderField("headers", "string_map", "额外请求头", "...", secret_values=True),
)
```

**词表就是代码读取的那套键**——按本仓库实际读取的清单核对，一个不多一个不少：
`api_format`、`api_key`、`base_url`、`default_model`、`models`、`max_tokens`、
`context_window`、`output_reserve`、`supports_vision`、`stream_usage`、`thinking`
（子键 `effort`/`models`）、`headers`（第一部分新增）。用测试钉住「词表 ⟷ 校验器 ⟷
示例配置」三者一致，谁漂了就红。

三个消费方：

1. **校验**（`_check_providers`）：按 `kind` 做类型检查；未知 provider 键 → 警告
   （对齐顶层 `_check_unknown_keys` 的做法；`_` 开头的 readme 伴生键照旧跳过）。
   新增一个 provider 键 = 词表加一行，校验自动覆盖。
2. **设置页 schema**：`GET /api/config` 的响应里带上 `provider_fields`（与既有的
   `thinking_efforts` 同一个理由）。前端**按 kind 渲染控件**，不硬编码任何字段名。
3. **CLI**（可选、最后做）：`agent config providers set/show/test` 与首次向导从同一词表
   生成提示；不做也不影响本部分完成。

### 后端接口（都是新增，POST 整份配置的老路径继续可用）

| 方法 | 路径 | 语义 |
|---|---|---|
| `POST` | `/api/providers/<name>` | 新建或局部更新一个 provider；按词表校验；未知键拒收并说明 |
| `DELETE` | `/api/providers/<name>` | 删除；**当前 active 或最后一个 provider 拒绝删** |
| `POST` | `/api/providers/<name>/activate` | 切换 active（保留原 provider 不动） |
| `POST` | `/api/providers/<name>/test` | 真发一次最小请求（`max_tokens=1`），返回 ok/延迟/模型或网关原话 |

两条硬要求：

- **密钥语义**：GET 永不返回明文；`secret` 字段与「像密钥的头值」（`Authorization`、
  含 `key`/`token`/`secret` 的头名）一律遮成 `******`，提交时 `******` 或空值表示
  「不改」，其余原样写入。这是把第一部分里那条「可选硬化」变成必需项——因为页面上
  现在真的会出现 `headers` 输入框，而 `_mask_api_keys` 目前只遮 `api_key`。
- **保存后即时生效**：走既有的 `save_config` + `config_revision += 1`（下一次 turn 会据
  新配置重建 session runtime）。测试连接必须走**与真实调用同一条 transport/client 路径**
  （否则它验证不了 base_url/格式/headers，等于没测）——`{session}` 在测试里回落到实例 id。

## 2.2 前端（`frontend/src/views/settings.tsx` + `hooks/useSettings.tsx`）

「Providers」卡片，取代现在只能靠 JSON 的那部分：

- 列表：名字 / 格式 / base_url / 模型数 / active 单选 / 「测试」与「删除」。
- 展开：按 `provider_fields` 渲染表单；`string_map` 渲成「键-值」行（headers 与
  thinking.models 共用这个控件，**不为 headers 特判**）。
- 「添加 Provider」：名字 + 从 `api_format` 的 choice 选格式——这就是「自定义
  OpenAI 兼容网关」的入口，加 opencode go 不再需要改代码。
- 「高级 JSON」卡**保留**：它是逃生门，也是老路径的兼容面。

**通用性验收标准**：把 `headers` 从词表里删掉，页面就不该再出现这个输入框；给词表加
一个假字段（如 `kind="string"` 的 `foo`），页面应当**自动**多出一个输入框——前端代码
一行不改。这条写成第二部分的验收步骤。

## 2.3 测试计划

后端（`tests/test_web_channel.py` 或新文件 `tests/test_provider_settings.py`）：

- [ ] 词表完整性：所有被读取的 provider 键都在词表里；`config.example.json` 里每个
      provider 的每个非 `_` 键都能被词表解释（三者一致性）。
- [ ] `GET /api/config` 返回 `provider_fields`，且每行的 kind/label/secret 与词表一致。
- [ ] `POST /api/providers/<name>`：新建（含自定义 base_url/headers）、局部更新（只传
      一个字段不影响其它字段）、非法值（格式非法/类型错/未知键）被拒且消息可读。
- [ ] 密钥往返：GET 里 api_key 与 `Authorization` 头是 `******`；POST `******` 回存后
      磁盘上仍是原值；POST 新值则更新。
- [ ] `activate` 切换后 `routing_table` 的归属随之改变；`DELETE` 拒绝删 active 与最后一个。
- [ ] `POST .../test` 走真实 transport：用本地 HTTP server 断言它命中的路径、模型名与
      请求头（就是第一部分那个 headers 在 UI 里可验证的证据）。
- [ ] 老的 `POST /api/config` 仍然可用，且不会把 provider 块写坏（回归）。

前端（现状没有测试框架，如实记录做法）：

- [ ] `npm run typecheck` 与 `npm run build` 通过。
- [ ] 手过一遍：加一个自定义 provider（base_url + models + headers）→ 切 active →
      测试连接 → 发一条消息成功；以及在 JSON 卡里手改 provider 后表单能同步显示。
- [ ] 上述「删掉一个词表字段/加一个假字段，页面随之变化」的通用性验收。

## 2.4 不做的事（两部分共同）

- 不为 opencode 或任何具体 provider 写专门代码路径。
- 不给 client 加 `default_headers`，不改 `provider_client_cache_key`，不引入每会话 client。
- 不动 transport 的请求构造语义、不动 usage 记账、不动 `models` 的校验方式。
- 第二部分不重写「高级 JSON」这一逃生门，也不动它作为保存源的既有行为。
