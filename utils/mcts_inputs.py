"""
mcts_inputs — MCTS 异步 Input 函数

供 input/*.json 配置引用（type: "input", async: true）。
返回 plain dict（与 COG 现有工具保持一致）。

函数列表：
    mcts_sandbox — 沙盒评估：重放累积 patch 链 → 运行 main.py → 解析 __METRICS__
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from typing import Any

from mcretro_mas.engine_optimizer import CodeInjector


# ── 沙盒评估 ─────────────────────────────────────────────────────────────────

async def mcts_sandbox(
    project_root: str,
    ancestor_patches: list[dict],
    new_patch: dict,
    timeout: float = 300,
) -> dict[str, Any]:
    """
    在隔离沙盒中评估一个候选节点的性能。

    执行步骤：
    1. 将项目复制到临时目录
    2. 按顺序应用 ancestor_patches（父节点累积改动）
    3. 应用 new_patch（本次新改动）
    4. 运行 main.py，解析最后一行 __METRICS__:<json>
    5. 计算 score（单指标直接取值，多指标求和）

    Args:
        project_root:     项目根目录绝对路径。
        ancestor_patches: 父节点累积 patch 列表（顺序应用）。
        new_patch:        本次新增 patch {target_file, target_type, target_name, code}。
        timeout:          main.py 执行超时秒数。

    Returns:
        {
            "ok":         bool,            # False = 注入失败或执行失败
            "score":      float,           # -999 表示失败
            "metrics":    dict,            # __METRICS__ 解析结果
            "output_log": str,             # main.py stdout
            "error":      str,             # 失败原因（ok=False 时）
            "all_patches": list[dict],     # ancestor_patches + new_patch
        }
    """
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_sandbox_sync,
                                      project_root, ancestor_patches, new_patch, timeout)


def _run_sandbox_sync(
    project_root: str,
    ancestor_patches: list[dict],
    new_patch: dict,
    timeout: float,
) -> dict[str, Any]:
    """同步沙盒执行（在 executor 线程中运行）。"""
    trial_id = str(uuid.uuid4())[:8]
    all_patches = list(ancestor_patches) + [new_patch]
    ignore_list = ["__pycache__", "*.pyc", ".git", "venv",
                   "data", "datasets", "logs", ".idea", "mcts_history"]

    with tempfile.TemporaryDirectory(prefix=f"mcts_{trial_id}_") as tmp:
        sandbox_root = os.path.join(tmp, "sandbox")
        ignore_fn = shutil.ignore_patterns(*ignore_list)

        try:
            shutil.copytree(project_root, sandbox_root, ignore=ignore_fn)
        except Exception as e:
            return _fail(all_patches, f"copytree failed: {e}")

        # 按顺序重放所有 patch（祖先 + 新增）
        for patch in all_patches:
            rel = patch["target_file"].lstrip("/").replace("\\", "/")
            target_path = os.path.join(sandbox_root, rel)
            ok, msg = CodeInjector.replace_code_block(
                target_path,
                patch["target_type"],
                patch["target_name"],
                patch["code"],
            )
            if not ok:
                return _fail(all_patches, f"inject failed on {rel}: {msg}")

        # 执行 main.py
        try:
            proc = subprocess.run(
                [sys.executable, "main.py"],
                cwd=sandbox_root,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = proc.stdout
        except subprocess.TimeoutExpired:
            return _fail(all_patches, "timeout")
        except Exception as e:
            return _fail(all_patches, str(e))

        # 解析 __METRICS__
        _PREFIX = "__METRICS__:"
        for line in reversed(output.splitlines()):
            if line.startswith(_PREFIX):
                try:
                    metrics = json.loads(line[len(_PREFIX):])
                    score = _score_fn(metrics)
                    return {
                        "ok": True,
                        "score": score,
                        "metrics": metrics,
                        "output_log": output,
                        "error": "",
                        "all_patches": all_patches,
                    }
                except Exception as e:
                    return _fail(all_patches, f"metrics parse error: {e}", output)

        return _fail(all_patches, "no __METRICS__ line in stdout", output)


def _score_fn(metrics: dict) -> float:
    if len(metrics) == 1:
        return next(iter(metrics.values()))
    return sum(metrics.values())


def _fail(
    all_patches: list[dict],
    error: str,
    output_log: str = "",
) -> dict[str, Any]:
    return {
        "ok": False,
        "score": -999.0,
        "metrics": {},
        "output_log": output_log,
        "error": error,
        "all_patches": all_patches,
    }
