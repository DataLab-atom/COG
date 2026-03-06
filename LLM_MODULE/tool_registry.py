from __future__ import annotations

import importlib
import json
import pkgutil
import traceback
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Callable, Dict, Iterable, List, Optional, Set


ToolArgs = Dict[str, Any]
ToolHandler = Callable[[ToolArgs], str]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: ToolHandler
    tags: Set[str] | None = None

    def to_openai_tool(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list_names(self) -> List[str]:
        return sorted(self._tools.keys())

    def to_openai_tools(self, names: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        if names is None:
            selected = list(self._tools.values())
        else:
            selected = []
            for n in names:
                tool = self._tools.get(n)
                if tool is not None:
                    selected.append(tool)
        return [t.to_openai_tool() for t in sorted(selected, key=lambda x: x.name)]

    def dispatch(self, name: str, args: ToolArgs) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"success": False, "error": f"unknown tool: {name}"}, ensure_ascii=False)
        try:
            return tool.handler(args or {})
        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "error": f"tool '{name}' failed: {str(e)}",
                    "traceback": traceback.format_exc(),
                },
                ensure_ascii=False,
            )


class AgentToolPolicy:
    """
    用「toolset」管理不同 agent 可用的工具集合。
    - 一个 agent 绑定一个 toolset
    - 一个 toolset 是一组 tool name（或 '*' 表示全部）
    """

    def __init__(self, default_toolset: str = "default") -> None:
        self.default_toolset = default_toolset
        self.toolsets: Dict[str, Set[str] | str] = {}
        self.agent_to_toolset: Dict[str, str] = {}

    def define_toolset(self, name: str, tools: Iterable[str] | str) -> None:
        if tools == "*":
            self.toolsets[name] = "*"
            return
        self.toolsets[name] = set(tools)

    def add_to_toolset(self, toolset: str, tool_names: Iterable[str]) -> None:
        current = self.toolsets.get(toolset)
        if current == "*":
            return
        if current is None:
            current = set()
            self.toolsets[toolset] = current
        if isinstance(current, set):
            current.update(tool_names)

    def bind_agent(self, agent: str, toolset: str) -> None:
        self.agent_to_toolset[agent] = toolset

    def resolve_toolset(self, agent: str | None = None, toolset: str | None = None) -> str:
        if toolset:
            return toolset
        if agent and agent in self.agent_to_toolset:
            return self.agent_to_toolset[agent]
        return self.default_toolset

    def allowed_names(
        self, registry: ToolRegistry, agent: str | None = None, toolset: str | None = None
    ) -> Optional[Set[str]]:
        ts = self.resolve_toolset(agent=agent, toolset=toolset)
        allow = self.toolsets.get(ts)
        if allow is None:
            return set()
        if allow == "*":
            return set(registry.list_names())
        return set(allow)


class ToolManager:
    def __init__(self) -> None:
        self.registry = ToolRegistry()
        self.policy = AgentToolPolicy(default_toolset="default")

    def register_tool(
        self,
        tool: ToolDefinition,
        toolsets: Optional[Iterable[str]] = None,
    ) -> None:
        self.registry.register(tool)
        if toolsets:
            for ts in toolsets:
                self.policy.add_to_toolset(ts, [tool.name])

    def define_toolset(self, name: str, tools: Iterable[str] | str) -> None:
        self.policy.define_toolset(name, tools)

    def bind_agent(self, agent: str, toolset: str) -> None:
        self.policy.bind_agent(agent, toolset)

    def openai_tools(self, agent: str | None = None, toolset: str | None = None) -> List[Dict[str, Any]]:
        names = self.policy.allowed_names(self.registry, agent=agent, toolset=toolset)
        return self.registry.to_openai_tools(names=names)

    def dispatch(
        self,
        name: str,
        args: ToolArgs,
        agent: str | None = None,
        toolset: str | None = None,
        enforce_policy: bool = False,
    ) -> str:
        if enforce_policy:
            allowed = self.policy.allowed_names(self.registry, agent=agent, toolset=toolset) or set()
            if name not in allowed:
                return json.dumps(
                    {"success": False, "error": f"tool not allowed: {name}", "agent": agent, "toolset": toolset},
                    ensure_ascii=False,
                )
        return self.registry.dispatch(name, args)

    def autodiscover_plugins(
        self,
        package: str,
        *,
        on_error: str = "raise",
    ) -> List[str]:
        """
        自动导入某个 package 下的所有子模块，并调用其 `register(manager)`（如果存在）。
        on_error:
          - 'raise': 任何插件出错直接抛出
          - 'skip' : 跳过出错插件，继续加载
        返回成功加载（完成 register）的模块名列表。
        """
        loaded: List[str] = []
        pkg = importlib.import_module(package)
        if not hasattr(pkg, "__path__"):
            return loaded

        for modinfo in pkgutil.walk_packages(pkg.__path__, prefix=f"{package}."):
            module_name = modinfo.name
            try:
                m = importlib.import_module(module_name)
                _maybe_register_plugin(m, self)
                loaded.append(module_name)
            except Exception:
                if on_error == "skip":
                    continue
                raise
        return loaded

    def toolsets_snapshot(self) -> Dict[str, List[str] | str]:
        out: Dict[str, List[str] | str] = {}
        for name, allow in self.policy.toolsets.items():
            if allow == "*":
                out[name] = "*"
            else:
                out[name] = sorted(list(allow))
        return out

    def agents_snapshot(self) -> Dict[str, str]:
        return dict(self.policy.agent_to_toolset)

    def manifest_json(self) -> str:
        return json.dumps(
            {
                "tools": self.registry.list_names(),
                "toolsets": self.toolsets_snapshot(),
                "agent_to_toolset": self.agents_snapshot(),
                "default_toolset": self.policy.default_toolset,
            },
            ensure_ascii=False,
        )


def _maybe_register_plugin(module: ModuleType, manager: ToolManager) -> None:
    register = getattr(module, "register", None)
    if callable(register):
        register(manager)
