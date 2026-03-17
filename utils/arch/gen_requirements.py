"""
arch_gen_requirements: 扫描已生成的 Python 文件，提取第三方库依赖，写入 requirements.txt。

处理逻辑：
- 用 ast 解析每个 .py 文件，收集 import / from...import 语句的顶层模块名
- 排除标准库（sys.stdlib_module_names，Python < 3.10 用内置回退集合）
- 排除项目内部模块（根据 written 文件的 basename 推断）
- 将已知 import 名 → PyPI 包名的差异通过映射表转换（如 sklearn → scikit-learn）
- 写入 output_dir/requirements.txt，返回包名列表
"""
from __future__ import annotations

import ast
import logging
import os
from pathlib import Path

from utils import ToolResult
from utils import stdlib_names as _stdlib_names

logger = logging.getLogger(__name__)

# 已知 import 模块名 → PyPI 包名的差异映射
_IMPORT_TO_PKG: dict[str, str] = {
    "sklearn":    "scikit-learn",
    "cv2":        "opencv-python",
    "PIL":        "Pillow",
    "yaml":       "pyyaml",
    "bs4":        "beautifulsoup4",
    "dateutil":   "python-dateutil",
    "dotenv":     "python-dotenv",
    "attr":       "attrs",
    "git":        "gitpython",
    "Crypto":     "pycryptodome",
    "OpenSSL":    "pyOpenSSL",
    "serial":     "pyserial",
    "pkg_resources": "setuptools",
}


class ArchGenRequirementsResult(ToolResult):
    ok: bool
    packages: list[str]
    requirements_file: str
    error: str


def arch_gen_requirements(
    written: list[str],
    output_dir: str,
) -> ArchGenRequirementsResult:
    """
    扫描 written 文件，提取第三方依赖并写入 output_dir/requirements.txt。

    Args:
        written:    已写入磁盘的 Python 文件绝对路径列表。
        output_dir: 项目根目录，requirements.txt 写入此处。

    Returns:
        {
            "ok":                bool,
            "packages":          list[str],   # 排序后的 PyPI 包名列表
            "requirements_file": str,          # requirements.txt 绝对路径
            "error":             str,
        }
    """
    stdlib = _stdlib_names()

    # 项目内部模块名（从写入文件 basename 推断）
    internal: set[str] = {Path(p).stem for p in written}

    third_party: set[str] = set()

    for filepath in written:
        try:
            source = Path(filepath).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=filepath)
        except Exception as e:
            logger.warning("跳过文件 %s（解析失败）: %s", filepath, e)
            continue

        for node in ast.walk(tree):
            top: str | None = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top and top not in stdlib and top not in internal:
                        third_party.add(_IMPORT_TO_PKG.get(top, top))
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0]
                    if top and top not in stdlib and top not in internal:
                        third_party.add(_IMPORT_TO_PKG.get(top, top))

    packages = sorted(third_party)
    req_path = os.path.join(output_dir, "requirements.txt")

    try:
        content = "\n".join(packages) + "\n" if packages else ""
        with open(req_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return ArchGenRequirementsResult(
            ok=False,
            packages=[],
            requirements_file=req_path,
            error=str(e),
        )

    return ArchGenRequirementsResult(
        ok=True,
        packages=packages,
        requirements_file=req_path,
        error="",
    )
