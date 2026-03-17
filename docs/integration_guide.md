# 外部项目集成指南

将现有 Python 项目按 open-agents 架构模式分解和集成的规范流程。

---

## 集成步骤

### 第一步：按维度分类

| 维度 | 判定标准 | 对应 open-agents 层 |
|------|---------|-------------------|
| 单次 LLM 调用 | 输入 → 模板 → LLM → 输出 | `configs/*.json` + `prompts/` |
| 确定性计算 | 无 LLM、无外部 I/O、幂等 | `tools/*.json` + `utils/*.py` |
| 外部数据获取 | HTTP/文件/队列等异步 I/O | `input/*.json` + `utils/input_utils.py` |
| 静态有序流程 | 步骤顺序固定、无条件分支 | `pipelines/*.json` |
| 并行 DAG 流程 | 步骤间有依赖但可并行 | `graphs/*.json` |
| 动态/递归逻辑 | 运行时决定结构（循环、递归、条件） | 封装为 `tool`（黑盒） |

### 第二步：处理动态逻辑

**原则：无法静态声明的逻辑，封装为 tool 黑盒。**

| 动态模式 | 推荐处理 |
|---------|---------|
| 递归树构建 | 整体封装为 tool |
| While 循环 | 整体封装为 tool |
| 条件分支 | router agent + 外层处理 |
| 动态步骤数 | 封装为 tool，内部遍历 |

### 第三步：创建集成层

```
project/
├── 原有代码/           # 保持不动
├── open-agents-integration/
│   ├── configs/        # Agent 配置
│   ├── tools/          # 工具配置
│   ├── input/          # 输入源配置
│   ├── graphs/         # 图配置
│   ├── skills/         # 能力声明
│   ├── prompts/        # 提示词模板
│   └── utils/          # Python 包装器（桥接原有代码）
```

### 第四步：桥接模式

`utils/` 中的包装器函数是桥梁——导入原有代码模块，适配为 `ToolResult` 返回格式。

```python
from utils import ToolResult

class MyToolResult(ToolResult):
    output: str
    success: bool
    error: str = ""

def my_tool(input_param: str) -> dict:
    try:
        from original_module import original_function
        result = original_function(input_param)
        return MyToolResult(output=str(result), success=True).model_dump()
    except Exception as e:
        return MyToolResult(output="", success=False, error=str(e)).model_dump()
```

---

## 关键约束

1. **路由 Agent 输出必须使用扁平字段**（非嵌套对象），因为 `_resolve_wire` 仅支持 `step_id.field` 一级访问
2. **long_running 必须传播**：子图含 long_running 节点时，父图也必须标记 `long_running: true`
3. **导入统一从 `utils` 包**，禁止 `from utils._xxx import`

---

## 框架增强需求记录

### 条件分支 (Conditional Dispatch)

图中步骤无法根据上游输出选择性执行。建议方案：

```json
{
  "id": "dispatch_cog",
  "type": "tool",
  "depends_on": ["route"],
  "condition": { "field": "route.subsystem", "equals": "cog" }
}
```

### 重入式图 (Re-entrant Graph)

通道只能读取一次数据，多轮交互需要外部循环推送。建议支持图内循环回到 `read_channel` 节点。
