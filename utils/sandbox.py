"""
sandbox — 通用隔离沙盒执行器

将 Python 项目复制到临时目录、应用 patch 链、运行入口脚本、解析 __METRICS__ 输出。

公开接口：
    SandboxResult   — sandbox_run() 返回值
    sandbox_run()   — 通用沙盒执行（供 input/sandbox_run.json 引用）

内部工具（供 mcts/inputs.py 等复用）：
    _copy_project()  — 复制项目到临时目录，返回 sandbox_root
    _apply_patch()   — 应用单条 patch（支持 rewrite / create）
    _run_script()    — 异步执行入口脚本，返回 stdout
    _parse_metrics() — 解析 __METRICS__ 行，返回 (score, metrics) 或 None
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import uuid

from utils._base import ToolResult

logger = logging.getLogger(__name__)


_DEFAULT_IGNORE: list[str] = [
    "__pycache__", "*.pyc", ".git", "venv", ".venv",
    "logs", ".idea", "mcts_history",
    # 注意：不再忽略 data/datasets 目录，因为项目 main.py 需要加载真实数据文件。
    # 若数据量过大导致复制过慢，可通过 ignore_dirs 参数显式指定忽略。
]

_METRICS_PREFIX = "__METRICS__:"


class SandboxResult(ToolResult):
    """sandbox_run() 的返回值。"""
    ok: bool
    score: float
    metrics: dict
    output_log: str
    stderr_log: str
    error: str
    sandbox_dir: str


# ── 内部工具函数 ─────────────────────────────────────────────────────────────────

def _copy_project(project_root: str, tmp_dir: str, ignore_list: list[str]) -> str:
    """将 project_root 复制到 tmp_dir/sandbox，返回 sandbox_root 路径。"""
    sandbox_root = os.path.join(tmp_dir, "sandbox")
    shutil.copytree(project_root, sandbox_root, ignore=shutil.ignore_patterns(*ignore_list))
    return sandbox_root


def _apply_patch(sandbox_root: str, patch: dict) -> tuple[bool, str]:
    """应用单条 patch 到沙盒目录。

    action="rewrite"（默认）：替换已有函数/类定义。
    action="create"：追加新代码块。
    """
    from utils.text_utils import _replace_code_block, _append_code_block

    rel = patch["target_file"].lstrip("/").replace("\\", "/")
    target_path = os.path.join(sandbox_root, rel)
    action = patch.get("action", "rewrite")
    if action == "create":
        return _append_code_block(target_path, patch["code"])
    return _replace_code_block(
        target_path,
        patch["target_type"],
        patch["target_name"],
        patch["code"],
    )


async def _run_script(sandbox_root: str, entry_point: str, timeout: float) -> tuple[str, str]:
    """在沙盒目录中异步执行入口脚本，返回 (stdout, stderr) 元组。

    超时时 kill 进程并抛出 asyncio.TimeoutError。
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable, entry_point,
        cwd=sandbox_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.communicate()
        except Exception as e:
            # 进程可能已自行退出，kill 失败属于正常清理场景，记录日志后继续抛出超时异常
            logger.warning("沙盒进程 kill 失败（可能已退出）: %s", e)
        raise
    return stdout_bytes.decode(), stderr_bytes.decode()


def _parse_metrics(output: str) -> tuple[float, dict] | None:
    """从 stdout 末尾向前扫描 __METRICS__:<json>，返回 (score, metrics)；未找到返回 None。

    json.JSONDecodeError 等异常会向上抛出，由调用方捕获。
    """
    for line in reversed(output.splitlines()):
        if line.startswith(_METRICS_PREFIX):
            metrics = json.loads(line[len(_METRICS_PREFIX):])
            score = next(iter(metrics.values())) if len(metrics) == 1 else sum(metrics.values())
            return score, metrics
    return None


# ── 公开接口 ──────────────────────────────────────────────────────────────────────

