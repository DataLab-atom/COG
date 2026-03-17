"""UCB-based frontier selection and batch planning for MCTS."""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models import Node, SearchConfig, SearchState


def ucb_score(node: "Node", total_visits: int, exploration: float = 1.41) -> float:
    """UCB1 score for a node."""
    if node.score is None or not node.success:
        return -float("inf")
    exploit = node.score
    explore = exploration * math.sqrt(math.log(max(total_visits, 1)) / max(node.visit_count, 1))
    return exploit + explore


def select_frontier(
    all_nodes: dict[str, "Node"],
    top_k: int,
    is_maximize: bool,
    total_visits: int,
) -> list[str]:
    """Select top-k frontier nodes by UCB score."""
    candidates = [
        n for n in all_nodes.values()
        if n.success and n.score is not None and n.status.value == "active"
    ]
    if not candidates:
        return []

    sign = 1.0 if is_maximize else -1.0
    scored = sorted(
        candidates,
        key=lambda n: sign * ucb_score(n, total_visits),
        reverse=True,
    )
    return [n.branch for n in scored[:top_k]]


def plan_batch(
    state: "SearchState",
) -> list[dict]:
    """Plan next generation batch: frontier nodes × untried ops."""
    is_max = state.config.objective.value == "max"
    frontier = state.frontier or [state.seed_branch]
    ops = state.config.ops
    beam_width = state.config.beam_width

    # Track which (parent_branch, op) combos already exist
    tried = {
        (n.parent_branch, n.op)
        for n in state.all_nodes.values()
    }

    items: list[dict] = []
    budget = state.config.max_evals - state.total_evals

    for parent_branch in frontier:
        untried_ops = [op for op in ops if (parent_branch, op) not in tried]
        # Sample up to beam_width untried ops, prefer less-explored ops
        chosen = untried_ops[:beam_width] if len(untried_ops) <= beam_width \
            else random.sample(untried_ops, beam_width)

        for op in chosen:
            if budget <= 0:
                break
            # Select node_b from frontier (second context for Critic)
            other = [b for b in frontier if b != parent_branch]
            node_b = random.choice(other) if other else parent_branch

            items.append({
                "op": op,
                "parent_branch": parent_branch,
                "node_a": parent_branch,
                "node_b": node_b,
            })
            budget -= 1

    return items


def select_survivors(
    all_nodes: dict[str, "Node"],
    frontier: list[str],
    top_k: int,
    is_maximize: bool,
    total_visits: int,
    best_branch: str | None,
) -> tuple[list[str], list[str]]:
    """Select top-k nodes as new frontier; return (keep, eliminate)."""
    # Consider all successful nodes as candidates for next frontier
    candidates = [
        n for n in all_nodes.values()
        if n.success and n.score is not None and n.status.value == "active"
    ]
    if not candidates:
        return frontier, []

    sign = 1.0 if is_maximize else -1.0
    ranked = sorted(
        candidates,
        key=lambda n: sign * ucb_score(n, total_visits),
        reverse=True,
    )
    keep_nodes = ranked[:top_k]
    keep = [n.branch for n in keep_nodes]

    # Never eliminate the global best
    if best_branch and best_branch not in keep:
        keep.append(best_branch)

    eliminate = [
        b for b in frontier
        if b not in keep and b != "seed-baseline"
    ]
    return keep, eliminate
