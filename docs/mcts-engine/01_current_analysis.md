# MCTS 现状分析

> 分析日期：2026-03-17
> 目标：搞清楚当前 MCTS 实现存在什么问题，哪些问题是结构性的、哪些是表面的

---

## 一、当前 MCTS 是什么

Sience-OS 的 MCTS 是一个 **Beam Search 变体**，用 8 种结构算子驱动 LLM 对任意代码项目做迭代优化。

核心流程（`graphs/mcts_run.json`）：

```
baseline_score + baseline_log
        ↓
mcts_create_root_node         → 初始化根节点 + frontier
        ↓
gen_loop（最多 50 代）:
  ├─ mcts_build_tree_text     → 序列化树状态为 LLM 可读文本
  ├─ mcts_build_fanout        → 为每个 frontier 节点生成 beam_width 个待评估任务
  ├─ map: mcts_single_combo   → 并行评估每个 op（Critic → Engineer → AST → Sandbox）
  ├─ mcts_build_children      → 将结果注册为子节点
  ├─ mcts_gate [BLOCKING]     → Human Gate：等待人工决策
  └─ mcts_update_state        → 根据决策更新 frontier / best_node / consecutive_bad
        ↓
mcts_apply_best               → 将最优 patch 链写回项目（.bak 备份）
```

### 单次 Combo 流程（`graphs/mcts_single_combo.json`）

```
op + demand + tree_text + node_summary
        ↓
mcts_critic（LLM）            → 提出 {node_a, node_b, direction, reasoning, new_nodes}
        ↓
mcts_parse_atomic_ops         → 拆解为 engineer_tasks（rewrite/create）
        ↓
map: mcts_engineer_task       → 并行生成每个原子任务的代码
        ↓
mcts_collect_patches          → 过滤 AST 合法的 patch
        ↓
mcts_sandbox_via_session      → 通过共享沙箱评估，返回 score + metrics + output_log
```

### 8 种结构算子

| 类型 | 算子 | 语义 |
|------|------|------|
| 二元 | insert | 在 A→B 间插入新节点 C |
| 二元 | merge | 合并 A→B 为单节点 |
| 二元 | decouple | 断开 A→B 的直接依赖 |
| 二元 | split | 将 B 拆分为 B1、B2 |
| 二元 | extract | 从 A→B 提取公共组件 C |
| 多元 | parallelize | 串行中间节点改并发 |
| 多元 | pipeline | 链路改流式处理 |
| 多元 | stratify | 跨层依赖重整为有序层级 |

---

## 二、现存问题

### 2.1 状态全在内存——崩溃即归零（P0）

```python
# gen_loop 的 state 字段（mcts_run.json）
state = {
    "frontier":        root_node.frontier,   # list[dict] in memory
    "all_nodes":       root_node.all_nodes,  # dict[str, dict] in memory
    "best_node":       root_node.root_node,  # dict in memory
    "consecutive_bad": 0,
    "generation":      1,
    "global_tried":    [],                   # list in memory
    "history":         [],
    ...
}
```

没有任何 checkpoint 机制。进程崩溃、网络断开、机器重启——所有已探索的节点、所有 sandbox 的评估结果、所有 gen 的历史——全部丢失。必须从头重跑。

**真实影响**：一次 MCTS run 可能跑几小时。Gen 8 崩溃了，之前所有 sandbox 评估（可能几十次 LLM + 沙盒调用）全部作废。

---

### 2.2 Human Gate 是同步阻塞的——必须人在电脑前（P0）

```python
# utils/mcts/inputs.py
async def mcts_gate(...) -> MctsGateResult:
    # 打印树状态到 terminal
    print(tree_text)
    print("等待决策... send_to_channel({action, next_mode, selected_ids})")

    # ← 这里阻塞，直到有人手动 send_to_channel
    data = await receive_from_channel(gate_channel_id)
    return MctsGateResult(...)
```

每代结束后，整个搜索进程挂起，等待人工介入。如果你离开了，进程就一直等。

**真实影响**：
- 无法在夜间跑长时间搜索
- 不能在手机上审查结果并决策
- 与进程的通信方式是内部 channel，外部无法触达

---

### 2.3 没有跨 run 记忆——每次重复同样的失败（P1）

```python
# mcts_build_fanout：只用了当前 run 的 global_tried
global_tried_set: set[str] = set(global_tried)   # 仅当前 run
```

`history_prompt` 只记录当前 run 最近 10 代的记录。没有"上次在类似代码上 merge 失败了 7 次"这样的跨 run 经验。

**Critic 的 system prompt 里也没有历史经验**：
```
# prompts/mcts_critic/system.txt
# 只有当前 run 的 tree_text 和 history_prompt
```

每次 MCTS 都在重新探索同样的算子，对同类项目反复踩同样的坑。

---

### 2.4 patch 链不可追溯（P1）

每个节点的 `ancestor_patches` 是一个内存中的 dict list：

```python
child = {
    "node_id":      node_id,
    "patches":      all_patches,   # [{"action": "rewrite", "target_file": ..., "code": ...}, ...]
    "score":        score,
    ...
}
```

没有办法回答：
- "Gen 3 的 insert 操作具体改了哪些代码？"
- "为什么 Gen 5 的 score 下降了？改了什么？"
- "当前最优节点和 baseline 相比有哪些变化？"

唯一能用的是 `mcts_build_tree_text`，但它只输出 score 和 op，不含代码 diff。

---

### 2.5 mcts_apply_best 用 .bak 文件做备份——脆弱（P2）

```python
# utils/mcts/tools.py
def mcts_apply_best(best_node, project_root):
    for patch in patches:
        path = os.path.join(project_root, rel)
        if os.path.exists(path):
            shutil.copy(path, path + ".bak")   # ← 每次覆盖同名 .bak
        ok, msg = _replace_code_block(path, ...)
```

如果有多个 patch 作用于同一文件，只有最后一个会保留 .bak，之前的备份被覆盖。

而且 apply 是单向的——一旦 apply 就没有 undo（除非手动从 .bak 恢复）。

---

### 2.6 评估结果不可复现（P2）

每次 sandbox 跑的是临时目录：
```python
with tempfile.TemporaryDirectory(prefix=f"mcts_{trial_id}_") as tmp:
    sandbox_root = _copy_project(project_root, tmp, _DEFAULT_IGNORE)
    ...
# ← 临时目录在这里被删除
```

一旦 with 块结束，这次评估的完整环境就消失了。如果想复现"Gen 3/insert 得到 0.847"这个结果，无法做到。

---

## 三、问题根因总结

所有问题的根源是同一个：**MCTS 把"搜索过的所有信息"都放在进程的内存里**，没有外化到任何可持久化的存储。

```
当前架构：
  进程内存
  ├── all_nodes (dict)     ← 所有探索过的节点
  ├── frontier (list)      ← 当前边界
  ├── best_node (dict)     ← 最优节点
  ├── global_tried (list)  ← 已试过的 op
  └── ancestor_patches     ← 每个节点的 patch 链

进程死 → 一切归零
```

正确的架构应该是：**搜索过的节点 = git commit（天然持久化 + 可追溯 + 可复现）**，状态机 = 独立的 MCP Server（与进程生命周期解耦），Human Gate = 异步消息（与进程通信解耦）。
