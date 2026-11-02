# 参考 dsh-superagent-prototype：复核与迭代计划

2026-11-01 · 设计记录，先于代码。上一轮我给出的"可借鉴清单"里有四条经不起复核，先把它们改正，再排计划。

## 被参考的对象

[deepseek-ai/dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype)：DeepSeek 2026-10-31 公开的研究原型，MIT，TypeScript，216 个文件，DSH bundle，领域是"一个 GitHub 仓库的主动型编码 agent"。README 自述：**research prototype for reference only, not an official DeepSeek product**；并明确说文章里有些设计是未来方向、**原型里没有实现**。

## 复核：上一轮结论逐条对账

| 上一轮的说法 | 判定 | 证据 |
|---|---|---|
| 它是官方研究原型，不是产品；TS / DSH bundle / 216 文件；docs 000–008 | **正确** | 仓库 tree + README；`docs/` 九篇齐全 |
| 授权是**三态强度**（explicit / inferred / not admitted）+ admission table | **正确，且已实现** | `docs/002-authorization.md` 的 Strength 表；`src/admission.ts` 存在 |
| Engine 拥有事务、调用者只拿到角色的 `Pick`、先存后做 | **正确** | `docs/007-architecture.md`；`src/core/engine.ts` |
| **"record-only 动作模式"最该抄** | **错（最重要的那条）** | 那句话只在 `docs/000-philosophy.md`；`docs/003-execution.md` 全文没有这个模式，已实现的是 `pause`（Source 继续记录、什么都不派发）与 accept mode `off`（不接主动候选）。原型自己没做 |
| 我们**完全没有**这个能力 | **错** | 我们已有强制侧的一半：`agent/scheduler/unattended.py` 的 `UnattendedOutputSink` 在无人时**拒绝并记录**每一次 "may I?"，每次运行写 `unattended-audit.md`（`agent/scheduler/build.py:567`）。缺的不是"记录"，是"**替身执行**"——拒绝会让任务半路失败，排练要让任务跑完并产出"我本来会做什么" |
| 设置要从 **103 键收敛到 60** | **数字与框架都错** | 真实是 **82 个叶子键**：`providers` 26、`context` 15、`file_access` 8、`connectors` 6、`orchestration` 4、`scheduler` 4、`permissions` 4、`memory` 3，其余 12 个单键。一半是**连接/管线参数**（provider 的 api_key、model、max_tokens），不是"团队策略"。"a default replaces a setting" 只对策略性子集成立，而那批多半还是 `max_*` 上限 |
| outbox：我们会"说了但没做到" | **错** | 定时运行的投递结果**已经**记在 run 上：`agent/scheduler/runtime.py:798-810` 的 `delivery_status` / `delivery_error`。桌面通知是**刻意的** best-effort，并在 `agent/channels/desktop_notify.py` 开头写明理由 |
| 2,327 stars | **当时准确，现在未复核** | GitHub API 匿名额度已耗尽；数字取自 2026-10-31 的查询 |

## 复核后的结论

1. **最值得借鉴的不是"record-only"，而是它的措辞纪律**：一个动作到底是"人点名的"、"规则允许的"还是"不该发生的"，三档要各自有名字。我们只有二元的 `requires_request` / `requires_intent`，缺的是**词汇**，不是机制。
2. **"排练模式"仍然值得做，但它是我们的发明，不是移植**：原型没有实现它；我们的 `UnattendedOutputSink` + `permissions` profile + run 输出目录已经把它垫到六成。这改变了它的性质——不再是"抄一个成熟设计"，而是"补一个我们已经有半个的能力"，风险更高，需要先红后绿。
3. **不移植 TS 代码、不移植 Engine 分层**：单行为者的个人 agent 用不上多角色 `Pick`，收益不抵改动面。
4. **设置收敛降级为小任务**：先把策略子集（`permissions` / `orchestration` / `notifications` / `evolution`，约 10 个）逐条问"默认值能替掉吗"，而不是大改 82 个。

## 追加需求：workflow 与任务「合并统一」

用户提出：自动化里的 workflow 和任务应该合并统一。查代码之后，这件事比听起来小得多——**领域模型早就是统一的**，分叉只在呈现层：

