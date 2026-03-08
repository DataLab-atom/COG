"""
arch_resolve_needs: 静态解析所有 fn 的 needs 字段，将未知辅助函数注入为 stub。

处理逻辑（迭代至收敛）：
- 遍历所有 fn.needs 条目：
  - depth > 3 → 记录错误并跳过（防止无限递归展开）
  - needs.location 不存在 → 记录错误并跳过
  - 否则：根据 description/params/returns 生成唯一 fn:: id，
    构造新函数节点（kind=function, needs=[], calls=[]），
    插入 target_file.functions，并将新 id 追加到原 fn.calls
- 每轮处理后清空 fn.needs；循环直到无新变化（支持嵌套 needs）
- 最终校验所有 fn.needs 均已清空

返回 ArchResolveNeedsResult(ok, errors, tree)
"""
from __future__ import annotations
import copy
import re
from typing import Any

from utils._base import ToolResult


class ArchResolveNeedsResult(ToolResult):
    ok: bool
    errors: list[str]
    tree: list[dict]


def _make_fn_id(description: str, existing_ids: set[str]) -> str:
    """根据描述生成唯一 fn:: id。"""
    words = re.findall(r"[a-zA-Z\u4e00-\u9fff]+", description)
    slug = "_".join(w.lower() for w in words[:4] if w.isascii()) or "helper"
    base = f"fn::{slug}"
    candidate = base
    i = 2
    while candidate in existing_ids:
        candidate = f"{base}_{i}"
        i += 1
    return candidate


def arch_resolve_needs(tree: list[dict]) -> ArchResolveNeedsResult:
    tree = copy.deepcopy(tree)
    errors: list[str] = []

    file_map: dict[str, dict] = {n["id"]: n for n in tree if n.get("kind") == "file"}
    fn_map:   dict[str, dict] = {n["id"]: n for n in tree if n.get("kind") == "function"}
    all_ids: set[str] = {n["id"] for n in tree}

    changed = True
    while changed:
        changed = False
        for fn in list(fn_map.values()):
            needs = fn.get("needs", [])
            if not needs:
                continue

            for need in needs:
                depth = need.get("depth", 1)
                if depth > 3:
                    errors.append(f"{fn['id']} 的 needs depth={depth} 超过最大值 3")
                    continue

                location = need.get("location")
                if not location or location not in file_map:
                    errors.append(f"{fn['id']} 的 needs.location={location!r} 不存在")
                    continue

                new_id = _make_fn_id(need.get("description", ""), all_ids)
                all_ids.add(new_id)

                new_fn: dict[str, Any] = {
                    "kind":        "function",
                    "id":          new_id,
                    "description": need.get("description", ""),
                    "is_async":    False,
                    "params":      need.get("params", []),
                    "returns":     need.get("returns", {"type": "None"}),
                    "calls":       [],
                    "needs":       [],
                    "reference":   None,
                }

                tree.append(new_fn)
                fn_map[new_id] = new_fn

                target_file = file_map[location]
                if new_id not in target_file.get("functions", []):
                    target_file.setdefault("functions", []).append(new_id)

                if new_id not in fn.get("calls", []):
                    fn.setdefault("calls", []).append(new_id)

                changed = True

            fn["needs"] = []

    remaining_needs = [
        n["id"] for n in tree
        if n.get("kind") == "function" and n.get("needs")
    ]
    if remaining_needs:
        errors.append(f"以下 fn 的 needs 未清空: {remaining_needs}")

    return ArchResolveNeedsResult(ok=len(errors) == 0, errors=errors, tree=tree)
