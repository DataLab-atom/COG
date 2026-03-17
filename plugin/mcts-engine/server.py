"""
COG MCTS Engine — MCP Server

Handles all deterministic MCTS bookkeeping:
search state, UCB selection, batch planning, node registration, score cache.

The agents call these tools; the LLM handles code generation and reflection.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from models import (
    BatchItem,
    Node,
    NodeStatus,
    Objective,
    SearchConfig,
    SearchState,
)
from selection import plan_batch, select_frontier, select_survivors

mcp = FastMCP("mcts-engine", instructions="COG MCTS search engine bookkeeping")

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

_STATE_DIR = os.environ.get("COG_STATE_DIR", os.path.expanduser("~/.openclaw/mcts-state"))
_state: SearchState | None = None


def _state_path() -> Path:
    return Path(_STATE_DIR) / "state.json"


def _save() -> None:
    if _state is None:
        return
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_state.model_dump_json(indent=2))


def _load() -> SearchState | None:
    p = _state_path()
    if p.exists():
        return SearchState.model_validate_json(p.read_text())
    return None


def _get_state() -> SearchState:
    global _state
    if _state is None:
        _state = _load()
    if _state is None:
        raise RuntimeError("Search not initialized. Call mcts_init first.")
    return _state


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def mcts_init(
    project_root: str,
    benchmark_cmd: str,
    baseline_score: float,
    objective: str = "max",
    beam_width: int = 3,
    max_evals: int = 150,
    gate_interval: int = 1,
    synergy_interval: int = 3,
    top_k_frontier: int = 3,
    quick_cmd: str = "",
) -> dict:
    """Initialize a new MCTS search run.

    Args:
        project_root: Path to the target git repository.
        benchmark_cmd: Command to run for score evaluation.
        baseline_score: Measured baseline score (run before calling this).
        objective: 'max' or 'min'.
        beam_width: Number of ops to try per frontier node per generation.
        max_evals: Maximum number of benchmark evaluations.
        gate_interval: Run GateAgent every N generations.
        synergy_interval: Run synergy check every N generations.
        top_k_frontier: Keep top K nodes in frontier after selection.
        quick_cmd: Optional fast pre-filter command.
    """
    global _state

    run_id = uuid.uuid4().hex[:6]
    config = SearchConfig(
        project_root=project_root,
        benchmark_cmd=benchmark_cmd,
        objective=Objective(objective),
        beam_width=beam_width,
        max_evals=max_evals,
        gate_interval=gate_interval,
        synergy_interval=synergy_interval,
        top_k_frontier=top_k_frontier,
        quick_cmd=quick_cmd or None,
    )
    _state = SearchState(
        config=config,
        run_id=run_id,
        seed_score=baseline_score,
        best_score=baseline_score,
        best_branch="seed-baseline",
        frontier=["seed-baseline"],
    )
    _save()

    return {
        "status": "initialized",
        "run_id": run_id,
        "project_root": project_root,
        "objective": objective,
        "baseline_score": baseline_score,
        "max_evals": max_evals,
    }


@mcp.tool()
def mcts_register_targets(targets: list[dict]) -> dict:
    """Register optimization targets identified by MapAgent.

    Args:
        targets: List of targets, each with keys:
            id, file, function, lines (optional), impact (optional), description (optional).
    """
    state = _get_state()
    # Store targets as part of config metadata (lightweight — just for memory paths)
    if not hasattr(state.config, "_targets"):
        state.config.__dict__["_targets"] = {}
    for t in targets:
        state.config.__dict__.setdefault("_targets", {})[t["id"]] = t
    _save()
    return {"registered": len(targets), "target_ids": [t["id"] for t in targets]}


@mcp.tool()
def mcts_check_cache(op: str, code_hash: str) -> dict:
    """Check if this (op, code) combination was already evaluated.

    Args:
        op: The MCTS operation type.
        code_hash: SHA256 hash of the target file content on the parent branch.
    """
    state = _get_state()
    cache_key = f"{op}:{code_hash}"
    if cache_key in state.score_cache:
        return {"cached": True, "score": state.score_cache[cache_key]}
    return {"cached": False}


@mcp.tool()
def mcts_get_lineage(branch: str) -> dict:
    """Trace the full ancestry of a branch through all_nodes.

    Args:
        branch: The branch to trace.
    """
    state = _get_state()
    lineage = []
    visited: set[str] = set()
    queue = [branch]
    while queue:
        b = queue.pop(0)
        if b in visited or b not in state.all_nodes:
            continue
        visited.add(b)
        node = state.all_nodes[b]
        lineage.append({
            "branch": node.branch,
            "parent_branch": node.parent_branch,
            "op": node.op,
            "generation": node.generation,
            "score": node.score,
            "success": node.success,
        })
        queue.append(node.parent_branch)
    return {"branch": branch, "lineage": lineage}


@mcp.tool()
def mcts_record_synergy(
    branch: str,
    op_branches: list[str],
    score: float,
    success: bool,
) -> dict:
    """Record the result of a synergy (cross-op combination) experiment.

    Args:
        branch: The synergy branch.
        op_branches: Source branches that were cherry-picked together.
        score: Combined score.
        success: Whether it succeeded.
    """
    state = _get_state()
    record = {
        "branch": branch,
        "generation": state.generation,
        "op_branches": op_branches,
        "score": score,
        "success": success,
    }
    state.synergy_records.append(record)
    _save()
    return record


@mcp.tool()
def mcts_freeze_branch(branch: str) -> dict:
    """Freeze a branch — stop exploring from it.

    Args:
        branch: The branch to freeze.
    """
    state = _get_state()
    if branch in state.all_nodes:
        state.all_nodes[branch].status = NodeStatus.FROZEN
    if branch in state.frontier:
        state.frontier.remove(branch)
    _save()
    return {"branch": branch, "status": "frozen"}


@mcp.tool()
def mcts_boost_branch(branch: str) -> dict:
    """Boost a branch — prioritize it in the next generation.

    Args:
        branch: The branch to boost (must be a known node).
    """
    state = _get_state()
    if branch in state.all_nodes:
        node = state.all_nodes[branch]
        node.status = NodeStatus.ACTIVE
        node.visit_count = max(1, node.visit_count // 2)  # lower visits → higher UCB
        if branch not in state.frontier:
            state.frontier.append(branch)
    _save()
    return {"branch": branch, "status": "boosted"}


@mcp.tool()
def mcts_get_status() -> dict:
    """Get current search status."""
    state = _get_state()
    improvement = None
    if state.seed_score and state.best_score is not None and state.seed_score != 0:
        pct = (state.best_score - state.seed_score) / abs(state.seed_score) * 100
        improvement = f"{pct:+.1f}%"
    return {
        "run_id": state.run_id,
        "generation": state.generation,
        "total_evals": state.total_evals,
        "budget_remaining": state.config.max_evals - state.total_evals,
        "seed_score": state.seed_score,
        "best_score": state.best_score,
        "best_branch": state.best_branch,
        "improvement": improvement,
        "frontier_size": len(state.frontier),
        "frontier": state.frontier,
        "total_nodes": len(state.all_nodes),
        "consecutive_bad": state.consecutive_bad,
    }


# ---------------------------------------------------------------------------
# mcts_step — main state machine
# ---------------------------------------------------------------------------

_PHASE_BEGIN    = "begin_generation"
_PHASE_CODE     = "code_ready"
_PHASE_POL_PASS = "policy_pass"
_PHASE_POL_FAIL = "policy_fail"
_PHASE_FITNESS  = "fitness_ready"
_PHASE_SELECT   = "select"
_PHASE_GATE     = "gate_done"
_PHASE_REFLECT  = "reflect_done"
_PHASE_DONE     = "done"


@mcp.tool()
def mcts_step(
    phase: str,
    branch: str = "",
    parent_commit: str = "",
    fitness: float = 0.0,
    success: bool = True,
    op: str = "",
    parent_branch: str = "",
    code_hash: str = "",
    raw_output: str = "",
    reason: str = "",
    action: str = "",
    selected_branch: str = "",
) -> dict:
    """MCTS loop state machine driver.

    Called by OrchestratorAgent and ComboAgents to advance the search.

    Phases:
      "begin_generation"  → {action="dispatch_combos", generation, items=[...]}
      "code_ready"        → {action="check_policy", diff, changed_files, ...}
      "policy_pass"       → {action="run_benchmark", branch, op, parent_branch}
      "policy_fail"       → {action="worker_done", rejected=True, reason}
      "fitness_ready"     → {action="worker_done", fitness, success, is_new_best}
      "select"            → {action="gate"|"reflect", keep, eliminate, ...}
      "gate_done"         → {action="reflect"} | {action="done"}
      "reflect_done"      → {action="dispatch_combos"} | {action="done"}
    """
    state = _get_state()

    # ------------------------------------------------------------------ begin
    if phase == _PHASE_BEGIN:
        return _begin_generation(state)

    # ------------------------------------------------------------------ code_ready
    if phase == _PHASE_CODE:
        if not branch:
            return {"error": "branch required for code_ready"}
        item = next((it for it in state.current_batch if it.branch == branch), None)

        parent = parent_commit
        if not parent and item:
            r = subprocess.run(
                ["git", "-C", state.config.project_root, "rev-parse", item.parent_branch],
                capture_output=True, text=True,
            )
            parent = r.stdout.strip() if r.returncode == 0 else item.parent_branch

        names_r = subprocess.run(
            ["git", "-C", state.config.project_root, "diff", "--name-only", f"{parent}..{branch}"],
            capture_output=True, text=True,
        )
        changed = [f for f in names_r.stdout.strip().splitlines() if f]

        diff_r = subprocess.run(
            ["git", "-C", state.config.project_root, "diff", f"{parent}..{branch}"],
            capture_output=True, text=True,
        )
        return {
            "action": "check_policy",
            "branch": branch,
            "parent_commit": parent,
            "op": item.op if item else op,
            "target_file": item.target_file if item else "",
            "parent_branch": item.parent_branch if item else parent_branch,
            "changed_files": changed,
            "diff": diff_r.stdout[:8000],
            "protected_patterns": state.config.protected_patterns,
        }

    # ------------------------------------------------------------------ policy_pass
    if phase == _PHASE_POL_PASS:
        if not branch:
            return {"error": "branch required for policy_pass"}
        item = next((it for it in state.current_batch if it.branch == branch), None)
        return {
            "action": "run_benchmark",
            "branch": branch,
            "op": item.op if item else op,
            "parent_branch": item.parent_branch if item else parent_branch,
        }

    # ------------------------------------------------------------------ policy_fail
    if phase == _PHASE_POL_FAIL:
        if not branch:
            return {"error": "branch required for policy_fail"}
        item = next((it for it in state.current_batch if it.branch == branch), None)
        fail_reason = reason or "policy violation"
        node = Node(
            branch=branch,
            parent_branch=item.parent_branch if item else parent_branch,
            op=item.op if item else op,
            generation=state.generation,
            success=False,
            raw_output=f"policy_violation: {fail_reason}",
        )
        state.all_nodes[branch] = node
        _save()
        return {"action": "worker_done", "branch": branch, "rejected": True, "reason": fail_reason}

    # ------------------------------------------------------------------ fitness_ready
    if phase == _PHASE_FITNESS:
        is_max = state.config.objective == Objective.MAX

        # Cache hit
        cache_key = f"{op}:{code_hash}" if code_hash else ""
        if cache_key and cache_key in state.score_cache:
            state.total_evals += 1
            _save()
            return {
                "action": "worker_done",
                "branch": branch,
                "cached": True,
                "fitness": state.score_cache[cache_key],
                "total_evals": state.total_evals,
            }

        node = Node(
            branch=branch,
            parent_branch=parent_branch,
            op=op,
            generation=state.generation,
            score=fitness if success else None,
            success=success,
            code_hash=code_hash or None,
            raw_output=raw_output[:500] if raw_output else None,
        )
        state.all_nodes[branch] = node
        state.total_evals += 1

        if code_hash and success:
            state.score_cache[f"{op}:{code_hash}"] = fitness

        # Update best
        is_new_best = False
        if success:
            if state.best_score is None:
                state.best_score = fitness
                state.best_branch = branch
                is_new_best = True
            elif is_max and fitness > state.best_score:
                state.best_score = fitness
                state.best_branch = branch
                is_new_best = True
            elif not is_max and fitness < state.best_score:
                state.best_score = fitness
                state.best_branch = branch
                is_new_best = True

        _save()
        return {
            "action": "worker_done",
            "branch": branch,
            "fitness": fitness,
            "success": success,
            "is_new_best": is_new_best,
            "total_evals": state.total_evals,
        }

    # ------------------------------------------------------------------ select
    if phase == _PHASE_SELECT:
        is_max = state.config.objective == Objective.MAX
        total_visits = sum(n.visit_count for n in state.all_nodes.values())

        keep, eliminate = select_survivors(
            all_nodes=state.all_nodes,
            frontier=state.frontier,
            top_k=state.config.top_k_frontier,
            is_maximize=is_max,
            total_visits=total_visits,
            best_branch=state.best_branch,
        )
        state.frontier = keep

        # Stagnation tracking
        gen_nodes = [n for n in state.all_nodes.values() if n.generation == state.generation]
        gen_improved = any(n.branch == state.best_branch for n in gen_nodes)
        state.consecutive_bad = 0 if gen_improved else state.consecutive_bad + 1

        state.generation += 1
        state.current_batch = []   # clear so next begin_generation plans fresh
        _save()

        # Build tree text for GateAgent
        tree_text = _build_tree_text(state)
        top_nodes = [
            {"branch": b, "score": state.all_nodes[b].score, "op": state.all_nodes[b].op}
            for b in keep if b in state.all_nodes
        ]

        # Gate or reflect?
        run_gate = (state.generation % state.config.gate_interval == 0)
        next_action = "gate" if run_gate else "reflect"

        return {
            "action": next_action,
            "keep": keep,
            "eliminate": eliminate,
            "best_branch": state.best_branch,
            "best_score": state.best_score,
            "top_nodes": top_nodes,
            "tree_text": tree_text,
            "generation": state.generation,
        }

    # ------------------------------------------------------------------ gate_done
    if phase == _PHASE_GATE:
        # Handle gate response actions
        if action == "stop":
            return {"action": _PHASE_DONE, "reason": "user stopped",
                    "best_branch": state.best_branch, "best_score": state.best_score}
        if action == "rollback" and state.generation > 1:
            # Revert frontier to previous generation's nodes
            prev_gen = state.generation - 2
            prev_nodes = [
                b for b, n in state.all_nodes.items()
                if n.generation == prev_gen and n.success
            ]
            if prev_nodes:
                state.frontier = prev_nodes[:state.config.top_k_frontier]
                _save()
        if action == "select" and selected_branch:
            state.frontier = [selected_branch]
            _save()
        if action == "freeze" and selected_branch:
            mcts_freeze_branch(selected_branch)
        if action == "boost" and selected_branch:
            mcts_boost_branch(selected_branch)
        return {"action": "reflect"}

    # ------------------------------------------------------------------ reflect_done
    if phase == _PHASE_REFLECT:
        budget = state.config.max_evals - state.total_evals
        if budget <= 0:
            return {"action": _PHASE_DONE, "reason": "budget exhausted",
                    "total_evals": state.total_evals, "best_score": state.best_score}
        return _begin_generation(state)

    return {"error": f"Unknown phase: {phase!r}"}


def _begin_generation(state: SearchState) -> dict:
    budget = state.config.max_evals - state.total_evals
    if budget <= 0:
        return {"action": _PHASE_DONE, "reason": "budget exhausted",
                "total_evals": state.total_evals}

    # Crash recovery: if we have an existing batch, return only unevaluated items.
    # This prevents duplicate evaluations when the process restarts mid-batch.
    if state.current_batch:
        evaluated = set(state.all_nodes.keys())
        remaining = [item for item in state.current_batch if item.branch not in evaluated]
        if remaining:
            return {
                "action": "dispatch_combos",
                "generation": state.generation,
                "batch_size": len(remaining),
                "items": [item.model_dump() for item in remaining],
                "resumed": True,
            }

    is_max = state.config.objective == Objective.MAX
    total_visits = sum(n.visit_count for n in state.all_nodes.values())

    # Refresh frontier by UCB
    state.frontier = select_frontier(
        all_nodes=state.all_nodes,
        top_k=state.config.top_k_frontier,
        is_maximize=is_max,
        total_visits=total_visits,
    ) or [state.seed_branch]

    raw_items = plan_batch(state)
    if not raw_items:
        return {"action": _PHASE_DONE, "reason": "all op combinations exhausted",
                "total_evals": state.total_evals}

    # Build BatchItems and assign branch names
    batch: list[BatchItem] = []
    for raw in raw_items[:budget]:
        target_info = _find_target(state)
        branch = f"mcts/{state.run_id}/gen-{state.generation}/{raw['op']}-{uuid.uuid4().hex[:8]}"
        batch.append(BatchItem(
            branch=branch,
            op=raw["op"],
            parent_branch=raw["parent_branch"],
            target_file=target_info.get("file", ""),
            target_function=target_info.get("function", ""),
            node_a=raw["node_a"],
            node_b=raw["node_b"],
        ))

    state.current_batch = batch
    _save()

    return {
        "action": "dispatch_combos",
        "generation": state.generation,
        "batch_size": len(batch),
        "items": [item.model_dump() for item in batch],
    }


def _find_target(state: SearchState) -> dict:
    """Return first registered target info, or empty dict."""
    targets = state.config.__dict__.get("_targets", {})
    if targets:
        return next(iter(targets.values()))
    return {}


def _build_tree_text(state: SearchState) -> str:
    """Build a compact ASCII tree of the search history."""
    lines = [f"[root] score={state.seed_score} baseline"]
    for branch, node in sorted(state.all_nodes.items(), key=lambda x: x[1].generation):
        indent = "  " * node.generation
        marker = " ← best" if branch == state.best_branch else ""
        score_str = f"{node.score:.4f}" if node.score is not None else "FAILED"
        lines.append(f"{indent}[gen{node.generation}/{node.op}] {score_str}{marker}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
