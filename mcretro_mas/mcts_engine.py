"""
MCTS Engine — 并发树搜索 + 人工确认门

搜索节奏：
  Gen 1      : LLM 模式，从 root 展开 i×j 变体
  Gen 2      : LLM 模式，每个 frontier 节点独立展开 i×j（基于自身 tree）
  Gen 3      : Enumerate 模式，遍历剩余 (op,angle) 组合，无 Critic 调用
  Gen 4+     : 用户在每代 Human Gate 处选择 llm / enumerate

每代结束后阻塞等待用户决策（Human Gate），用户可选：
  continue   : 接受算法推荐的 top-K frontier，继续搜索
  select     : 手动指定 frontier 节点 id
  rollback   : 回滚到父节点层
  architecture: 停止并升级至架构重设计层
  stop       : 终止搜索

全部静态失败时直接触发 architecture escalation 提示。
consecutive_bad >= 3 时自动回滚。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv
from omegaconf import DictConfig

from LLM_MODULE.llm import OpenAILLM
from mcretro_mas.engine_history import HistoryManager
from mcretro_mas.engine_logging import log_event
from mcretro_mas.engine_optimizer import AutoOptimizer, CodeInjector
from mcretro_mas.mcts_gate import HumanGate
from mcretro_mas.mcts_generator import VariantGenerator
from mcretro_mas.mcts_node import ALL_COMBOS, SearchNode
from mcretro_mas.metric_agent import infer_task_type
from tree_cot.utils import generate_project_tree_text


class MCTSEngine:
    """
    MCTS Beam Search 引擎。

    外部使用方式：
        engine = MCTSEngine(cfg, project_root)
        best_node = asyncio.run(engine.run())

    Human Gate 使用方式（独立协程）：
        await engine.gate.send_decision(channel_id, decision_dict)
    当 engine 阻塞在 wait_for_decision 时，外部读取 engine.pending_channel_id
    并在展示完结果后调用 send_decision。
    """

    BEAM_WIDTH = 3
    CONSECUTIVE_BAD_LIMIT = 3
    # Gen 1/2 每个节点 LLM 模式的变体数
    LLM_VARIANTS_PER_COMBO = 3
    # Gen 3 enumerate 模式的变体数
    ENUM_VARIANTS_PER_COMBO = 2

    def __init__(self, cfg: DictConfig, project_root: str) -> None:
        load_dotenv()

        self.cfg = cfg
        self.project_root = os.path.abspath(project_root)
        self.entry_point = "main.py"
        self.ignore_list = [
            "__pycache__", "*.pyc", ".git", "venv",
            "data", "datasets", "logs", ".idea", "mcts_history",
        ]
        self.timeout: int = cfg.parameters.mcr.timeout

        # LLM 客户端
        self.critic_llm = OpenAILLM(model=cfg.models.mcr.critic_model)
        self.engineer_llm = OpenAILLM(model=cfg.models.mcr.engineer_model)

        # AutoOptimizer 仅用于 baseline 评分和 apply_change
        self.optimizer = AutoOptimizer(
            project_root=self.project_root,
            entry_point=self.entry_point,
            timeout=self.timeout,
            ignore_list=self.ignore_list,
            history_dir=os.path.join(self.project_root, "mcts_history"),
        )
        self.score_fn = self.optimizer.score_fn

        # 变体生成器、Human Gate、历史管理
        self.generator = VariantGenerator(
            critic_llm=self.critic_llm,
            engineer_llm=self.engineer_llm,
            project_root=self.project_root,
        )
        self.gate = HumanGate()
        self.history = HistoryManager()

        # 项目元信息（静态，不随代际变化）
        self.tree_text = generate_project_tree_text(self.project_root)
        self.demand: str = cfg.problems.demand
        self.task_metric: str = cfg.problems.metric

        # 全局已尝试的 (op, angle) 组合（跨所有节点）
        self.global_tried: Set[Tuple[str, str]] = set()

        # 供外部读取的当前 gate channel id（外部调用方据此发送决策）
        self.pending_channel_id: Optional[str] = None

    # ── 主循环 ──────────────────────────────────────────────

    async def run(self) -> SearchNode:
        """执行 MCTS 搜索，返回全局最优节点。"""
        baseline_score, baseline_log = self.optimizer.get_baseline_score()
        log_event("mcts_start", baseline_score=baseline_score)

        root = SearchNode(
            generation=0,
            score=baseline_score,
            output_log=baseline_log,
            patches=[],
        )

        frontier: List[SearchNode] = [root]
        best_node: SearchNode = root
        consecutive_bad: int = 0
        generation: int = 0
        mode: str = "llm"  # 当前展开模式，gen>=4 后由用户决策控制

        while frontier:
            generation += 1

            # 确定本代模式
            current_mode = self._resolve_mode(generation, mode)

            log_event(
                "mcts_gen_start",
                generation=generation,
                mode=current_mode,
                frontier_size=len(frontier),
                consecutive_bad=consecutive_bad,
            )

            # ── 1. 并发展开所有 frontier 节点 ──────────────
            all_candidates = await self._expand_frontier(
                frontier=frontier,
                mode=current_mode,
                baseline_log=baseline_log,
            )

            static_fail_count = len(
                [c for c in all_candidates if c.score == float("-inf") and not c.patches]
            )
            # 通过静态检查的候选（patches 非空 = 已通过 mcts_generator 的 _static_check）
            valid_candidates = [c for c in all_candidates if c.patches]

            log_event(
                "mcts_gen_expanded",
                generation=generation,
                total=len(all_candidates),
                static_fail=static_fail_count,
                valid=len(valid_candidates),
            )

            # ── 2. 全部静态失败 → 架构升级提示 ─────────────
            if not valid_candidates:
                HumanGate.display_arch_escalation(generation)
                decision = await self._gate_wait(f"gate_arch_{generation}")
                action = decision.get("action", "stop")
                if action == "architecture":
                    log_event("mcts_arch_escalation", generation=generation)
                break

            # ── 3. 并发沙盒评估 ─────────────────────────────
            evaluated = await self._evaluate_parallel(valid_candidates)

            sandbox_fail_count = sum(1 for n in evaluated if n.score == -999)
            improved = [
                n for n in evaluated
                if n.score != -999 and n.score > n.parent.score
            ]

            # 更新全局最优
            for n in evaluated:
                if n.score != -999 and n.score > best_node.score:
                    best_node = n

            # ── 4. Human Gate ────────────────────────────────
            remaining = len(ALL_COMBOS) - len(self.global_tried)
            HumanGate.display_generation_summary(
                generation=generation,
                mode=current_mode,
                total_expanded=len(all_candidates),
                static_fail_count=static_fail_count,
                sandbox_fail_count=sandbox_fail_count,
                improved=improved,
                remaining_space=remaining,
            )

            decision = await self._gate_wait(f"gate_gen_{generation}")
            action = decision.get("action", "continue")
            next_mode = decision.get("next_mode", current_mode)
            selected_ids: Optional[List[str]] = decision.get("selected_ids")

            log_event(
                "mcts_gate_decision",
                generation=generation,
                action=action,
                next_mode=next_mode,
                selected_ids=selected_ids,
            )

            # ── 5. 处理用户决策 ──────────────────────────────
            if action == "stop":
                break

            if action in ("architecture",):
                log_event("mcts_arch_escalation_user", generation=generation)
                break

            if action == "rollback":
                frontier = self._rollback(frontier)
                consecutive_bad = 0
                log_event("mcts_rollback_user", generation=generation, new_size=len(frontier))
                if not frontier:
                    break
                mode = next_mode
                continue

            # action == "continue" or "select"
            if improved:
                # 挑选 frontier
                if selected_ids:
                    next_frontier = [n for n in improved if n.node_id in selected_ids]
                    if not next_frontier:
                        next_frontier = improved  # fallback
                else:
                    next_frontier = improved

                frontier = sorted(next_frontier, key=lambda n: -n.score)[: self.BEAM_WIDTH]
                consecutive_bad = 0

                # 记录已尝试的 (op, angle)
                for n in frontier:
                    if n.op and n.angle:
                        self.global_tried.add((n.op, n.angle))

                # 如果搜索到新全局最优，写入实际项目
                if frontier[0].score > best_node.score or (
                    best_node.score == root.score and frontier[0].score > root.score
                ):
                    self._apply_best_to_project(frontier[0])
                    best_node = frontier[0]
                    baseline_log = frontier[0].output_log or baseline_log

            else:
                consecutive_bad += 1
                log_event(
                    "mcts_no_improvement",
                    generation=generation,
                    consecutive_bad=consecutive_bad,
                )

                if consecutive_bad >= self.CONSECUTIVE_BAD_LIMIT:
                    frontier = self._rollback(frontier)
                    consecutive_bad = 0
                    log_event(
                        "mcts_rollback_auto",
                        generation=generation,
                        new_size=len(frontier),
                    )
                    if not frontier:
                        break

            # 保存本代 best 到历史（供下一代 Critic 参考）
            self._record_generation(generation, improved, evaluated)

            mode = next_mode  # 用户选择的下一代模式

        log_event("mcts_done", best_score=best_node.score, generations=generation)
        print(f"\n{'═'*62}")
        print(f"  MCTS 搜索结束  |  Best score: {best_node.score:.4f}")
        print(f"  Best node: {best_node.summary()}")
        print(f"{'═'*62}")
        return best_node

    # ── 展开 ────────────────────────────────────────────────

    def _resolve_mode(self, generation: int, user_mode: str) -> str:
        """Gen 1/2 强制 llm，Gen 3 强制 enumerate，Gen 4+ 使用用户选择。"""
        if generation <= 2:
            return "llm"
        if generation == 3:
            return "enumerate"
        return user_mode

    async def _expand_frontier(
        self,
        frontier: List[SearchNode],
        mode: str,
        baseline_log: str,
    ) -> List[SearchNode]:
        """并发展开所有 frontier 节点，每个节点基于自身 tree 独立展开。"""
        tasks = []
        for node in frontier:
            combos = node.remaining_combos(self.global_tried)
            if not combos:
                log_event("mcts_no_combos_left", node_id=node.node_id)
                continue

            if mode == "enumerate":
                task = self.generator.generate_enumerate(
                    node=node,
                    combos=combos,
                    demand=self.demand,
                    n_variants=self.ENUM_VARIANTS_PER_COMBO,
                )
            else:  # llm
                task = self.generator.generate_llm(
                    node=node,
                    combos=combos,
                    tree_text=self.tree_text,
                    history_prompt=self.history.get_critic_prompt(),
                    baseline_log=baseline_log,
                    demand=self.demand,
                    n_variants=self.LLM_VARIANTS_PER_COMBO,
                )
            tasks.append(task)

        if not tasks:
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)
        all_candidates: List[SearchNode] = []
        for r in results:
            if isinstance(r, list):
                all_candidates.extend(r)
            elif isinstance(r, Exception):
                log_event("mcts_expand_error", error=str(r))
        return all_candidates

    # ── 沙盒评估 ────────────────────────────────────────────

    async def _evaluate_parallel(
        self, candidates: List[SearchNode]
    ) -> List[SearchNode]:
        """对所有候选节点并发运行沙盒，写入 score/metrics/output_log。"""
        tasks = [self._run_sandbox_for_node(node) for node in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        evaluated: List[SearchNode] = []
        for r in results:
            if isinstance(r, SearchNode):
                evaluated.append(r)
            elif isinstance(r, Exception):
                log_event("mcts_sandbox_error", error=str(r))
        return evaluated

    async def _run_sandbox_for_node(self, node: SearchNode) -> SearchNode:
        """
        创建临时沙盒，按顺序重放节点祖先链的所有 patch，
        然后运行 main.py 并解析 __METRICS__ 输出。

        注意：因为 subprocess 是阻塞调用，使用 run_in_executor 避免阻塞事件循环。
        """
        loop = asyncio.get_running_loop()
        trial_id = str(uuid.uuid4())[:8]

        def _run() -> SearchNode:
            with tempfile.TemporaryDirectory(prefix=f"mcts_{trial_id}_") as tmp:
                sandbox_root = os.path.join(tmp, "sandbox")
                ignore_fn = shutil.ignore_patterns(*self.ignore_list)
                try:
                    shutil.copytree(self.project_root, sandbox_root, ignore=ignore_fn)
                except Exception as e:
                    log_event("mcts_sandbox_copy_fail", trial_id=trial_id, error=str(e))
                    node.score = -999
                    return node

                # 按顺序应用所有祖先 patch（累积改动重放）
                for patch in node.patches:
                    rel = patch["target_file"].lstrip("/").replace("\\", "/")
                    target_path = os.path.join(sandbox_root, rel)
                    ok, msg = CodeInjector.replace_code_block(
                        target_path,
                        patch["target_type"],
                        patch["target_name"],
                        patch["code"],
                    )
                    if not ok:
                        log_event(
                            "mcts_sandbox_inject_fail",
                            trial_id=trial_id,
                            rel=rel,
                            msg=msg,
                        )
                        node.score = -999
                        return node

                # 执行 main.py
                try:
                    proc = subprocess.run(
                        [sys.executable, self.entry_point],
                        cwd=sandbox_root,
                        capture_output=True,
                        text=True,
                        timeout=self.timeout,
                    )
                    output = proc.stdout
                except subprocess.TimeoutExpired:
                    log_event("mcts_sandbox_timeout", trial_id=trial_id)
                    node.score = -999
                    return node
                except Exception as e:
                    log_event("mcts_sandbox_exec_fail", trial_id=trial_id, error=str(e))
                    node.score = -999
                    return node

                # 解析 __METRICS__
                _PREFIX = "__METRICS__:"
                for line in reversed(output.splitlines()):
                    if line.startswith(_PREFIX):
                        try:
                            metrics = json.loads(line[len(_PREFIX):])
                            node.score = self.score_fn(metrics)
                            node.metrics = metrics
                            node.output_log = output
                            log_event(
                                "mcts_sandbox_scored",
                                trial_id=trial_id,
                                score=node.score,
                                op=node.op,
                                angle=node.angle,
                            )
                            return node
                        except Exception:
                            break

                node.score = -999
                node.output_log = output
                log_event("mcts_sandbox_no_metrics", trial_id=trial_id)
                return node

        return await loop.run_in_executor(None, _run)

    # ── Human Gate 辅助 ──────────────────────────────────────

    async def _gate_wait(self, channel_id: str) -> Dict[str, Any]:
        """创建 channel，阻塞等待决策，关闭 channel。"""
        self.gate.create_channel(channel_id)
        self.pending_channel_id = channel_id
        log_event("mcts_gate_waiting", channel_id=channel_id)

        decision = await self.gate.wait_for_decision(channel_id)

        self.pending_channel_id = None
        self.gate.close_channel(channel_id)
        return decision

    # ── 回滚 ────────────────────────────────────────────────

    def _rollback(self, frontier: List[SearchNode]) -> List[SearchNode]:
        """将 frontier 回退到各节点的父节点（去重）。"""
        parents: List[SearchNode] = []
        seen: Set[str] = set()
        for node in frontier:
            p = node.parent
            if p is not None and p.node_id not in seen:
                parents.append(p)
                seen.add(p.node_id)
        log_event("mcts_rollback_exec", old_size=len(frontier), new_size=len(parents))
        return parents

    # ── 写回最优改动 ─────────────────────────────────────────

    def _apply_best_to_project(self, node: SearchNode) -> None:
        """将节点累积的所有 patch 写入实际项目（带 .bak 备份）。"""
        for patch in node.patches:
            rel = patch["target_file"].lstrip("/").replace("\\", "/")
            target_path = os.path.join(self.project_root, rel)
            if os.path.exists(target_path):
                shutil.copy(target_path, target_path + ".bak")
            CodeInjector.replace_code_block(
                target_path,
                patch["target_type"],
                patch["target_name"],
                patch["code"],
            )
        log_event(
            "mcts_best_applied",
            node_id=node.node_id,
            score=node.score,
            patches=len(node.patches),
        )

    # ── 历史记录 ─────────────────────────────────────────────

    def _record_generation(
        self,
        generation: int,
        improved: List[SearchNode],
        evaluated: List[SearchNode],
    ) -> None:
        """将本代最优结果写入 HistoryManager（供下一代 Critic 参考）。"""
        from mcretro_mas.engine_history import (
            EvolutionRecord,
            OptimizationProposal,
            OptimizationStatus,
        )

        all_nodes = evaluated
        if not all_nodes:
            return

        best = max(all_nodes, key=lambda n: n.score if n.score != -999 else float("-inf"))
        if best.score == -999 or not best.patches:
            return

        last_patch = best.patches[-1]
        proposal = OptimizationProposal(
            target_file=last_patch["target_file"],
            target_type=last_patch["target_type"],
            target_name=last_patch["target_name"],
            original_code=last_patch.get("code", ""),
            direction=f"{best.op} × {best.angle}",
            reasoning=f"MCTS gen {generation}, score={best.score:.4f}",
        )
        status = self.history.calculate_optimization_status(best.score)
        record = EvolutionRecord(
            generation=generation,
            proposal=proposal,
            score=best.score,
            status=status,
        )
        self.history.add_record(record)


# ── 入口 ─────────────────────────────────────────────────────

def main(cfg: DictConfig, project_root: str) -> SearchNode:
    """
    MCTS 引擎入口（供 Pipeline 调用）。

    Human Gate 处理：engine.run() 会阻塞在 gate.wait_for_decision()。
    外部可通过另一个协程调用：
        await engine.gate.send_decision(engine.pending_channel_id, decision)
    或者在 CLI 模式下，在 engine.run() 外层包一个协程读取 stdin 并发送决策。
    """
    engine = MCTSEngine(cfg, project_root)

    async def _run_with_cli_gate():
        """CLI 模式：独立协程读取 stdin 并注入决策。"""

        async def _cli_listener():
            while True:
                channel_id = engine.pending_channel_id
                if channel_id is None:
                    await asyncio.sleep(0.1)
                    continue
                # 等待用户输入
                loop = asyncio.get_running_loop()
                raw = await loop.run_in_executor(None, input, "\n> 你的决策: ")
                decision = HumanGate.parse_cli_input(raw)
                await engine.gate.send_decision(channel_id, decision)
                # 如果用户选择 stop/architecture，退出监听
                if decision.get("action") in ("stop", "architecture"):
                    break

        listener_task = asyncio.create_task(_cli_listener())
        best = await engine.run()
        listener_task.cancel()
        return best

    best = asyncio.run(_run_with_cli_gate())
    return best
