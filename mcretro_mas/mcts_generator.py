"""
MCTS Variant Generator

两种展开模式：
  LLM 模式（Gen 1/2）: Critic 针对 (op, angle) 提出方向，Engineer 实现代码。
  Enumerate 模式（Gen 3 / 用户选择）: 跳过 Critic，直接用模板 prompt 让 Engineer
      为每个剩余 (op, angle) 组合生成代码，零额外 LLM 推理成本（不调 Critic）。

静态检查：ast.parse 快速验证，失败直接丢弃，不计惩罚。
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

try:
    from json_repair import repair_json  # type: ignore
except Exception:
    def repair_json(t: str) -> str:
        return t

from mcretro_mas.engine_agents import MonteCarloEngineerAgent
from mcretro_mas.engine_history import OptimizationProposal
from mcretro_mas.engine_logging import log_event
from mcretro_mas.mcts_node import SearchNode
from tree_cot.utils import extract_code_with_structures

# ── Critic prompt（LLM 模式）────────────────────────────────
_CRITIC_MCTS_SYS = """\
You are an expert algorithm engineer performing targeted code optimization.

Your task: Propose ONE specific optimization for the given operator type and angle.

Operator types:
  compute       — algorithm complexity, vectorization, parallelization
  memory        — caching, data structures, memory layout
  io            — I/O batching, async operations, data loading pipelines
  algorithm     — better algorithms, mathematical shortcuts, approximations
  data_structure— better data structures, indexing, lookup acceleration

Angles:
  aggressive        — maximize change, high risk/reward, structural refactor acceptable
  conservative      — minimal safe change, preserve existing behavior fully
  bottleneck_focused— target the measured performance bottleneck in runtime output

