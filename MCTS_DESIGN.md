# COG 系统全局设计文档

## 零、系统总览

COG 是一个**自动化代码优化流水线**，接收一个用户项目（含 `main.py`）和需求描述，输出性能更优的代码版本。

```
用户项目（main.py + 数据集）
         │
         ▼
┌─────────────────────────────┐
│  0. Dataset Report          │  分析数据集特征，生成报告
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│  1. Tree CoT                │  架构设计 → 生成初始项目代码
│     arch_build graph        │  （模块划分 → 文件划分 → 函数定义 → 代码生成）
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│  2. Auto Optimizer（可选）  │  Optuna 超参数搜索（Fast/Balanced/Aggressive）
│     auto_opt.py             │  LLM 将 main.py 改写为 optuna 脚本并运行
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│  3. MCR（标准优化循环）      │  Critic + Engineer 循环（无树结构）
│     engine.py               │  counts=100 代，单前沿，串行每代
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│  4. MCTS（树搜索优化）       │  Beam Search + Human Gate（本文档重点）
│     mcts_engine.py          │
└─────────────────────────────┘
```

> MCR 和 MCTS 是同一优化目标的两种策略，可单独使用也可串联。

---

## 一、MCR 标准优化循环（engine.py）

MCR 是 MCTS 的"前身"，也是默认优化策略。理解 MCR 有助于理解 MCTS 在哪些地方做了改进。

### 每代流程

```
Critic LLM（带 tool use：read_file / 搜索）
  输入：项目树, 历史记录, 上次运行日志, 需求
  温度：基础 0.2，每代停滞 +0.2，最高 1.2（随停滞程度升温）
  输出：proposals 列表（每个 proposal 含 1 个 target + K 个 direction）
         ↓
对每个 proposal 的每个 direction：
  Engineer × N 并发变体（MonteCarloEngineerAgent）
  → Optimizer.run()：沙盒跑所有变体，挑最优
         ↓
比较各 direction 的最优分数，选出本代最优方向
  有改善 → apply_change() 写回磁盘，更新 baseline
  无改善 → stagnation_streak += 1
         ↓
停滞 5 代 → history.perform_reset()（清除近期轨迹，重置视角）
```

### MCR vs MCTS 核心差异

| | MCR | MCTS |
|---|---|---|
| 搜索结构 | 线性（每代一个状态）| 树（多代 Beam 并行） |
| 展开策略 | Critic 自由选 target | (op×angle) 15 种固定组合 |
| 多样性来源 | temperature 升温 | op/angle 组合保证维度覆盖 |
| 人工介入 | 无 | Human Gate（每代） |
| 回滚能力 | 无（直接写磁盘）| 有（patch 树，可回溯）|
| 停滞处理 | history reset | rollback + architecture 升级 |

---

## 二、MCTS 树搜索引擎（mcts_engine.py）

用蒙特卡洛树搜索在代码版本空间中做 **Beam Search**。每个节点代表"从原始项目出发，依次应用若干 patch 之后"的一个代码版本。搜索目标是让 `main.py` 的 `__METRICS__` 输出最大化。每个节点代表"从原始项目出发，依次应用若干 patch 之后"的一个代码版本。搜索目标是让 `main.py` 的 `__METRICS__` 输出最大化。

```
root（原始代码，baseline score）
 ├─ node_A  [compute × aggressive]         score=0.82
 │   ├─ node_A1  [memory × conservative]   score=0.85  ← best
 │   └─ node_A2  [io × bottleneck]         score=0.79
 └─ node_B  [algorithm × aggressive]       score=0.80
```

每一代：frontier 中的每个节点 → **展开**（生成候选子节点） → **并发沙盒评估** → Human Gate 决定下一代 frontier。

---

## 三、MCTS 搜索空间

```
OPERATORS (i=5):  compute | memory | io | algorithm | data_structure
ANGLES    (j=3):  aggressive | conservative | bottleneck_focused

ALL_COMBOS = i × j = 15 种 (op, angle) 组合
```

- `op` 指定"从哪个维度优化"
- `angle` 指定"优化力度/视角"
- 每个节点记录"祖先链已用过的组合"，展开时只选剩余组合

---

## 四、节点结构（SearchNode）

```python
node.node_id          # 短 UUID，用于 Human Gate 选择
node.generation       # 第几代
node.parent           # 父节点引用
node.op / node.angle  # 到达此节点时使用的组合

node.patches          # 【核心】从 root 到此节点的累积 patch 列表
                      # patches = parent.patches + [本次新 patch]
                      # patch = {target_file, target_type, target_name, code}

node.score            # 沙盒执行后写入（生成时为 -inf）
node.metrics          # __METRICS__ 原始 dict
node.output_log       # main.py stdout
```

**子节点先建立（无分数），沙盒评估后再写入 score。**

---

## 五、两种展开模式（VariantGenerator）

### 4a. LLM 模式（Gen 1 / Gen 2）

每个 (op, angle) 组合并发执行以下流程：

```
Critic LLM
  输入：op, angle, demand, 项目树, 历史记录, 上次运行日志
  输出：{target_file, target_type, target_name, direction, reasoning}
    → "改哪里、往哪个方向改"
         ↓
Engineer LLM × N 并发（MonteCarloEngineerAgent）
  temperature 从 0.4 线性升至 1.2，生成 N 份不同变体
  从磁盘提取 original_code（extract_code_with_structures）
         ↓
AST 静态检查（ast.parse）
  失败 → 直接丢弃，不惩罚，不计入 consecutive_bad
  通过 → 构建 SearchNode（patches = parent.patches + [新 patch]）
```

