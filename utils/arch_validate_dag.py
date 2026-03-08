"""
arch_validate_dag: 对 module imports、file imports、fn calls 进行有向图环检测。
返回 ArchValidateDagResult(ok, errors)
"""
from __future__ import annotations
from typing import Any

from utils._base import ToolResult


class ArchValidateDagResult(ToolResult):
    ok: bool
    errors: list[str]


def _detect_cycle(graph: dict[str, list[str]]) -> list[str]:
    """拓扑排序检测有向图中的环，返回成环的节点列表。"""
    visited = set()
    in_stack = set()
    cycle_nodes: list[str] = []

    def dfs(node: str) -> bool:
        visited.add(node)
        in_stack.add(node)
        for neighbor in graph.get(node, []):
            if neighbor not in visited:
                if dfs(neighbor):
                    return True
            elif neighbor in in_stack:
                cycle_nodes.append(neighbor)
                return True
        in_stack.discard(node)
        return False

    for node in list(graph.keys()):
        if node not in visited:
            if dfs(node):
                break

    return cycle_nodes


def arch_validate_dag(tree: list[dict], check_targets: list[str]) -> ArchValidateDagResult:
    errors: list[str] = []

    modules   = {n["id"]: n for n in tree if n.get("kind") == "module"}
    files     = {n["id"]: n for n in tree if n.get("kind") == "file"}
    functions = {n["id"]: n for n in tree if n.get("kind") == "function"}

    if "module_imports" in check_targets:
        graph = {mid: m.get("imports", []) for mid, m in modules.items()}
        cycle = _detect_cycle(graph)
        if cycle:
            errors.append(f"module_imports 有环，涉及节点: {cycle}")

    if "file_imports" in check_targets:
        graph = {fid: [imp["from"] for imp in f.get("imports", [])]
                 for fid, f in files.items()}
        cycle = _detect_cycle(graph)
        if cycle:
            errors.append(f"file_imports 有环，涉及节点: {cycle}")

    if "fn_calls" in check_targets:
        graph = {fid: fn.get("calls", []) for fid, fn in functions.items()}
        cycle = _detect_cycle(graph)
        if cycle:
            errors.append(f"fn_calls 有环，涉及节点: {cycle}")

    return ArchValidateDagResult(ok=len(errors) == 0, errors=errors)
