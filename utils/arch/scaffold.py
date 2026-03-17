"""
arch_scaffold: 根据架构树静态生成目录结构和代码骨架。

处理内容：
- module：为每个模块创建子目录及 __init__.py（含 exports 重新导出）
  - module.root=True → __init__.py 放置于 output_dir/ 根目录，不创建子目录
- file：为每个文件生成 .py 骨架（imports、constants、类定义、函数 stub）
  - file.root=True → .py 文件放置于 output_dir/ 根目录，import 路径仅用文件名
- 函数体用占位符 {{body:<fn_id>}} 标记，由下游 arch_assemble 填充

返回 ArchScaffoldResult(ok, skeletons, fn_stubs)
  - skeletons：{file_path: skeleton_code}
  - fn_stubs：{fn_id: {file_id, class_id, method, indent, is_init?}}
"""
from __future__ import annotations
import os
import re
import json
from typing import Any

from utils import ToolResult
from utils.arch._common import _fn_name, _short_id, _build_tree_maps, _build_fn_to_type_map


# ── 自动 import 检测 ──────────────────────────────────────────────────────────

# typing 模块中常见的类型名 → 对应的 import 语句
_TYPING_NAMES: dict[str, str] = {
    "Any":       "from typing import Any",
    "Tuple":     "from typing import Tuple",
    "List":      "from typing import List",
    "Dict":      "from typing import Dict",
    "Set":       "from typing import Set",
    "Optional":  "from typing import Optional",
    "Union":     "from typing import Union",
    "Iterable":  "from typing import Iterable",
    "Iterator":  "from typing import Iterator",
    "Sequence":  "from typing import Sequence",
    "Callable":  "from typing import Callable",
    "Generator": "from typing import Generator",
    "Mapping":   "from typing import Mapping",
}

# 常见库别名 → 对应的 import 语句
_LIB_ALIASES: dict[str, str] = {
    "np":  "import numpy as np",
    "pd":  "import pandas as pd",
    "plt": "import matplotlib.pyplot as plt",
    "sp":  "import scipy as sp",
    "tf":  "import tensorflow as tf",
    "nn":  "import torch.nn as nn",
}


def _detect_needed_imports(code: str) -> list[str]:
    """扫描生成的代码，检测引用了哪些 typing 类型和库别名，返回需要添加的 import 行。

    自动检测函数签名中的类型注解引用，如 ``Tuple[float, ...]``、``np.ndarray``、
    ``pd.DataFrame`` 等，并返回对应的 import 语句列表。
    """
    needed: list[str] = []
    seen: set[str] = set()

    # 检测 typing 名称（作为独立标识符或泛型参数出现）
    for name, import_stmt in _TYPING_NAMES.items():
        # 匹配 word boundary：Tuple[, Tuple,, -> Tuple, : Tuple 等
        if re.search(rf'\b{name}\b', code) and import_stmt not in seen:
            needed.append(import_stmt)
            seen.add(import_stmt)

    # 检测库别名（np.xxx, pd.xxx 等模式）
    for alias, import_stmt in _LIB_ALIASES.items():
        if re.search(rf'\b{alias}\.', code) and import_stmt not in seen:
            needed.append(import_stmt)
            seen.add(import_stmt)

    return needed


class ArchScaffoldResult(ToolResult):
    ok: bool
    skeletons: dict[str, str]
    fn_stubs: dict[str, dict]


def _file_id_to_path(file_id: str, output_dir: str, root: bool = False) -> str:
    """file::training/trainer → output_dir/training/trainer.py
    root=True → output_dir/trainer.py（根目录文件，忽略模块前缀）
    """
    parts = file_id.replace("file::", "").split("/")
    if root:
        return os.path.join(output_dir, parts[-1] + ".py")
    return os.path.join(output_dir, *parts[:-1], parts[-1] + ".py")


def _file_import_path(file_id: str, files: dict[str, dict]) -> str:
    """将 file id 转换为 Python import 路径，root 文件只取文件名。"""
    parts = file_id.replace("file::", "").split("/")
    if files.get(file_id, {}).get("root"):
        return parts[-1]
    return ".".join(parts)


def _module_id_to_dir(module_id: str, output_dir: str) -> str:
    name = module_id.replace("mod::", "")
    return os.path.join(output_dir, name)


def _type_name(type_id: str) -> str:
    return type_id.replace("type::", "")


def _python_type(t: str) -> str:
    """将 type:: 引用转换为 Python 类名，支持复合类型如 list[type::Foo]。"""
    return re.sub(r"type::(\w+)", r"\1", t)


def _build_params_str(params: list[dict]) -> str:
    parts = []
    for p in params:
        name = p.get("name", "_")
        ptype = _python_type(p.get("type", "Any"))
        parts.append(f"{name}: {ptype}")
    return ", ".join(parts)


