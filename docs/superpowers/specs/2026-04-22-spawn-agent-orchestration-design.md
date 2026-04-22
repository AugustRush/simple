# Spawn-Agent Orchestration Design

## Goal

在不新增公开工具原语的前提下，把多 agent 协作收敛为 `spawn_agent` 之上的内部编排能力，并用内置 skill 决定何时采用并行、串行或有限轮同步策略。

## First Principles

1. 公开原语越少越稳定。
2. 编排的本质是同步与隔离，不是“团队感”。
3. prompt/skill 负责决策，runtime 负责执行边界。
4. 默认直接回答，编排是例外路径。
5. 所有编排都必须能退化为单 agent 执行。

## Problem Statement

当前系统只有 `spawn_agent` 这个公开委派原语。此前尝试把“团队协作”包装成新的公开工具 `team_run`，但这会引入额外概念，且能力上并没有超出 `spawn_agent` 可以表达的范围。更合理的做法是保留 `spawn_agent` 作为唯一公开原语，把“agent teams”内置为 lead agent 的一种内部运行策略。

需要解决的问题只有四类：

- 何时编排，何时直接回答
- 独立子任务如何并行执行
- 依赖型子任务如何按阶段同步
- 多轮协作如何在不引入 agent 直连的前提下收敛

## Non-Goals

- 不新增 `team_run`、`review_team` 等公开工具
- 不做 agent-to-agent 自由通信
- 不做持久化 team state / mailbox / task board
- 不做独立 team UI
- 不做递归 team of teams

## Proposed Architecture

分成两层：

### 1. Skill Layer

新增一个内置 skill，暂名 `multi-agent-orchestration`。

职责：

- 判断当前任务应使用 `direct / parallel / pipeline / rendezvous`
- 约束何时不要编排
- 约束 subtasks 必须独立、明确、可收敛

skill 不直接实现执行逻辑，不引入新工具，只给 lead agent 决策规则。

### 2. Runtime Layer

新增内部编排执行器模块，例如：

- `agent/orchestration/runtime.py`

职责：

- 接收 lead 构造的 subtask specs
- 运行并发/分阶段/有限轮同步
- 统一处理超时、失败、汇总输入
- 最终仍通过 `spawn_agent` 创建独立子 agent

这层不向用户暴露新的公开工具。

## Supported Modes

### direct

适用：

- 简单问题
- 单域问题
- 用户只是要一个直接答案

行为：

- 不触发任何编排

### parallel

适用：

- 子任务互相独立
- 多视角审查/研究/候选方案比较

行为：

- lead 一次性 fan-out 多个 subtask
- 全部完成后 fan-in 汇总

### pipeline

适用：

- B 依赖 A 的输出
- 明确的阶段链

行为：

- 严格按依赖顺序执行
- 每阶段只向下游传必要摘要，不传完整上下文

### rendezvous

适用：

- 需要一轮独立分析后，对关键分歧做二次判断
- 需要“受控讨论”，而不是自由互聊

行为：

- 第 1 轮各自独立输出
- lead 汇总关键分歧或候选结论
- 第 2 轮把 lead 摘要发给部分/全部 agent
- 最终综合

## Why No Agent-to-Agent Messaging

第一性上，系统真正需要的是“同步点”，不是“自由聊天”。

agent 直连会导致：

- token 消耗难控
- 信息流不可追踪
- 非确定性更强
- 测试困难

lead-controlled rendezvous 已足以覆盖多数协作价值。

## Internal Data Model

```python
@dataclass
class SubtaskSpec:
    id: str
    role: str
    task: str
    depends_on: list[str] = field(default_factory=list)
    expected_output: str = ""
    write_scope: list[str] = field(default_factory=list)


@dataclass
class SubtaskResult:
    id: str
    ok: bool
    content: str
    tool_calls_made: list[str]
    summary: str = ""
    error: str | None = None
```

说明：

- 第一版只保留最小字段
- 不持久化
- 只在一次 `send_message()` 生命周期内存在

## Runtime Execution API

内部 API，不公开暴露：

- `run_parallel_subtasks(...)`
- `run_pipeline_subtasks(...)`
- `run_rendezvous_round(...)`
- `summarize_subtask_result(...)`
- `synthesize_subtask_results(...)`

这些函数最终都基于 `spawn_agent` 语义创建子 agent，而不是引入新工具。

## Safety Rules

1. 子 agent 不能递归触发更高层编排。
2. 同一 `write_scope` 的 subtasks 不允许并行。
3. `pipeline` 只传必要摘要，不传完整对话上下文。
4. `rendezvous` 必须有固定轮次上限，默认最多 2 轮。
5. 任一子任务失败不能自动取消所有独立 sibling；由 lead 决定是否继续综合。

## Integration Plan

### Prompt / Skill

- 新增内置 orchestration skill
- 默认系统提示只保留 `spawn_agent` 的公开原语说明
- skill 在需要时引导模型选择 `direct / parallel / pipeline / rendezvous`

### Runtime

- 新增内部 orchestration module
- `BaseAgent` 保持 `spawn_agent` 为唯一公开委派工具
- lead 在内部调用 orchestration runtime，实际执行仍回落到 `spawn_agent`

### Tests

至少覆盖：

- `parallel` 不退化为串行
- `pipeline` 严格按依赖顺序执行
- `rendezvous` 第二轮只接收 lead 摘要
- 同一 `write_scope` 不可并行
- 子任务失败不会无条件取消其它独立任务
- 子 agent 不会再获得更高层编排能力

## Tradeoffs

优点：

- 公开概念更少
- `spawn_agent` 保持唯一原语
- 编排逻辑可测试、可演化
- 避免把临时 workflow 固化为产品能力

代价：

- lead agent 仍承担调度与综合责任
- 第一版不支持真正的 agent 间自由协作
- 复杂协作依赖 rendezvous，而非自然会话

## Recommendation

按 `skill + internal runtime + spawn_agent only` 的方式落地。
不要恢复 `team_run`，也不要引入新的公开团队工具。
