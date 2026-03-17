"""
usage_example.py — llm_config.py 使用示例

API Key 通过 dynaconf 读取，无需硬编码：
  - 将 .secrets.toml.example 复制为 .secrets.toml 并填入真实密钥
  - 或设置环境变量: export OPENAGENTS_OPENAI_API_KEY=sk-...
"""
import asyncio

from llm_config import load_config_from_file, run, run_from_file, run_from_dict
from config import settings
print(settings.OPENAI_API_BASE)
print(settings.OPENAI_API_KEY)


import os
import asyncio
from openai import AsyncOpenAI

client = AsyncOpenAI(
    api_key=settings.get("OPENAI_API_KEY"),        # 从 .secrets.toml / 环境变量读取
    base_url=settings.get("OPENAI_API_BASE"),      # 从 settings.toml 读取
)

async def main():
    resp = await client.chat.completions.create(
        model="gpt-4o-mini",  # 换成你用的模型
        messages=[{"role": "user", "content": "hello"}],
    )
    print(resp.choices[0].message.content)

asyncio.run(main())


async def main():
    # ── 示例 1: JSON 输出（从文件加载配置）─────────────────────────────
    result = await run_from_file(
        "configs/extract_user.json",
        variables={
            "operator_name": "管理员",
            "input_text": "我叫张三，今年28岁，住在上海。",
        }
    )
    print(result)
    # → {"name": "张三", "age": 28, "city": "上海"}


    # ── 示例 2: 多轮对话 + 流式输出────────────────────────────────────
    history = []
    config = load_config_from_file("configs/chat.json")

    # 第一轮
    async for chunk in await run(config, variables={"operator_name": "系统", "input_text": "你好"}, history=history):
        print(chunk, end="", flush=True)
    print()

    # 追加历史（过滤掉其他智能体的 system 消息）
    history.append({"role": "user",      "content": "你好"})
    history.append({"role": "assistant", "content": "你好，有什么可以帮你？", "name": config.agent_name})

    # 第二轮
    async for chunk in await run(config, variables={"operator_name": "系统", "input_text": "今天天气怎么样？"}, history=history):
        print(chunk, end="", flush=True)
    print()


    # ── 示例 3: 多智能体共享历史──────────────────────────────────────
    shared_history = []

    config_a = load_config_from_file("configs/extract_user.json")
    config_b = load_config_from_file("configs/vllm_summarize.json")

    result_a = await run(config_a, variables={"operator_name": "系统", "input_text": "张三，28岁，上海"}, history=shared_history)
    shared_history.append({"role": "user",      "content": "张三，28岁，上海"})
    shared_history.append({"role": "assistant", "content": str(result_a), "name": config_a.agent_name})

    # config_b 的历史里会自动看到 extract_agent 的动作
    result_b = await run(config_b, variables={"operator_name": "系统", "input_text": "请总结上述信息"}, history=shared_history)
    print(result_b)


    # ── 示例 4: 计算图并行流式合并（aiostream）───────────────────────
    from utils import run_graph_stream

    graph_config = {
        "steps": [
            {"id": "extract",   "type": "agent", "ref": "configs/extract_user.json",   "variables": {"operator_name": "系统", "input_text": "{{input_text}}"}},
            {"id": "translate", "type": "agent", "ref": "configs/chat.json",            "variables": {"operator_name": "系统", "input_text": "{{input_text}}"}},
        ]
    }

    # extract 和 translate 并行运行，token 流实时交错输出
    async for step_id, token in run_graph_stream(graph_config, {"input_text": "张三，28岁，上海"}):
        print(f"[{step_id}] {token}", end="", flush=True)
    print()


if __name__ == "__main__":
    asyncio.run(main())
