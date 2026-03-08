# MCTS 代码优化引擎 — 设计文档

## 一、整体思路

用蒙特卡洛树搜索（MCTS）在代码版本空间中做 **Beam Search**。每个节点代表"从原始项目出发，依次应用若干 patch 之后"的一个代码版本。搜索目标是让 `main.py` 的 `__METRICS__` 输出最大化。

```
root（原始代码，baseline score）
 ├─ node_A  [compute × aggressive]         score=0.82
 │   ├─ node_A1  [memory × conservative]   score=0.85  ← best
 │   └─ node_A2  [io × bottleneck]         score=0.79
 └─ node_B  [algorithm × aggressive]       score=0.80
```

每一代：frontier 中的每个节点 → 并发展开 → 评估 → Human Gate 决定下一代 frontier。

---

## 二、搜索空间

```
OPERATORS (i=5):  compute | memory | io | algorithm | data_structure
ANGLES    (j=3):  aggressive | conservative | bottleneck_focused

ALL_COMBOS = i × j = 15 种 (op, angle) 组合
```

- `op` 指定"从哪个维度优化"（算法复杂度、内存、IO、算法替换、数据结构）
- `angle` 指定"优化力度/视角"（大幅重构、保守安全、专打瓶颈）
- 每个节点记录"祖先链已用过的组合"，展开时只选剩余组合

---

## 三、节点结构（SearchNode）

```python
node.node_id          # 短 UUID，用于 Human Gate 选择
node.generation       # 第几代
node.parent           # 父节点
node.op / node.angle  # 到达此节点时用的组合
node.patches          # 【关键】从 root 到此节点的累积 patch 列表
                      # patches = parent.patches + [本次 patch]
                      # 每个 patch = {target_file, target_type, target_name, code}
node.score            # 沙盒执行后的 __METRICS__ 分数
node.metrics          # 原始 metrics dict
node.output_log       # main.py stdout
```

`patches` 是累积的——沙盒评估时按顺序全部重放，才能还原这个节点对应的代码状态。

---

## 四、单次展开流程（每个 (op, angle) 组合）

```
Critic Agent
  输入：op, angle, demand, 项目树, 历史记录, 上次运行日志
  输出：{target_file, target_type, target_name, direction, reasoning}
    → 告诉我"改哪里、往哪个方向改"
         ↓
Engineer Agent
  输入：Critic 的输出 + 原始代码（从文件读取）
  输出：{code}  — 替换后的完整代码
  注：并发生成 N 份变体（temperature 从 0.4 线性升至 1.2），取最优
         ↓
AST 静态检查
  输入：engineer.code
  输出：{ok, patch, error}
  失败 → 直接丢弃，不惩罚，不计入 consecutive_bad
         ↓
沙盒评估（异步，long_running）
  1. 把整个项目复制到临时目录
  2. 按顺序重放 ancestor_patches（父节点的全部历史改动）
  3. 注入 new_patch（本次改动）
  4. subprocess 运行 main.py
  5. 解析最后一行 __METRICS__:<json>
  输出：{ok, score, metrics, output_log, all_patches}
```

graph 文件：`graphs/mcts_single_combo.json`
```
critic → engineer → ast_check → sandbox
```

---

## 五、每代搜索节奏

```
Gen 1, 2  → LLM 模式
  对 frontier 中每个节点，各取若干 (op, angle) 组合
  每个组合并发运行 mcts_single_combo graph × VARIANTS_PER_COMBO 次
  （VARIANTS_PER_COMBO=3，靠不同 temperature 生成多样性）

Gen 3     → Enumerate 模式
  遍历所有剩余未尝试的 (op, angle) 组合
  （穷举，确保搜索空间覆盖）

Gen 4+    → 由用户在 Human Gate 处选择 llm / enumerate
```

---

## 六、Human Gate（每代结束后阻塞）

引擎每代结束后打印摘要，然后通过 **open-agents channel** 阻塞：

```python
# 引擎内部
create_channel(channel_id)
decision = await receive_from_channel(channel_id)  # 阻塞在此
close_channel(channel_id)
```

外部注入决策：
```python
await send_to_channel(engine.pending_channel_id, {
    "action":       "continue",        # continue | select | rollback | architecture | stop
    "next_mode":    "llm",             # llm | enumerate
    "selected_ids": ["a1b2", "c3d4"],  # 指定保留哪些候选节点（None = 自动选 top-K）
})
```

CLI 模式下引擎自动监听 stdin，格式：
```
continue llm
select a1b2,c3d4 enumerate
rollback
architecture
stop
```

---

## 七、每代结束后的决策逻辑

```
有改善分支？
  YES → 取 top-BEAM_WIDTH（默认3）进入下一代 frontier
        consecutive_bad = 0
        写回最优 patch 到真实项目文件
  NO  → consecutive_bad += 1
        达到 CONSECUTIVE_BAD_LIMIT（默认3）→ 自动 rollback（frontier 退回父节点）

rollback 动作：frontier = [各节点的 parent]
全静态检查失败：提示升级到 Architecture Redesign 层
```

---

## 八、文件结构（open-agents 标准）

```
configs/
  mcts_critic.json          ← Critic agent 配置（model, prompt路径, schema）
  mcts_engineer.json        ← Engineer agent 配置

prompts/
  mcts_critic/system.txt    ← Critic 系统提示词（含 {{op}}, {{angle}}）
  mcts_critic/user.txt      ← Critic 用户提示词（含项目上下文）
  mcts_engineer/system.txt
  mcts_engineer/user.txt

tools/
  mcts_ast_check.json       ← AST 检查工具配置
utils/
  mcts_tools.py             ← 工具实现（同步函数，返回 plain dict）

input/
  mcts_sandbox.json         ← 沙盒评估输入配置（long_running: true）
utils/
  mcts_inputs.py            ← 输入实现（async 函数，返回 plain dict）

graphs/
  mcts_single_combo.json    ← 单组合 DAG: critic→engineer→ast_check→sandbox

skills/
  mcts_critic/engineer/ast_check/sandbox/single_combo.json

mcretro_mas/
  mcts_node.py              ← SearchNode 数据结构
  mcts_engine.py            ← MCTSEngine 主循环 + Human Gate
```

---

## 九、关键参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `BEAM_WIDTH` | 3 | 每代保留的 frontier 节点数 |
| `CONSECUTIVE_BAD_LIMIT` | 3 | 连续无改善多少代后自动 rollback |
| `VARIANTS_PER_COMBO` | 3 | 每个 (op,angle) 并发跑几份变体 |
| `timeout` | 300s | 沙盒 main.py 执行超时 |
| `MCR_ENGINEER_TEMP_MIN` | 0.4 | Engineer 最低 temperature |
| `MCR_ENGINEER_TEMP_MAX` | 1.2 | Engineer 最高 temperature |

---

## 十、数据流总结

```
项目代码（磁盘）
    ↓ generate_project_tree_text
tree_text
    ↓
[Critic] × (op, angle) 并发
    ↓ proposal
[Engineer] × N 变体并发
    ↓ code
[AST Check] 过滤
    ↓ patch
[Sandbox] 隔离执行
    ↓ score
[MCTSEngine] 排序 → Human Gate → 更新 frontier
    ↓ 写回最优 patch
项目代码（磁盘，已更新）
```
