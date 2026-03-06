# LLM_MODULE 工具管理使用说明

本目录提供一套“工具注册 + 工具集合(toolset) + 按 agent 控制可用工具”的轻量框架，用来把未来要集成的各种工具统一管理，并能方便地给不同 agent 分配不同工具。

核心文件：
- `LLM_MODULE/tools.py`：对外入口（工具清单、调用分发、插件加载、按 toolset/agent 过滤）
- `LLM_MODULE/tool_registry.py`：底层实现（ToolRegistry/ToolManager/AgentToolPolicy）
- `LLM_MODULE/tools_plugins/`：插件目录（新增工具推荐放这里）

---

## 1. 现有内置工具与默认 toolset

内置工具（已注册）：
- `code_executer`：在当前进程执行 Python 代码并返回 JSON（含 stdout/stderr）
- `read_text`：读取文本文件并返回 JSON
- `list_dir`：列出目录内容并返回 JSON

默认 toolset（在 `LLM_MODULE/tools.py` 里注册）：
- `default`：`code_executer` + `read_text` + `list_dir`
- `coder`：仅 `code_executer`
- `fs`：`read_text` + `list_dir`

你可以新增/覆盖 toolset，或把 agent 绑定到某个 toolset（见后面章节）。

---

## 2. 快速开始：获取工具定义 + 分发调用

> 注意：建议从项目根目录运行（保证根目录在 `sys.path`），例如：`Set-Location C:\Users\...\HMACF_PRO_MAX` 后再运行 python。

### 2.1 获取 OpenAI function-calling 需要的 tools 描述

```python
from LLM_MODULE.tools import tool_spec

tools = tool_spec()                 # 默认 toolset=default
coder_tools = tool_spec(toolset="coder")
fs_tools = tool_spec(toolset="fs")
```

### 2.2 直接调用工具（不走 LLM）

```python
from LLM_MODULE.tools import dispatch_tool

print(dispatch_tool("list_dir", {"path": "LLM_MODULE"}))
print(dispatch_tool("read_text", {"path": "LLM_MODULE/tools.py"}))
print(dispatch_tool("code_executer", {"code": "print(1+1)"}))
```

`dispatch_tool` 的返回值永远是字符串（通常是 JSON 字符串）。

---

## 3. 与 `OpenAILLM.chat_with_tools` 集成（推荐用法）

`LLM_MODULE/llm.py` 的 `OpenAILLM.chat_with_tools(...)` 需要两个东西：
- `tools`：OpenAI tools 定义列表
- `tool_handler(name, args)`：收到模型 tool_calls 后，实际执行工具的 handler

本框架提供 `make_tool_handler(...)` 直接生成符合签名的 handler，并且默认强制按 policy 校验“是否允许调用该工具”。

### 3.1 只给 coder agent 开放执行代码工具

```python
from LLM_MODULE.llm import OpenAILLM
from LLM_MODULE.tools import tool_spec, make_tool_handler

llm = OpenAILLM(model="gpt-4o-mini")

messages = [
    {"role": "system", "content": "你是一个会写代码的助手。"},
    {"role": "user", "content": "请调用工具执行：print('hi')"},
]

reply = llm.chat_with_tools(
    messages,
    tools=tool_spec(toolset="coder"),
    tool_handler=make_tool_handler(toolset="coder", enforce_policy=True),
)
print(reply)
```

### 3.2 通过 `agent` 自动决定 toolset（先绑定，再用 agent）

```python
from LLM_MODULE.tools import bind_agent_toolset, tool_spec, make_tool_handler

bind_agent_toolset("coder_agent", "coder")
bind_agent_toolset("fs_agent", "fs")

coder_tools = tool_spec(agent="coder_agent")  # 自动解析为 toolset=coder
coder_handler = make_tool_handler(agent="coder_agent")
```

---

## 4. 按 agent 分配不同工具（两种方式）

### 方式 A：直接传 `toolset=...`（最简单）

适合你在代码里明确知道“这次对话属于哪个工具集合”的场景。

```python
tools = tool_spec(toolset="fs")
handler = make_tool_handler(toolset="fs")
```

### 方式 B：绑定 `agent -> toolset`（更适合多 agent 系统）

适合你的上层框架里有多个 agent（如 scientist/coder/reviewer），希望统一用 agent 名来路由。

```python
from LLM_MODULE.tools import bind_agent_toolset, tool_spec, make_tool_handler

bind_agent_toolset("scientist", "fs")
bind_agent_toolset("coder", "coder")

tools = tool_spec(agent="coder")
handler = make_tool_handler(agent="coder")
```

---

## 5. 注册新工具（插件方式，推荐）

推荐把新工具做成插件模块，放到 `LLM_MODULE/tools_plugins/` 下，然后用 `load_plugins()` 自动发现并注册。

### 5.1 插件文件模板

在 `LLM_MODULE/tools_plugins/xxx.py` 里实现：
- `register(manager)`：由框架自动调用
- 在 `register()` 内用 `manager.register_tool(...)` 注册工具
- 可选：用 `manager.define_toolset(...)` 创建该插件自己的 toolset

参考示例：`LLM_MODULE/tools_plugins/math_tools.py`

```python
from __future__ import annotations

import json
from typing import Any, Dict

from LLM_MODULE.tool_registry import ToolDefinition, ToolManager

def register(manager: ToolManager) -> None:
    manager.define_toolset("math", ["add"])

    def _add(args: Dict[str, Any]) -> str:
        a = float(args.get("a"))
        b = float(args.get("b"))
        return json.dumps({"success": True, "result": a + b}, ensure_ascii=False)

    manager.register_tool(
        ToolDefinition(
            name="add",
            description="将两个数字相加，返回 result。",
            parameters={
                "type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                "required": ["a", "b"],
            },
            handler=_add,
        ),
        toolsets=["math"],
    )
```

### 5.2 加载插件

```python
from LLM_MODULE.tools import load_plugins

loaded = load_plugins(on_error="raise")  # 或 on_error="skip"
print(loaded)
```

加载后即可：
- `tool_spec(toolset="math")` 得到 `add` 的 tool 定义
- `dispatch_tool("add", {...})` 直接调用

---

## 6. 工具调用的“权限校验”（enforce_policy）

为了兼容旧代码，`dispatch_tool(...)` 默认 `enforce_policy=False`，也就是“只要工具已注册就能被调用”。

但在 LLM function-calling 场景里，通常希望严格限制工具集合，避免模型越权调用：
- `make_tool_handler(...)` 默认 `enforce_policy=True`
- 你也可以在 `dispatch_tool(..., enforce_policy=True)` 手动开启

示例（拒绝 coder 调用读文件）：

```python
from LLM_MODULE.tools import make_tool_handler

h = make_tool_handler(toolset="coder", enforce_policy=True)
print(h("read_text", {"path": "LLM_MODULE/tools.py"}))  # 会返回 tool not allowed
```

---

## 7. 调试：导出当前工具/工具集清单

```python
from LLM_MODULE.tools import tools_manifest_json, load_plugins

print(tools_manifest_json())
load_plugins(on_error="skip")
print(tools_manifest_json())
```

也可以直接运行示例：
- `python LLM_MODULE/tools_demo.py`

