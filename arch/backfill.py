"""
arch_backfill: 静态回填以下字段：
- file.imports[].names  →  根据 file 内 types/functions 实际引用的跨文件 id 推导
- module.exports        →  根据跨模块引用推导对外暴露的 id
- module.dependencies   →  根据非 type:: / fn:: 前缀的类型引用推导外部库
- entrypoint            →  找到 fn::main 并设置
返回 ArchBackfillResult(ok, errors, tree)
"""
from __future__ import annotations
import copy
import re
from typing import Any

from utils._base import ToolResult


class ArchBackfillResult(ToolResult):
    ok: bool
    errors: list[str]
    tree: list[dict]


_BUILTIN_TYPES = {"str", "int", "float", "bool", "list", "dict", "None",
                  "bytes", "tuple", "set", "Any", "Optional", "List",
                  "Dict", "Tuple", "Set", "Union"}


def _extract_external_libs(type_str: str) -> set[str]:
    """从类型字符串提取外部库名，如 nn.Module → torch。"""
    libs = set()
    for part in re.split(r"[\[\],\s|]", type_str):
        part = part.strip()
        if "." in part:
            lib = part.split(".")[0]
            if lib not in _BUILTIN_TYPES:
                libs.add(lib)
    return libs


def arch_backfill(tree: list[dict]) -> ArchBackfillResult:
    tree = copy.deepcopy(tree)
    errors: list[str] = []

    modules   = {n["id"]: n for n in tree if n.get("kind") == "module"}
    files     = {n["id"]: n for n in tree if n.get("kind") == "file"}
    types_map = {n["id"]: n for n in tree if n.get("kind") == "type"}
    fns_map   = {n["id"]: n for n in tree if n.get("kind") == "function"}

    # 建立 id → file 映射
    id_to_file: dict[str, str] = {}
    for fid, f in files.items():
        for tid in f.get("types", []):
            id_to_file[tid] = fid
        for fnid in f.get("functions", []):
            id_to_file[fnid] = fid

    # 建立 file → module 映射
    file_to_module: dict[str, str] = {}
    for mid, m in modules.items():
        for fid in m.get("files", []):
            file_to_module[fid] = mid

    # 1. 回填 file.imports[].names
    for fid, f in files.items():
        local_ids = set(f.get("types", [])) | set(f.get("functions", []))
        referenced_ids: dict[str, set[str]] = {}

        def collect_refs(type_str: str):
            if type_str.startswith("type::") and type_str not in local_ids:
                src_file = id_to_file.get(type_str)
                if src_file and src_file != fid:
                    referenced_ids.setdefault(src_file, set()).add(
                        type_str.split("::")[-1]
                    )

        for tid in f.get("types", []):
            t = types_map.get(tid, {})
            for field in t.get("fields", []):
                collect_refs(field.get("type", ""))
            for ovl in t.get("overloads", []):
                fn_id = ovl.get("fn", "")
                if fn_id and fn_id not in local_ids:
                    src_file = id_to_file.get(fn_id)
                    if src_file and src_file != fid:
                        referenced_ids.setdefault(src_file, set()).add(
                            fn_id.split("::")[-1]
                        )

        for fnid in f.get("functions", []):
            fn = fns_map.get(fnid, {})
            for param in fn.get("params", []):
                collect_refs(param.get("type", ""))
            collect_refs(fn.get("returns", {}).get("type", ""))
            for called in fn.get("calls", []):
                if called not in local_ids:
                    src_file = id_to_file.get(called)
                    if src_file and src_file != fid:
                        referenced_ids.setdefault(src_file, set()).add(
                            called.split("::")[-1]
                        )

        existing_froms = {imp["from"] for imp in f.get("imports", [])}
        for src_fid, names in referenced_ids.items():
            if src_fid in existing_froms:
                for imp in f["imports"]:
                    if imp["from"] == src_fid:
                        imp["names"] = list(set(imp.get("names", [])) | names)
            else:
                f.setdefault("imports", []).append(
                    {"from": src_fid, "names": list(names)}
                )

    # 2. 回填 module.exports（被其它模块引用的 id）
    all_cross_module_refs: dict[str, set[str]] = {mid: set() for mid in modules}
    for fid, f in files.items():
        src_mod = file_to_module.get(fid)
        if not src_mod:
            continue
        for imp in f.get("imports", []):
            ref_fid = imp.get("from", "")
            ref_mod = file_to_module.get(ref_fid)
            if ref_mod and ref_mod != src_mod:
                for name in imp.get("names", []):
                    all_cross_module_refs[ref_mod].add(name)

    for mid, m in modules.items():
        exposed = set()
        for fid in m.get("files", []):
            f = files.get(fid, {})
            for tid in f.get("types", []):
                short = tid.split("::")[-1]
                if short in all_cross_module_refs.get(mid, set()):
                    exposed.add(tid)
            for fnid in f.get("functions", []):
                short = fnid.split("::")[-1]
                if short in all_cross_module_refs.get(mid, set()):
                    exposed.add(fnid)
        m["exports"] = list(exposed)

    # 3. 回填 module.dependencies（外部库）
    for mid, m in modules.items():
        libs: set[str] = set()
        for fid in m.get("files", []):
            f = files.get(fid, {})
            for tid in f.get("types", []):
                t = types_map.get(tid, {})
                if t.get("base_class"):
                    libs |= _extract_external_libs(t["base_class"])
                for field in t.get("fields", []):
                    libs |= _extract_external_libs(field.get("type", ""))
            for fnid in f.get("functions", []):
                fn = fns_map.get(fnid, {})
                for param in fn.get("params", []):
                    libs |= _extract_external_libs(param.get("type", ""))
                libs |= _extract_external_libs(fn.get("returns", {}).get("type", ""))
        m["dependencies"] = list(libs)

    # 4. 设置 entrypoint
    entrypoint = None
    for node in tree:
        if node.get("kind") == "entrypoint":
            entrypoint = node
            break

    main_fn = next(
        (n for n in tree if n.get("kind") == "function" and n["id"] == "fn::main"),
        None,
    )
    if main_fn:
        if entrypoint:
            entrypoint["fn"] = "fn::main"
        else:
            tree.append({"kind": "entrypoint", "fn": "fn::main"})
    else:
        errors.append("未找到 fn::main，无法设置 entrypoint")

    return ArchBackfillResult(ok=len(errors) == 0, errors=errors, tree=tree)
