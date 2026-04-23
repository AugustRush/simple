# Feishu Mode-Aware Orchestration Feedback Design

## Goal

在不改变 orchestration runtime 协议的前提下，让 Feishu 进度反馈从“通用事件流”升级为“模式感知的高质量反馈”。

具体目标：

- 对 `parallel / pipeline / rendezvous` 给出不同的批次级反馈
- 默认保持简洁，但能按模式和结果自动展开关键诊断信息
- 对成功的 `parallel` 批次默认展示详细指标
- 保持 runtime 产出结构化事实，Feishu 仅负责展示策略

## Problem

当前 Feishu sink 已经能显示多 agent 进度，但它基本只是把 `SubAgentProgressEvent.message` 追加到同一张进度卡里。

这有两个问题：

1. 它能显示“发生了什么”，但不能表达“这是什么编排模式”。
2. runtime 已经产出 `execution_mode`、`stage_count`、`rounds_completed`、`duration_seconds`、`write_scope_check_seconds` 等结构化信息，但 Feishu 没有真正消费这些指标。

结果是：

- `parallel`、`pipeline`、`rendezvous` 在飞书里的观感差异很小
- 用户很难一眼看懂当前批次是在并行 fan-out、依赖推进，还是多轮会合
- 成功批次缺少可观测性，异常批次缺少有层次的解释

## First Principles

1. runtime 负责事实，channel 负责呈现。
2. 展示策略不应反向污染 orchestration runtime。
3. 高质量反馈的本质不是“多发消息”，而是“按最小必要层级表达结构”。
4. 成功路径应该便于快速扫读，失败或复杂路径应该自动补充诊断信息。
5. 新展示逻辑必须兼容旧事件；缺少 metrics 时应安全退化。

## Chosen Approach

选择方案 B：在 Feishu sink 内新增基于 `event.metrics` 的模式感知格式化层。

这意味着：

- 不修改 `SubAgentProgressEvent` 的公开结构
- 不让 `BaseAgent` 承担 Feishu 专属文案责任
- 不新增公开编排工具或新协议字段
- 只在 Feishu 渠道内消费已有结构化指标并生成更好的批次级反馈

## Scope

### In Scope

- 增强 Feishu 对 `batch_started` 和 `batch_finished` 的渲染
- 基于 `event.metrics.execution_mode` 做模式感知文案
- 为 `parallel` 成功批次展示详细指标
- 为 `pipeline` / `rendezvous` 展示模式关键指标
- 为提前停止或异常情况自动展开更详细说明
- 在无 metrics 或未知模式时回退到现有通用文案

### Out of Scope

- 不修改 CLI sink 的显示策略
- 不改变 orchestration runtime 的调度行为
- 不给单个 `agent_started/finished/failed` 设计复杂的新卡片语义
- 不在这一轮切换中英文化风格
- 不引入新的 Feishu card schema 分区模型

## Rendering Policy

只增强批次级事件：

- `batch_started`
- `batch_finished`

单 agent 事件保持简洁，避免把进度卡变成日志墙。

### Parallel

#### batch_started

显示并行语义和并发限制。

示例：

`Parallel batch: 3 subtasks, max concurrency 2`

#### batch_finished

即使成功也展示详细指标。

示例：

`Parallel batch finished: 3/3 in 1.24s (scope check 0.004s)`

默认展示：

- `completed/spec_count`
- `duration_seconds`
- `write_scope_check_seconds`（若存在）

### Pipeline

#### batch_started

显示依赖推进语义。

示例：

`Pipeline batch: 3 subtasks, dependency-driven execution`

#### batch_finished

成功时展示阶段信息：

`Pipeline batch finished: 3/3 across 2 stages in 1.02s`

若 `completed < spec_count`，则显示提前结束语义：

`Pipeline batch ended early: 2/3 across 2 stages in 0.88s`

这里不猜测具体失败内容，只明确：

- 这是 pipeline
- 没有走完整条链
- 已经推进到多少阶段

### Rendezvous

#### batch_started

显示会合式、多轮收敛语义。

示例：

`Rendezvous batch: 2 subtasks, max 2 rounds`

