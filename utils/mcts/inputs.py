"""
mcts_inputs — MCTS 异步 Input 函数

供 input/*.json 配置引用（type: "input", async: true）。

函数列表：
    mcts_sandbox              — 沙盒评估：重放累积 patch 链 → 运行 main.py → 解析 __METRICS__
    mcts_init                 — 基线评估：在隔离沙盒中运行 main.py（无 patch），获取初始分数
    mcts_gate                 — Human Gate：阻塞等待外部通过 channel 注入决策
    start_sandbox_session     — 后台启动共享沙箱会话，立即返回通道 ID
    mcts_sandbox_via_session  — 通过共享沙箱会话评估候选 patch（使用独占 reply_channel_id 路由）
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from typing import Any

from utils import ToolResult

logger = logging.getLogger(__name__)
from utils.sandbox import _copy_project, _apply_patch, _run_script, _parse_metrics, _DEFAULT_IGNORE


class MctsSandboxResult(ToolResult):
    ok: bool
    score: float
    metrics: dict
    output_log: str
    stderr_log: str
    error: str
    all_patches: list[dict]


# ── 沙盒评估 ─────────────────────────────────────────────────────────────────

async def mcts_sandbox(
    project_root: str,
    ancestor_patches: list[dict],
    new_patches: list[dict],
    timeout: float = 300,
) -> MctsSandboxResult:
    """
    在隔离沙盒中评估一个候选节点的性能。

    执行步骤：
    1. 将项目复制到临时目录
    2. 按顺序应用 ancestor_patches（父节点累积改动）
    3. 按顺序应用 new_patches（本次原子改动列表）
    4. 运行 main.py，解析最后一行 __METRICS__:<json>
    5. 计算 score（单指标直接取值，多指标求和）

    Args:
        project_root:     项目根目录绝对路径。
        ancestor_patches: 父节点累积 patch 列表（顺序应用）。
        new_patches:      本次新增 patch 列表，每项含 action/target_file/target_type/target_name/code。
                          action="rewrite" 替换已有节点；action="create" 追加新节点。
        timeout:          main.py 执行超时秒数。
    """
    trial_id    = str(uuid.uuid4())[:8]
    all_patches = list(ancestor_patches) + list(new_patches)

    with tempfile.TemporaryDirectory(prefix=f"mcts_{trial_id}_") as tmp:
        try:
            sandbox_root = _copy_project(project_root, tmp, _DEFAULT_IGNORE)
        except Exception as e:
            return _fail(all_patches, f"copytree failed: {e}")

        # 按顺序重放所有 patch（祖先 + 新增），根据 action 选择注入方式
        for patch in all_patches:
            ok, msg = _apply_patch(sandbox_root, patch)
            if not ok:
                rel = patch["target_file"].lstrip("/").replace("\\", "/")
                return _fail(all_patches, f"inject failed on {rel}: {msg}")

        # 异步执行 main.py
        try:
            output, stderr = await _run_script(sandbox_root, "main.py", timeout)
        except asyncio.TimeoutError:
            return _fail(all_patches, "timeout")
        except Exception as e:
            return _fail(all_patches, str(e))

        # 解析 __METRICS__
        try:
            result = _parse_metrics(output)
        except Exception as e:
            return _fail(all_patches, f"metrics parse error: {e}", output, stderr)

        if result is None:
            return _fail(all_patches, "no __METRICS__ line in stdout", output, stderr)
        score, metrics = result
        return MctsSandboxResult(
            ok=True, score=score, metrics=metrics,
            output_log=output, stderr_log=stderr, error="", all_patches=all_patches,
        )


def _fail(
    all_patches: list[dict],
    error: str,
    output_log: str = "",
) -> MctsSandboxResult:
    return MctsSandboxResult(
        ok=False, score=-999.0, metrics={},
        output_log=output_log, error=error, all_patches=all_patches,
    )


# ── mcts_init ─────────────────────────────────────────────────────────────────

class MctsInitResult(ToolResult):
    ok:             bool
    baseline_score: float
    baseline_log:   str
    error:          str


async def mcts_init(
    project_root: str,
    timeout: float = 300.0,
) -> MctsInitResult:
    """
    在隔离沙盒中执行 main.py（不应用任何 patch），获取项目基线分数。

    内部调用通用 sandbox_run（无 patch），将结果字段映射为 MCTS 约定的
    baseline_score / baseline_log 命名，供 mcts_run.json 图中的下游步骤引用。

    Args:
        project_root: 项目根目录绝对路径。
        timeout:      main.py 执行超时秒数，默认 300。
    """
    from utils.sandbox import sandbox_run
    r = await sandbox_run(project_root=project_root, timeout=timeout)
    return MctsInitResult(
        ok=r.ok,
        baseline_score=r.score,
        baseline_log=r.output_log,
        error=r.error,
    )


# ── mcts_gate ─────────────────────────────────────────────────────────────────

class MctsGateResult(ToolResult):
    action:       str
    next_mode:    str
    selected_ids: list


async def mcts_gate(
    gate_channel_id: str,
    generation:      int,
    tree_text:       str,
    new_nodes:       list,
) -> MctsGateResult:
    """
    Human Gate：阻塞等待外部通过 channel 注入搜索决策。

    调用方须在图启动前创建通道 gate_channel_id，并在每代结束后通过
    ``send_to_channel(gate_channel_id, decision)`` 注入决策。
    """
    from utils import receive_from_channel

    top5 = sorted(new_nodes, key=lambda n: -n.get("score", -999) if isinstance(n, dict) else 0)[:5]
    print(f"\n{'═' * 62}")
    print(f"  MCTS Gate — Generation {generation}")
    print(f"{'─' * 62}")
    print(tree_text)
    if top5:
        print("\n  本代 Top 候选节点:")
        for i, n in enumerate(top5, 1):
            print(
                f"    #{i}  [{n.get('node_id')}]  "
                f"op={n.get('op', ''):<16}  "
                f"score={n.get('score', 'N/A')}"
            )
    else:
        print("\n  ⚠️  本代无有效候选节点。")
    print(f"{'─' * 62}")
    print("  等待决策... send_to_channel({action, next_mode, selected_ids})")
    print(f"{'═' * 62}")

    data = await receive_from_channel(gate_channel_id)
    return MctsGateResult(
        action=data.get("action", "continue"),
        next_mode=data.get("next_mode", "llm"),
        selected_ids=data.get("selected_ids") or [],
    )


# ── start_sandbox_session ─────────────────────────────────────────────────────

class StartSandboxResult(ToolResult):
    sandbox_in_ch:  str   # 共享沙箱会话的输入通道 ID（自动生成，唯一）
    sandbox_out_ch: str   # 共享沙箱会话的输出通道 ID（批量模式下使用）


async def start_sandbox_session(
    project_root:  str,
    tree:          list[dict],
    max_workers:   int        = 4,
    timeout:       float      = 300.0,
    base_patches:  list[dict] | None = None,
) -> StartSandboxResult:
    """后台启动并发沙箱会话，立即返回自动生成的通道 ID 供下游步骤使用。

    在 asyncio 后台 Task 中启动 sandbox_session_concurrent（初始化共享沙箱 + venv +
    安装依赖），调用方无需等待初始化完成即可向 sandbox_in_ch 发送请求——请求会在队列中
    等待，直到会话完成初始化后被处理。

    通道由本函数内部自动创建（UUID 命名，保证多实例并发时唯一性）；
    调用方在不再需要时通过 close_sandbox_session(sandbox_in_ch) 关闭会话。

    Args:
        project_root: 项目根目录绝对路径（用于复制项目 + 创建 venv）。
        tree:         初始架构树节点列表（含函数体代码）。
        max_workers:  最大并发评估数量，默认 4。
        timeout:      单次沙盒运行超时秒数，默认 300。
        base_patches: 可选的基准 patch 列表，启动时应用到 tree 作为所有评估的起点。
                      典型用途：传入上一阶段（如 MCTS）的 best_node.patches，
                      使新会话直接以最优状态为基准，调用方无需在每次请求中重复传递。

    Returns:
        StartSandboxResult:
            .sandbox_in_ch   输入通道 ID（调用方用于发送评估请求 / 关闭会话）
            .sandbox_out_ch  输出通道 ID（批量模式下接收结果；独占 reply_channel 模式下不使用）
    """
    from utils import create_channel
    from utils.sandbox_inputs import sandbox_session_concurrent

    run_id         = uuid.uuid4().hex[:12]
    sandbox_in_ch  = f"sandbox_in_{run_id}"
    sandbox_out_ch = f"sandbox_out_{run_id}"

    create_channel(sandbox_in_ch)
    create_channel(sandbox_out_ch)

    # 后台启动，不等待初始化完成——首条请求会在队列中阻塞直到会话就绪
    asyncio.create_task(
        sandbox_session_concurrent(
            project_root=project_root,
            tree=tree,
            input_channel_id=sandbox_in_ch,
            output_channel_id=sandbox_out_ch,
            max_workers=max_workers,
            timeout=timeout,
            base_patches=base_patches,
        )
    )

    return StartSandboxResult(
        sandbox_in_ch=sandbox_in_ch,
        sandbox_out_ch=sandbox_out_ch,
    )


# ── mcts_sandbox_via_session ──────────────────────────────────────────────────

async def mcts_sandbox_via_session(
    sandbox_in_ch:    str,
    ancestor_patches: list[dict],
    new_patches:      list[dict],
) -> MctsSandboxResult:
    """通过共享沙箱会话评估候选 patch，输出与 mcts_sandbox 完全兼容。

    协议（独占 reply_channel_id 路由）：
        1. 合并 ancestor_patches + new_patches → all_patches
        2. 创建独占 reply_ch（UUID 命名，仅本次调用监听）
        3. 向 sandbox_in_ch 发送 {request_id, patches, reply_channel_id}
        4. 在 reply_ch 上阻塞等待会话返回结果
        5. 关闭 reply_ch，将结果包装为 MctsSandboxResult 返回

    每个并发调用使用独立的 reply_channel，不会互相干扰，无需协调者。

    Args:
        sandbox_in_ch:    共享沙箱会话输入通道 ID（由 start_sandbox_session 创建）。
        ancestor_patches: 父节点累积 patch 列表（已应用到父节点的所有改动）。
        new_patches:      本次新增 patch 列表（LLM Engineer 本轮生成的改动）。

    Returns:
        MctsSandboxResult（含 all_patches），与直接调用 mcts_sandbox 字段完全一致。
    """
    from utils import create_channel, send_to_channel, receive_from_channel, close_channel

    all_patches = list(ancestor_patches) + list(new_patches)
    request_id  = str(uuid.uuid4())[:8]
    reply_ch    = f"mcts_reply_{uuid.uuid4().hex[:12]}"

    create_channel(reply_ch)
    try:
        await send_to_channel(sandbox_in_ch, {
            "request_id":       request_id,
            "patches":          all_patches,
            "reply_channel_id": reply_ch,
        })
        result = await receive_from_channel(reply_ch)
    finally:
        try:
            close_channel(reply_ch)
        except Exception as e:
            # reply_ch 可能已被提前关闭，记录日志后忽略
            logger.warning("关闭 reply_channel '%s' 失败: %s", reply_ch, e)

    failure = result.get("failure")
    error_msg = (
        failure.get("message", "") if isinstance(failure, dict) else ""
    ) if failure else ""

    return MctsSandboxResult(
        ok=result.get("ok", False),
        score=result.get("score", -999.0),
        metrics=result.get("metrics", {}),
        output_log=result.get("output_log", ""),
        stderr_log=result.get("stderr_log", ""),
        error=error_msg,
        all_patches=all_patches,
    )
