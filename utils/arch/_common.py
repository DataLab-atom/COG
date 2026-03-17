"""
arch/_common.py — arch 子系统共享辅助函数

提供各 arch 工具模块复用的纯计算函数，消除跨文件重复定义。

公开接口：
    _short_id(full_id)          — 提取 :: 后缀短名称，替代散落的 split("::")[-1]
    _fn_name(fn_id)             — 提取函数短名称（去掉 fn:: 前缀）
    _build_tree_maps(tree)      — 将架构树按 kind 分组为四个查找表
    _build_fn_to_type_map(...)  — 从 types_map 构建 fn_id → type_id 反查表
"""
from __future__ import annotations


def _short_id(full_id: str) -> str:
    """提取 :: 后缀短名称，如 'fn::train' → 'train'，'type::Model' → 'Model'。"""
    return full_id.split("::")[-1]


def _fn_name(fn_id: str) -> str:
    """从 fn_id 提取函数名，如 'fn::train' → 'train'，'fn::mod/sub::foo' → 'foo'。"""
    return fn_id.split("::")[-1]


def _build_tree_maps(
    tree: list[dict],
) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict], dict[str, dict]]:
    """将架构树按 kind 分组，返回 (modules, files, types_map, fns_map) 四个查找表。

    Args:
        tree: 架构树节点列表。

    Returns:
        modules:   {id: node}，kind == "module"
        files:     {id: node}，kind == "file"
        types_map: {id: node}，kind == "type"
        fns_map:   {id: node}，kind == "function"
    """
    modules   = {n["id"]: n for n in tree if n.get("kind") == "module" and "id" in n}
    files     = {n["id"]: n for n in tree if n.get("kind") == "file" and "id" in n}
    types_map = {n["id"]: n for n in tree if n.get("kind") == "type" and "id" in n}
    fns_map   = {n["id"]: n for n in tree if n.get("kind") == "function" and "id" in n}
    return modules, files, types_map, fns_map


def _build_fn_to_type_map(types_map: dict[str, dict]) -> dict[str, str]:
    """从 types_map 构建 fn_id → type_id 反查表。

    遍历每个 type 节点的 overloads 和 init_fn，建立从函数 id 到所属类型 id 的映射。

    Args:
        types_map: {type_id: type_node} 查找表。

    Returns:
        {fn_id: type_id} 映射。
    """
    fn_to_type: dict[str, str] = {}
    for tid, t in types_map.items():
        for ovl in t.get("overloads", []):
            if isinstance(ovl, dict) and "fn" in ovl:
                fn_to_type[ovl["fn"]] = tid
        if t.get("init_fn"):
            fn_to_type[t["init_fn"]] = tid
    return fn_to_type
