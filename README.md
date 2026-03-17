# ScienceOS

> **AI 阅读提示**：本项目文件较多，请**分片按需读取**，禁止一次性读取全部文件。
> 阅读顺序：本文件 → `docs/ai_reading_guide.md` → 按需读取其他文件。

**端到端的 AI 科研自动化系统**

从自然语言需求出发，自动完成：架构设计 → MCTS 代码优化 → U2E 进化搜索 → 论文撰写（含文献检索与 LaTeX 排版），全流程通过 Web UI 操控与监控。

---

## 快速启动

### 安装

```bash
pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
```

### 配置

```bash
cp .secrets.toml.example .secrets.toml
# 填入 API Key
```

或使用环境变量：
```bash
export OPENAGENTS_OPENAI_API_KEY="sk-..."
```

切换环境：
```bash
export OPENAGENTS_ENV=production   # development（默认）/ production / vllm
```

配置读取优先级（从低到高）：`settings.toml` → `.secrets.toml` → 环境变量

### 运行

```bash
python serve.py
# 自动打开 http://localhost:8765
```

---

## 项目结构

```
serve.py                   Web 服务入口（FastAPI + WebSocket）
config.py                  Dynaconf 全局配置入口
settings.toml              项目级配置（模型默认值、preset 定义、运行参数）
.secrets.toml              敏感信息（API Key），gitignore
frontend/                  React 前端

graphs/*.json              DAG 计算图（声明依赖，自动并行）
configs/*.json             Agent 配置（LLM 调用描述，支持 preset 继承）
tools/*.json               工具配置（同步，确定性函数）
input/*.json               输入源配置（异步 I/O）
prompts/**/*.txt           提示词模板
skills/*.json              能力声明（I/O 契约）⚠️ 仅在改写 skills 时读取

utils/                     执行层（外部统一通过 utils/__init__.py 导入）
├── _base.py               ToolResult 基类
├── _graph.py              计算图执行器
├── _pipeline.py           流水线执行器
├── _channel.py            运行时数据通道（gate 机制）
├── _registry.py           工具/输入源注册表
├── _validator.py          配置校验
├── _trace.py              追踪/序列化（safe_serialize）
├── arch/                  架构设计子系统
├── mcts/                  MCTS Beam Search 子系统
├── u2e_tools.py           U2E 进化算法工具
├── papw_tools.py          论文写作工具
├── papw_input.py          论文写作异步输入源
└── sandbox.py             沙盒执行环境

llm_config.py              LLM 调用核心库（配置加载、preset 解析、提示词插值、API 调用）
```

顶层入口图为 `graphs/arch_then_mcts_then_u2e_ppw.json`，内部嵌套关系看该文件即可。

---

## 开发规范

### 导入规则

- 外部代码统一从 `utils` 包导入，禁止直接引用 `_` 前缀的内部模块
  ```python
  # ✅
  from utils import ToolResult, call_tool, run_graph_from_file
  # ❌
  from utils._base import ToolResult
  ```

### 配置管理

- **唯一入口**：`from config import settings`
- 所有默认值定义在 `settings.toml`，禁止在代码中硬编码
- 敏感信息写 `.secrets.toml`，禁止提交到 git

### LLM Config Preset

`configs/*.json` 支持 preset 继承，在 `settings.toml` 的 `[default.presets.*]` 中定义。

```json
{
  "preset": "default",
  "agent_name": "my_agent",
  "system_prompt_path": "./prompts/my_agent/system.txt",
  "params": { "temperature": 0.5 }
}
```

- `preset` 字段指定预设名（`default` / `fast` / `strong` / `creative`）
- 配置中显式写的字段覆盖预设同名字段
- `params` 深合并（配置覆盖预设中的同名 key，保留其余 key）
- 不写 `preset` 则完全自定义
- 预设的具体参数见 `settings.toml`

### long_running 传播规则

若图/流水线中任一步骤为长期运行节点，顶层配置必须标记 `long_running: true`。

```python
from utils import validate_long_running_propagation, validate_all_configs
errors = validate_long_running_propagation(config_dict)
violations = validate_all_configs()
```

### 自定义类型

- 自定义数据类型（dataclass / Pydantic model）可以使用
- **禁止在自定义类型中定义成员函数**，逻辑写为独立函数

---

## Web API

### 启动运行

```
POST /api/runs
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `requirement` | string | 必填 | 自然语言需求描述 |
| `output_dir` | string | 必填 | 输出目录 |
| `auto_approve_arch` | bool | false | 自动跳过架构审核 gate |
| `auto_approve_mcts` | bool | false | 自动跳过 MCTS 决策 gate |
| `beam_width` | int | 3 | MCTS 束宽 |
| `max_generations` | int | 50 | MCTS 最大代数 |
| `pop_size` | int | 10 | U2E 种群大小 |
| `max_fe` | int | 5000 | U2E 最大函数评估次数 |
| `tex_template_path` | string | "" | LaTeX 模板路径 |
| `existing_project_root` | string | "" | 已有项目路径（跳过架构设计） |

默认值来自 `settings.toml`，通过 `_apply_defaults()` 填充。

### 实时事件流

```
WebSocket /ws/runs/{run_id}/events
```

事件类型：`step_start` / `step_done` / `step_error` / `loop_jump` / `gate_waiting` / `run_done` / `run_error`

### Gate 决策提交

```
POST /api/runs/{run_id}/gate/{gate_id}
```

- arch_gate: `{"feedback": "..."}`（空 = 批准）
- mcts_gate: `{"action": "continue|select|rollback|stop", ...}`

### 断点续跑

```
GET  /api/checkpoints?output_dir=...     → 可用断点列表
POST /api/runs  + resume_checkpoint_dir  → 从断点恢复
```
