"""
arch_static_check: 对完整项目树执行全量静态检查。
检查项：
1. 所有 id 引用可解析
2. module.imports 无环
3. file.imports 无环
4. fn.calls 无环
5. overloads.args 与 fn:: params 名称一致
6. needs 全部清空
7. entrypoint 存在且 fn 可达
8. exports 合法性（只能包含该 module 旗下 file 中的 id）
9. depth > 3 的 needs 不存在
返回 ArchStaticCheckResult(ok, errors, warnings)
"""
from __future__ import annotations
import copy
from typing import Any
from utils._base import ToolResult
from utils.arch_validate_dag import arch_validate_dag


class ArchStaticCheckResult(ToolResult):
    ok: bool
    errors: list[str]
    warnings: list[str]


def arch_static_check(tree: list[dict]) -> ArchStaticCheckResult:
    errors: list[str] = []
    warnings: list[str] = []

    all_ids = {n["id"] for n in tree if "id" in n}
    modules   = {n["id"]: n for n in tree if n.get("kind") == "module"}
    files     = {n["id"]: n for n in tree if n.get("kind") == "file"}
    types_map = {n["id"]: n for n in tree if n.get("kind") == "type"}
    fns_map   = {n["id"]: n for n in tree if n.get("kind") == "function"}
    entrypoint = next((n for n in tree if n.get("kind") == "entrypoint"), None)

    # 建立 file 包含的 id 集合
    file_contains: dict[str, set[str]] = {}
    for fid, f in files.items():
        file_contains[fid] = set(f.get("types", [])) | set(f.get("functions", []))

    module_contains: dict[str, set[str]] = {}
    for mid, m in modules.items():
        s: set[str] = set()
        for fid in m.get("files", []):
            s |= file_contains.get(fid, set())
        module_contains[mid] = s

    # 1. id 引用可解析
    for fid, f in files.items():
        for imp in f.get("imports", []):
            if imp["from"] not in files:
                errors.append(f"{fid} imports.from={imp['from']!r} 不存在")
            for name in imp.get("names", []):
                matched = any(
                    i.split("::")[-1] == name
                    for i in file_contains.get(imp["from"], set())
                )
                if not matched:
                    errors.append(
                        f"{fid} imports name={name!r} 在 {imp['from']} 中未找到"
                    )

    for fnid, fn in fns_map.items():
        for called in fn.get("calls", []):
            if called not in all_ids:
                errors.append(f"{fnid} calls={called!r} 不存在")
        for param in fn.get("params", []):
            t = param.get("type", "")
            if t.startswith("type::") and t not in all_ids:
                errors.append(f"{fnid} param type={t!r} 不存在")

    for tid, t in types_map.items():
        for ovl in t.get("overloads", []):
            fn_id = ovl.get("fn", "")
            if fn_id and fn_id not in all_ids:
                errors.append(f"{tid} overload fn={fn_id!r} 不存在")

    # 2-4. 环检测
    dag_result = arch_validate_dag(
        tree, ["module_imports", "file_imports", "fn_calls"]
    )
    errors.extend(dag_result.errors)

    # 5. overloads.args 与 fn params 一致
    for tid, t in types_map.items():
        for ovl in t.get("overloads", []):
            fn_id = ovl.get("fn", "")
            fn = fns_map.get(fn_id)
            if not fn:
                continue
            fn_param_names = [p["name"] for p in fn.get("params", [])]
            ovl_args = ovl.get("args", [])
            if ovl_args and ovl_args != fn_param_names:
                errors.append(
                    f"{tid} overload.args={ovl_args} 与 {fn_id} params={fn_param_names} 不一致"
                )

    # 6. needs 全部清空
    for fn in fns_map.values():
        if fn.get("needs"):
            errors.append(f"{fn['id']} needs 未清空: {fn['needs']}")

    # 7. entrypoint 存在且可达
    if not entrypoint:
        errors.append("缺少 entrypoint 节点")
    else:
        ep_fn = entrypoint.get("fn", "")
        if ep_fn not in all_ids:
            errors.append(f"entrypoint fn={ep_fn!r} 不存在")
        else:
            # 可达性：从 entrypoint 沿 calls 做 BFS
            reachable: set[str] = set()
            queue = [ep_fn]
            while queue:
                cur = queue.pop()
                if cur in reachable:
                    continue
                reachable.add(cur)
                fn = fns_map.get(cur, {})
                queue.extend(fn.get("calls", []))

            dead = set(fns_map.keys()) - reachable
            if dead:
                warnings.append(f"死函数（不可达）: {sorted(dead)}")

    # 8. exports 合法性
    for mid, m in modules.items():
        for exp_id in m.get("exports", []):
            if exp_id not in module_contains.get(mid, set()):
                errors.append(
                    f"{mid} exports={exp_id!r} 不在该模块旗下任何 file 中"
                )

    return ArchStaticCheckResult(ok=len(errors) == 0, errors=errors, warnings=warnings)
