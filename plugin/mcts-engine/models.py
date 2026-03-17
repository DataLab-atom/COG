"""Data models for MCTS search state."""

from __future__ import annotations

from enum import Enum
from typing import Optional
import time

from pydantic import BaseModel, Field


class Objective(str, Enum):
    MIN = "min"
    MAX = "max"


class NodeStatus(str, Enum):
    ACTIVE = "active"
    FROZEN = "frozen"


class Node(BaseModel):
    """A single search node living on a git branch."""
    branch: str
    parent_branch: str
    op: str
    generation: int
    score: Optional[float] = None
    success: bool = False
    code_hash: Optional[str] = None
    raw_output: Optional[str] = None
    visit_count: int = 1
    ucb_value: float = 0.0
    status: NodeStatus = NodeStatus.ACTIVE
    timestamp: float = Field(default_factory=time.time)


class BatchItem(BaseModel):
    """A single search operation to execute in the next batch."""
    branch: str
    op: str
    parent_branch: str
    target_file: str
    target_function: str
    node_a: str       # branch name of first context node
    node_b: str       # branch name of second context node (may equal node_a)
    direction_hint: str = ""


class SearchConfig(BaseModel):
    """Configuration for a search run."""
    project_root: str
    benchmark_cmd: str
    objective: Objective = Objective.MAX
    beam_width: int = 3
    max_evals: int = 150
    gate_interval: int = 1       # run GateAgent every N generations
    synergy_interval: int = 3    # run synergy check every N generations
    top_k_frontier: int = 3      # keep top K nodes in frontier
    quick_cmd: Optional[str] = None
    protected_patterns: list[str] = Field(default_factory=lambda: [
        "benchmark*.py", "eval*.py", "evaluate*.py",
        "run_eval*", "test_bench*", "*.sh",
    ])
    ops: list[str] = Field(default_factory=lambda: [
        "insert", "merge", "decouple", "parallelize",
        "cache", "stratify", "pipeline",
    ])


class SearchState(BaseModel):
    """Full MCTS search state, persisted to disk."""
    config: SearchConfig
    run_id: str
    generation: int = 0
    total_evals: int = 0
    seed_score: Optional[float] = None
    seed_branch: str = "seed-baseline"
    best_score: Optional[float] = None
    best_branch: Optional[str] = None
    # frontier: active leaf nodes selected by UCB
    frontier: list[str] = Field(default_factory=list)
    # all evaluated nodes, keyed by branch name
    all_nodes: dict[str, Node] = Field(default_factory=dict)
    # code_hash → score (skip duplicate patches)
    score_cache: dict[str, float] = Field(default_factory=dict)
    # synergy records
    synergy_records: list[dict] = Field(default_factory=list)
    # current generation batch (stored server-side)
    current_batch: list[BatchItem] = Field(default_factory=list)
    # consecutive generations without improvement
    consecutive_bad: int = 0
