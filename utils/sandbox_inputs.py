"""
sandbox_inputs — 沙箱会话 async 输入函数

所有函数为 async，涉及文件系统或子进程 I/O，返回 ToolResult 子类。

公开接口：
    FailureReport             — 结构化失败报告（嵌套在 RunResult 中）
    InitSandboxResult         — init_sandbox_session() 返回值
    InstallInSandboxResult    — install_in_sandbox() 返回值
    WriteTreeResult           — write_tree_to_dir() 返回值
    RunResult                 — run_sandbox_entry() 返回值
    CollectOutputsResult      — collect_output_files() 返回值
    FinalizeSessionResult     — finalize_sandbox_session() 返回值
    SandboxConcurrentResult   — sandbox_session_concurrent() 返回值

    init_sandbox_session()         — 复制项目到临时目录并创建 venv
    install_in_sandbox()           — 在沙箱 venv 中安装包列表
    write_tree_to_dir()            — 将架构树 changed 节点写入沙箱目录（增量）
    run_sandbox_entry()            — 执行入口脚本，返回结构化 RunResult（含自动重试）
    collect_output_files()         — 按 glob 模式收集运行产生的文件
    finalize_sandbox_session()     — 清理沙箱（删除 venv / __pycache__），返回最终路径
    sandbox_session_concurrent()   — 并发沙箱会话：初始化一次，并发处理多路 patch 评估请求
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel

import logging

from utils import ToolResult
from utils.sandbox import _apply_patch, _DEFAULT_IGNORE, _parse_metrics, _run_script
from utils.text_utils import _strip_fences

logger = logging.getLogger(__name__)


# ── 结构化失败报告 ─────────────────────────────────────────────────────────────

class FailureReport(BaseModel):
    """结构化失败报告，嵌套在 RunResult.failure 中。"""
    model_config = {"frozen": True}

    stage: str        # 失败阶段：write / run / parse
    error_type: str   # TIMEOUT / SYNTAX_ERROR / RUNTIME_ERROR /
                      # IMPORT_ERROR / NO_METRICS / OS_ERROR
    message: str      # 人可读的一句话描述
    traceback: str = ""   # 完整 Python traceback（来自 stderr）
    stderr: str = ""      # 完整 stderr
    context: dict = {}    # 结构化上下文，随 error_type 不同


# ── 返回值类型定义 ─────────────────────────────────────────────────────────────

class InitSandboxResult(ToolResult):
    """init_sandbox_session() 的返回值。"""
    sandbox_root: str    # 沙箱目录绝对路径（mkdtemp 创建，需手动清理）
    venv_python: str     # 沙箱 venv 内 python 可执行文件路径


class InstallInSandboxResult(ToolResult):
    """install_in_sandbox() 的返回值。"""
    ok: bool
    installed: list[str]   # 成功安装的包
    failed: list[str]      # 安装失败的包
    stdout: str
    stderr: str


class WriteTreeResult(ToolResult):
    """write_tree_to_dir() 的返回值。"""
    ok: bool
    files_written: list[str]   # 实际写入的文件路径（相对 sandbox_root）
    error: str                 # 失败时的错误描述


class RunResult(ToolResult):
    """run_sandbox_entry() 的返回值。"""
    ok: bool
    score: float
    metrics: dict
    output_log: str          # 完整 stdout
    stderr_log: str          # 完整 stderr
    failure: FailureReport | None = None   # 成功时为 None


class CollectOutputsResult(ToolResult):
    """collect_output_files() 的返回值。"""
    files: list[dict]   # [{rel_path, abs_path, size}]


class FinalizeSessionResult(ToolResult):
    """finalize_sandbox_session() 的返回值。"""
    final_code_path: str   # 清理后的沙箱目录路径
    final_tree: list[dict]  # 最新架构树（原样透传）


# ── 内部辅助 ──────────────────────────────────────────────────────────────────


def _detect_error_type(stderr: str, exit_code: int | None) -> tuple[str, dict]:
    """从 stderr 识别错误类型，返回 (error_type, context)。"""
    # IMPORT_ERROR
    m = re.search(r"ModuleNotFoundError: No module named '([^']+)'", stderr)
    if m:
        missing = m.group(1).split(".")[0]
        return "IMPORT_ERROR", {
            "missing_module": missing,
            "suggested_install": f"pip install {missing}",
        }
    # SYNTAX_ERROR
    m = re.search(r'File "([^"]+)", line (\d+)\n.*\nSyntaxError: (.+)', stderr)
    if m:
        return "SYNTAX_ERROR", {
            "file": m.group(1),
            "line": int(m.group(2)),
            "message": m.group(3),
        }
    # RUNTIME_ERROR（通用）
    if stderr.strip():
        last_lines = stderr.strip().splitlines()[-5:]
        return "RUNTIME_ERROR", {
            "exit_code": exit_code,
            "last_stderr_lines": last_lines,
        }
    return "RUNTIME_ERROR", {"exit_code": exit_code}


def _extract_traceback(stderr: str) -> str:
    """从 stderr 中提取 Traceback 块。"""
    idx = stderr.find("Traceback (most recent call last):")
    return stderr[idx:].strip() if idx >= 0 else ""


# ── 公开接口 ──────────────────────────────────────────────────────────────────

async def init_sandbox_session(
    project_root: str,
    ignore_dirs: list[str] | None = None,
) -> InitSandboxResult:
    """复制项目到临时目录并在其中创建隔离 venv。

    使用 mkdtemp（不自动清理），调用方须在 finalize_sandbox_session 中清理。

    Args:
        project_root: 项目根目录绝对路径。
        ignore_dirs:  复制时忽略的 glob 模式列表，默认忽略常见缓存目录。

    Returns:
        InitSandboxResult:
            .sandbox_root  沙箱目录绝对路径
            .venv_python   沙箱 venv 的 python 可执行路径
    """
    import time as _time
    t0 = _time.perf_counter()
    ignore = ignore_dirs or _DEFAULT_IGNORE
    _sandbox_tmp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".sandbox_tmp")
    os.makedirs(_sandbox_tmp, exist_ok=True)
    sandbox_root = tempfile.mkdtemp(prefix="sandbox_session_", dir=_sandbox_tmp)
    logger.info("沙箱会话初始化  project=%s  sandbox=%s", project_root, sandbox_root)

    # 复制项目
    dest = os.path.join(sandbox_root, "code")
    await asyncio.to_thread(
        shutil.copytree,
        project_root,
        dest,
        ignore=shutil.ignore_patterns(*ignore),
    )

    # 创建隔离 venv
    venv_dir = os.path.join(sandbox_root, ".venv")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "venv", venv_dir,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()

    # venv python 路径
    venv_python = os.path.join(venv_dir, "bin", "python")
    if not os.path.exists(venv_python):
        venv_python = os.path.join(venv_dir, "Scripts", "python.exe")  # Windows

    elapsed = _time.perf_counter() - t0
    logger.info("沙箱会话就绪  sandbox=%s  venv=%s  耗时=%.1fs", sandbox_root, venv_python, elapsed)

    return InitSandboxResult(
        sandbox_root=os.path.join(sandbox_root, "code"),
        venv_python=venv_python,
    )


async def install_in_sandbox(
    packages: list[str],
    venv_python: str,
    timeout: float = 120.0,
) -> InstallInSandboxResult:
    """在沙箱 venv 中安装指定包列表。

    逐包尝试安装，部分失败不中止整体流程。

    Args:
        packages:    要安装的包名列表。
        venv_python: 沙箱 venv 的 python 可执行路径（来自 InitSandboxResult）。
        timeout:     单次 pip install 超时秒数，默认 120。

    Returns:
        InstallInSandboxResult:
            .ok        所有包均安装成功则 True
            .installed 成功安装的包列表
            .failed    安装失败的包列表
            .stdout / .stderr  最后一次 pip 调用的输出
    """
    if not packages:
        return InstallInSandboxResult(
            ok=True, installed=[], failed=[], stdout="", stderr="",
        )

    logger.info("沙箱依赖安装  packages=%d  names=%s", len(packages), packages[:10])

    def _install(pkgs: list[str]) -> tuple[int, str, str]:
        import subprocess
        proc = subprocess.run(
            [venv_python, "-m", "pip", "install", *pkgs, "--quiet"],
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr

    installed: list[str] = []
    failed: list[str] = []
    last_out = last_err = ""

    # 先尝试批量安装，失败则逐包重试
    code, last_out, last_err = await asyncio.to_thread(_install, packages)
    if code == 0:
        installed = list(packages)
    else:
        logger.warning("沙箱批量安装失败，逐包重试  stderr=%s", last_err[:300])
        for pkg in packages:
            code, out, err = await asyncio.to_thread(_install, [pkg])
            if code == 0:
                installed.append(pkg)
            else:
                failed.append(pkg)
            last_out, last_err = out, err

    if failed:
        logger.warning("沙箱依赖安装部分失败  installed=%s  failed=%s", installed, failed)
    else:
        logger.info("沙箱依赖安装完成  installed=%d", len(installed))

    return InstallInSandboxResult(
        ok=len(failed) == 0,
        installed=installed,
        failed=failed,
        stdout=last_out,
        stderr=last_err,
    )


async def write_tree_to_dir(
    tree: list[dict],
    sandbox_root: str,
) -> WriteTreeResult:
    """将架构树中标记 changed=True 的节点写入沙箱目录（增量更新）。

    对 action="create" 节点调用 _append_code_block；
    对 action="rewrite"（默认）节点调用 _replace_code_block。
    写入成功后将节点的 changed 标记重置为 False。

    Args:
        tree:         架构树节点列表（原地修改 changed 标记）。
        sandbox_root: 沙箱代码目录绝对路径。

    Returns:
        WriteTreeResult:
            .ok            是否全部写入成功
            .files_written 写入的相对路径列表
            .error         首个失败的错误描述
    """
    from utils.text_utils import _replace_code_block, _append_code_block

    written: list[str] = []

    def _do_write() -> tuple[bool, str]:
        for node in tree:
            if not node.get("changed"):
                continue
            rel = node.get("target_file", "").lstrip("/").replace("\\", "/")
            abs_path = os.path.join(sandbox_root, rel)
            action = node.get("action", "rewrite")

            if action == "create":
                ok, msg = _append_code_block(abs_path, node.get("code", ""))
            else:
                ok, msg = _replace_code_block(
                    abs_path,
                    node.get("target_type", "function"),
                    node.get("target_name", ""),
                    node.get("code", ""),
                )

            if not ok:
                return False, f"{rel}: {msg}"
            node["changed"] = False
            written.append(rel)
        return True, ""

    ok, error = await asyncio.to_thread(_do_write)
    return WriteTreeResult(ok=ok, files_written=written, error=error)


async def run_sandbox_entry(
    sandbox_root: str,
    venv_python: str,
    entry_point: str = "main.py",
    timeout: float = 300.0,
) -> RunResult:
    """在沙箱目录中执行入口脚本，返回结构化 RunResult。

    自动捕获 stderr，识别 IMPORT_ERROR 后尝试在 venv 中安装缺失包并重试一次。

    Args:
        sandbox_root: 沙箱代码目录绝对路径。
        venv_python:  沙箱 venv 的 python 可执行路径。
        entry_point:  相对于 sandbox_root 的入口脚本，默认 main.py。
        timeout:      执行超时秒数，默认 300。

    Returns:
        RunResult: 含 ok / score / metrics / output_log / stderr_log / failure
    """
    async def _exec() -> tuple[str, str, int | None]:
        proc = await asyncio.create_subprocess_exec(
            venv_python, entry_point,
            cwd=sandbox_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.communicate()
            except Exception as e:
                # 进程可能已自行退出，kill 失败属于正常清理场景
                logger.warning("沙箱进程 kill 失败（可能已退出）: %s", e)
            raise
        return out_b.decode(), err_b.decode(), proc.returncode

    def _build_failure(stage: str, stderr: str, exit_code: int | None,
                       message: str = "") -> FailureReport:
        error_type, context = _detect_error_type(stderr, exit_code)
        return FailureReport(
            stage=stage,
            error_type=error_type,
            message=message or f"{error_type} during {stage}",
            traceback=_extract_traceback(stderr),
            stderr=stderr,
            context=context,
        )

    # 首次执行
    try:
        stdout, stderr, exit_code = await _exec()
    except asyncio.TimeoutError:
        return RunResult(
            ok=False, score=-999.0, metrics={},
            output_log="", stderr_log="",
            failure=FailureReport(
                stage="run", error_type="TIMEOUT",
                message=f"执行超时（>{timeout}s）",
                context={"timeout_limit": timeout},
            ),
        )

    # 检查 IMPORT_ERROR → 自动安装后重试一次
    if exit_code != 0:
        error_type, context = _detect_error_type(stderr, exit_code)
        if error_type == "IMPORT_ERROR":
            missing = context.get("missing_module", "")
            if missing:
                await install_in_sandbox([missing], venv_python, timeout=60.0)
                try:
                    stdout, stderr, exit_code = await _exec()
                except asyncio.TimeoutError:
                    pass  # 重试也超时，继续走下面的解析逻辑

    # 解析 __METRICS__
    if exit_code != 0 and not stdout.strip():
        failure = _build_failure("run", stderr, exit_code)
        return RunResult(
            ok=False, score=-999.0, metrics={},
            output_log=stdout, stderr_log=stderr, failure=failure,
        )

    try:
        result = _parse_metrics(stdout)
    except Exception as e:
        failure = FailureReport(
            stage="parse", error_type="NO_METRICS",
            message=f"__METRICS__ 解析失败: {e}",
            stderr=stderr,
            context={"stdout_tail": stdout[-500:] if stdout else ""},
        )
        return RunResult(
            ok=False, score=-999.0, metrics={},
            output_log=stdout, stderr_log=stderr, failure=failure,
        )

    if result is None:
        failure = FailureReport(
            stage="parse", error_type="NO_METRICS",
            message="stdout 中未找到 __METRICS__ 行",
            stderr=stderr,
            context={"stdout_tail": stdout[-500:] if stdout else ""},
        )
        return RunResult(
            ok=False, score=-999.0, metrics={},
            output_log=stdout, stderr_log=stderr, failure=failure,
        )

    score, metrics = result

    # exit_code != 0 但有 __METRICS__ → 部分成功，仍返回 ok=True 但记录 stderr
    return RunResult(
        ok=True, score=score, metrics=metrics,
        output_log=stdout, stderr_log=stderr,
        failure=None,
    )


async def collect_output_files(
    sandbox_root: str,
    patterns: list[str] | None = None,
) -> CollectOutputsResult:
    """按 glob 模式收集沙箱运行后产生的文件。

    默认排除 .py 和 .pyc 文件，只收集运行产物。
    空 patterns 时收集 sandbox_root 下所有非 .py/.pyc 文件。

    Args:
        sandbox_root: 沙箱代码目录绝对路径。
        patterns:     glob 模式列表（相对 sandbox_root），如 ["output/**", "*.csv"]。

    Returns:
        CollectOutputsResult:
            .files  [{rel_path, abs_path, size}] 列表
    """
    _exclude = {".py", ".pyc"}

    def _collect() -> list[dict]:
        results: list[dict] = []
        if patterns:
            import glob as _glob
            for pat in patterns:
                for abs_path in _glob.glob(
                    os.path.join(sandbox_root, pat), recursive=True
                ):
                    if os.path.isfile(abs_path):
                        ext = os.path.splitext(abs_path)[1]
                        if ext not in _exclude:
                            rel = os.path.relpath(abs_path, sandbox_root)
                            results.append({
                                "rel_path": rel,
                                "abs_path": abs_path,
                                "size": os.path.getsize(abs_path),
                            })
        else:
            for root, _, files in os.walk(sandbox_root):
                for fname in files:
                    ext = os.path.splitext(fname)[1]
                    if ext in _exclude:
                        continue
                    abs_path = os.path.join(root, fname)
                    rel = os.path.relpath(abs_path, sandbox_root)
                    results.append({
                        "rel_path": rel,
                        "abs_path": abs_path,
                        "size": os.path.getsize(abs_path),
                    })
        return results

    files = await asyncio.to_thread(_collect)
    return CollectOutputsResult(files=files)


class SandboxConcurrentResult(ToolResult):
    """sandbox_session_concurrent() 的返回值。"""
    ok: bool
    total_evals: int          # 实际完成的评估次数
    final_code_path: str      # 基准代码目录路径（已清理 .venv / __pycache__）
    final_tree: list[dict]    # 初始架构树（原样透传，调用方自行维护最新状态）


async def finalize_sandbox_session(
    sandbox_root: str,
    final_tree: list[dict],
    keep_venv: bool = False,
) -> FinalizeSessionResult:
    """清理沙箱临时文件，返回最终代码路径和架构树。

    默认删除 .venv 目录和所有 __pycache__（体积大，无需保留）。
    源代码目录保留，调用方可继续访问。

    Args:
        sandbox_root: 沙箱代码目录绝对路径（init_sandbox_session 返回的 code/ 子目录）。
        final_tree:   最新架构树，原样透传。
        keep_venv:    是否保留 .venv 目录，默认 False（删除）。

    Returns:
        FinalizeSessionResult:
            .final_code_path  清理后的代码目录路径（即 sandbox_root）
            .final_tree       最新架构树
    """
    # sandbox_root 是 <tmp>/code/，其父目录含 .venv
    parent = os.path.dirname(sandbox_root)

    def _cleanup() -> None:
        # 删除 .venv（除非 keep_venv）
        if not keep_venv:
            venv_dir = os.path.join(parent, ".venv")
            if os.path.isdir(venv_dir):
                shutil.rmtree(venv_dir, ignore_errors=True)
        # 删除所有 __pycache__
        for root, dirs, _ in os.walk(sandbox_root):
            for d in dirs:
                if d == "__pycache__":
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)
            dirs[:] = [d for d in dirs if d != "__pycache__"]

    await asyncio.to_thread(_cleanup)
    return FinalizeSessionResult(
        final_code_path=sandbox_root,
        final_tree=final_tree,
    )


async def sandbox_session_concurrent(
    project_root: str,
    tree: list[dict],
    input_channel_id: str,
    output_channel_id: str,
    max_workers: int = 4,
    entry_point: str = "main.py",
    timeout: float = 300.0,
    base_patches: list[dict] | None = None,
) -> SandboxConcurrentResult:
    """并发沙箱会话：初始化一次共享环境，并发处理多路 patch 评估请求。

    协议：
        调用方向 input_channel_id 推送 {"request_id": str, "patches": list[dict]}，
        函数将结果写入 output_channel_id：
            {"request_id": str, "ok": bool, "score": float,
             "metrics": dict, "output_log": str, "stderr_log": str,
             "failure": dict | None}
        关闭 input_channel_id 后，函数等待所有飞行中的任务结束，清理并返回。

    设计要点：
        - 共享 venv：依赖仅安装一次，所有并发 eval 复用同一 venv_python。
        - 隔离代码目录：每个 eval 将基准代码 copytree 到独立 temp 目录，互不干扰。
        - 背压控制：asyncio.Semaphore(max_workers) 限制同时运行的子进程数量。
        - patches 语义：调用方须传入完整 patch 链（ancestor + new），直接应用到初始 tree。
        - base_patches：若提供，在写入基准目录前先将这些 patch 应用到 tree，使后续所有
          评估以已打过 patch 的状态为起点，调用方可用 ancestor_patches=[] 发起请求。

    Args:
        project_root:      项目根目录绝对路径（用于 init_sandbox_session）。
        tree:              初始架构树节点列表（每个 eval 从此树出发应用 patches）。
        input_channel_id:  输入通道 ID，接收 {request_id, patches} 评估请求。
        output_channel_id: 输出通道 ID，写入每个请求的评估结果。
        max_workers:       最大并发 eval 数量，默认 4。
        entry_point:       相对于沙箱代码目录的入口脚本，默认 main.py。
        timeout:           单次 run_sandbox_entry 超时秒数，默认 300。
        base_patches:      可选的基准 patch 列表，启动时应用到 tree 作为所有评估的起点。
                           典型用途：传入上一阶段（如 MCTS）的 best_node.patches，
                           使新会话直接以最优状态为基准，调用方无需在每次请求中重复传递。

    Returns:
        SandboxConcurrentResult:
            .ok              全程无致命错误则 True
            .total_evals     完成的评估次数
            .final_code_path 基准代码目录路径（已清理）
            .final_tree      初始 tree 原样透传
    """
    from utils import receive_from_channel, send_to_channel, ChannelClosedError
    from utils.sandbox_tools import decode_requirements, apply_patches_to_tree

    # ── 1. 初始化共享沙箱 ──────────────────────────────────────────────────────
    init_result = await init_sandbox_session(project_root)
    sandbox_root = init_result.sandbox_root
    venv_python = init_result.venv_python

    # 写入基准代码（标记所有节点 changed=True 以确保全量写入）
    # 若提供 base_patches，先将其应用到 tree，使基准目录已包含指定改动
    base_tree = [dict(node, changed=True) for node in tree]
    if base_patches:
        patched = apply_patches_to_tree(base_tree, base_patches)
        base_tree = [dict(node, changed=True) for node in patched.tree]
    await write_tree_to_dir(base_tree, sandbox_root)

    # 安装依赖（仅一次）：优先读 requirements.txt，其次 AST 解析兜底
    def _read_requirements() -> list[str]:
        req_path = os.path.join(sandbox_root, "requirements.txt")
        if os.path.isfile(req_path):
            lines = Path(req_path).read_text(encoding="utf-8").splitlines()
            return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]
        return []

    packages = await asyncio.to_thread(_read_requirements)
    if not packages:
        packages = decode_requirements(tree).packages
    if packages:
        await install_in_sandbox(packages, venv_python)

    # ── 2. 并发评估循环 ────────────────────────────────────────────────────────
    semaphore = asyncio.Semaphore(max_workers)
    pending: set[asyncio.Task] = set()
    total_evals = 0

    async def _run_isolated(
        code_dir: str, py: str, ep: str, t: float
    ) -> RunResult:
        """在隔离目录中运行入口脚本，不做 IMPORT_ERROR 自动安装（避免并发写共享 venv）。"""
        from utils.sandbox import _run_script, _parse_metrics
        try:
            stdout, stderr = await _run_script(code_dir, ep, t)
        except asyncio.TimeoutError:
            return RunResult(
                ok=False, score=-999.0, metrics={},
                output_log="", stderr_log="",
                failure=FailureReport(
                    stage="run", error_type="TIMEOUT",
                    message=f"执行超时（>{t}s）",
                    context={"timeout_limit": t},
                ),
            )
        try:
            result = _parse_metrics(stdout)
        except Exception as e:
            return RunResult(
                ok=False, score=-999.0, metrics={},
                output_log=stdout, stderr_log=stderr,
                failure=FailureReport(
                    stage="parse", error_type="NO_METRICS",
                    message=f"__METRICS__ 解析失败: {e}",
                    stderr=stderr, context={},
                ),
            )
        if result is None:
            return RunResult(
                ok=False, score=-999.0, metrics={},
                output_log=stdout, stderr_log=stderr,
                failure=FailureReport(
                    stage="parse", error_type="NO_METRICS",
                    message="stdout 中未找到 __METRICS__ 行",
                    stderr=stderr, context={},
                ),
            )
        score, metrics = result
        return RunResult(ok=True, score=score, metrics=metrics,
                         output_log=stdout, stderr_log=stderr)

    async def _eval_one(
        request_id: str,
        patches: list[dict],
        reply_channel_id: str | None = None,
    ) -> None:
        """隔离沙箱中应用 patches 并运行，结果写入 reply_channel_id 或 output_channel_id。

        若请求携带 reply_channel_id，则将结果写入该独占通道（调用方单独监听），
        否则写入共享的 output_channel_id。
        """
        import time as _time
        nonlocal total_evals
        eval_tmp: str | None = None
        dest_ch = reply_channel_id or output_channel_id
        t0 = _time.perf_counter()
        async with semaphore:
            eval_tmp = tempfile.mkdtemp(prefix=f"eval_{request_id[:8]}_")
            eval_code_dir = os.path.join(eval_tmp, "code")
            try:
                # 复制基准代码到隔离目录
                await asyncio.to_thread(shutil.copytree, sandbox_root, eval_code_dir)

                # 将 patches 应用到 tree 副本并写入隔离目录
                patched = apply_patches_to_tree(tree, patches)
                await write_tree_to_dir(patched.tree, eval_code_dir)

                # 运行（复用共享 venv，禁用自动安装以避免并发写 venv）
                run_result = await _run_isolated(
                    eval_code_dir, venv_python, entry_point, timeout
                )
                total_evals += 1
                elapsed = _time.perf_counter() - t0
                logger.info(
                    "沙箱并发评估完成  request=%s  ok=%s  score=%.4f  耗时=%.1fs  total_evals=%d",
                    request_id, run_result.ok, run_result.score, elapsed, total_evals,
                )
                if not run_result.ok and run_result.failure:
                    logger.warning(
                        "沙箱并发评估失败详情  request=%s  stage=%s  error_type=%s  message=%s",
                        request_id, run_result.failure.stage,
                        run_result.failure.error_type, run_result.failure.message,
                    )

                failure_dict = (
                    run_result.failure.model_dump()
                    if run_result.failure is not None
                    else None
                )
                await send_to_channel(dest_ch, {
                    "request_id": request_id,
                    "ok":         run_result.ok,
                    "score":      run_result.score,
                    "metrics":    run_result.metrics,
                    "output_log": run_result.output_log,
                    "stderr_log": run_result.stderr_log,
                    "failure":    failure_dict,
                })
            except Exception as exc:
                # 评估失败时仍向目标通道发送错误结果，不中断整个会话
                total_evals += 1
                logger.error("沙箱并发评估异常  request=%s  error=%s", request_id, exc, exc_info=True)
                await send_to_channel(dest_ch, {
                    "request_id": request_id,
                    "ok":         False,
                    "score":      -999.0,
                    "metrics":    {},
                    "output_log": "",
                    "stderr_log": str(exc),
                    "failure":    {
                        "stage": "eval", "error_type": "RUNTIME_ERROR",
                        "message": str(exc), "traceback": "", "stderr": "", "context": {},
                    },
                })
            finally:
                if eval_tmp is not None:
                    await asyncio.to_thread(shutil.rmtree, eval_tmp, True)

    # 持续读取输入通道，直到通道关闭
    try:
        while True:
            data = await receive_from_channel(input_channel_id)
            task = asyncio.create_task(
                _eval_one(
                    data["request_id"],
                    data.get("patches", []),
                    data.get("reply_channel_id"),
                )
            )
            pending.add(task)
            task.add_done_callback(pending.discard)
    except ChannelClosedError:
        pass

    # 等待所有飞行中的评估完成
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    # ── 3. 清理共享沙箱 ────────────────────────────────────────────────────────
    finalize = await finalize_sandbox_session(sandbox_root, tree)

    return SandboxConcurrentResult(
        ok=True,
        total_evals=total_evals,
        final_code_path=finalize.final_code_path,
        final_tree=tree,
    )
