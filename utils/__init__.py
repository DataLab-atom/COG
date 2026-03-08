"""
utils 包 — open-agents 编排工具库

所有入口函数均为 async，需在 asyncio 事件循环中调用。

━━ 流水线（线性多步） ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    result = await run_pipeline_from_file("pipelines/my.json", variables={...})
    result.steps["extracted"]["name"]   # 某步骤某字段
    result.final                        # 最后一步输出

    # 实时监控最后一步输出（yield (step_id, token)）
    async for step_id, token in run_pipeline_stream_from_file("pipelines/my.json", variables={...}):
        print(f"[{step_id}] {token}", end="", flush=True)

━━ 计算图（DAG，自动并行） ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    result = await run_graph_from_file("graphs/my.json", variables={...})
    result.steps["extract"]["name"]   # 某步骤某字段
    result.terminals                  # 所有叶节点输出
    result.final                      # 唯一叶节点输出（或全部叶节点 dict）

    # 实时监控所有并行终端节点（aiostream.merge 交错输出，yield (step_id, token)）
    async for step_id, token in run_graph_stream(config, variables={...}):
        print(f"[{step_id}] {token}", end="", flush=True)

    # 从文件路径流式执行
    async for step_id, token in run_graph_stream_from_file("graphs/my.json", variables={...}):
        print(f"[{step_id}] {token}", end="", flush=True)

    # 结构化生命周期事件流（供后端持续运行时追踪会话状态）
    async for event in run_graph_events(config, variables={...}):
        match event["type"]:
            case "step_start": print(f"开始: {event['step_id']}")
            case "step_done":  print(f"完成: {event['step_id']}")
            case "step_error": print(f"错误: {event['step_id']} → {event['error']}")
            case "loop_jump":  print(f"回跳: {event['from_step']} → {event['to_step']}")
            case "graph_done": print(f"图完成: {event['final']}")

━━ 单步工具调用 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    call_tool("truncate", {"text": "...", "max_chars": 10})
    call_from_file("tools/truncate.json", params={...})

━━ 外部数据接入（input） ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 通过名称（自动在 input/ 目录下查找）
    result = await call_input("http_get", {"url": "https://api.example.com/data"})
    result.status_code   # 200
    result.body          # 响应正文

    # 通过 JSON 文件路径
    result = await call_input_from_file("input/http_get.json", params={...})

    # 列出所有已注册输入源
    list_inputs()

━━ 运行时数据通道（长期运行节点透传）━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 1. 调用方：创建通道（图启动前）
    create_channel("ch_001")

    # 2. 图内步骤（input/read_channel）将阻塞等待：
    #    { "id": "recv", "type": "input", "ref": "read_channel",
    #      "params": { "channel_id": "{{channel_id}}" } }

    # 3. 后台启动图
    task = asyncio.create_task(run_graph(config, {"channel_id": "ch_001"}))

    # 4. 实时推送数据，图内节点继续执行
    await send_to_channel("ch_001", {"user_input": "第一轮"})
    await send_to_channel("ch_001", {"user_input": "第二轮"})

    # 5. 关闭通道，节点收到 ChannelClosedError 并退出
    close_channel("ch_001")

    # 辅助工具
    channel_exists("ch_001")   # 检查通道是否存在
    list_channels()             # 列出所有活跃通道

━━ 引用语法（pipeline / graph JSON 通用） ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    inputs 连线（类型安全，推荐）:
        "input_text": "step_id.field"
        "op":         "$.top_level_var"

    {{}} 插值（字符串拼接）:
        "{{step_id.field}}"   "{{var}}"

━━ 工具发现 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    list_tools()
    to_openai_schema("truncate")
"""

from . import text_utils, input_utils
from ._base import ToolResult
from ._validator import validate_long_running_propagation, validate_all_configs
from ._registry import (
    call_tool, call_from_file, call_from_dict, list_tools, to_openai_schema,
    call_input, call_input_from_file, list_inputs,
)
from ._channel import (
    create_channel, send_to_channel, close_channel,
    channel_exists, list_channels, ChannelClosedError,
)
from ._pipeline import run_pipeline, run_pipeline_from_file, run_pipeline_stream, run_pipeline_stream_from_file, PipelineResult
from ._graph import run_graph, run_graph_from_file, run_graph_stream, run_graph_stream_from_file, run_graph_events, GraphResult

__all__ = [
    "text_utils",
    "input_utils",
    "ToolResult",
    "validate_long_running_propagation",
    "validate_all_configs",
    "call_tool",
    "call_from_file",
    "call_from_dict",
    "list_tools",
    "to_openai_schema",
    "call_input",
    "call_input_from_file",
    "list_inputs",
    "run_pipeline",
    "run_pipeline_from_file",
    "run_pipeline_stream",
    "run_pipeline_stream_from_file",
    "PipelineResult",
    "run_graph",
    "run_graph_from_file",
    "run_graph_stream",
    "run_graph_stream_from_file",
    "run_graph_events",
    "GraphResult",
    "create_channel",
    "send_to_channel",
    "close_channel",
    "channel_exists",
    "list_channels",
    "ChannelClosedError",
]