Return ONLY valid JSON (no markdown, no extra text):
{
  "target_file": "relative/path/to/file.py",
  "target_type": "function or class",
  "target_name": "FunctionOrClassName",
  "direction": "Specific one-sentence optimization direction",
  "reasoning": "Why this improves the metric"
}
"""

# ── Engineer prompt（Enumerate 模式）────────────────────────
_ENGINEER_ENUMERATE_SYS = """\
You are an expert Python engineer. Rewrite the given code using the specified
optimization strategy. Preserve the interface contract exactly.
Output ONLY the replacement code, wrapped in ```python ... ```.
"""


def _strip_fences(code: str) -> str:
    m = re.search(r"```(?:python|py)?\s*(.*?)```", code, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else code.strip()


def _static_check(code: str) -> bool:
    """快速 AST 语法检查，失败 → 丢弃（不计惩罚）。"""
    try:
        ast.parse(_strip_fences(code))
        return True
    except SyntaxError:
        return False


class VariantGenerator:
    """
    生成候选 SearchNode 的工厂。

    每个候选节点携带从 root 到该节点的完整 patch 列表，
    可直接在沙盒中重放所有改动。
    """

    def __init__(self, critic_llm: Any, engineer_llm: Any, project_root: str) -> None:
        self.critic_llm = critic_llm
        self.engineer = MonteCarloEngineerAgent(engineer_llm)
        self.project_root = os.path.abspath(project_root)

    # ── 公开接口 ────────────────────────────────────────────

    async def generate_llm(
        self,
        node: SearchNode,
        combos: List[Tuple[str, str]],
        tree_text: str,
        history_prompt: str,
        baseline_log: str,
        demand: str,
        n_variants: int = 3,
    ) -> List[SearchNode]:
        """
        LLM 模式：对每个 (op, angle) 并发调用 Critic + Engineer。
        返回通过静态检查的子节点列表。
        """
        tasks = [
            self._llm_one_combo(
                node, op, angle,
                tree_text, history_prompt, baseline_log, demand, n_variants,
            )
            for op, angle in combos
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        children: List[SearchNode] = []
        for r in results:
            if isinstance(r, Exception):
                log_event("mcts_gen_llm_error", error=str(r))
            elif r:
                children.extend(r)
        return children

    async def generate_enumerate(
        self,
        node: SearchNode,
        combos: List[Tuple[str, str]],
        demand: str,
        n_variants: int = 2,
    ) -> List[SearchNode]:
        """
        Enumerate 模式：跳过 Critic，对每个剩余 (op, angle) 直接用 Engineer 生成。
        使用模板 prompt，低温度，确定性强。
        """
        tasks = [
            self._enumerate_one_combo(node, op, angle, demand, n_variants)
            for op, angle in combos
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        children: List[SearchNode] = []
        for r in results:
            if isinstance(r, Exception):
                log_event("mcts_gen_enum_error", error=str(r))
            elif r:
                children.extend(r)
        return children

    # ── 私有：LLM 模式 ──────────────────────────────────────

    async def _llm_one_combo(
        self,
        node: SearchNode,
        op: str,
        angle: str,
        tree_text: str,
        history_prompt: str,
        baseline_log: str,
        demand: str,
        n_variants: int,
    ) -> Optional[List[SearchNode]]:
        """Critic 为 (op, angle) 提方向 → Engineer 实现 → 静态检查 → 子节点。"""
        loop = asyncio.get_running_loop()

        # 1. Critic
        messages = [
            {"role": "system", "content": _CRITIC_MCTS_SYS},
            {
                "role": "user",
                "content": (
                    f"Operator: {op}\nAngle: {angle}\n\n"
                    f"Demand:\n{demand}\n\n"
                    f"Project tree:\n{tree_text}\n\n"
                    f"Optimization history:\n{history_prompt}\n\n"
                    f"Last run output:\n{baseline_log}"
                ),
            },
        ]
        reply = await loop.run_in_executor(
            None, lambda: self.critic_llm.chat(messages, temperature=0.6)
        )

        try:
            payload: Dict = json.loads(reply)
        except Exception:
            try:
                payload = json.loads(repair_json(reply))
            except Exception:
                log_event("mcts_critic_parse_fail", op=op, angle=angle)
                return None

        target_file = payload.get("target_file", "")
        target_type = payload.get("target_type", "function")
        target_name = payload.get("target_name", "")
        direction = payload.get("direction", "")
        reasoning = payload.get("reasoning", "")

        if not (target_file and target_name and direction):
            return None

        # 2. 提取原始代码
        target_path = os.path.join(self.project_root, target_file.lstrip("/"))
        try:
            code_blocks = extract_code_with_structures(target_path)
            original_code = next(
                (b["code_source"] for b in code_blocks
                 if b["code_name"] == target_name and b["type"] == target_type),
                "",
            )
        except Exception:
            original_code = ""

        proposal = OptimizationProposal(
            target_file=target_file,
            target_type=target_type,
            target_name=target_name,
            original_code=original_code,
            direction=direction,
            reasoning=reasoning,
        )

        # 3. Engineer 并发生成 n_variants 份代码
        variants = await self.engineer.generate_variants(proposal, n=n_variants)
        log_event(
            "mcts_gen_llm_variants",
            op=op, angle=angle, requested=n_variants, returned=len(variants),
        )

        # 4. 静态检查 → 构建子节点
        return self._build_children(node, op, angle, target_file, target_type, target_name, variants)

    # ── 私有：Enumerate 模式 ─────────────────────────────────

    async def _enumerate_one_combo(
        self,
        node: SearchNode,
        op: str,
        angle: str,
        demand: str,
        n_variants: int,
    ) -> Optional[List[SearchNode]]:
        """
        无 Critic：复用父节点最后一个 patch 的 target，
        用模板 prompt 让 Engineer 用 (op, angle) 风格重写。
        """
        if not node.patches:
            # root 节点无法 enumerate（没有先前改动可继承）
            return None

        last = node.patches[-1]
        target_file = last["target_file"]
        target_type = last["target_type"]
        target_name = last["target_name"]

        # 枚举模式的方向描述（结构化，无需 Critic 创造力）
        direction = (
            f"Apply {angle.replace('_', ' ')} {op} optimization: "
            f"rewrite {target_name} using {op}-focused techniques "
            f"with a {angle.replace('_', ' ')} approach."
        )
        reasoning = f"Enumerate remaining search space: {op} × {angle}"

        # 提取原始代码（从当前节点祖先链的 patch 还原）
        original_code = last.get("code", "")

        proposal = OptimizationProposal(
            target_file=target_file,
            target_type=target_type,
            target_name=target_name,
            original_code=original_code,
            direction=direction,
            reasoning=reasoning,
        )

        # Engineer 以低温度（确定性）生成
        loop = asyncio.get_running_loop()
        messages = [
            {"role": "system", "content": _ENGINEER_ENUMERATE_SYS},
            {
                "role": "user",
                "content": (
                    f"Optimization strategy: {direction}\n\n"
                    f"Target: {target_file} → {target_type} `{target_name}`\n\n"
                    f"Demand context: {demand}\n\n"
                    f"Current implementation:\n{original_code or '[not available]'}"
                ),
            },
        ]

        async def _gen_one(temp: float) -> str:
            return await loop.run_in_executor(
                None, lambda: self.engineer.llm.chat(messages, temperature=temp)
            )

        # n_variants 份，温度从 0.2 到 0.6 均匀分布
        temps = [0.2 + (0.4 / max(n_variants - 1, 1)) * i for i in range(n_variants)]
        variants = await asyncio.gather(*[_gen_one(t) for t in temps])

        log_event(
            "mcts_gen_enum_variants",
            op=op, angle=angle, requested=n_variants, returned=len(variants),
        )

        return self._build_children(node, op, angle, target_file, target_type, target_name, list(variants))

    # ── 工具方法 ─────────────────────────────────────────────

    def _build_children(
        self,
        parent: SearchNode,
        op: str,
        angle: str,
        target_file: str,
        target_type: str,
        target_name: str,
        variants: List[str],
    ) -> List[SearchNode]:
        """
        对每个通过静态检查的变体，构建子 SearchNode。
        累积 patch = parent.patches + [新 patch]。
        """
        children: List[SearchNode] = []
        for code in variants:
            if not _static_check(code):
                log_event("mcts_static_fail", op=op, angle=angle)
                continue
            patch = {
                "target_file": target_file,
                "target_type": target_type,
                "target_name": target_name,
                "code": _strip_fences(code),
            }
            child = SearchNode(
                generation=parent.generation + 1,
                parent=parent,
                op=op,
                angle=angle,
                patches=parent.patches + [patch],
            )
            children.append(child)
            parent.children.append(child)
        return children
