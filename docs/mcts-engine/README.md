# MCTS Engine 改造文档索引

> 目标：将 Sience-OS 的 MCTS Beam Search 改造为 git-native 异步搜索引擎，作为 OpenClaw 插件接入

---

## 文档列表

| 文件 | 内容 |
|------|------|
| [01_current_analysis.md](./01_current_analysis.md) | 当前 MCTS 实现的深度分析：6 个结构性问题及根因 |
| [02_platform_mapping.md](./02_platform_mapping.md) | 平台能力映射：OpenClaw 已有什么、MCTS 如何对接 |
| [DESIGN.md](./DESIGN.md) | 主设计文档：完整改造方案、架构、实现路径 |

## 核心结论

**三件事**：

1. **git-native 节点**：search node → git branch，`ancestor_patches` 消失，变成 `git log`
2. **mcts-engine MCP Server**：`mcts_step` 状态机，持久化到磁盘，进程无关
3. **Async Human Gate**：同步阻塞 → 消息推送 + cron 超时 auto-continue

**与 Evo-anything 改造 U2E 的对比**：

| 维度 | Evo-anything vs U2E | mcts-engine vs MCTS |
|------|--------------------|---------------------|
| 核心映射 | individual → git branch | search node → git branch |
| 状态机 | evo-engine MCP Server | mcts-engine MCP Server |
| 记忆系统 | per-target + global | per-op + per-project |
| **关键差异** | 无 | **Async Human Gate** |

Async Human Gate 是本次改造独有的升华——让 MCTS 从"必须人守着跑"变成"手机上睡前看一眼决定"。