| 证据 | 说明 |
|---|---|
| `models.py:1542` `WorkflowStep` 的 docstring | "**One step of a workflow: a task, plus what has to finish before it runs**"；无上游的入口步"就是一个普通 scheduled task" |
| `tests/test_scheduler_workflows.py:11` | "**A step is a task that already exists.**" |
| schema `workflow_runs.root_task_id` / `root_run_id` | 每一次链运行都锚定在一个任务运行上 |
| `scheduled_tasks.workflow_id` / `step_key` | 步就是行，归属记在任务自己身上 |
| `attention.py` 的 `latest_by_task` | 注意力/badge 已经是任务级的，链的注意力就是其步骤之和 |

所以真正的重复在**三个面**：两套列表 API（`/api/schedules`、`/api/workflows`）、两套工具（`schedule_*` / `workflow_*`）、两个页签（任务 / 流程）。

### 目标模型

一个聚合 **Automation**：`{name, enabled, steps[], delivery, policies, next_run_at, …}`。

- 普通任务 = **1 个步骤**的 Automation；
- 流程 = **N 个步骤**的 Automation，边在 `depends_on` 上；
- 第一步（入口步）带触发条件，其余步骤由上游完成触发——这条今天已经成立。

### 分阶段（每阶段独立可验证、可回滚）

| 阶段 | 内容 | 风险 | 验收 |
|---|---|---|---|
| **M0 · 读模型**（本次已做） | `agent/scheduler/automations.py` + `GET /api/automations`：一个列表，两种行（`kind: task\|chain`），**步骤永不单独成行**，注意力与 badge 用同一个 snapshot | 零（只读） | 3 个任务 + 1 链 → 2 行；`sum(rows.attention) == total`；7 条测试 |
| **M0b · 一个列表页**（已完成） | 自动化页两个页签合成一个列表，`任务/流程/全部` 变成工具栏筛选；行点开复用现有详情面板 | 低（前端） | 浏览器 e2e：两个页签、空状态、API 建的任务出现且只出现一次 |
| **M1 · 一套工具**（已完成创建部分） | `automation_create`：无 `steps` = 一个任务，有 `steps` = 一条链；`schedule_create` / `workflow_create` **从工具面移除**（方法保留给内部与测试），维护类工具（update/delete/run…）暂留 | 中 | 工具面上旧的两个名字**不存在**；新工具能建 1 步与 N 步；链仍然先校验图再写 |
| **M2 · 一条写路径**（入口已完成） | 一个 `新建`；任务编辑器里「改成流程（多步）」把草稿搬成 1 步的链 | 中 | e2e：只有一个创建按钮、转换后出现链编辑器 |
| **M2 余下（未做）** | 维护面：任务的 `update/delete/run/…` 与链的 `update/delete`；两个列表视图里的批量操作与内联运行历史 | 中 | 一条 `automation_update` 能改 1..N 步；批量操作对链有意义之后 |
| **M3 · 折叠存储（可选）** | `workflows.graph_json` → `scheduled_tasks.steps_json`；`workflow_runs` → run 上的 `group_id`；迁移 v+1 | 高（迁移） | 迁移脚本把现有链完整搬过去；旧库升级后读模型输出不变 |

**为什么不从 M3 开始**：M0–M2 能让用户与模型看到一件事，而 M3 只是让硬盘上少两张表。在活数据上做 schema 折叠，收益是"开发者少看两张表"，代价是迁移事故——这一轮已经反复证明，"看得见的重复"和"存储的重复"要分开处理。

## 迭代计划

### P0 · 勘误与收尾（0.5 天，已完成）

- 本文档入库；README 的 Roadmap 增补"排练模式"与"授权词汇"两条，删掉已完成的"重启墙"（上一轮已关）。
- `bash scripts/check.sh` 绿（当前基线 4,380 passed / 0 failed / 2 skipped）。
- **验收**：无代码改动，文档准确，测试绿。

### P1 · 排练模式（Rehearsal）：让主动任务先"只说不做"（2–3 天）