def _build_import_lines(imports: list[dict], files: dict[str, dict]) -> list[str]:
    lines = []
    for imp in imports:
        if not isinstance(imp, dict):
            continue
        # import X / import X as Y
        if "import" in imp:
            mod = imp["import"]
            alias = imp.get("as")
            lines.append(f"import {mod} as {alias}" if alias else f"import {mod}")
            continue
        # from X import a, b
        from_id = imp.get("from", "")
        names = imp.get("names", [])
        if not from_id or not names:
            continue
        module_path = _file_import_path(from_id, files)
        lines.append(f"from {module_path} import {', '.join(names)}")
    return lines


def arch_scaffold(tree: list[dict], output_dir: str) -> ArchScaffoldResult:
    modules, files, types_map, fns_map = _build_tree_maps(tree)

    # id → file 映射
    id_to_file: dict[str, str] = {}
    for fid, f in files.items():
        for tid in f.get("types", []):
            id_to_file[tid] = fid
        for fnid in f.get("functions", []):
            id_to_file[fnid] = fid

    fn_to_type = _build_fn_to_type_map(types_map)

    skeletons: dict[str, str] = {}
    fn_stubs: dict[str, dict] = {}

    for fid, f in files.items():
        file_path = _file_id_to_path(fid, output_dir, root=f.get("root", False))
        lines: list[str] = []

        # imports
        import_lines = _build_import_lines(f.get("imports", []), files)
        if import_lines:
            lines.extend(import_lines)
            lines.append("")

        # constants
        for const in f.get("constants", []):
            if not isinstance(const, dict) or "name" not in const:
                continue
            c_val = const.get("value", None)
            val = repr(c_val)
            lines.append(f"{const['name']}: {const.get('type', 'Any')} = {val}")
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
                if not isinstance(const, dict) or "name" not in const:
                    continue
                c_val = const.get("value", None)
                val = repr(c_val)
                lines.append(f"    {const['name']}: {const.get('type', 'Any')} = {val}")
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
                    lines.append(f"        self.{field.get('name', '_')} = {field.get('name', '_')}")
                lines.append("")
            else:
                lines.append("    def __init__(self):")
                lines.append("        pass")
                lines.append("")

            # overloaded methods
            for ovl in t.get("overloads", []):
                fn_id = ovl.get("fn", "")
                if not fn_id:
                    continue
                fn = fns_map.get(fn_id, {})
                method_name = ovl.get("method", fn_id.rsplit("::", 1)[-1])
                # method 签名（跳过 self）
                params = [p for p in fn.get("params", []) if p.get("name") != "self"]
                params_str = _build_params_str(params)
                ret_type = _python_type((fn.get("returns") or {}).get("type", "None"))
                async_kw = "async " if fn.get("is_async") else ""
                sig_params = f"self, {params_str}" if params_str else "self"
                sig = f"    {async_kw}def {method_name}({sig_params}) -> {ret_type}:"
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
            ret_type = _python_type((fn.get("returns") or {}).get("type", "None"))
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

        # 自动检测并注入缺失的 typing/library imports
        raw_code = "\n".join(lines)
        auto_imports = _detect_needed_imports(raw_code)
        if auto_imports:
            existing_imports = {ln.strip() for ln in lines if ln.strip().startswith(("import ", "from "))}
            new_imports = [imp for imp in auto_imports if imp not in existing_imports]
            if new_imports:
                # 找到现有 import 块末尾，插入新 import
                insert_idx = 0
                for idx, ln in enumerate(lines):
                    if ln.strip().startswith(("import ", "from ")):
                        insert_idx = idx + 1
                for imp in reversed(new_imports):
                    lines.insert(insert_idx, imp)

        skeletons[file_path] = "\n".join(lines)

    # 创建 module __init__.py
    # fn_to_type 已在上方 scaffold 文件时构建，用于过滤类方法（类方法不能从模块顶层 import）
    for mid, m in modules.items():
        if m.get("root"):
            init_path = os.path.join(output_dir, "__init__.py")
        else:
            dir_path = _module_id_to_dir(mid, output_dir)
            init_path = os.path.join(dir_path, "__init__.py")

        exports = m.get("exports", [])
        init_lines: list[str] = []
        for exp_id in exports:
            # 跳过类方法：类方法属于类实例，不能从模块顶层直接 import
            if exp_id in fn_to_type:
                continue
            name = _short_id(exp_id)
            src_fid = id_to_file.get(exp_id, "")
            if src_fid:
                module_path = _file_import_path(src_fid, files)
                init_lines.append(f"from {module_path} import {name}")
        skeletons[init_path] = "\n".join(init_lines)

    return ArchScaffoldResult(ok=True, skeletons=skeletons, fn_stubs=fn_stubs)
