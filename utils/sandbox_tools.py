"""
sandbox_tools — 沙箱会话同步工具函数

所有函数为纯计算（无 I/O），返回 ToolResult 子类。

公开接口：
    DecodeRequirementsResult  — decode_requirements() 返回值
    ApplyPatchesResult        — apply_patches_to_tree() 返回值
    ConcatPatchesResult       — concat_patches() 返回值

    decode_requirements()     — 从架构树所有 code 字段提取 pip 包名列表
    apply_patches_to_tree()   — 将 patches 应用到架构树，返回更新后的树
    concat_patches()          — 将两段 patch 列表拼接为一个，供图中合并 ancestor+new 使用
"""
from __future__ import annotations

import ast as _ast

from utils import ToolResult
from utils import stdlib_names as _stdlib_names


# ── 返回值类型 ─────────────────────────────────────────────────────────────────

class DecodeRequirementsResult(ToolResult):
    """decode_requirements() 的返回值。"""
    packages: list[str]   # 去重后的第三方包名列表（已过滤标准库）


class ApplyPatchesResult(ToolResult):
    """apply_patches_to_tree() 的返回值。"""
    tree: list[dict]      # 更新后的架构树
    changed_count: int    # 本次修改的节点数


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def decode_requirements(tree: list[dict]) -> DecodeRequirementsResult:
    """从架构树所有节点的 code 字段提取第三方 pip 包名列表。

    遍历 tree 中每个含 code 字段的节点，用 ast 解析 import 语句，
    提取顶级模块名，过滤标准库后去重返回。

    Args:
        tree: 架构树节点列表，每项可含 code 字段（字符串）。

    Returns:
        DecodeRequirementsResult:
            .packages  去重的第三方包名列表
    """
    stdlib = _stdlib_names()
    found: set[str] = set()

    for node in tree:
        code = node.get("code", "")
        if not code:
            continue
        try:
            parsed = _ast.parse(code)
        except SyntaxError:
            continue
        for stmt in _ast.walk(parsed):
            if isinstance(stmt, _ast.Import):
                for alias in stmt.names:
                    top = alias.name.split(".")[0]
                    if top and top not in stdlib:
                        found.add(top)
            elif isinstance(stmt, _ast.ImportFrom):
                if stmt.module:
                    top = stmt.module.split(".")[0]
                    if top and top not in stdlib and stmt.level == 0:
                        found.add(top)

    return DecodeRequirementsResult(packages=sorted(found))


def apply_patches_to_tree(tree: list[dict], patches: list[dict]) -> ApplyPatchesResult:
    """将 patches 应用到架构树副本，返回更新后的树。

    对于 action="rewrite"（默认）：找到匹配 target_file + target_name 的节点，
    更新其 code 字段并标记 changed=True。
    对于 action="create"：在树中追加新节点并标记 changed=True。

    Args:
        tree:    架构树节点列表（不可变输入，函数内部深拷贝后修改）。
        patches: patch 列表，每项须含 target_file / code，
                 rewrite 还须含 target_type / target_name。

    Returns:
        ApplyPatchesResult:
            .tree          更新后的架构树（新列表，原 tree 不变）
            .changed_count 本次实际修改的节点数
    """
    import copy
    new_tree: list[dict] = copy.deepcopy(tree)
    changed = 0

    for patch in patches:
        action = patch.get("action", "rewrite")
        target_file = patch.get("target_file", "")
        code = patch.get("code", "")

        if action == "create":
            new_tree.append({
                "target_file": target_file,
                "target_type": patch.get("target_type", "function"),
                "target_name": patch.get("target_name", ""),
                "code": code,
                "changed": True,
                "action": "create",
            })
            changed += 1
        else:
            target_name = patch.get("target_name", "")
            target_type = patch.get("target_type", "function")
            for node in new_tree:
                if (node.get("target_file", "") == target_file
                        and node.get("target_name", "") == target_name):
                    node["code"] = code
                    node["target_type"] = target_type
                    node["changed"] = True
                    node["action"] = "rewrite"
                    changed += 1
                    break

    return ApplyPatchesResult(tree=new_tree, changed_count=changed)


class ConcatPatchesResult(ToolResult):
    """concat_patches() 的返回值。"""
    all_patches: list[dict]   # ancestor_patches + new_patches 合并后的完整列表


def concat_patches(
    ancestor_patches: list[dict],
    new_patches: list[dict],
) -> ConcatPatchesResult:
    """将 ancestor_patches 和 new_patches 拼接为完整 patch 链。

    用于在图中合并两段 patch 列表，供下游节点存储累积改动历史。

    Args:
        ancestor_patches: 父节点累积 patch 列表。
        new_patches:      本次新增 patch 列表。

    Returns:
        ConcatPatchesResult:
            .all_patches  合并后的完整列表（ancestor + new 顺序）
    """
    return ConcatPatchesResult(all_patches=list(ancestor_patches) + list(new_patches))
