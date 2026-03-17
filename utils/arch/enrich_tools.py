"""arch/enrich_tools.py — arch tree 描述补全工具函数

工具列表：
    arch_extract_undescribed  — 从 tree 中提取缺乏真实 docstring 的函数节点列表
    arch_apply_descriptions   — 将 agent 生成的描述写回 tree 节点
"""
from __future__ import annotations

from utils import ToolResult


class ArchExtractUndescribedResult(ToolResult):
    undescribed: list      # [{fn_id, fn_name, signature, code}, ...]
    has_undescribed: bool  # 是否存在需要补全的节点


class ArchApplyDescriptionsResult(ToolResult):
    tree: list   # 写回描述后的 tree
    applied: int # 实际写回的条数


def arch_extract_undescribed(tree: list) -> ArchExtractUndescribedResult:
    """从 arch tree 中提取没有真实 docstring 的函数/类型节点。

    判断标准：description 为空，或与自动生成的签名格式一致
    （即 "funcname(...) -> ReturnType"，单行且含 " -> "）。

    Args:
        tree: arch_parse_project 输出的 tree。

    Returns:
        ArchExtractUndescribedResult:
            .undescribed       需要补全的节点列表，每项含 fn_id / fn_name / signature / code
            .has_undescribed   是否存在需要补全的节点
    """
    undescribed: list = []

    for node in tree:
        if node.get("kind") not in ("function", "type"):
            continue

        fn_id = node.get("id", "")
        fn_name = fn_id.split("::", 1)[-1] if "::" in fn_id else fn_id
        desc = node.get("description", "")
        code = node.get("code", "")

        if not code:
            continue

        # 判断是否为自动生成的签名格式：单行、含 " -> "、以函数名开头
        is_auto = (
            not desc
            or (
                "\n" not in desc
                and " -> " in desc
                and desc.startswith(fn_name + "(")
            )
        )

        if is_auto:
            params = node.get("params", [])
            returns = node.get("returns", {})
            param_str = ", ".join(
                f"{p.get('name', '_')}: {p.get('type', 'Any')}" for p in params
            )
            ret_str = returns.get("type", "Any") if isinstance(returns, dict) else "Any"
            undescribed.append({
                "fn_id":     fn_id,
                "fn_name":   fn_name,
                "signature": f"def {fn_name}({param_str}) -> {ret_str}",
                "code":      code,
            })

    return ArchExtractUndescribedResult(
        undescribed=undescribed,
        has_undescribed=len(undescribed) > 0,
    )


def arch_apply_descriptions(tree: list, descriptions: list) -> ArchApplyDescriptionsResult:
    """将 agent 生成的描述列表写回 arch tree 节点的 description 字段。

    Args:
        tree:         arch_parse_project 输出的 tree。
        descriptions: [{fn_id, description}, ...] — agent 返回的描述列表。

    Returns:
        ArchApplyDescriptionsResult: .tree（更新后）、.applied（写回条数）
    """
    if not descriptions:
        return ArchApplyDescriptionsResult(tree=list(tree), applied=0)

    desc_map: dict[str, str] = {
        item["fn_id"]: item["description"]
        for item in descriptions
        if item.get("fn_id") and item.get("description")
    }

    applied = 0
    new_tree: list = []
    for node in tree:
        nid = node.get("id", "")
        if nid in desc_map:
            node = dict(node)
            node["description"] = desc_map[nid]
            applied += 1
        new_tree.append(node)

    return ArchApplyDescriptionsResult(tree=new_tree, applied=applied)