### 4b. Enumerate 模式（Gen 3，或用户选择）

**跳过 Critic**，复用父节点最后一个 patch 的 target，直接用模板 prompt 驱动 Engineer：

```
无 Critic 调用（零额外推理成本）
         ↓
Engineer LLM × N 并发
  direction 由模板生成：
    "Apply {angle} {op} optimization: rewrite {target_name} using
     {op}-focused techniques with a {angle} approach."
  temperature 从 0.2 到 0.6（确定性更强）
  original_code 从父节点最后一个 patch 的 code 字段取
         ↓
AST 静态检查 → 构建 SearchNode
```

> Enumerate 模式的意义：穷举剩余搜索空间，用低温度确定性补全未探索的 (op, angle) 组合。

---

## 六、两段式执行（每代）

```
阶段 1 — 展开（generate_llm / generate_enumerate）
  对 frontier 中每个节点的每个剩余 (op, angle)：
    并发调用 VariantGenerator
    → 得到一批 SearchNode（patches 已建，score = -inf）

阶段 2 — 沙盒评估（_evaluate_parallel）
  对阶段 1 的所有有效候选节点，并发运行沙盒：
    1. 把整个项目复制到临时目录
    2. 按顺序重放 node.patches（全部祖先改动 + 本次改动）
    3. subprocess 运行 main.py
    4. 解析最后一行 __METRICS__:<json>
    5. 写入 node.score / node.metrics / node.output_log
```

---

## 七、每代搜索节奏

| 代 | 模式 | 说明 |
|---|---|---|
| Gen 1 | LLM | 从 root 展开，Critic × 15 combos |
| Gen 2 | LLM | 每个 frontier 节点独立展开（基于自身 patches 树）|
| Gen 3 | Enumerate | 强制遍历所有剩余组合，不调 Critic |
| Gen 4+ | 用户选 | Human Gate 处用户指定 `llm` 或 `enumerate` |

---

## 八、Human Gate（每代结束后阻塞）

引擎在每代结束后打印摘要，然后阻塞等待决策：

```python
# 引擎内部（asyncio.Queue 模式）
decision = await self.gate.wait_for_decision(channel_id)
```

外部注入决策：
```python
await engine.gate.send_decision(engine.pending_channel_id, {
    "action":       "continue",        # continue | select | rollback | architecture | stop
    "next_mode":    "llm",             # llm | enumerate（下一代用哪种模式）
    "selected_ids": ["a1b2", "c3d4"],  # 手动指定 frontier 节点（None = 自动 top-K）
})
```

CLI 模式下自动监听 stdin：
```
continue llm
select a1b2,c3d4 enumerate
rollback
architecture
stop
```

---

## 九、每代结束后的决策逻辑

```
全部候选静态失败？
  YES → 提示升级 Architecture Redesign，等待 [architecture] 或 [stop]

有改善分支（score > parent.score）？
  YES → 取 top-BEAM_WIDTH（默认3）进入下一代 frontier
        consecutive_bad = 0
        全局最优更新时写回磁盘（apply patches to real project）
  NO  → consecutive_bad += 1
        达到 CONSECUTIVE_BAD_LIMIT（默认3） → 自动 rollback
          rollback = frontier 退回各节点的 parent 层
          若 parent 为空 → 停止
```

---

## 十、关键参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `BEAM_WIDTH` | 3 | 每代保留的 frontier 节点数 |
| `CONSECUTIVE_BAD_LIMIT` | 3 | 连续无改善多少代后自动 rollback |
| `LLM_VARIANTS_PER_COMBO` | 3 | LLM 模式每个 (op,angle) 并发变体数 |
| `ENUM_VARIANTS_PER_COMBO` | 2 | Enumerate 模式每个 (op,angle) 变体数 |
| `timeout` | 300s | 沙盒 main.py 执行超时 |
| `MCR_ENGINEER_TEMP_MIN` | 0.4 | LLM 模式 Engineer 最低 temperature |
| `MCR_ENGINEER_TEMP_MAX` | 1.2 | LLM 模式 Engineer 最高 temperature |

---

## 十一、MCTS 数据流总结

```
项目代码（磁盘）
    │
    ├─ generate_project_tree_text ──→ tree_text
    │
    ├─【阶段 1：展开】────────────────────────────────────────────
    │
    │  LLM 模式                        Enumerate 模式
    │  ┌─────────────────┐             ┌──────────────────────┐
    │  │ Critic × combos │             │ 跳过 Critic          │
    │  │ → direction     │             │ 复用 parent.patches   │
    │  │ → target        │             │ 的最后一个 target     │
    │  └────────┬────────┘             └──────────┬───────────┘
    │           │                                  │
    │  Engineer × N 变体（并发，线性 temperature）  │
    │           │                                  │
    │  AST 静态检查（失败丢弃）                     │
    │           │                                  │
    │  SearchNode（patches 累积，score=-inf）       │
    │
    ├─【阶段 2：沙盒评估（并发）】────────────────────────────────
    │
    │  临时目录 = 项目副本
    │  重放 node.patches（全部祖先 + 本次）
    │  subprocess main.py → __METRICS__:<json>
    │  写入 node.score
    │
    └─【Human Gate】──────────────────────────────────────────────
       打印摘要 → 阻塞 → 用户决策 → 更新 frontier
       全局最优 → apply patches → 写回磁盘
```
