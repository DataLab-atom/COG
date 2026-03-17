"""arch/runtime_fix.py — 代码库运行时错误修复工具函数

提供四个工具函数，配合 arch_verify_and_fix 图使用：
    arch_collect_error_context  — 解析 traceback，读取失败文件内容并格式化错误信息
    arch_init_fix_state         — 初始化修复循环状态（needs_fix = not run_ok）
    arch_update_fix_state       — 每轮修复后更新循环状态
    arch_write_patches          — 将 MCTS 风格 patch 永久写入磁盘（AST 定位 + 替换）
                                  支持 target_type: function / class / import / file
"""
from __future__ import annotations

import ast
import json
import os
import re
import textwrap

from utils import ToolResult


# ── 常见 import 自动检测 ──────────────────────────────────────────────────────

# typing 模块中常见类型名 → 对应 import 语句
_TYPING_IMPORTS: dict[str, str] = {
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

# 常见库别名 → 对应 import 语句
_LIB_IMPORTS: dict[str, str] = {
    "np":  "import numpy as np",
    "pd":  "import pandas as pd",
    "plt": "import matplotlib.pyplot as plt",
    "sp":  "import scipy as sp",
}


# ── 返回类型 ───────────────────────────────────────────────────────────────────

class ArchErrorContextResult(ToolResult):
    file_contents: dict       # {相对路径: 文件内容}
    file_contents_json: str   # JSON 字符串，供 LLM agent 直接插入提示词
    error_info: str           # 格式化后的完整错误信息字符串


class ArchFixStateResult(ToolResult):
    needs_fix: bool
    run_ok: bool


class ArchWritePatchesResult(ToolResult):
    report: list  # [{file, name, status, detail?}]
    ok: bool      # 所有 patch 均成功替换则为 True


# ── arch_collect_error_context ─────────────────────────────────────────────────

def arch_collect_error_context(
    stderr_log: str,
    error: str,
    output_log: str,
    output_dir: str,
) -> ArchErrorContextResult:
    """解析 stderr traceback，找出项目内失败文件并读取内容。

    若 traceback 中没有识别到项目文件，自动 fallback 到 main.py。

    Args:
        stderr_log:  sandbox_run 返回的 stderr 完整内容。
        error:       sandbox_run 返回的 error 字段（简短错误描述）。
        output_log:  sandbox_run 返回的 stdout 内容。
        output_dir:  项目根目录绝对路径，用于判断路径归属。

    Returns:
        file_contents:      {相对路径: 内容} dict。
        file_contents_json: JSON 字符串版本。
        error_info:         格式化后的错误信息，供 LLM 阅读。
    """
    norm_root = os.path.normpath(output_dir)

    # 将 stderr 中的 sandbox 临时目录路径替换为项目相对路径，
    # 避免 LLM 误将 "sandbox/main.py" 当作项目内路径
    sanitized_stderr = re.sub(
        r'/tmp/sandbox_[a-z0-9_]+/sandbox/',
        '',
        stderr_log or '',
    )

    # 从 traceback 中提取 File "..." 路径
    raw_text = sanitized_stderr + "\n" + (error or "")
    found_paths: list[str] = re.findall(r'File "([^"]+)", line \d+', raw_text)

    # 额外：从 ImportError/ModuleNotFoundError 消息里提取括号内的源模块路径
    # 示例：ImportError: cannot import name 'Z_corr' from 'harmonypy.harmony' (harmonypy/harmony.py)
    import_error_paths: list[str] = re.findall(
        r'(?:ImportError|ModuleNotFoundError)[^\(]*\(([^\)]+\.py)\)',
        raw_text,
    )
    found_paths = found_paths + import_error_paths

    relevant: list[str] = []
    seen: set[str] = set()
    for p in found_paths:
        # sanitized 后路径可能是相对路径（如 "harmonypy/harmony.py"），
        # 需要拼接 output_dir 再判断归属
        abs_p = p if os.path.isabs(p) else os.path.join(output_dir, p)
        norm_p = os.path.normpath(abs_p)
        if (norm_p.startswith(norm_root + os.sep) or norm_p == norm_root) and abs_p not in seen:
            relevant.append(abs_p)
            seen.add(abs_p)

    # fallback：如果没找到项目内文件，尝试 main.py；若 main.py 也不存在，
    # 只读取 __init__.py 了解 API 表面，避免把全量代码灌入 LLM
    if not relevant:
        candidate = os.path.join(output_dir, "main.py")
        if os.path.exists(candidate):
            relevant.append(candidate)
        else:
            # main.py 不存在——只收集 __init__.py 文件，帮 fixer 了解模块结构
            for root, _dirs, filenames in os.walk(output_dir):
                for fn in filenames:
                    if fn == "__init__.py":
                        relevant.append(os.path.join(root, fn))

    file_contents: dict[str, str] = {}
    for path in relevant:
        try:
            rel = os.path.relpath(path, output_dir)
            with open(path, "r", encoding="utf-8") as f:
                file_contents[rel] = f.read()
        except Exception:  # noqa: BLE001
            pass

    # 始终收集所有 __init__.py 的内容，让 fixer LLM 了解项目模块结构和可用导出
    for root_dir, _dirs, filenames in os.walk(output_dir):
        for fn in filenames:
            if fn == "__init__.py":
                init_path = os.path.join(root_dir, fn)
                rel = os.path.relpath(init_path, output_dir)
                if rel not in file_contents:
                    try:
                        with open(init_path, "r", encoding="utf-8") as f:
                            file_contents[rel] = f.read()
                    except Exception:  # noqa: BLE001
                        pass

    # 检查是否因为 main.py 缺失导致失败
    # 注意：沙盒始终会提供自己的默认 main.py，因此 stderr 中不会出现 "can't open file"；
    # 只要项目目录中没有 main.py，就应告知 LLM 需要创建。
    main_py_path = os.path.join(output_dir, "main.py")
    main_py_hint = ""
    if not os.path.exists(main_py_path):
        existing_py = [
            os.path.relpath(os.path.join(r, f), output_dir)
            for r, _, fs in os.walk(output_dir) for f in fs if f.endswith(".py")
        ]
        main_py_hint = (
            f"\n\n## 重要提示\n"
            f"项目目录中不存在 main.py 入口文件。\n"
            f"请使用 target_type=\"file\" 创建 main.py 作为项目入口点，"
            f"调用项目核心函数并输出 __METRICS__ 行。\n"
            f"现有 .py 文件：{existing_py}"
        )

    # 收集项目所有 .py 文件列表，帮助 fixer LLM 确定正确的 import 路径
    all_py_files = sorted(
        os.path.relpath(os.path.join(r, f), output_dir)
        for r, _, fs in os.walk(output_dir) for f in fs if f.endswith(".py")
    )
    project_structure_hint = (
        f"\n\n## 项目文件结构\n"
        f"以下是项目中所有 .py 文件，请根据此结构确定正确的 import 路径：\n"
        + "\n".join(f"  - {fp}" for fp in all_py_files)
    )

    # 收集可用的数据文件，帮助 fixer 生成正确的数据加载代码
    from utils.arch.parse_project import _collect_data_files, _DEFAULT_IGNORE as _PARSE_IGNORE
    data_files = _collect_data_files(output_dir, _PARSE_IGNORE)
    data_files_hint = ""
    if data_files:
        data_files_hint = (
            f"\n\n## 项目中可用的数据文件（main 函数应优先加载这些文件而非生成合成数据）\n"
            + "\n".join(
                f"  - {df['path']} ({df['size_mb']} MB, {df['ext']})"
                for df in data_files
            )
        )

    error_info = (
        f"## 错误摘要\n{error or '（无）'}\n\n"
        f"## Stderr（完整）\n{sanitized_stderr or '（空）'}\n\n"
        f"## Stdout（末尾）\n{(output_log or '')[-2000:]}"
        f"{main_py_hint}"
        f"{project_structure_hint}"
        f"{data_files_hint}"
    )

    return ArchErrorContextResult(
        file_contents=file_contents,
        file_contents_json=json.dumps(file_contents, ensure_ascii=False, indent=2),
        error_info=error_info,
    )


# ── arch_init_fix_state / arch_update_fix_state ────────────────────────────────

def arch_init_fix_state(run_ok: bool) -> ArchFixStateResult:
    """初始化修复循环状态：needs_fix = not run_ok。"""
    return ArchFixStateResult(needs_fix=not run_ok, run_ok=run_ok)


def arch_update_fix_state(run_ok: bool) -> ArchFixStateResult:
    """每轮修复后更新状态：needs_fix = not run_ok。"""
    return ArchFixStateResult(needs_fix=not run_ok, run_ok=run_ok)


# ── arch_write_patches ─────────────────────────────────────────────────────────

def _conflicting_import_lines(source: str, import_line: str) -> list[str]:
    """返回 source 中与 import_line 针对同一模块的冲突 import 行。

    冲突定义——同一顶层模块但 **import 形式不同** 的行：
      - ``import X …`` vs ``from X import …``  → 形式冲突，互相标记
      - 两个 ``from X import …`` 行只要导入名称不同就 **不是冲突**，可以共存
      - 两个 ``import X`` 行别名不同（如 ``import X`` vs ``import X as Y``）→ 冲突

    例如：import_line = "import pandas as pd"
    冲突行示例：  "from pandas import pd"  /  "import pandas"
    """
    stripped = import_line.strip()

    # 判断新增行是 "import ..." 还是 "from ... import ..."
    new_is_from = stripped.startswith("from ")
    mod_name: str | None = None

    if stripped.startswith("import "):
        mod_name = stripped[len("import "):].split()[0].split(".")[0]
    elif new_is_from:
        parts = stripped[len("from "):].split()
        if parts:
            mod_name = parts[0].split(".")[0]
    if not mod_name:
        return []

    # 用正则做精确的模块名边界匹配，避免 "test" 误中 "testing"
    import_pat = re.compile(
        rf'^import\s+{re.escape(mod_name)}(?:\s|$|\.)'
    )
    from_pat = re.compile(
        rf'^from\s+{re.escape(mod_name)}(?:\s|\.)'
    )

    conflicts: list[str] = []
    for src_line in source.splitlines():
        sl = src_line.strip()
        if sl == stripped:
            continue  # 完全相同，不算冲突

        # 判断源行是否涉及同一模块
        src_is_import = import_pat.match(sl) is not None
        src_is_from   = from_pat.match(sl) is not None
        if not src_is_import and not src_is_from:
            continue  # 不同模块，跳过

        # 形式不同 → 冲突（import X vs from X import Y）
        if new_is_from != src_is_from:
            conflicts.append(src_line)
            continue

        # 同为 "import X ..." 形式：别名不同 → 冲突
        if not new_is_from and src_is_import:
            conflicts.append(src_line)
            continue

        # 同为 "from X import ..." 形式：不视为冲突（可以共存）
        # 例如 from typing import Any 和 from typing import List 不冲突

    return conflicts


def _add_imports_to_source(source: str, import_code: str) -> tuple[str, list[str]]:
    """将 import 语句插入到文件现有 import 块之后（跳过已存在的行）。

    若源文件中存在针对同一模块的冲突 import（如 ``from pandas import pd``
    而新增行为 ``import pandas as pd``），会先移除冲突行再插入正确行，
    避免修复循环因冲突行未删除而永远失败。

    Args:
        source:       原始文件内容。
        import_code:  一行或多行 import 语句。

    Returns:
        (new_source, added): added 为实际插入的 import 行列表。
    """
    import_lines = [ln for ln in import_code.splitlines() if ln.strip()]

    # 移除冲突行（针对同一模块的错误 import），并收集需要新增的行
    working_source = source
    new_lines: list[str] = []
    for ln in import_lines:
        # 无论正确行是否已存在，都必须先删除冲突行
        conflicts = _conflicting_import_lines(working_source, ln)
        for conflict in conflicts:
            working_source = "\n".join(
                src_ln for src_ln in working_source.splitlines()
                if src_ln != conflict
            )
            if working_source and not working_source.endswith("\n"):
                working_source += "\n"
        # 基于最新 working_source 判断正确行是否已存在
        current_lines = {sl.strip() for sl in working_source.splitlines()}
        if ln.strip() in current_lines:
            continue  # 正确行已存在，冲突已清理，无需再追加
        new_lines.append(ln)

    if not new_lines and working_source == source:
        return source, []

    # 以 working_source 为基础追加新行
    source = working_source

    # 找到最后一条 import 语句的行号（1-indexed end_lineno）
    last_import_lineno = 0
    try:
        module = ast.parse(source)
        for node in ast.walk(module):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                last_import_lineno = max(last_import_lineno, node.end_lineno)
    except SyntaxError:
        # AST 解析失败时用正则回退定位最后一条 import 行
        for i, ln in enumerate(source.splitlines(), 1):
            sl = ln.strip()
            if sl.startswith("import ") or sl.startswith("from "):
                last_import_lineno = i

    if new_lines:
        lines = source.splitlines(keepends=True)
        insert_pos = last_import_lineno  # 0-indexed：插到第 last_import_lineno 行之后
        new_block = "".join(ln + "\n" for ln in new_lines)
        source = (
            "".join(lines[:insert_pos])
            + new_block
            + "".join(lines[insert_pos:])
        )

    return source, new_lines


def _split_imports_and_body(code: str) -> tuple[str, str]:
    """将代码拆分为前置 import 块和主体（def/class/decorator 起始处）。

    用于处理 LLM 在函数/类 patch 中附带 import 语句的情况。
    会把 def/class 之前紧邻的 decorator 行也归入 body 部分。

    Returns:
        (imports_part, body_part)
    """
    lines = code.splitlines(keepends=True)
    body_start = 0
    for i, ln in enumerate(lines):
        stripped = ln.lstrip()
        if stripped.startswith("def ") or stripped.startswith("class ") or stripped.startswith("async def "):
            # 从 def/class 往前回溯，把紧邻的 decorator 行纳入 body
            body_start = i
            while body_start > 0 and lines[body_start - 1].lstrip().startswith("@"):
                body_start -= 1
            break
    imports_part = "".join(lines[:body_start])
    body_part    = "".join(lines[body_start:])
    return imports_part, body_part


def _replace_node_in_source(
    source: str,
    target_type: str,
    target_name: str,
    new_code: str,
) -> tuple[str, bool]:
    """用 AST 定位目标函数/类，将其替换为 new_code。

    如果 new_code 开头包含 import 语句，会自动将其提升到文件顶部 import 块；
    保留原始缩进：先 dedent body 部分，再按目标节点的缩进重新对齐。

    Returns:
        (new_source, replaced): replaced 为 True 表示成功找到并替换。
    """
    try:
        module = ast.parse(source)
    except SyntaxError:
        return source, False

    lines = source.splitlines(keepends=True)

    # 按 lineno 排序收集所有匹配节点，优先选择顶层（col_offset == 0）节点
    def _match(n: ast.AST) -> bool:
        if target_type == "function":
            return isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == target_name
        if target_type == "class":
            return isinstance(n, ast.ClassDef) and n.name == target_name
        return False

    candidates = [n for n in ast.walk(module) if _match(n)]
    if not candidates:
        return source, False
    # 优先取顶层（col_offset == 0），其次按行号最小
    candidates.sort(key=lambda n: (n.col_offset != 0, n.lineno))
    node = candidates[0]

    if True:  # 保持原代码缩进结构
        start = node.lineno - 1  # 转为 0-indexed
        # 包含 decorator 行（decorator_list 存在时从最早的 decorator 开始替换）
        if hasattr(node, "decorator_list") and node.decorator_list:
            start = min(start, node.decorator_list[0].lineno - 1)
        end   = node.end_lineno  # lines[end:] 为节点之后的行

        # 原始缩进量
        orig_line   = lines[start]
        orig_indent = len(orig_line) - len(orig_line.lstrip())
        indent_str  = " " * orig_indent

        # 若 new_code 包含前置 import，先提取出来
        imports_part, body_part = _split_imports_and_body(new_code)

        # dedent body，再加正确缩进
        dedented  = textwrap.dedent(body_part)
        new_lines = [
            (indent_str + ln if ln.strip() else ln)
            for ln in dedented.splitlines()
        ]
        new_block = "\n".join(new_lines) + "\n"

        new_source = "".join(lines[:start]) + new_block + "".join(lines[end:])

        # 将提取出的 import 插入文件顶部 import 块之后
        if imports_part.strip():
            new_source, _ = _add_imports_to_source(new_source, imports_part)

        return new_source, True

    return source, False


def arch_write_patches(
    patches: list,
    output_dir: str,
) -> ArchWritePatchesResult:
    """将 MCTS 风格的 patch 列表永久写入磁盘文件。

    每个 patch 格式：{target_file, target_type, target_name, code}
    支持的 target_type：
      - "function" / "class"：用 AST 定位并就地替换，保留文件其余部分不变。
        若 code 开头含 import 语句，会自动提升到文件顶部 import 块之后。
      - "import"：向已有文件的 import 块末尾追加缺失的 import 语句（幂等）。
      - "file"：创建或覆写整个文件（如缺少 main.py 时使用）。

    Args:
        patches:    patch 列表，来自 arch_runtime_fixer agent 输出。
        output_dir: 项目根目录绝对路径，target_file 为相对此目录的路径。

    Returns:
        report: 每个 patch 的处理结果。
        ok:     所有 patch 均成功则为 True。
    """
    report: list[dict] = []

    for patch in patches:
        target_file = patch.get("target_file", "")
        target_type = patch.get("target_type", "function")
        target_name = patch.get("target_name", "")
        code        = patch.get("code", "")

        entry: dict = {
            "file": target_file, "name": target_name, "status": "ok",
        }
        file_path = os.path.join(output_dir, target_file)

        try:
            # target_type == "import" 表示向已有文件追加缺失的 import 语句
            if target_type == "import":
                if not os.path.isfile(file_path):
                    entry["status"] = "not_found"
                    entry["detail"] = f"文件 {target_file} 不存在，无法插入 import"
                    report.append(entry)
                    continue
                with open(file_path, "r", encoding="utf-8") as f:
                    source = f.read()
                new_source, added = _add_imports_to_source(source, code)
                changed = new_source != source
                if changed:
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(new_source)
                    if added:
                        entry["status"] = "import_added"
                        entry["detail"] = f"已插入: {added}"
                    else:
                        entry["status"] = "import_conflict_removed"
                        entry["detail"] = "已移除冲突 import 行（正确行已存在）"
                else:
                    entry["status"] = "import_skipped"
                    entry["detail"] = "所有 import 已存在，无需修改"
                report.append(entry)
                continue

            # target_type == "file" 表示创建/覆写整个文件（如 main.py）
            if target_type == "file":
                os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(code)
                entry["status"] = "created"
                report.append(entry)
                continue

            if not os.path.isfile(file_path):
                # 文件不存在：自动降级为创建整个文件（LLM 可能误用 function/class 类型）
                os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(code)
                entry["status"] = "created"
                entry["detail"] = (
                    f"文件 {target_file} 不存在，已自动降级为 target_type='file' 创建"
                )
                report.append(entry)
                continue

            with open(file_path, "r", encoding="utf-8") as f:
                source = f.read()

            new_source, replaced = _replace_node_in_source(
                source, target_type, target_name, code
            )

            if replaced:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(new_source)
                entry["status"] = "replaced"
            else:
                entry["status"] = "not_found"
                entry["detail"] = f"{target_type} '{target_name}' 未在 {target_file} 中找到"
        except Exception as exc:  # noqa: BLE001
            entry["status"] = "error"
            entry["detail"] = str(exc)

        report.append(entry)

    all_ok = all(
        r["status"] in ("replaced", "created", "import_added", "import_skipped", "import_conflict_removed")
        for r in report
    ) if report else True
    return ArchWritePatchesResult(report=report, ok=all_ok)


# ── arch_batch_fix_imports ────────────────────────────────────────────────────

class ArchBatchFixImportsResult(ToolResult):
    fixed_files: list[str]
    total_imports_added: int


def arch_batch_fix_imports(output_dir: str) -> ArchBatchFixImportsResult:
    """批量扫描项目所有 .py 文件，自动添加缺失的 typing/library imports。

    在沙盒运行前调用，一次性修复所有文件中的 NameError 级别的 import 缺失，
    避免 fix_loop 每次迭代只能修复一个文件的低效问题。

    检测逻辑：
      - 扫描文件中使用的 typing 类型名（Any, Tuple, Iterable 等）
      - 扫描文件中使用的库别名（np., pd. 等）
      - 只添加文件中缺失的 import 语句

    Args:
        output_dir: 项目根目录绝对路径。

    Returns:
        fixed_files:        被修改的文件相对路径列表。
        total_imports_added: 总共添加的 import 行数。
    """
    fixed_files: list[str] = []
    total_added = 0

    for root, _dirs, filenames in os.walk(output_dir):
        # 跳过常见的非项目目录
        if any(skip in root for skip in ("__pycache__", ".git", "venv", ".venv")):
            continue
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            file_path = os.path.join(root, fn)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    source = f.read()
            except Exception:  # noqa: BLE001
                continue

            if not source.strip():
                continue

            # 收集文件中已有的 import 行
            existing_imports = {
                ln.strip()
                for ln in source.splitlines()
                if ln.strip().startswith(("import ", "from "))
            }

            needed: list[str] = []

            # 检测 typing 名称
            for name, import_stmt in _TYPING_IMPORTS.items():
                if re.search(rf'\b{name}\b', source) and import_stmt not in existing_imports:
                    # 排除字符串中的误匹配：检查是否在类型注解上下文中使用
                    # 简单策略：只要不是纯注释/字符串中的出现就添加
                    needed.append(import_stmt)

            # 检测库别名
            for alias, import_stmt in _LIB_IMPORTS.items():
                if re.search(rf'\b{alias}\.', source) and import_stmt not in existing_imports:
                    needed.append(import_stmt)

            if not needed:
                continue

            # 去重并添加
            new_source, added = _add_imports_to_source(source, "\n".join(needed))
            if new_source != source:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(new_source)
                rel_path = os.path.relpath(file_path, output_dir)
                fixed_files.append(rel_path)
                total_added += len(added)

    return ArchBatchFixImportsResult(
        fixed_files=fixed_files,
        total_imports_added=total_added,
    )
