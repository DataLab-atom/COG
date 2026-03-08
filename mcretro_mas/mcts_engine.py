"""
MCTS Engine — open-agents 标准实现

使用 open-agents 框架：
  - run_graph_from_file("graphs/mcts_single_combo.json")  执行每个 (op,angle) 组合
  - create_channel / send_to_channel / close_channel       Human Gate 通道
  - utils/mcts_tools.py, utils/mcts_inputs.py             工具/输入实现

搜索节奏：
  Gen 1/2  : LLM 模式，i×j 个 mcts_single_combo graph 并发运行
  Gen 3    : Enumerate 模式（遍历全部剩余组合，仍调用 Critic）
  Gen 4+   : 用户在 Human Gate 处选择 llm / enumerate

每代结束后阻塞在 Human Gate（read_channel 模式），等待外部通过
    await send_to_channel(engine.pending_channel_id, decision)
注入决策后继续。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv
from omegaconf import DictConfig

# open-agents 框架（从本地目录导入）
_OA_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "open-agents")
)
if _OA_PATH not in sys.path:
    sys.path.insert(0, _OA_PATH)

from utils._channel import (  # noqa: E402
    create_channel,
    send_to_channel,
    close_channel,
    receive_from_channel,
)
from utils._graph import run_graph_from_file  # noqa: E402

from mcretro_mas.engine_history import (
    EvolutionRecord,
    HistoryManager,
    OptimizationProposal,
)
from mcretro_mas.engine_logging import log_event
from mcretro_mas.engine_optimizer import AutoOptimizer, CodeInjector
from mcretro_mas.mcts_node import ALL_COMBOS, SearchNode
from tree_cot.utils import generate_project_tree_text


class MCTSEngine:
    """
    MCTS Beam Search 引擎（open-agents 标准实现）。

    Human Gate 由 open-agents channel 机制驱动：
    - engine.run() 在每代结束后调用 receive_from_channel() 阻塞
    - 外部通过 send_to_channel(engine.pending_channel_id, decision) 注入决策

    decision 结构：
        {
            "action":       "continue" | "select" | "rollback" | "architecture" | "stop",
            "next_mode":    "llm" | "enumerate",
            "selected_ids": ["node_id", ...] | None,
        }
    """

    BEAM_WIDTH = 3
    CONSECUTIVE_BAD_LIMIT = 3
    # 每个 (op, angle) 并发运行的 graph 副本数（增加多样性）
    VARIANTS_PER_COMBO = 3

    _GRAPH_PATH = "graphs/mcts_single_combo.json"

    def __init__(self, cfg: DictConfig, project_root: str) -> None:
        load_dotenv()
        self.cfg = cfg
        self.project_root = os.path.abspath(project_root)
        self.timeout: int = cfg.parameters.mcr.timeout
        self.demand: str = cfg.problems.demand

        self.optimizer = AutoOptimizer(
            project_root=self.project_root,
            entry_point="main.py",
            timeout=self.timeout,
            ignore_list=["__pycache__", "*.pyc", ".git", "venv",
                         "data", "datasets", "logs", ".idea", "mcts_history"],
            history_dir=os.path.join(self.project_root, "mcts_history"),
        )
        self.history = HistoryManager()
        self.tree_text = generate_project_tree_text(self.project_root)
        self.global_tried: Set[Tuple[str, str]] = set()

        # 当前 Human Gate 的 channel_id（外部据此发送决策）
        self.pending_channel_id: Optional[str] = None

    # ── 主循环 ──────────────────────────────────────────────

    async def run(self) -> SearchNode:
        """执行 MCTS 搜索，返回全局最优节点。"""
        baseline_score, baseline_log = self.optimizer.get_baseline_score()
        log_event("mcts_start", baseline_score=baseline_score)

        root = SearchNode(generation=0, score=baseline_score, output_log=baseline_log)
        frontier: List[SearchNode] = [root]
        best_node = root
        consecutive_bad = 0
        generation = 0
        mode = "llm"

        while frontier:
            generation += 1
            current_mode = (
                "enumerate" if generation == 3
                else ("llm" if generation <= 2 else mode)
            )

            log_event("mcts_gen_start", generation=generation,
                      mode=current_mode, frontier_size=len(frontier))

            # ── 1. 并发展开 ──────────────────────────────────
            all_results = await self._expand_frontier(frontier, baseline_log)

            static_ok    = [r for r in all_results if r.get("ast_ok")]
            static_fail  = len(all_results) - len(static_ok)
            sandbox_ok   = [r for r in static_ok if r.get("result", {}).get("ok")]
            sandbox_fail = len(static_ok) - len(sandbox_ok)

            log_event("mcts_gen_stats", generation=generation,
                      total=len(all_results), static_fail=static_fail,
                      sandbox_fail=sandbox_fail, sandbox_ok=len(sandbox_ok))

            # ── 2. 全静态失败 → 架构升级 ─────────────────────
            if not static_ok:
                self._print_arch_escalation(generation)
                decision = await self._gate_wait(f"gate_arch_{generation}")
                log_event("mcts_arch_escalation", generation=generation,
                          action=decision.get("action"))
                break

            # ── 3. 构建子节点 ─────────────────────────────────
            children = self._build_children(frontier, sandbox_ok)
            improved = [n for n in children if n.score > n.parent.score]

            for n in children:
                if n.score > best_node.score:
                    best_node = n

            # ── 4. Human Gate ─────────────────────────────────
            remaining = len(ALL_COMBOS) - len(self.global_tried)
            self._print_summary(generation, current_mode,
                                len(all_results), static_fail, sandbox_fail,
                                improved, remaining)

            decision    = await self._gate_wait(f"gate_gen_{generation}")
            action      = decision.get("action", "continue")
            next_mode   = decision.get("next_mode", current_mode)
            sel_ids     = decision.get("selected_ids")

            log_event("mcts_gate_decision", generation=generation,
                      action=action, next_mode=next_mode)

            # ── 5. 处理决策 ───────────────────────────────────
            if action == "stop":
                break
            if action == "architecture":
                log_event("mcts_arch_user", generation=generation)
                break
            if action == "rollback":
                frontier = self._rollback(frontier)
                consecutive_bad = 0
                if not frontier:
                    break
                mode = next_mode
                continue

            # continue / select
            if improved:
                pool = ([n for n in improved if n.node_id in sel_ids]
                        if sel_ids else None) or improved
                frontier = sorted(pool, key=lambda n: -n.score)[: self.BEAM_WIDTH]
                consecutive_bad = 0
                for n in frontier:
                    if n.op and n.angle:
                        self.global_tried.add((n.op, n.angle))
                if frontier[0].score > best_node.score:
                    self._apply_best(frontier[0])
                    best_node    = frontier[0]
                    baseline_log = frontier[0].output_log or baseline_log
            else:
                consecutive_bad += 1
                log_event("mcts_no_improvement", generation=generation,
                          consecutive_bad=consecutive_bad)
                if consecutive_bad >= self.CONSECUTIVE_BAD_LIMIT:
                    frontier = self._rollback(frontier)
                    consecutive_bad = 0
                    if not frontier:
                        break

            self._record_history(generation, children)
            mode = next_mode

        log_event("mcts_done", best_score=best_node.score)
        print(f"\n{'═'*60}\n  MCTS 完成  Best score: {best_node.score:.4f}\n{'═'*60}")
        return best_node

    # ── 并发展开 ─────────────────────────────────────────────

    async def _expand_frontier(
        self,
        frontier: List[SearchNode],
        baseline_log: str,
    ) -> List[Dict[str, Any]]:
        """
        对每个 frontier 节点的每个剩余 (op, angle) 组合，
        并发运行 mcts_single_combo graph（VARIANTS_PER_COMBO 次/组合）。
        """
        graph_path = os.path.join(self.project_root, self._GRAPH_PATH)
        history_prompt = self.history.get_critic_prompt()

        tasks: List[asyncio.Task] = []
        meta_list: List[Dict] = []

        for node in frontier:
            combos = node.remaining_combos(self.global_tried)
            for op, angle in combos:
                for _ in range(self.VARIANTS_PER_COMBO):
                    variables = {
                        "op":               op,
                        "angle":            angle,
                        "demand":           self.demand,
                        "tree_text":        self.tree_text,
                        "history_prompt":   history_prompt,
                        "baseline_log":     baseline_log,
                        "project_root":     self.project_root,
                        "ancestor_patches": node.patches,
                        "sandbox_timeout":  float(self.timeout),
                    }
                    tasks.append(asyncio.create_task(
                        self._run_combo_graph(graph_path, variables)
                    ))
                    meta_list.append({"node": node, "op": op, "angle": angle})

        if not tasks:
            return []

        raw = await asyncio.gather(*tasks, return_exceptions=True)

        results: List[Dict[str, Any]] = []
        for meta, r in zip(meta_list, raw):
            if isinstance(r, Exception):
                log_event("mcts_combo_error", op=meta["op"],
                          angle=meta["angle"], error=str(r))
                continue
            r["_meta"] = meta
            results.append(r)

        return results

    async def _run_combo_graph(
        self, graph_path: str, variables: Dict[str, Any]
    ) -> Dict[str, Any]:
        """运行一次 mcts_single_combo graph，返回其输出字典。"""
        result = await run_graph_from_file(graph_path, variables=variables)
        out = result.final if isinstance(result.final, dict) else {}
        # 确保关键字段存在
        if "result" not in out:
            out["result"] = result.steps.get("sandbox", {})
        if "ast_ok" not in out:
            out["ast_ok"] = result.steps.get("ast_check", {}).get("ok", False)
        return out

    # ── 子节点构建 ────────────────────────────────────────────

    def _build_children(
        self,
        frontier: List[SearchNode],
        sandbox_ok: List[Dict[str, Any]],
    ) -> List[SearchNode]:
        """将沙盒成功的 graph 结果转换为 SearchNode。"""
        # 建立 node_id → SearchNode 映射
        node_map = {n.node_id: n for n in frontier}

        children: List[SearchNode] = []
        for r in sandbox_ok:
            meta   = r.get("_meta", {})
            parent = meta.get("node")
            op     = r.get("op") or meta.get("op", "")
            angle  = r.get("angle") or meta.get("angle", "")
            result = r.get("result", {})
            patches = result.get("all_patches", [])

            if parent is None or not patches or result.get("score", -999) == -999:
                continue

            child = SearchNode(
                generation=parent.generation + 1,
                parent=parent,
                op=op,
                angle=angle,
                patches=patches,
                score=result.get("score", -999.0),
                metrics=result.get("metrics", {}),
                output_log=result.get("output_log", ""),
            )
            parent.children.append(child)
            children.append(child)

        return children

    # ── Human Gate（open-agents channel）─────────────────────

    async def _gate_wait(self, channel_id: str) -> Dict[str, Any]:
        """创建 channel → 阻塞等待 → 关闭 channel（open-agents 标准模式）。"""
        create_channel(channel_id)
        self.pending_channel_id = channel_id
        log_event("mcts_gate_open", channel_id=channel_id)

        decision = await receive_from_channel(channel_id)

        self.pending_channel_id = None
        close_channel(channel_id)
        log_event("mcts_gate_closed", channel_id=channel_id)
        return decision

    # ── 显示 ─────────────────────────────────────────────────

    @staticmethod
    def _print_summary(
        generation: int, mode: str,
        total: int, static_fail: int, sandbox_fail: int,
        improved: List[SearchNode], remaining: int,
    ) -> None:
        s = sorted(improved, key=lambda n: -n.score)
        print(f"\n{'═'*62}")
        print(f"  Generation {generation}   [mode: {mode}]")
        print(f"{'─'*62}")
        print(f"  展开分支:       {total}")
        print(f"  静态检查失败:   {static_fail}  → 已丢弃")
        print(f"  沙盒执行失败:   {sandbox_fail}  → 已丢弃")
        print(f"  有效改善:       {len(s)}")
        print(f"  剩余搜索空间:   {remaining} / {len(ALL_COMBOS)} 组合")
        if s:
            print("\n  Top candidates:")
            for i, n in enumerate(s[:5], 1):
                print(f"    #{i}  [{n.node_id}]  "
                      f"{n.op:<16}×{n.angle:<20}  "
                      f"score={n.score:.4f}  {n.delta_str()}")
        else:
            print("\n  ⚠️  本代无改善分支。")
        print(f"{'─'*62}")
        print("  Actions : [continue] [select <id1,id2>] [rollback] [architecture] [stop]")
        print("  Next mode: [llm] [enumerate]")
        print(f"{'═'*62}")

    @staticmethod
    def _print_arch_escalation(generation: int) -> None:
        print(f"\n{'═'*62}")
        print(f"  ⚠️  Generation {generation}: 全部分支静态检查失败")
        print("  建议升级到 Architecture Redesign 层。")
        print("  Actions: [architecture] [stop]")
        print(f"{'═'*62}")

    # ── 回滚 ─────────────────────────────────────────────────

    def _rollback(self, frontier: List[SearchNode]) -> List[SearchNode]:
        parents: List[SearchNode] = []
        seen: Set[str] = set()
        for n in frontier:
            if n.parent and n.parent.node_id not in seen:
                parents.append(n.parent)
                seen.add(n.parent.node_id)
        log_event("mcts_rollback", old=len(frontier), new=len(parents))
        return parents

    # ── 写回最优 ─────────────────────────────────────────────

    def _apply_best(self, node: SearchNode) -> None:
        for patch in node.patches:
            rel = patch["target_file"].lstrip("/").replace("\\", "/")
            path = os.path.join(self.project_root, rel)
            if os.path.exists(path):
                shutil.copy(path, path + ".bak")
            CodeInjector.replace_code_block(
                path, patch["target_type"], patch["target_name"], patch["code"]
            )
        log_event("mcts_best_applied", score=node.score, patches=len(node.patches))

    # ── 历史记录 ─────────────────────────────────────────────

    def _record_history(self, generation: int, nodes: List[SearchNode]) -> None:
        valid = [n for n in nodes if n.score != -999 and n.patches]
        if not valid:
            return
        best = max(valid, key=lambda n: n.score)
        last = best.patches[-1]
        proposal = OptimizationProposal(
            target_file=last["target_file"],
            target_type=last["target_type"],
            target_name=last["target_name"],
            original_code=last.get("code", ""),
            direction=f"{best.op} × {best.angle}",
            reasoning=f"MCTS gen {generation}, score={best.score:.4f}",
        )
        self.history.add_record(EvolutionRecord(
            generation=generation,
            proposal=proposal,
            score=best.score,
            status=self.history.calculate_optimization_status(best.score),
        ))


# ── CLI 入口 ──────────────────────────────────────────────────

def main(cfg: DictConfig, project_root: str) -> SearchNode:
    """MCTS 引擎入口（供 Pipeline 调用）。CLI 模式下自动监听 stdin。"""
    engine = MCTSEngine(cfg, project_root)

    async def _run_with_cli():
        async def _listen():
            loop = asyncio.get_running_loop()
            while True:
                ch = engine.pending_channel_id
                if ch is None:
                    await asyncio.sleep(0.05)
                    continue
                raw = await loop.run_in_executor(None, input, "\n> 你的决策: ")
                decision = _parse_decision(raw)
                await send_to_channel(ch, decision)
                if decision["action"] in ("stop", "architecture"):
                    break

        listener = asyncio.create_task(_listen())
        best = await engine.run()
        listener.cancel()
        return best

    return asyncio.run(_run_with_cli())


def _parse_decision(raw: str) -> Dict[str, Any]:
    """将 CLI 输入解析为标准 decision dict。"""
    parts = raw.strip().lower().split()
    if not parts:
        return {"action": "continue", "next_mode": "llm", "selected_ids": None}
    action = parts[0]
    next_mode, selected_ids = "llm", None
    if action == "select" and len(parts) >= 2:
        selected_ids = parts[1].split(",")
        if len(parts) >= 3 and parts[2] in ("llm", "enumerate"):
            next_mode = parts[2]
    elif action == "continue" and len(parts) >= 2:
        if parts[1] in ("llm", "enumerate"):
            next_mode = parts[1]
    valid = {"continue", "select", "rollback", "architecture", "stop"}
    return {
        "action":       action if action in valid else "continue",
        "next_mode":    next_mode,
        "selected_ids": selected_ids,
    }
