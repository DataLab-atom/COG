"""u2e_sandbox_inputs — U2E 沙盒评估异步 Input 函数

供 input/*.json 配置引用（type: "input", async: true）。

函数列表：
    u2e_eval_via_mcts_sandbox — 通过 MCTS 共享沙箱会话评估单个 U2E individual（patch 模式）
"""
from __future__ import annotations

from utils import ToolResult
from utils.mcts.inputs import MctsSandboxResult


async def u2e_eval_via_mcts_sandbox(
    sandbox_in_ch:    str,
    ancestor_patches: list,
    new_patches:      list,
) -> MctsSandboxResult:
    """通过 MCTS 共享沙箱会话评估一组 patch，返回与 mcts_sandbox_via_session 兼容的结果。

    委托给 mcts_sandbox_via_session，输出与 MCTS 完全一致（ok / score / output_log / error）。
    方向转换（score → obj = -score）由 u2e_aggregate_sandbox_results 负责，此处不处理。

    Args:
        sandbox_in_ch:    共享沙箱会话输入通道 ID（由 start_sandbox_session 创建）。
        ancestor_patches: 祖先 patch 列表（已应用到父节点的累积改动）。
        new_patches:      本次新增 patch 列表（U2E individual 的 patches 字段）。

    Returns:
        MctsSandboxResult（含 ok / score / metrics / output_log / error / all_patches）。
    """
    from utils.mcts.inputs import mcts_sandbox_via_session
    return await mcts_sandbox_via_session(
        sandbox_in_ch=sandbox_in_ch,
        ancestor_patches=ancestor_patches,
        new_patches=new_patches,
    )