async def sandbox_run(
    project_root: str,
    patches: list[dict] | None = None,
    entry_point: str = "main.py",
    timeout: float = 300.0,
    ignore_dirs: list[str] | None = None,
    persist_dir: str | None = None,
) -> SandboxResult:
    """通用隔离沙盒执行器：复制项目 → 应用 patch 链 → 运行入口脚本 → 解析 __METRICS__ 返回评分。

    适用于任何需要在隔离环境中运行 Python 项目并获取量化评分的场景。
    项目约定：入口脚本须在 stdout 中输出至少一行 ``__METRICS__:<json>``，
    其中 <json> 为 ``{metric_name: value, ...}`` 格式的 JSON 对象。
    单指标直接取其值作为 score；多指标对所有值求和。

    patch 格式（每项）::

        {
            "target_file": "src/model.py",   # 相对项目根的路径
            "target_type": "function",        # "function" 或 "class"
            "target_name": "train_step",      # 要替换的函数名或类名
            "code":        "def train_step(...):\\n    ..."
        }

    Args:
        project_root: 项目根目录绝对路径。
        patches:      按顺序应用的 patch 列表，默认为空（净跑基线）。
        entry_point:  相对于项目根的入口脚本，默认 ``main.py``。
        timeout:      脚本执行超时秒数，默认 300。
        ignore_dirs:  复制时忽略的目录/文件名模式列表，默认为常见缓存目录。
        persist_dir:  若指定，沙盒执行完后将整个沙盒目录移动到该路径保留，而非删除。
                      调用方可在此目录中读取生成的文件（如 output/、图表等）。

    Returns:
        SandboxResult:
            .ok          是否成功（含沙盒执行 + 指标解析）
            .score       量化评分（失败时为 -999.0）
            .metrics     原始 __METRICS__ dict（失败时为空）
            .output_log  入口脚本 stdout 完整输出
            .error       失败原因（ok=True 时为空字符串）
            .sandbox_dir 沙盒目录路径（persist_dir 指定时为 persist_dir，否则为空字符串）

    Example:
        # 净跑基线（无 patch）
        result = await sandbox_run("/abs/path/to/project", timeout=120)

        # 应用 patch 并保留沙盒目录
        result = await sandbox_run(
            "/abs/path/to/project",
            patches=[{
                "target_file": "src/solver.py",
                "target_type": "function",
                "target_name": "solve",
                "code": "def solve(x):\\n    return x * 2",
            }],
            persist_dir="/tmp/my_experiment",
        )
        # result.sandbox_dir == "/tmp/my_experiment"
    """
    ignore_list = ignore_dirs or _DEFAULT_IGNORE
    import time as _time
    trial_id = str(uuid.uuid4())[:8]
    _patches = list(patches or [])

    def _make_fail(error: str, output_log: str = "", stderr_log: str = "") -> SandboxResult:
        logger.warning("沙箱执行失败  trial=%s  error=%s", trial_id, error)
        return SandboxResult(ok=False, score=-999.0, metrics={},
                             output_log=output_log, stderr_log=stderr_log, error=error,
                             sandbox_dir="")

    logger.info("沙箱启动  trial=%s  project=%s  patches=%d  entry=%s  timeout=%.0fs",
                trial_id, project_root, len(_patches), entry_point, timeout)
    t0 = _time.perf_counter()

    _sandbox_tmp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".sandbox_tmp")
    os.makedirs(_sandbox_tmp, exist_ok=True)
    tmp_obj = tempfile.TemporaryDirectory(prefix=f"sandbox_{trial_id}_", dir=_sandbox_tmp)
    tmp = tmp_obj.name
    try:
        try:
            sandbox_root = _copy_project(project_root, tmp, ignore_list)
        except Exception as e:
            return _make_fail(f"copytree failed: {e}")

        for i, patch in enumerate(_patches):
            ok, msg = _apply_patch(sandbox_root, patch)
            if not ok:
                rel = patch["target_file"].lstrip("/").replace("\\", "/")
                logger.warning("沙箱 patch 失败  trial=%s  patch=%d/%d  file=%s  msg=%s",
                               trial_id, i + 1, len(_patches), rel, msg)
                return _make_fail(f"inject failed on {rel}: {msg}")

        try:
            output, stderr = await _run_script(sandbox_root, entry_point, timeout)
        except asyncio.TimeoutError:
            logger.warning("沙箱超时  trial=%s  timeout=%.0fs", trial_id, timeout)
            return _make_fail("timeout")
        except Exception as e:
            return _make_fail(str(e))

        try:
            result = _parse_metrics(output)
        except Exception as e:
            return _make_fail(f"metrics parse error: {e}", output, stderr)

        if result is None:
            return _make_fail("no __METRICS__ line in stdout", output, stderr)
        score, metrics = result

        elapsed = _time.perf_counter() - t0
        logger.info("沙箱完成  trial=%s  score=%.4f  metrics=%s  耗时=%.1fs",
                     trial_id, score, json.dumps(metrics, default=str), elapsed)

        final_dir = ""
        if persist_dir:
            if os.path.exists(persist_dir):
                shutil.rmtree(persist_dir)
            shutil.copytree(sandbox_root, persist_dir)
            final_dir = persist_dir

        return SandboxResult(ok=True, score=score, metrics=metrics,
                             output_log=output, stderr_log=stderr, error="",
                             sandbox_dir=final_dir)
    finally:
        tmp_obj.cleanup()
