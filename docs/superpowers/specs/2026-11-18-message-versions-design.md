# 被编辑掉的那一轮，不该消失——消息版本设计

**Goal:** 把「编辑上一轮 / 重新生成」从**破坏性截断**改成**可切换的版本**，让被替换的回答
还能拿回来。

---

## 1. 问题：现在是数据丢失

`rewind_last_turn` 做三件事，第三件是删除：

```python
removed_turns, removed_events = store.truncate_transcript(session_id=clean, from_seq=seq)
```

`truncate_transcript` 是 `DELETE FROM conversation_turns WHERE session_id=? AND seq>=?`，
`agent_events` 同样。于是：

> 你发现上一句提问打错了一个字，点「编辑」改好重发——**原来那轮完整的回答永久消失**。

这不是缺一个功能。误点一次、改错一次，就是一次不可恢复的损失，而界面上没有任何提示
说它会这样。

它自己的 docstring 已经知道正确的答案：

> Only the **last** turn can be cut. A middle turn would leave every later turn turning
> a context that no longer exists, and repairing that is a **branch**, not a truncation.

也就是说：**代码里写着"这是分支问题"，但实现成了截断。**

## 2. 关键约束：29 个读取点

`conversation_turns` 有 29 处读取（journal、sqlite_fts、queries、migrations、
retrieval、consolidation、paging）。如果引入真正的树（`parent_id` + `selected_child_id`，
像 DeepSeek chat-demo 那样），这 29 处**每一处**都要加"只读选中路径"的条件——包括
分页游标、记忆整理、检索。那是一次高风险的大改，而它解决的问题比实际需要的大。

所以设计的目标是：**修掉数据丢失，同时不改任何一个读取点。**

## 3. 设计：活动表仍然只有一条路径，版本放在旁边

```
conversation_turns             ← 不变。永远只装「当前选中的那条路径」
conversation_turns_archive     ← 新增。被替换掉的版本，按 turn_index + branch_id 分组
agent_events_archive           ← 新增。同上，工具轨迹也要能跟着回来
```

**为什么这样就够了**：`rewind` 只能切**最后一轮**（既有语义，不改）。所以"有多个版本的
位置"永远是最新那一轮。这不需要树——需要的是**同一位置的多个版本**。

### 数据形状

```sql
CREATE TABLE conversation_turns_archive (
    -- conversation_turns 的全部列
    session_id, role, content, channel, message_id, reply_to_id,
    metadata_json, created_at, seq,
    -- 加上：
    turn_index   INTEGER NOT NULL,   -- 这是第几轮（按 user 行数，1 起）
    branch_id    TEXT    NOT NULL,   -- 一次被替换的版本，user+assistant+插话 同一 id
    archived_at  TEXT    NOT NULL,
    archived_by  TEXT    NOT NULL    -- 'rewind' | 'switch'
);
```

`turn_index` 在归档时算一次并写死，因为活动表的行数会变，事后算不出来。

### 三个操作

**A. 回退（现在的 rewind）**——删除改成搬运：

```
1. 找到最新一轮 user 行的 seq（不变）
2. 算它的 turn_index = 活动表里 user 行数（不变）
3. 把 seq >= 该 seq 的行从 conversation_turns 搬到 archive，
   统一打上一个新 branch_id，turn_index 记下来
4. agent_events 同样搬
5. provider checkpoint / live ctx.messages 照旧切断（这两处是内存与重放，本来就要断）
```

读取点一个都不动：搬完之后活动表的内容和今天删除后的内容**完全一样**。

**B. 列出某个位置的版本**（新端点）：

```
GET /api/sessions/{id}/turns/{turn_index}/versions
→ [{branch_id, request_text, created_at, selected}, ...]
```

`selected` 的那条就是活动表里现在这一轮（没有 branch_id，或者用固定标记）。

**C. 切到另一个版本**（新端点）：

```
POST /api/sessions/{id}/turns/{turn_index}/switch  {branch_id}
1. 把活动表里 seq >= 该位置的行搬进 archive，新 branch_id（这样还能切回来）
2. 把目标 branch_id 的行从 archive 搬回 conversation_turns（原 seq 原样）
3. 删掉 archive 里这些行（它们现在是活动的了）
```

对称，且**永不删除**：两个方向都是搬运。

### 为什么不是"恢复"而是"切换"

只做「恢复原回答」是不够的：恢复之后你就回不到新版本了。**切换**是对称操作，两个方向都
保留，用户不会因为点了一下而丢掉另一个。

## 4. 明确不做的

1. **不做中间轮的分支。** rewind 仍然只能切最后一轮。中间轮的分支需要真正的树 + 29 个
   读取点改造，而当前产品里没有入口能产生它。
2. **不动记忆与 staging。** rewind 的既有决定是"不 unlearn"（见它的 docstring）：记忆
   是从对话里提炼的事实，改一个句子不是在说"忘掉这件事"。版本切换同理——切回来不会
   让记忆重新整理一遍。这个后果写在这里，而不是藏着。
3. **不做对话级导出。** 那是另一件事（chat-demo 有，我们还没有），不混进来。
4. **不显示"树"。** UI 只在最新一轮显示 `‹ 2 / 3 ›`，因为它只有一个位置可能有版本。

## 5. 验收

- [ ] 编辑上一轮后，原回答可在版本切换里找回来（**这是现在的数据丢失**）
- [ ] 切回去之后，新版本仍然可切回（对称，不丢任何一边）
- [ ] 活动表的内容与今天 rewind 后**逐行一致**（读取点无需改动即可证明）
- [ ] 工具轨迹跟着版本走：切回旧版本，它的 tool 事件也在
- [ ] 只有最新一轮有版本；更早的轮次没有 switcher
- [ ] 空会话 / 运行中的会话：切换被拒绝（和 rewind 同样的忙判断）
- [ ] 归档表有 30 天清理（和 patrol_ticks 同一个先例，避免只增不减）

## 6. 风险与代价

| 风险 | 处理 |
|---|---|
| 归档表只增不减 | 按时间保留（30 天），跟着 rewind 顺手清 |
| seq 不重置（既有决定）导致切回来的行 seq 有空洞 | 保持现状：seq 是身份，不是位置。分页游标本来就接受空洞（`truncate_transcript` 的 docstring 已说明） |
| 切换时 seq 冲突（新旧版本都有 seq=12） | 活动表允许——归档行的 seq 只用于恢复时排序，不与活动行共存 |
| 并发：切版本与正在运行的回合 | 沿用 rewind 的 `SessionBusyError` |