若 `max_rounds` 不可得，则只显示模式名和参与数。

#### batch_finished

显示轮次信息。

成功示例：

`Rendezvous batch finished: 2 subtasks, 2 rounds in 1.88s`

如果只跑了 1 轮，也应照实显示：

`Rendezvous batch finished: 2 subtasks, 1 round in 0.93s`

如果运行过程中通过 `stop` 或 `continue_with` 收缩参与者，仍以最终 `rounds_completed` 和 `result_count` 为准，不在 Feishu 层补充推断性说明。

### Unknown / Legacy

若不存在 `metrics.execution_mode`，或模式未知：

- 回退到当前通用文案
- 不阻塞进度卡渲染
- 不要求调用方补齐新字段

## Data Sources

Feishu 格式化器仅读取已有事件字段：

- `event.kind`
- `event.message`
- `event.completed`
- `event.total`
- `event.metrics.execution_mode`
- `event.metrics.spec_count`
- `event.metrics.max_parallel_agents`
- `event.metrics.duration_seconds`
- `event.metrics.write_scope_check_seconds`
- `event.metrics.stage_count`
- `event.metrics.rounds_completed`

不新增 runtime 依赖，不新增 Feishu 私有回传字段。

## Implementation Plan

### 1. Add Feishu-Specific Batch Formatters

在 `FeishuOutputSink` 中新增私有格式化函数，例如：

- `_format_batch_started_event(event)`
- `_format_batch_finished_event(event)`
- `_format_mode_aware_subagent_event(event)`

入口仍保持 `on_subagent_event()` 调用单一格式化函数。

### 2. Keep Agent-Level Events Simple

对于：

- `agent_started`
- `agent_finished`
- `agent_failed`

继续沿用当前的简洁展示逻辑，避免 per-agent 明细淹没批次级摘要。

### 3. Prefer Structured Metrics Over Free-Form Messages

Feishu 渠道对批次级事件优先使用 `metrics` 渲染。

只有在以下场景回退到 `event.message`：

- 缺少 `metrics`
- 模式未知
- 必要指标缺失到无法稳定格式化

这样能把渠道策略和 runtime 字符串文案解耦。

### 4. Preserve Existing Progress Card Flow

不改变当前 Feishu 的消息结构：

- 仍使用同一张进度卡承载过程信息
- 仍由 `_append_progress_text()` 把格式化后的文本追加进去
- 仍在 turn complete 时复用当前 finalize 逻辑

这一轮优化的是“内容质量”，不是“卡片结构模型”。

## Testing

先写失败测试，再补实现。

最小测试集：

1. `parallel` 的 `batch_started` 显示模式和最大并发
2. `parallel` 的 `batch_finished` 成功批次显示耗时和 `scope check`
3. `pipeline` 的成功批次显示 `stage_count`
4. `pipeline` 的提前结束批次显示“ended early”语义
5. `rendezvous` 的成功批次显示 `rounds_completed`
6. 未知模式或缺少 metrics 时，回退到当前通用文本

这些测试应写在：

- `tests/test_feishu_channel.py`

不需要新增 runtime 层测试，因为本轮不修改 runtime 协议或调度行为。

## Risks

### 1. Feishu 文案与 CLI 文案分叉

这是有意为之。CLI 保持通用，Feishu 做模式感知。

风险可接受，因为两者职责不同。

### 2. Metrics Shape Drift

如果未来 runtime 调整某些 metric 字段名，Feishu 渲染可能退化。

缓解方式：

- 所有 mode-aware 渲染都做字段存在性判断
- 缺字段时回退到通用文案，而不是报错

### 3. 过度冗长

如果批次级文案写得太长，Feishu 进度卡会失去可扫读性。

缓解方式：

- 只增强 `batch_started` / `batch_finished`
- 单 agent 事件保持简洁
- 指标数量控制在 2-4 个核心字段

## Recommendation

按最小实现推进：

- 只改 `channels/feishu.py`
- 只补 `tests/test_feishu_channel.py`
- 不改 runtime，不改 CLI，不改 card schema

这样可以用最小风险，把 Feishu 从“能看到事件”提升到“能看懂编排模式和关键指标”。
