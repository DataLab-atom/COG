from __future__ import annotations
from typing import Dict, Any
from .tool_registry import ToolManager


TOOL_MANAGER = ToolManager()


def load_plugins(package: str = "LLM_MODULE.tools_plugins", *, on_error: str = "raise") -> list[str]:
    """
    加载插件目录下的工具模块（约定每个模块可实现 register(manager)）。
    返回成功加载的模块名列表。
    """
    return TOOL_MANAGER.autodiscover_plugins(package, on_error=on_error)


def tool_spec(agent: str | None = None, toolset: str | None = None) -> list[dict[str, Any]]:
    """返回 OpenAI function-calling 的工具描述（按 agent/toolset 过滤）。"""
    return TOOL_MANAGER.openai_tools(agent=agent, toolset=toolset)

def define_toolset(name: str, tools: list[str] | str) -> None:
    """定义/覆盖一个 toolset（tools='*' 表示全量工具）。"""
    TOOL_MANAGER.define_toolset(name, tools)


def bind_agent_toolset(agent: str, toolset: str) -> None:
    """把某个 agent 绑定到一个 toolset。"""
    TOOL_MANAGER.bind_agent(agent, toolset)


def tools_manifest_json() -> str:
    """导出当前已注册工具与 toolset 的清单（便于调试/展示）。"""
    return TOOL_MANAGER.manifest_json()


def make_tool_handler(
    *,
    agent: str | None = None,
    toolset: str | None = None,
    enforce_policy: bool = True,
):
    """返回可直接传给 OpenAI function-calling 循环的 handler(name, args)。"""

    def _handler(name: str, args: Dict[str, Any]) -> str:
        return dispatch_tool(name, args, agent=agent, toolset=toolset, enforce_policy=enforce_policy)

    return _handler


def dispatch_tool(
    name: str,
    args: Dict[str, Any],
    *,
    agent: str | None = None,
    toolset: str | None = None,
    enforce_policy: bool = False,
) -> str:
    return TOOL_MANAGER.dispatch(
        name,
        args,
        agent=agent,
        toolset=toolset,
        enforce_policy=enforce_policy,
    )
