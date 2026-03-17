# MCTS Engine 设计文档：Git-Native 异步搜索引擎

> 本文档描述如何将 Sience-OS 的 MCTS Beam Search 改造为：
> - 基于 git 的持久化搜索树（崩溃可恢复）
> - 异步 Human Gate（手机端决策，无需守在电脑前）
> - 跨 run 记忆系统（不重复探索同样失败的方向）
> - OpenClaw 插件（标准 MCP + Skills 格式，可脱离 Sience-OS 独立运行）
>
> 面向后续开发者，详细说明改造动机、架构设计、实现路径。

---

## 目录

1. [现状与改造动机](#1-现状与改造动机)
2. [核心设计理念](#2-核心设计理念)
3. [系统架构总览](#3-系统架构总览)
4. [mcts-engine MCP Server](#4-mcts-engine-mcp-server)
5. [异步 Human Gate 协议](#5-异步-human-gate-协议)
6. [分层记忆体系](#6-分层记忆体系)
7. [Agent 定义](#7-agent-定义)
8. [分支命名与 git 约定](#8-分支命名与-git-约定)
9. [OpenClaw 接入](#9-openclaw-接入)
10. [与现有代码的映射关系](#10-与现有代码的映射关系)
11. [实现路径](#11-实现路径)

---

## 1. 现状与改造动机

### 1.1 当前实现

Sience-OS 的 MCTS (`graphs/mcts_run.json`) 是一个 Beam Search 变体，流程：

```
baseline → 初始化根节点 → gen_loop（最多50代）:
  ├── 为每个 frontier 节点构建 beam_width 个任务
  ├── 并行评估：Critic → Engineer → AST → Sandbox
  ├── Human Gate（同步阻塞）
  └── 更新 frontier / best_node
→ mcts_apply_best（写回项目）
```

### 1.2 现存问题

| 问题 | 严重性 | 原因 |
|------|--------|------|
| 崩溃即归零 | P0 | 所有状态在进程内存 |
| Human Gate 同步阻塞 | P0 | `receive_from_channel` 无超时 |
| 没有跨 run 记忆 | P1 | `global_tried` 仅当前 run |
| patch 链不可追溯 | P1 | 内存中的 dict list，无 diff |
| apply_best 用 .bak 脆弱 | P2 | 同名 .bak 被覆盖 |
| 评估结果不可复现 | P2 | tempfile 用后即删 |

### 1.3 改造目标

将 MCTS 改造为：

- **输入**：任意项目路径 + benchmark 命令（任意 `__METRICS__` 输出的脚本）
- **过程**：git-native 节点持久化 + 异步 Human Gate + 跨 run 记忆
- **输出**：最优代码分支（可直接 checkout 或发 PR），完整搜索谱系可追溯

---

## 2. 核心设计理念

### 2.1 搜索节点即 git 分支

| MCTS 概念 | git 映射 | 优势 |
|----------|---------|------|
| search node | git branch + commit | 天然持久化 |
| root node | `seed-baseline` tag | 不可变基准 |
| ancestor_patches | git log（commit 链）| 自动 diff，无需维护 |
| score | commit message | 随 commit 持久化 |
| tried_combos | branch 命名（可枚举）| 无需单独存储 |
| rollback | git checkout 父 branch | 天然支持 |
| apply best | git checkout 目标 branch | 幂等、可 undo |
| tree structure | git branch --graph | 完整谱系图 |
| 并行评估 | git worktree | 真正隔离 |

核心洞见：**`ancestor_patches` 这个最重的数据结构可以完全消失**——它就是从 `seed-baseline` 到当前 branch HEAD 的 git diff，通过 `git diff seed-baseline..HEAD` 随时还原。

### 2.2 mcts-engine 只做确定性记账

`mcts-engine` MCP Server 不生成代码、不做 LLM 判断，只负责：
- 跟踪哪些节点已评估、哪些 op 已尝试
- 维护 frontier 和 best_node
- 管理 git worktree 生命周期
- 持久化状态到磁盘（进程无关）

所有 LLM 工作由 Sience-OS 现有的 Critic / Engineer 完成。

### 2.3 Human Gate 异步化

Human Gate 不应阻塞进程。改造后：

```
每代结束 → 序列化树状态 → 推送到 messaging channel
         → 启动 cron 超时任务 → 等待响应或超时
         → 收到响应（或超时自动 continue）→ 推进状态机
```

进程不再 hang，用户可以在手机上任意时间决策。

---

## 3. 系统架构总览

```
用户指令（如 /search project_root benchmark_cmd）
        ↓
┌─────────────────────────────────────────────────────────┐
│                    mcts-engine 插件层                     │
│  SOUL.md / AGENTS.md / Skills                           │
└──────┬───────────────┬────────────────┬─────────────────┘
       │               │                │
       ▼               ▼                ▼
  mcts-engine       Sience-OS       OpenClaw
  MCP Server        现有组件         平台能力
  ├ mcts_step       ├ Critic         ├ exec（git/sandbox）
  ├ mcts_init       ├ Engineer       ├ messaging channels
  ├ mcts_register   ├ AST check      ├ cron（超时推进）
  ├ mcts_fanout     └ Sandbox        ├ canvas（可视化）
  ├ mcts_gate_*                      └ memory（跨run记忆）
  └ mcts_cache
```

### 组件职责分工

| 组件 | 职责 | 变化 |
|------|------|------|
| **mcts-engine MCP Server** | 状态机 + 持久化 + git 管理 | **新建** |
| **OrchestratorAgent** | 驱动主循环 | **重构** |
| **ComboAgent**（原 mcts_single_combo）| Critic → Engineer → 评估 | 调整接口 |
| **GateAgent** | 异步 Human Gate | **新建** |
| **ReflectAgent** | 写记忆、提取 op 教训 | **新建** |
| mcts_critic（原 configs/mcts_critic.json）| 提出优化提案 | 增加记忆注入 |
| mcts_engineer（原 configs/mcts_engineer.json）| 生成代码变体 | 不变 |
| sandbox 系统 | 代码评估 | 改为 git worktree 模式 |

---

## 4. mcts-engine MCP Server

### 4.1 设计原则

- 无状态工具接口（每次调用完整传参）+ 服务端持久化状态
- 状态文件：`~/.openclaw/mcts-state/{project_hash}.json`
- 所有 git 操作通过 exec 在服务外执行，mcts-engine 只管记账
- 进程无关：重启后可完整恢复

### 4.2 核心工具

#### `mcts_init`

```python
mcts_init(
    project_root: str,          # 项目绝对路径
    demand:       str,          # 优化目标描述
    benchmark_cmd: str,         # 评估命令（输出 __METRICS__ 或末行 float）
    baseline_score: float,      # 基线分数（外部已测好）
    baseline_log:   str,        # 基线输出日志
    beam_width:  int = 3,
    max_generations: int = 50,
    objective: str = "max",     # "max" | "min"
) -> InitResult
# 返回：run_id, state_path
# 副作用：创建 memory/ 目录，记录初始状态到磁盘
```

#### `mcts_step` — 核心状态机

状态机驱动搜索推进，每次调用推进一个阶段：

```
mcts_step("begin_generation")
  → {action: "dispatch_combos", generation, tasks: [{op, parent_branch, ...}, ...]}

mcts_step("combo_ready", branch=..., parent_commit=...)
  → {action: "check_op_cache", op, path_hash, cached_score?}
  # 如果命中 cache：直接返回 skip，不跑 LLM + sandbox

mcts_step("score_ready", branch=..., score=..., success=...)
  → {action: "combo_done", is_new_best, total_evals}

mcts_step("select")
  → {action: "gate", top_nodes, tree_text, best_node, generation}

mcts_step("gate_done", action=..., selected_ids=[...])
  → {action: "reflect"}
  | {action: "begin_generation"}   ← continue
  | {action: "done"}               ← stop

mcts_step("reflect_done")
  → {action: "begin_generation"} | {action: "done"}
```

#### `mcts_register_node`

```python
mcts_register_node(
    branch:    str,       # git branch name
    parent_branch: str,
    op:        str,
    score:     float,
    success:   bool,
    gen:       int,
    metrics:   dict,
) -> NodeResult
# 注册节点到 all_nodes，更新 best_node
```

#### `mcts_build_fanout`

```python
mcts_build_fanout(
    generation:  int,
    beam_width:  int,
) -> FanoutResult
# 返回每个 frontier 节点 × 未试过的 op 的任务列表
# 已试 op 来自：当前 run 的 git branch 枚举 + 历史 cache
```

#### `mcts_get_lineage`

```python
mcts_get_lineage(branch: str) -> LineageResult
# 返回从 seed-baseline 到 branch 的完整节点路径
# 内部：git log seed-baseline..{branch} --oneline
```

#### `mcts_check_op_cache`

```python
mcts_check_op_cache(
    op:        str,
    path_hash: str,   # hash of ancestor_patches content
) -> CacheResult
# 检查同一 op + 同一代码路径是否已评估过（跨 run）
# path_hash = sha256(sorted ancestor patch codes)
```

#### `mcts_gate_notify` + `mcts_gate_wait`

```python
mcts_gate_notify(
    channel_id: str,         # messaging channel（WhatsApp/Telegram）
    generation: int,
    tree_text:  str,
    top_nodes:  list,
    timeout_minutes: int = 30,
) -> GateNotifyResult
# 发送树状态快照到 messaging channel
# 创建 cron 任务：timeout 后自动注入 {action: "continue"}
# 返回 resume_token

mcts_gate_wait(
    resume_token: str,
) -> GateWaitResult
# 阻塞等待 messaging channel 响应，或超时后 auto-continue
# 返回 {action, selected_ids}
```

### 4.3 状态文件结构

```json
{
  "run_id": "a3f9b2",
  "project_root": "/path/to/project",
  "demand": "optimize feature extraction speed",
  "benchmark_cmd": "python main.py",
  "objective": "max",
  "baseline_score": 0.817,
  "beam_width": 3,
  "max_generations": 50,
  "current_generation": 5,
  "phase": "gate",
  "frontier": ["mcts/a3f9b2/gen-5/merge-0c4f"],
  "best_branch": "mcts/a3f9b2/gen-5/merge-0c4f",
  "best_score": 0.847,
  "all_nodes": {
    "root": {"branch": "seed-baseline", "score": 0.817, "gen": 0},
    "mcts/a3f9b2/gen-1/insert-8a2d": {"score": 0.831, "gen": 1, "op": "insert", "parent": "root"}
  },
  "global_tried_ops": ["insert", "parallelize"],
  "consecutive_bad": 0,
  "history": [
    {"gen": 1, "best_score": 0.831, "op": "insert", "action": "continue"}
  ]
}
```

---

## 5. 异步 Human Gate 协议

### 5.1 当前协议（同步阻塞）

```
每代结束
  → print(tree_text) to terminal
  → await receive_from_channel(gate_channel_id)  ← 进程挂起，等待
  → 收到 {action, selected_ids}
```

限制：进程 hang、无法离线、外部无法触达。

### 5.2 新协议（异步 + 超时）

```
每代结束
  → mcts_step("select") → {action: "gate", top_nodes, tree_text}
  → mcts_gate_notify(channel_id, tree_text, top_nodes, timeout=30min)
      ├── 向 messaging channel 推送格式化快照
      └── 创建 cron 任务（30分钟后自动 continue）
  → mcts_gate_wait(resume_token)
      ├── 等待 messaging channel 响应（用户回复 "continue" / "stop" / "select <id>"）
      └── 超时：cron 注入 {action: "continue"}，自动推进
  → mcts_step("gate_done", action=..., selected_ids=[...])
```

### 5.3 消息格式

发送给用户的消息（WhatsApp/Telegram）：

```
[ScienceOS MCTS] Gen 5 完成 ✓

最优: score=0.847 (+0.030 vs baseline)
历代趋势: 0.817 → 0.831 → 0.833 → 0.841 → 0.847

搜索树:
  [root] score=0.817 baseline
  ├─ [gen1/insert] 0.831
  │   └─ [gen3/parallelize] 0.839
  │       └─ [gen5/merge] 0.847 ← best
  └─ [gen2/decouple] 0.820

本代新节点:
  #1 gen5/merge     0.847  +0.008 ← 推荐
  #2 gen5/stratify  0.831  +0.000
  #3 gen5/pipeline  0.812  -0.019

操作（30分钟内无响应将自动 continue）:
  continue          — 继续，以本代最优为 frontier
  select gen5/merge — 指定节点
  rollback          — 回退到父节点层
  stop              — 停止，应用当前最优
```

### 5.4 用户响应处理

```python
# 接受的响应格式（大小写不敏感）
"continue"           → {action: "continue", selected_ids: []}
"stop"               → {action: "stop", selected_ids: []}
"rollback"           → {action: "rollback", selected_ids: []}
"select gen5/merge"  → {action: "select", selected_ids: ["mcts/a3f9/gen-5/merge-0c4f"]}
"select #1"          → 解析为 top_nodes[0] 的 branch id
```

---

## 6. 分层记忆体系

### 6.1 目录结构

```
{project_root}/memory/
├── global/
│   ├── op_lessons.md        ← 跨项目 op 经验汇总
│   └── domain_patterns.md   ← 代码领域模式（"数据管道类适合 insert"）
├── projects/{project_hash}/
│   ├── op_history.md        ← 该项目各 op 成功率表
│   ├── long_term.md         ← 该项目累积经验
│   └── runs/
│       ├── run_{N}/
│       │   ├── tree_final.md   ← 最终搜索树快照
│       │   ├── best_diff.md    ← 最优节点 vs baseline 的 diff 摘要
│       │   └── lessons.md      ← 本次 run 反思
│       └── ...
└── op_stats/
    ├── insert.md            ← insert 全局成功/失败记录
    ├── merge.md
    └── ...（其余 6 个算子）
```

### 6.2 ReflectAgent 写记忆的时机

每代 gate_done 之后、下代开始之前：

1. `git diff {parent_branch}..{best_new_branch}` → 提取本代改动摘要
2. 写 `memory/projects/{hash}/runs/run_N/gen_{G}.md`（本代反思）
3. 如果本代 best_score 高于上代：记录该 op + 改动 → 成功案例
4. 如果连续 N 代未改进：记录最近失败的 op 组合 → 失败案例
5. 每 3 代：汇总到 `long_term.md`，更新 `op_stats/{op}.md`

### 6.3 Critic 读取记忆

Critic 的 user prompt 增加 `memory_context` 变量：

```
历史经验（来自记忆系统）:
{{memory_context}}
```

`memory_context` 的内容（由 OrchestratorAgent 在每代开始前读取并注入）：

```
[当前 op: insert]
- 本项目历史：成功 2/3 次
  - 成功：在 data_loader→feature_extractor 之间插入缓存层，score +0.08
  - 失败：在 model→loss 之间插入，接口不兼容
- 全局历史：成功率 62%（12/19）
- 注意：避免在已有 3+ 层嵌套的调用链上使用 insert（历史失败率 83%）
```

---

## 7. Agent 定义

### OrchestratorAgent

驱动主循环。每代调用 `mcts_step` 推进状态机，分发 ComboAgent，协调 GateAgent 和 ReflectAgent。

**工具**：`mcts_step`, `mcts_build_fanout`, exec（git tag/branch）, canvas（更新 dashboard）

### ComboAgent（对应原 mcts_single_combo）

每个 op 对应一个 ComboAgent 实例，并行执行。

**流程**：
1. `git checkout -b {branch} from {parent_branch}`
2. 调用 Critic → 获取 {node_a, node_b, direction}
3. 调用 Engineer → 生成代码
4. AST check
5. `mcts_step("combo_ready", branch, parent_commit)` → 检查 op cache
   - 如果 cache hit：`mcts_step("score_ready", branch, cached_score)` 直接返回
   - 如果 cache miss：继续评估
6. `git worktree add` → 跑 sandbox → `git worktree remove`
7. `mcts_step("score_ready", branch, score)`

### GateAgent

管理异步 Human Gate。

**流程**：
1. `mcts_gate_notify(channel_id, tree_text, top_nodes)` → 推送消息 + 创建 cron
2. `mcts_gate_wait(resume_token)` → 等待响应
3. `mcts_step("gate_done", action, selected_ids)`

### ReflectAgent

每代 gate 结束后运行，提取经验写入记忆。

**流程**：
1. `git diff {prev_best}..{new_best} --stat` → 本代变化摘要
2. 写 `memory/projects/{hash}/runs/{run_id}/gen_{G}.md`
3. 更新 `op_stats/{op}.md`（成功 or 失败记录）
4. 每 3 代：汇总 long_term.md，更新全局 op_lessons.md
5. `mcts_step("reflect_done")`

---

## 8. 分支命名与 git 约定

### 分支命名

```
mcts/{run_id}/gen-{N}/{op}-{uuid8}
mcts/{run_id}/synergy/{opA}+{opB}-{uuid8}   ← 未来：多算子组合探索

Tags:
  seed-baseline                  ← 初始基准（不可变）
  best-mcts-{run_id}             ← 本次 run 最优
  best-overall                   ← 跨 run 全局最优
```

`run_id` = 6位十六进制（`uuid.hex[:6]`），全局唯一，避免多 run 分支冲突。

### Commit Message 格式

```
mcts(score=0.8423,op=insert,gen=3,run=a3f9b2): insert cache layer between data_loader→feature_extractor

Direction: 在 data_loader 和 feature_extractor 之间插入一个缓存节点，
避免重复特征计算，预期减少 IO 次数。
Reasoning: data_loader 每次调用都重新读取磁盘，feature_extractor 依赖其输出，
缓存层可消除重复 IO。
```

机器可解析：`mcts\(score=([0-9.]+),op=(\w+),gen=(\d+),run=(\w+)\)`

### 评估协议

1. **Op cache check**（`mcts_check_op_cache`）——跳过重复评估
2. **Static check**（AST parse）——语法错误直接丢弃，不进 sandbox
3. **Full eval**（git worktree + sandbox）——通过前两步才跑

崩溃处理：
- 如果 sandbox 超时：`mcts_step("score_ready", success=False)` → 记为失败，不影响 frontier
- 如果 Engineer 生成失败（AST error）：不创建 branch，直接跳过

---

## 9. OpenClaw 接入

### Skills

| Skill | 功能 |
|-------|------|
| `/search {project_path} {benchmark_cmd}` | 完整搜索入口（类比 Evo-anything 的 `/evolve`）|
| `/mcts-status` | 查看当前搜索进度、树状态、best score |
| `/mcts-stop` | 停止搜索，应用当前最优节点 |
| `/mcts-rollback` | 回退到上一代 frontier |
| `/mcts-report` | 生成搜索报告（树谱系 + op 分析 + 改动 diff）|

### Workflows（lobster）

`mcts-setup.lobster`（确定性 setup）：
```
check_clean → run_baseline → tag_seed-baseline → create_memory_dirs → init_canvas
```

`mcts-finish.lobster`（收尾 + Human approval gate）：
```
tag_best-overall → show_diff_summary → [approval gate] → gh pr create
```

### Plugin Manifest

```json
{
  "id": "openclaw-mcts",
  "name": "MCTS Search Engine",
  "description": "Git-native MCTS beam search for structural code optimization on any git repository.",
  "kind": "agent-tool",
  "skills": ["./skills"],
  "agents": ["./agents"],
  "workflows": ["./workflows"],
  "tools": {
    "alsoAllow": ["lobster"]
  }
}
```

---

## 10. 与现有代码的映射关系

| 现有代码 | 改造后对应 | 变化 |
|---------|-----------|------|
| `graphs/mcts_run.json` | OrchestratorAgent（AGENTS.md）| 驱动逻辑移到 Agent |
| `graphs/mcts_single_combo.json` | ComboAgent | 增加 git branch 管理 |
| `utils/mcts/tools.py:mcts_update_state` | `mcts_step("gate_done")` | 移入 MCP Server |
| `utils/mcts/tools.py:mcts_build_children` | `mcts_step("score_ready")` + `mcts_register_node` | 移入 MCP Server |
| `utils/mcts/tools.py:mcts_apply_best` | `git checkout best-mcts-{run_id}` | 被 git 替代 |
| `utils/mcts/tools.py:mcts_build_fanout` | `mcts_build_fanout` MCP 工具 | 移入 MCP Server |
| `utils/mcts/inputs.py:mcts_gate` | GateAgent + `mcts_gate_notify/wait` | 同步 → 异步 |
| `utils/mcts/inputs.py:mcts_sandbox_via_session` | ComboAgent 内 git worktree 评估 | 保留 sandbox 逻辑 |
| `configs/mcts_critic.json` | 不变，增加 `memory_context` 变量 | 注入历史经验 |
| `configs/mcts_engineer.json` | 不变 | 无变化 |

**保留不动的**：
- `mcts_ast_check`（AST 语法检查）
- `mcts_parse_atomic_ops`（Critic 提案拆解）
- `mcts_collect_patches`（patch 收集）
- `mcts_critic` / `mcts_engineer` prompt 系统（只增加 memory_context）
- sandbox 评估逻辑（改为 git worktree 模式但核心不变）

---

## 11. 实现路径

按照 Evo-anything 的实际提交顺序，推荐以下路径：

### Phase 1：建好骨骼（mcts-engine MCP Server）

```
plugin/
├── mcts-engine/
│   ├── server.py        ← MCP Server（mcts_step 状态机核心）
│   ├── models.py        ← 数据模型（SearchState, Node, etc.）
│   ├── selection.py     ← frontier 选择算法
│   └── pyproject.toml
```

实现优先级：
1. `mcts_init` + `mcts_step` 状态机（最核心）
2. `mcts_register_node` + `mcts_build_fanout`
3. `mcts_check_op_cache`
4. `mcts_gate_notify` + `mcts_gate_wait`

### Phase 2：接入 Sience-OS（替换现有图和工具）

修改 `graphs/mcts_run.json`：
- 移除 `gen_loop` 的 in-memory state
- 改为调用 `mcts_step` 状态机
- Human Gate 替换为 GateAgent

修改 `utils/mcts/inputs.py`：
- `mcts_gate` 改为异步推送模式

### Phase 3：记忆系统

- 新建 `plugin/agents/reflect_agent.md`
- 修改 `prompts/mcts_critic/user.txt` 增加 `{{memory_context}}`
- ReflectAgent 每代写 memory/ 目录

### Phase 4：OpenClaw 插件化

- 建 `plugin/openclaw.plugin.json`
- 建 `plugin/skills/search/SKILL.md`
- 建 `plugin/workflows/mcts-setup.lobster` + `mcts-finish.lobster`
- 建 `plugin/AGENTS.md`（完整 Agent 协议文档）

### Phase 5：回溯文档

- 生态能力全景图（类比 Evo-anything 的 `research/04_ecosystem_capability_map.md`）
- 完整使用文档和 README
