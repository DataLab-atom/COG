# ScienceOS 架构规范

## 步骤类型

| 类型 | 说明 | 示例 |
|------|------|------|
| `agent` | LLM 调用 | `configs/mcts_critic.json` |
| `tool` | 同步确定性函数 | `arch_scaffold`、`mcts_build_children` |
| `input` | 异步 I/O | `sandbox_run`、`read_channel`、`papw_build_references` |
| `graph` | 嵌套子图 | `graphs/mcts_run.json` |
| `map` | 并行映射 | `fn_codegen_fanout`（并行代码生成） |
| `loop` | 有状态循环 | `mcts_optimize`（多代搜索）、`u2e_optimize`（进化迭代） |
| `cond` | 条件分支 | `arch_step`（新建 vs 已有项目） |

## inputs 连线 vs {{}} 插值

| 特性 | inputs 连线 | {{}} 插值 |
|------|------------|---------|
| 类型保留 | int/dict/list 原样传递 | 全部转字符串 |
| 嵌套访问 | `step.field.sub` | 仅单级 `{{step.field}}` |
| 适用场景 | 类型敏感参数 | 字符串拼接 |

**规则**：类型敏感参数必须使用 inputs 连线，`{{}}` 仅用于字符串拼接场景。

## 提示词模板语法

模板使用 `{{var}}` 双花括号（**不是单花括号**），底层转换为 Python `string.Template`。

## Web API 路由

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/runs` | 启动运行 |
| GET | `/api/checkpoints` | 查询断点 |
| POST | `/api/runs/{run_id}/gate/{gate_id}` | Gate 决策提交 |
| POST | `/api/runs/{run_id}/abort` | 中止运行 |
| GET | `/api/defaults` | 获取前端表单默认值 |
| POST | `/api/preflight` | 预检（API Key、PDF、嵌入模型等） |
| GET | `/api/run-configs` | 列出运行配置 |
| GET | `/api/files` | 文件浏览 |
| WS | `/ws/runs/{run_id}/events` | 实时事件流 |