问题：agent 自己创建的定时任务/工作流**第一次上线就真的执行**；没人时 `UnattendedOutputSink` 会拒绝并记录，但任务自己是半路失败的，人看到的是一份"被拒清单"，不是"我本来会做什么"。

设计：

- `PermissionProfile` 增加 `rehearsal`。在排练档下，`state_write` / `side_effect` 类调用**不执行**，返回 `{"rehearsed": true, "would_have": {...}}` 并记账。关键区别：不是拒绝（任务继续跑完），是替身返回值。
- `automation_create` 由 **agent 创建时**默认 `first_run="rehearsal"`；人创建时默认 live（人已经在对话里决定了）。
- run 状态新增 `rehearsed`：与 `succeeded` / `failed` 并列，不触发重试、不计入失败、不做投递。
- 报告写进 run 输出目录的 `rehearsal.md`，复用 `unattended-audit.md` 的表格形态：每一行是一条"如果执行会做的事"，带完整命令/参数/目标路径。
- 晋升：`schedule_update(first_run="live")`，或自动化页一个按钮；晋升后第一次真实运行照常通知。

**验收（先红后绿）**：

1. 一个"写文件 + 跑 shell"的排练任务跑完 → 文件不存在、命令未执行、报告含两条完整动作、状态 `rehearsed`。
2. 排练运行不投递、不计失败、不重试。
3. 晋升后同一任务真实执行 → 文件存在、状态 `succeeded`。
4. 人创建的任务默认 live（"全都排练"等于没有自动化）。

**风险与回滚**：纯增量（新 profile 档 + 新状态），既有任务行为不变；回滚 = 去掉该档与状态。

### P2 · 授权词汇：三档各有名字（0.5 天，可与 P1 并行）

- 在**审计与 UI**里把现有判定分成 `explicit`（人点名要求）/ `inferred`（规则允许、非点名）/ `not_admitted`（拒绝）。
- **不改变门禁语义**：词汇只描述审计记录，不引入第二套真相（现有 `requires_request` / `requires_intent` / authorizer 保持唯一判定点）。
- **验收**：审计里每条被允/被拒的动作都带档位；现有门禁测试一条不改、全绿。

### P3 · 简化 A：`agent/plugins/catalog.py` 拆分（1 天）

- 现 2,631 行 / 53 方法 / 一个账本。
- 先搬"发现"：`agent/plugins/discovery.py`（扫描、marketplace 展开、模块加载、provenance、first-look 基线），`PluginCatalog` 保留同名方法**委托**，对外 API 不变。
- **验收**：diff 只含移动 + 委托；`tests/test_plugin_catalog.py`、`test_hot_swap.py`、`test_composition_refresh.py` 全绿；文件降到 ~1,700 行。
- 不用 mixin（mixin 是新的复杂度来源）。

### P4 · 简化 B：`agent/channels/web.py` 按领域拆（1.5 天）

- 3,029 行 / 113 个方法，每个 handler 都很短，问题是"全都住在一个类里"。
- 按域拆：sessions / memory / plugins+skills+composition / config / scheduler；路由表留在 `_build_app`（它就是索引）。
- **验收**：`tests/test_web_route_contract.py` 仍绿（它证明没有路由在搬家时漏掉）；`web.py` < 800 行。

### P5 ·（可选，需回滚准备）`turn_loop.run_turn_loop` 450 行（2 天）

- 仓库最热的一条路径，动它之前先做**录制/回放**夹具：用现有 scripted provider 录一轮真实 turn 的事件序列，重构后逐事件比对。
- 只在 P0–P4 完成后做；没有回放夹具就不动。

### 明确不做

- **outbox**：投递结果已记录，通知是刻意的 best-effort。
- **设置大收敛**：82 键里 41 个是 providers/context 的管线参数。
- **Engine / 角色化 Pick 移植**：单行为者用不上。
- **移植原型代码**：语言、形态、领域三处都不同。

## 验证契约（贯穿全部阶段）

1. 每个阶段结束跑 `bash scripts/check.sh`（锁文件 + 全量测试 + dist 新鲜度）。
2. 新能力必须先红后绿；改结构的阶段，diff 里不出现行为变化。
3. 每个阶段的提交信息写清"没做什么"，与本仓库既有风格一致。
