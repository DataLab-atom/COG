"""
arch_assemble: 将函数体填入代码骨架并写入磁盘。

处理逻辑：
- 遍历 skeletons（{file_path: skeleton_code}）
- 查找骨架中所有 {{body:<fn_id>}} 占位符
- 从 fn_bodies 取出对应 body，按 fn_stubs[fn_id]["indent"] 对每行加缩进
- 替换占位符后创建目录（os.makedirs）并写入 .py 文件

返回 ArchAssembleResult(ok, errors, written)
  - written：成功写入的文件路径列表
"""
from __future__ import annotations
import os
import re
from typing import Any

from utils._base import ToolResult


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


def arch_assemble(
    skeletons:  dict[str, str],
    fn_bodies:  dict[str, str],
    fn_stubs:   dict[str, dict],
    output_dir: str,
) -> ArchAssembleResult:
    written: list[str] = []
    errors:  list[str] = []

    for file_path, skeleton in skeletons.items():
        code = skeleton

        # 找所有 {{body:<fn_id>}} 占位符并替换
        placeholders = re.findall(r"\{\{body:(fn::[^}]+)\}\}", code)
        for fn_id in placeholders:
            body    = fn_bodies.get(fn_id, "pass")
            stub    = fn_stubs.get(fn_id, {})
            indent  = stub.get("indent", "    ")
            indented = _indent_body(body, indent)
            code = code.replace("{{body:" + fn_id + "}}", indented)

        # 创建目录并写文件
        dir_path = os.path.dirname(file_path)
        try:
            os.makedirs(dir_path, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(code)
            written.append(file_path)
        except Exception as e:
            errors.append(f"写入 {file_path} 失败: {e}")

    return ArchAssembleResult(ok=len(errors) == 0, errors=errors, written=written)
