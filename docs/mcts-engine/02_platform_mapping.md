# 平台能力映射

> 分析日期：2026-03-17
> 核心问题：OpenClaw 生态已经提供了什么？哪些可以直接承接 MCTS 的问题？

---

## 一、MCTS 问题 → 平台能力对应关系

| MCTS 的问题 | 平台已有的解 | 需要新建的 |
|------------|------------|-----------|
| 状态在内存，崩溃即丢 | `exec`（git 操作天然持久化）| git-native 节点约定 |
| patch 链不可追溯 | git log / git diff / git show | 分支命名规范 |
| Human Gate 同步阻塞 | messaging channels（WhatsApp/Telegram）| 异步 gate 协议 |
| Gate 无法超时自动推进 | `cron`（定时调度）| 超时 auto-continue 逻辑 |
| 没有跨 run 记忆 | `memory`（MEMORY.md + 向量搜索）| 结构化 op 记忆系统 |
| 评估结果不可复现 | git branch（完整代码快照）| 无（git 已经解决） |
| mcts_apply_best 脆弱 | git checkout（幂等、可 undo）| 无（git 已经解决） |
| 树状态只在 terminal 可见 | `canvas`（实时 HTML 渲染）| 搜索树可视化模板 |

---

## 二、最关键的同构：search node ↔ git branch

和 Evo-anything 把 U2E 的 individual 映射到 git branch 完全对称：

| MCTS 概念 | git 映射 | 优势 |
|----------|---------|------|
| search node (dict) | git branch + commit | 天然持久化、可复现 |
| root node | `seed-baseline` tag | 不可变基准点 |
| ancestor_patches | git log（commit 链 diff）| 自动 diff 记录，无需手工维护 |
| score | git commit message / notes | 随 commit 持久化 |
| tried_combos | branch 命名（可枚举）| `git branch --list 'mcts/*/gen-N/*'` |
| rollback | git checkout 父 branch | 天然支持，无副作用 |
| apply best | git checkout + 文件已是最优状态 | 幂等、可 undo |
| tree structure | git branch --graph --all | 完整谱系图 |
| 评估隔离 | git worktree | 并行评估互不干扰 |

**分支命名规范**：
```
mcts/{run_id}/gen-{N}/{op}-{uuid8}

Tags:
  seed-baseline          ← 初始基准（不可变）
  best-mcts-{run_id}     ← 本次 run 最优节点
  best-overall           ← 跨 run 全局最优
```

**score 存储方式**：
```bash
# commit message 格式
mcts(score=0.8423,op=insert,gen=3): restructure data_loader→feature_extractor

# 或用 git notes（不污染 commit message）
git notes add -m "score=0.8423 metrics={...}" <commit>
```

---

## 三、平台提供的各层能力详细映射

### exec — MCTS 最重度依赖

| MCTS 需要做的事 | 依赖 exec 做什么 |
|---------------|----------------|
| 评估代码变体 | 在 git worktree 中跑 main.py |
| 创建节点分支 | `git checkout -b mcts/{run_id}/gen-N/{op}-{uuid8}` |
| 应用 patch | `git add + git commit` |
| 回滚到父节点 | `git checkout {parent_branch}` |
| 枚举已试过的 op | `git branch --list 'mcts/{run_id}/*'` |
| apply best | `git checkout best-mcts-{run_id}` |
| 查看 diff | `git diff seed-baseline..best-mcts-{run_id}` |
| 隔离评估 | `git worktree add/remove` |

### messaging channels — Human Gate 的异步化

**现在**：Human Gate 是内部 channel 上的阻塞等待。外部无法触达，进程挂起。

**改造后**：每代结束后，mcts-engine 向 messaging channel 推送快照：

```
[WhatsApp/Telegram]
Gen 5 完成 ✓  score: 0.847 (+0.03 vs baseline)

搜索树:
  [root] score=0.817 baseline
  ├─ [gen1/insert] 0.831
  │   └─ [gen3/parallelize] 0.839
  │       └─ [gen5/merge] 0.847 ← current best
  └─ [gen2/decouple] 0.820

本代新节点:
  #1 [gen5/merge]     score=0.847  op=merge   ← 推荐
  #2 [gen5/stratify]  score=0.831  op=stratify
  #3 [gen5/pipeline]  score=0.812  op=pipeline

操作: continue | select gen5/merge | rollback | stop
30分钟内无响应将自动 continue
```

用户在手机上回复 `continue` 或 `select gen5/merge`，cron 处理超时自动推进。

### cron — 超时自动推进

```
每代 Gate 触发后：
  创建 cron 任务：30 分钟后检查 gate_channel 是否已有响应
  如无响应：自动注入 {action: "continue"}，解除阻塞
```

这让 MCTS 可以完全无人值守地跑，只在有改进时发通知。

### memory — 跨 run 的 op 经验

```
memory/
├── global/
│   └── op_lessons.md        ← "insert 在数据管道类项目成功率 67%"
├── projects/{project_hash}/
│   ├── op_history.md        ← 该项目各 op 成功/失败统计
│   ├── long_term.md         ← 累积经验（"该项目 feature_extractor 是瓶颈"）
│   └── runs/
│       └── run_{N}/
│           ├── tree_final.md  ← 最终搜索树快照
│           └── lessons.md     ← 本次反思
└── op_stats/
    ├── insert.md            ← insert 全局记录
    ├── merge.md
    └── ...
```

Critic 在生成提案时，从 memory 读取该 op 的历史经验：
- "上次在同类项目 merge 失败了 4 次，原因：合并后接口破坏"
- "insert 在 data pipeline 类代码成功率最高"

### canvas — 搜索树实时可视化

每代结束后更新 `~/clawd/canvas/mcts-dashboard.html`：
- 树形结构展示（节点 = score，边 = op 类型）
- fitness 折线图（每代 best score）
- per-op 成功率饼图
- Human Gate 状态指示（waiting / auto-continuing in N min）

---

## 四、平台目前**没有**、MCTS 改造需要新建的

| 缺失 | 说明 | 解法 |
|------|------|------|
| 持久化搜索状态 | 进程间共享 + 崩溃恢复 | **mcts-engine MCP Server** |
| 异步 Human Gate 协议 | 发送 + 等待 + 超时 + 自动推进 | mcts_gate_notify + mcts_gate_wait |
| op cache（跨 run 去重）| 同一代码路径 + 同一 op 不重复评估 | 基于 tree hash + op 的 cache |
| git worktree 生命周期管理 | 并行评估时自动 add/remove | mcts-engine 内部管理 |

---

## 五、升华路径总结

```
OpenClaw 已有能力                    MCTS 改造升华为
─────────────────────────────────────────────────────────
exec（git 操作）          →   git branch 即搜索节点
                               + worktree 隔离并行评估
                               + patch 链 = git log

messaging channels        →   异步 Human Gate
                               + 手机端实时决策
                               + 超时 auto-continue

cron（定时调度）          →   Gate 超时自动推进
                               + 无人值守夜间搜索

memory（跨会话持久化）    →   结构化 op 经验记忆
                               + 跨 run 避免重复失败

canvas（可视化）          →   实时搜索树渲染
                               + per-op 成功率展示

sessions_spawn（多 Agent）→   并行 combo 评估
                               （当前已有 map 步骤）
```
