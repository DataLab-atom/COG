"""
arch_scaffold: 根据架构树静态生成目录结构和代码骨架。
- 为每个 module 创建目录
- 为每个 file 生成 .py 骨架（imports + constants + 类定义 + 函数 stub）
- 返回 ArchScaffoldResult(ok, skeletons, fn_stubs)
"""
from __future__ import annotations
import os
import re
import json
from typing import Any

from utils._base import ToolResult


class ArchScaffoldResult(ToolResult):
    ok: bool
    skeletons: dict[str, str]
    fn_stubs: dict[str, dict]


def _file_id_to_path(file_id: str, output_dir: str) -> str:
    """file::training/trainer → output_dir/training/trainer.py"""
    parts = file_id.replace("file::", "").split("/")
    return os.path.join(output_dir, *parts[:-1], parts[-1] + ".py")


def _module_id_to_dir(module_id: str, output_dir: str) -> str:
    name = module_id.replace("mod::", "")
    return os.path.join(output_dir, name)


def _type_name(type_id: str) -> str:
    return type_id.replace("type::", "")


def _fn_name(fn_id: str) -> str:
    return fn_id.replace("fn::", "")


def _python_type(t: str) -> str:
    """将 type:: 引用转换为 Python 类名，支持复合类型如 list[type::Foo]。"""
    return re.sub(r"type::(\w+)", r"\1", t)


def _build_params_str(params: list[dict]) -> str:
    parts = []
    for p in params:
        ptype = _python_type(p["type"])
        parts.append(f"{p['name']}: {ptype}")
    return ", ".join(parts)


def _build_import_lines(imports: list[dict], files: dict[str, dict]) -> list[str]:
    lines = []
    for imp in imports:
        from_id = imp.get("from", "")
        names = imp.get("names", [])
        if not from_id or not names:
            continue
        parts = from_id.replace("file::", "").split("/")
        module_path = ".".join(parts)
        lines.append(f"from {module_path} import {', '.join(names)}")
    return lines


def arch_scaffold(tree: list[dict], output_dir: str) -> ArchScaffoldResult:
    modules   = {n["id"]: n for n in tree if n.get("kind") == "module"}
    files     = {n["id"]: n for n in tree if n.get("kind") == "file"}
    types_map = {n["id"]: n for n in tree if n.get("kind") == "type"}
    fns_map   = {n["id"]: n for n in tree if n.get("kind") == "function"}

    # id → file 映射
    id_to_file: dict[str, str] = {}
    for fid, f in files.items():
        for tid in f.get("types", []):
            id_to_file[tid] = fid
        for fnid in f.get("functions", []):
            id_to_file[fnid] = fid

    # fn → 所属 type（通过 overloads 和 init_fn 反查）
    fn_to_type: dict[str, str] = {}
    for tid, t in types_map.items():
        for ovl in t.get("overloads", []):
            fn_to_type[ovl["fn"]] = tid
        if t.get("init_fn"):
            fn_to_type[t["init_fn"]] = tid

    skeletons: dict[str, str] = {}
    fn_stubs: dict[str, dict] = {}

    for fid, f in files.items():
        file_path = _file_id_to_path(fid, output_dir)
        lines: list[str] = []

        # imports
        import_lines = _build_import_lines(f.get("imports", []), files)
        if import_lines:
            lines.extend(import_lines)
            lines.append("")

        # constants
        for const in f.get("constants", []):
            val = json.dumps(const["value"]) if not isinstance(const["value"], str) else repr(const["value"])
            lines.append(f"{const['name']}: {const['type']} = {val}")
        if f.get("constants"):
            lines.append("")

        # types（类定义）
        for tid in f.get("types", []):
            t = types_map.get(tid, {})
            if not t:
                continue
            tname = _type_name(tid)
            base = t.get("base_class") or ""
            class_decl = f"class {tname}({base}):" if base else f"class {tname}:"
            lines.append(class_decl)

            # 类级别常量
            for const in t.get("constants", []):
                val = json.dumps(const["value"]) if not isinstance(const["value"], str) else repr(const["value"])
                lines.append(f"    {const['name']}: {const['type']} = {val}")
            if t.get("constants"):
                lines.append("")

            # __init__
            fields = t.get("fields", [])
            field_params = _build_params_str(fields) if fields else ""
            init_fn_id = t.get("init_fn")
            if init_fn_id:
                # LLM 生成 __init__ 体
                sig = f"    def __init__(self, {field_params}):" if field_params else "    def __init__(self):"
                lines.append(sig)
                lines.append("{{body:" + init_fn_id + "}}")
                lines.append("")
                fn_stubs[init_fn_id] = {
                    "file_id":  fid,
                    "class_id": tid,
                    "method":   "__init__",
                    "indent":   "        ",
                    "is_init":  True,
                }
            elif fields:
                # 静态展开 self.x = x
                lines.append(f"    def __init__(self, {field_params}):")
                for field in fields:
                    lines.append(f"        self.{field['name']} = {field['name']}")
                lines.append("")
            else:
                lines.append("    def __init__(self):")
                lines.append("        pass")
                lines.append("")

            # overloaded methods
            for ovl in t.get("overloads", []):
                fn_id = ovl["fn"]
                fn = fns_map.get(fn_id, {})
                method_name = ovl["method"]
                # method 签名（跳过 self）
                params = [p for p in fn.get("params", []) if p["name"] != "self"]
                params_str = _build_params_str(params)
                ret_type = _python_type(fn.get("returns", {}).get("type", "None"))
                async_kw = "async " if fn.get("is_async") else ""
                sig = f"    {async_kw}def {method_name}(self, {params_str}) -> {ret_type}:"
                lines.append(sig)
                lines.append("{{body:" + fn_id + "}}")
                lines.append("")

                fn_stubs[fn_id] = {
                    "file_id":    fid,
                    "class_id":   tid,
                    "method":     method_name,
                    "indent":     "        ",
                }

            lines.append("")

        # standalone functions
        for fnid in f.get("functions", []):
            if fnid in fn_to_type:
                continue  # 已在类中生成
            fn = fns_map.get(fnid, {})
            if not fn:
                continue
            fname = _fn_name(fnid)
            params_str = _build_params_str(fn.get("params", []))
            ret_type = _python_type(fn.get("returns", {}).get("type", "None"))
            async_kw = "async " if fn.get("is_async") else ""
            sig = f"{async_kw}def {fname}({params_str}) -> {ret_type}:"
            lines.append(sig)
            lines.append("{{body:" + fnid + "}}")
            lines.append("")

            fn_stubs[fnid] = {
                "file_id": fid,
                "class_id": None,
                "method":   None,
                "indent":   "    ",
            }

        skeletons[file_path] = "\n".join(lines)

    # 创建 module __init__.py
    for mid, m in modules.items():
        if m.get("root"):
            init_path = os.path.join(output_dir, "__init__.py")
        else:
            dir_path = _module_id_to_dir(mid, output_dir)
            init_path = os.path.join(dir_path, "__init__.py")

        exports = m.get("exports", [])
        init_lines: list[str] = []
        for exp_id in exports:
            name = exp_id.split("::")[-1]
            # 找到 exp_id 所在的 file
            src_fid = id_to_file.get(exp_id, "")
            if src_fid:
                parts = src_fid.replace("file::", "").split("/")
                module_path = ".".join(parts)
                init_lines.append(f"from {module_path} import {name}")
        skeletons[init_path] = "\n".join(init_lines)

    return ArchScaffoldResult(ok=True, skeletons=skeletons, fn_stubs=fn_stubs)
