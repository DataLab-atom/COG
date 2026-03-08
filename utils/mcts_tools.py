"""
mcts_tools — MCTS 搜索工具函数

同步工具，供 tools/*.json 配置引用（type: "tool"）。
返回 plain dict（与 COG 现有 arch_* 工具保持一致）。

工具列表：
    mcts_ast_check   — 对 LLM 代码输出执行 AST 语法检查，返回 {ok, patch, error}
"""
from __future__ import annotations

import ast
import re
from typing import Any


# ── 代码清洗 ─────────────────────────────────────────────────────────────────

def _strip_fences(code: str) -> str:
    """去除 markdown 代码围栏（```python ... ```）。"""
    m = re.search(r"```(?:python|py)?\s*(.*?)```", code, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else code.strip()


# ── 工具函数 ─────────────────────────────────────────────────────────────────

def mcts_ast_check(
    code: str,
    target_file: str,
    target_type: str,
    target_name: str,
) -> dict[str, Any]:
    """
    对 LLM 生成的代码字符串执行 AST 语法检查，并构建完整 patch 对象。

    先去除 markdown 围栏，再用 ast.parse 验证语法。
    静态失败时返回 ok=False, patch=None（引擎直接丢弃，不计 consecutive_bad）。

    Args:
        code:        LLM 原始输出，可含 markdown 围栏。
        target_file: 相对路径，例如 src/model.py。
        target_type: "function" 或 "class"。
        target_name: 要替换的函数名或类名。

    Returns:
        {
            "ok":    bool,          # True = 语法合法
            "patch": dict | None,   # ok=True 时为完整 patch 对象，否则 None
            "error": str,           # 语法错误信息（ok=False 时有效）
        }
    """
    clean = _strip_fences(code)
    try:
        ast.parse(clean)
        patch = {
            "target_file": target_file,
            "target_type": target_type,
            "target_name": target_name,
            "code": clean,
        }
        return {"ok": True, "patch": patch, "error": ""}
    except SyntaxError as e:
        return {"ok": False, "patch": None, "error": str(e)}
