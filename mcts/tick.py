"""mcts/tick.py — mcts_init 和 mcts_tick 的 input 实现。

mcts_init : 运行 baseline 沙盒，构造根节点，返回初始 state dict。
mcts_tick : 单代 MCTS 执行（展开 → beam 选择 → Human Gate → 决策），
            返回新 state dict 和 should_continue。

state dict 结构（完全可序列化，graph 层直接可见）：
{
    "nodes":          {node_id: node_dict},   # 所有已探索节点（扁平，无对象引用）
    "frontier_ids":   [node_id, ...],          # 当前 frontier
    "root_id":        node_id,
    "generation":     int,
    "consecutive_bad": int,
    "global_tried":   [[op, angle], ...],      # 全局已尝试组合（可 JSON 序列化）
    "baseline_score": float,
    "baseline_log":   str,
    "best_node_id":   node_id,
    "best_score":     float,
    "mode":           "llm" | "enumerate",
    "history_prompt": str,                     # 给 Critic 的历史上下文
    "history_records": [record_dict, ...],
}

node_dict 结构：
{
    "node_id":    str,
    "generation": int,
    "parent_id":  str | None,
    "op":         str | None,
    "angle":      str | None,
    "patches":    [patch_dict, ...],
    "score":      float,
    "metrics":    dict,
    "output_log": str,
}
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from typing import Any

# ── open-agents 框架路径 ──────────────────────────────────────────────────────
_OA_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "open-agents"))
if _OA_PATH not in sys.path:
    sys.path.insert(0, _OA_PATH)

from utils._base import ToolResult  # noqa: E402
from utils._channel import (  # noqa: E402
    create_channel, close_channel, receive_from_channel,
)
from utils._graph import run_graph_from_file  # noqa: E402

from mcretro_mas.engine_logging import log_event  # noqa: E402
from mcretro_mas.engine_optimizer import AutoOptimizer, CodeInjector  # noqa: E402
from mcretro_mas.mcts_node import ALL_COMBOS  # noqa: E402

# ── 常量 ──────────────────────────────────────────────────────────────────────
BEAM_WIDTH           = 3
CONSECUTIVE_BAD_LIMIT = 3
VARIANTS_PER_COMBO   = 3
_GRAPH_PATH          = "graphs/mcts_single_combo.json"


# ── 节点辅助 ──────────────────────────────────────────────────────────────────

def _make_node(
    node_id=None, generation=0, parent_id=None,
    op=None, angle=None, patches=None,
    score=float("-inf"), metrics=None, output_log="",
) -> dict:
    return {
        "node_id":    node_id or str(uuid.uuid4())[:8],
        "generation": generation,
        "parent_id":  parent_id,
        "op":         op,
        "angle":      angle,
        "patches":    patches or [],
        "score":      score,
        "metrics":    metrics or {},
        "output_log": output_log,
    }


def _node_tried_combos(node_id: str, nodes: dict) -> set:
    """沿祖先链收集已尝试的 (op, angle) 组合。"""
    tried: set = set()
    cur = node_id
    while cur:
        n = nodes.get(cur)
        if n is None:
            break
        if n.get("op") and n.get("angle"):
            tried.add((n["op"], n["angle"]))
        cur = n.get("parent_id")
    return tried


def _remaining_combos(node_id: str, nodes: dict, global_tried: set) -> list:
    tried = _node_tried_combos(node_id, nodes) | global_tried
    return [c for c in ALL_COMBOS if c not in tried]


# ── mcts_init ─────────────────────────────────────────────────────────────────

@dataclass
class MctsInitResult(ToolResult):
    state: dict
    ok: bool


async def mcts_init(
    project_root: str,
    demand: str,
    timeout: float = 300,
) -> MctsInitResult:
    """baseline 沙盒 + 初始 state 构造。"""
    project_root = os.path.abspath(project_root)
    optimizer = AutoOptimizer(
        project_root=project_root,
        entry_point="main.py",
        timeout=int(timeout),
        ignore_list=[
            "__pycache__", "*.pyc", ".git", "venv",
            "data", "datasets", "logs", ".idea", "mcts_history",
        ],
        history_dir=os.path.join(project_root, "mcts_history"),
    )

    loop = asyncio.get_event_loop()
    baseline_score, baseline_log = await loop.run_in_executor(
        None, optimizer.get_baseline_score
    )
    log_event("mcts_init", baseline_score=baseline_score)

    root_id = str(uuid.uuid4())[:8]
    root = _make_node(
        node_id=root_id,
        generation=0,
        score=baseline_score,
        output_log=baseline_log,
    )

    state = {
        "nodes":          {root_id: root},
        "frontier_ids":   [root_id],
        "root_id":        root_id,
        "generation":     0,
        "consecutive_bad": 0,
        "global_tried":   [],
        "baseline_score": baseline_score,
        "baseline_log":   baseline_log,
        "best_node_id":   root_id,
        "best_score":     baseline_score,
        "mode":           "llm",
        "history_prompt": "",
        "history_records": [],
    }
    return MctsInitResult(state=state, ok=True)


# ── mcts_tick ─────────────────────────────────────────────────────────────────

@dataclass
class MctsTickResult(ToolResult):
    state: dict
    should_continue: bool


async def mcts_tick(
    state: dict,
    project_root: str,
    demand: str,
    tree_text: str,
    timeout: float = 300,
) -> MctsTickResult:
    """单代 MCTS 执行。接收 carry 注入的 state，返回新 state 和 should_continue。"""

    # ── 解包 state ────────────────────────────────────────────────────────────
    generation     = state["generation"] + 1
    nodes: dict    = dict(state["nodes"])
    frontier_ids   = list(state["frontier_ids"])
    global_tried   = {tuple(p) for p in state["global_tried"]}
    consecutive_bad = state["consecutive_bad"]
    baseline_log   = state["baseline_log"]
    baseline_score = state["baseline_score"]
    best_node_id   = state["best_node_id"]
    best_score     = state["best_score"]
    history_prompt = state.get("history_prompt", "")
    history_records = list(state.get("history_records", []))
    mode           = state.get("mode", "llm")

    current_mode = (
        "enumerate" if generation == 3
        else ("llm" if generation <= 2 else mode)
    )
    log_event("mcts_gen_start", generation=generation,
              mode=current_mode, frontier_size=len(frontier_ids))

    # ── 1. 并发展开 frontier ──────────────────────────────────────────────────
    graph_path = os.path.join(project_root, _GRAPH_PATH)
    tasks: list = []
    meta_list: list = []

    for node_id in frontier_ids:
        node = nodes[node_id]
        combos = _remaining_combos(node_id, nodes, global_tried)
        for op, angle in combos:
            for _ in range(VARIANTS_PER_COMBO):
                variables = {
                    "op":               op,
                    "angle":            angle,
                    "demand":           demand,
                    "tree_text":        tree_text,
                    "history_prompt":   history_prompt,
                    "baseline_log":     baseline_log,
                    "project_root":     project_root,
                    "ancestor_patches": node["patches"],
                    "sandbox_timeout":  float(timeout),
                }
                tasks.append(asyncio.create_task(
                    run_graph_from_file(graph_path, variables=variables,
                                        base_dir=project_root)
                ))
                meta_list.append({"node_id": node_id, "op": op, "angle": angle})

    if not tasks:
        log_event("mcts_no_combos_left")
        return MctsTickResult(
            state={**state, "generation": generation},
            should_continue=False,
        )

    raw = await asyncio.gather(*tasks, return_exceptions=True)

    all_results: list[dict] = []
    for meta, r in zip(meta_list, raw):
        if isinstance(r, Exception):
            log_event("mcts_combo_error", op=meta["op"], angle=meta["angle"], error=str(r))
            continue
        out = r.final if isinstance(r.final, dict) else {}
        if "result" not in out:
            out["result"] = r.steps.get("sandbox", {})
        if "ast_ok" not in out:
            out["ast_ok"] = r.steps.get("ast_check", {}).get("ok", False)
        out["_meta"] = meta
        all_results.append(out)

    static_ok    = [r for r in all_results if r.get("ast_ok")]
    static_fail  = len(all_results) - len(static_ok)
    sandbox_ok   = [r for r in static_ok if r.get("result", {}).get("ok")]
    sandbox_fail = len(static_ok) - len(sandbox_ok)

    log_event("mcts_gen_stats", generation=generation, total=len(all_results),
              static_fail=static_fail, sandbox_fail=sandbox_fail,
              sandbox_ok=len(sandbox_ok))

    # ── 2. 全静态失败 → 架构升级 ─────────────────────────────────────────────
    if not static_ok:
        _print_arch_escalation(generation)
        await _gate_wait(f"gate_arch_{generation}")
        return MctsTickResult(
            state={**state, "generation": generation},
            should_continue=False,
        )

    # ── 3. 构建子节点 ─────────────────────────────────────────────────────────
    new_nodes: dict[str, dict] = {}
    improved_ids: list[str]    = []

    for r in sandbox_ok:
        meta     = r.get("_meta", {})
        parent_id = meta.get("node_id")
        parent   = nodes.get(parent_id, {})
        op       = r.get("op") or meta.get("op", "")
        angle    = r.get("angle") or meta.get("angle", "")
        result   = r.get("result", {})
        patches  = result.get("all_patches", [])

        if not parent_id or not patches or result.get("score", -999) <= -999:
            continue

        child_id = str(uuid.uuid4())[:8]
        child = _make_node(
            node_id=child_id,
            generation=generation,
            parent_id=parent_id,
            op=op,
            angle=angle,
            patches=patches,
            score=result.get("score", -999.0),
            metrics=result.get("metrics", {}),
            output_log=result.get("output_log", ""),
        )
        new_nodes[child_id] = child

        if child["score"] > parent.get("score", float("-inf")):
            improved_ids.append(child_id)

        if child["score"] > best_score:
            best_score   = child["score"]
            best_node_id = child_id

    all_nodes = {**nodes, **new_nodes}

    # ── 4. Human Gate ─────────────────────────────────────────────────────────
    remaining_count  = len(ALL_COMBOS) - len(global_tried)
    improved_nodes   = [all_nodes[nid] for nid in improved_ids]
    _print_summary(generation, current_mode, len(all_results),
                   static_fail, sandbox_fail, improved_nodes, remaining_count)

    decision  = await _gate_wait(f"gate_gen_{generation}")
    action    = decision.get("action", "continue")
    next_mode = decision.get("next_mode", current_mode)
    sel_ids   = decision.get("selected_ids")

    log_event("mcts_gate_decision", generation=generation,
              action=action, next_mode=next_mode)

    # ── 5. 处理决策 ───────────────────────────────────────────────────────────
    if action in ("stop", "architecture"):
        return MctsTickResult(
            state={**state, "nodes": all_nodes, "generation": generation,
                   "best_node_id": best_node_id, "best_score": best_score},
            should_continue=False,
        )

    if action == "rollback":
        new_frontier = _rollback(frontier_ids, all_nodes)
        return MctsTickResult(
            state={
                **state,
                "nodes":          all_nodes,
                "frontier_ids":   new_frontier or frontier_ids,
                "generation":     generation,
                "consecutive_bad": 0,
                "best_node_id":   best_node_id,
                "best_score":     best_score,
                "mode":           next_mode,
            },
            should_continue=bool(new_frontier),
        )

    # continue / select
    if improved_ids:
        pool = improved_ids
        if sel_ids:
            pool = [nid for nid in improved_ids
                    if all_nodes[nid]["node_id"] in sel_ids] or improved_ids
        frontier_ids = sorted(pool, key=lambda nid: -all_nodes[nid]["score"])[:BEAM_WIDTH]
        consecutive_bad = 0
        for nid in frontier_ids:
            n = all_nodes[nid]
            if n.get("op") and n.get("angle"):
                global_tried.add((n["op"], n["angle"]))

        # 写回最优节点
        best_node = all_nodes[best_node_id]
        if best_node["score"] > baseline_score and best_node.get("patches"):
            _apply_best(best_node, project_root)
            baseline_log = best_node.get("output_log", baseline_log)
    else:
        consecutive_bad += 1
        log_event("mcts_no_improvement", generation=generation,
                  consecutive_bad=consecutive_bad)
        if consecutive_bad >= CONSECUTIVE_BAD_LIMIT:
            frontier_ids    = _rollback(frontier_ids, all_nodes) or frontier_ids
            consecutive_bad = 0

    # ── 6. 更新历史 ───────────────────────────────────────────────────────────
    updated_records, new_history_prompt = _update_history(
        generation, list(new_nodes.values()), history_records, baseline_score
    )

    new_state = {
        "nodes":          all_nodes,
        "frontier_ids":   frontier_ids,
        "root_id":        state["root_id"],
        "generation":     generation,
        "consecutive_bad": consecutive_bad,
        "global_tried":   [list(p) for p in global_tried],
        "baseline_score": baseline_score,
        "baseline_log":   baseline_log,
        "best_node_id":   best_node_id,
        "best_score":     best_score,
        "mode":           next_mode,
        "history_prompt": new_history_prompt,
        "history_records": updated_records,
    }
    return MctsTickResult(state=new_state, should_continue=bool(frontier_ids))


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

async def _gate_wait(channel_id: str) -> dict:
    """创建 channel → 阻塞等待决策 → 关闭 channel。"""
    create_channel(channel_id)
    log_event("mcts_gate_open", channel_id=channel_id)
    decision = await receive_from_channel(channel_id)
    close_channel(channel_id)
    log_event("mcts_gate_closed", channel_id=channel_id)
    return decision


def _rollback(frontier_ids: list, nodes: dict) -> list:
    parents, seen = [], set()
    for nid in frontier_ids:
        pid = nodes[nid].get("parent_id")
        if pid and pid not in seen:
            parents.append(pid)
            seen.add(pid)
    log_event("mcts_rollback", old=len(frontier_ids), new=len(parents))
    return parents


def _apply_best(node: dict, project_root: str) -> None:
    for patch in node.get("patches", []):
        rel  = patch["target_file"].lstrip("/").replace("\\", "/")
        path = os.path.join(project_root, rel)
        if os.path.exists(path):
            shutil.copy(path, path + ".bak")
        CodeInjector.replace_code_block(
            path, patch["target_type"], patch["target_name"], patch["code"]
        )
    log_event("mcts_best_applied", score=node["score"],
              patches=len(node.get("patches", [])))


def _update_history(
    generation: int,
    new_nodes: list[dict],
    history_records: list[dict],
    baseline_score: float,
) -> tuple[list, str]:
    valid = [n for n in new_nodes if n["score"] > -999 and n.get("patches")]
    if not valid:
        return history_records, _build_history_prompt(history_records)

    best = max(valid, key=lambda n: n["score"])
    last = best["patches"][-1]
    record = {
        "generation":  generation,
        "target_file": last["target_file"],
        "target_type": last["target_type"],
        "target_name": last["target_name"],
        "direction":   f"{best.get('op','')} × {best.get('angle','')}",
        "score":       best["score"],
        "reasoning":   f"MCTS gen {generation}, score={best['score']:.4f}",
        "status":      "SUCCESS" if best["score"] > baseline_score else "STAGNANT",
    }
    updated = history_records + [record]
    return updated, _build_history_prompt(updated)


def _build_history_prompt(records: list[dict]) -> str:
    if not records:
        return "[History]\nNo optimization history yet."
    lines = ["[Optimization History]"]
    for r in records[-10:]:
        lines.append(
            f"  Gen {r['generation']} | {r['direction']} "
            f"| score={r['score']:.4f} | {r['status']}"
        )
    return "\n".join(lines)


def _print_summary(
    generation: int, mode: str,
    total: int, static_fail: int, sandbox_fail: int,
    improved: list[dict], remaining: int,
) -> None:
    s = sorted(improved, key=lambda n: -n["score"])
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
            print(f"    #{i}  [{n['node_id']}]  "
                  f"{n.get('op',''):<16}×{n.get('angle',''):<20}  "
                  f"score={n['score']:.4f}")
    else:
        print("\n  本代无改善分支。")
    print(f"{'─'*62}")
    print("  Actions : [continue] [select <id1,id2>] [rollback] [architecture] [stop]")
    print(f"{'═'*62}")


def _print_arch_escalation(generation: int) -> None:
    print(f"\n{'═'*62}")
    print(f"  Generation {generation}: 全部分支静态检查失败")
    print("  建议升级到 Architecture Redesign 层。")
    print("  Actions: [architecture] [stop]")
    print(f"{'═'*62}")
