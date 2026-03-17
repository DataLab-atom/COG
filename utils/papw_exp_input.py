"""papw_exp_input — experimenting 异步输入源

异步输入源，供 input/*.json 配置引用（type: "input"）。
封装脚本执行等需要异步 I/O 的操作，不含 LLM 调用。

函数列表：
    papw_exp_run_script — 在子进程中执行 Python 脚本
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from utils import ToolResult

_executor = ThreadPoolExecutor(max_workers=2)


class PapwExpRunScriptResult(ToolResult):
    stdout: str
    stderr: str
    returncode: int
    success: bool


async def papw_exp_run_script(
    script_content: str,
    work_dir: str,
    script_name: str = "tmp_script.py",
    timeout: int = 300,
) -> PapwExpRunScriptResult:
    """在子进程中执行 Python 脚本，返回执行结果。

    Args:
        script_content: Python 脚本内容。
        work_dir:       脚本执行的工作目录。
        script_name:    脚本文件名。
        timeout:        执行超时（秒）。
    """
    os.makedirs(work_dir, exist_ok=True)
    script_path = os.path.join(work_dir, script_name)
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script_content)

    def _run():
        try:
            result = subprocess.run(
                ["python", script_path],
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "stdout": result.stdout[:10000],
                "stderr": result.stderr[:5000],
                "returncode": result.returncode,
                "success": result.returncode == 0,
            }
        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": f"TimeoutExpired: script exceeded {timeout}s",
                "returncode": -1,
                "success": False,
            }
        except Exception as e:
            return {
                "stdout": "",
                "stderr": str(e),
                "returncode": -1,
                "success": False,
            }

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, _run)

    # 保存执行结果到 work_dir
    result_path = os.path.join(work_dir, "..", "results", "execution_result.json")
    os.makedirs(os.path.dirname(result_path), exist_ok=True)
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    return PapwExpRunScriptResult(**result)
