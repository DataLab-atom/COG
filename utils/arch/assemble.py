"""
arch_assemble: 将函数体填入代码骨架并写入磁盘。

处理逻辑：
- 遍历 skeletons（{file_path: skeleton_code}）
- 查找骨架中所有 {{body:<fn_id>}} 占位符
- 从 fn_bodies 取出对应 body，按 fn_stubs[fn_id]["indent"] 对每行加缩进
- 将 file_imports 中收集到的外部库 import 语句注入到文件顶部（架构树 imports 之后，去重）
- 替换占位符后创建目录（os.makedirs）并写入 .py 文件

返回 ArchAssembleResult(ok, errors, written)
  - written：成功写入的文件路径列表
"""
from __future__ import annotations
import os
import re
from typing import Any

from utils import ToolResult
from utils.arch.scaffold import _detect_needed_imports


class ArchAssembleResult(ToolResult):
    ok: bool
    errors: list[str]
    written: list[str]


def _indent_body(body: str, indent: str) -> str:
    """将函数体每行加上 indent，空行不加。"""
    lines = body.split("\n")
    result = []
    for line in lines:
        if line.strip():
            result.append(indent + line)
        else:
            result.append("")
    return "\n".join(result)


def _inject_imports(code: str, extra_imports: list[str]) -> str:
    """将 extra_imports 注入到文件顶部，跳过已存在的行，保留文件结构。"""
    if not extra_imports:
        return code
    lines = code.split("\n")
    existing = {ln.strip() for ln in lines}
    new_imports = [stmt for stmt in extra_imports if stmt.strip() not in existing]
    if not new_imports:
        return code
    # 找到最后一个现有 import 行的位置，插在其后
    last_import_idx = -1
    for i, line in enumerate(lines):
        if line.startswith("import ") or line.startswith("from "):
            last_import_idx = i
    insert_at = last_import_idx + 1
    lines = lines[:insert_at] + new_imports + lines[insert_at:]
    return "\n".join(lines)


def arch_assemble(
    skeletons:    dict[str, str],
    fn_bodies:    dict[str, str],
    fn_stubs:     dict[str, dict],
    output_dir:   str,
    file_imports: dict[str, list[str]] | None = None,
) -> ArchAssembleResult:
    written: list[str] = []
    errors:  list[str] = []

    # file_id → file_path 反查表（从 fn_stubs 中推断）
    # fn_stubs[fn_id]["file_id"] 对应 fanout 中的 file_id key
    # skeletons key 是 file_path，需要通过 fn_stubs 建立映射
    file_id_to_path: dict[str, str] = {}
    for fn_id, stub in (fn_stubs or {}).items():
        fid = stub.get("file_id", "")
        if fid and fid not in file_id_to_path:
            # 找该 fn_id 对应的 file_path（在 skeletons 中通过占位符匹配）
            placeholder = "{{body:" + fn_id + "}}"
            for fp, sk in skeletons.items():
                if placeholder in sk:
                    file_id_to_path[fid] = fp
                    break

    for file_path, skeleton in skeletons.items():
        code = skeleton

        # 找所有 {{body:<fn_id>}} 占位符并替换
        placeholders = re.findall(r"\{\{body:(fn::[^}]+)\}\}", code)
        for fn_id in placeholders:
            body     = fn_bodies.get(fn_id, "pass")
            stub     = fn_stubs.get(fn_id, {})
            indent   = stub.get("indent", "    ")
            indented = _indent_body(body, indent)
            code = code.replace("{{body:" + fn_id + "}}", indented)

        # 注入外部库 imports
        if file_imports:
            # 尝试通过 file_id_to_path 反查找到对应 file_id
            matched_file_id = next(
                (fid for fid, fp in file_id_to_path.items() if fp == file_path),
                None,
            )
            if matched_file_id and matched_file_id in file_imports:
                code = _inject_imports(code, file_imports[matched_file_id])

        # 自动检测并注入缺失的 typing/library imports（补全 scaffold 和 codegen 遗漏的）
        auto_imports = _detect_needed_imports(code)
        if auto_imports:
            code = _inject_imports(code, auto_imports)

        # 如果是 main.py 且定义了 main() 函数，确保末尾有 if __name__ 入口
        if os.path.basename(file_path) == "main.py" and re.search(
            r'^def main\s*\(', code, re.MULTILINE
        ):
            if 'if __name__' not in code:
                code = code.rstrip("\n") + "\n\n\nif __name__ == \"__main__\":\n    main()\n"

        # 只写发生变化的文件（diff-only），避免覆盖已与生成内容相同的文件
        dir_path = os.path.dirname(file_path)
        try:
            existing_content = ""
            file_exists = os.path.exists(file_path)
            if file_exists:
                with open(file_path, "r", encoding="utf-8") as f:
                    existing_content = f.read()
            if code != existing_content:
                os.makedirs(dir_path, exist_ok=True)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(code)
                written.append(file_path)
            elif file_exists:
                written.append(file_path)
            # 文件不存在且内容为空时不写入也不加入 written
        except Exception as e:
            errors.append(f"写入 {file_path} 失败: {e}")

    return ArchAssembleResult(ok=len(errors) == 0, errors=errors, written=written)
