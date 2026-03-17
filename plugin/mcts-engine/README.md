# mcts-engine

COG MCTS 搜索引擎 — 基于 git 分支的代码优化 MCP server。

## 安装

### 前置要求

- Python ≥ 3.11
- git ≥ 2.20（需要 worktree 支持）
- pip / pipx

### 安装方式

**方式一：直接从源码安装（推荐开发时使用）**

```bash
pip install -e /path/to/COG/plugin/mcts-engine
```

**方式二：构建 wheel 后安装**

```bash
cd /path/to/COG/plugin/mcts-engine
pip install build
python -m build
pip install dist/mcts_engine-*.whl
```

安装完成后，`mcts-engine` 命令会出现在 PATH 中：

```bash
which mcts-engine   # /usr/local/bin/mcts-engine
```

### 依赖说明

| 包 | 版本要求 | 说明 |
|---|---|---|
| `mcp` | ≥ 1.0 | MCP protocol SDK |
| `pydantic` | ≥ 2.0 | 数据验证 |

## MCP 注册

在 Claude Code（或其他 MCP host）的配置文件中添加：

```json
{
  "mcpServers": {
    "mcts-engine": {
      "command": "mcts-engine",
      "args": [],
      "env": {
        "COG_STATE_DIR": "/path/to/state"   // 可选，默认 ~/.cog/state
      }
    }
  }
}
```

server 通过 stdio transport 运行，无需额外端口。

## 快速验证

```bash
# 检查 server 可以正常导入
python3 -c "from server import mcts_init, mcts_step; print('OK')"

# 检查命令行入口
mcts-engine --help 2>/dev/null || echo "server starts OK (no --help flag needed)"
```

## 工具列表

| 工具 | 说明 |
|---|---|
| `mcts_init` | 初始化一次搜索 run，设置 baseline/beam_width/max_evals 等 |
| `mcts_step` | 驱动状态机：`begin_generation` → `fitness_ready` → `select` → `gate` / `reflect_done` |
| `mcts_check_cache` | 按 `(op, code_hash)` 查 score 缓存，避免重复评估 |
| `mcts_get_status` | 查询当前 run 的进度和最优结果 |
| `mcts_freeze_branch` | 强制冻结某个分支（不再扩展） |
| `mcts_boost_branch` | 手动提升某个分支的 UCB 优先级 |

## 典型调用顺序

```
mcts_init(...)
  └─ begin_generation  →  返回本代 batch（branch 列表 + op 列表）
       ├─ [ComboAgent] apply op → run benchmark
       └─ fitness_ready(branch, fitness, success)   ← 每个 branch 一次
  └─ select            →  返回 keep / eliminate / best
  └─ gate / reflect_done
  └─ begin_generation  （下一代）
  ...
```

## 状态持久化

`COG_STATE_DIR`（默认 `~/.cog/state`）下保存 `state.json`，进程崩溃后重启可从断点继续。

## 已知问题 / 注意事项

- `pyproject.toml` 需要 `[tool.hatch.build.targets.wheel] include = [...]`，
  否则 hatchling 因找不到 `mcts_engine/` 目录而报错。（已在本 repo 中修复）
- server 以 stdio 运行，不支持直接 `mcts-engine --help`；
  MCP host 会负责启动和通信。
