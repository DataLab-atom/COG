"""
MCTS Search Node
每个 SearchNode 代表搜索树中的一个状态（项目代码的某个版本）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

# ── 搜索空间定义 ──────────────────────────────────────────────
# operators (i=5): 优化的维度类别
OPERATORS = ["compute", "memory", "io", "algorithm", "data_structure"]

# angles (j=3): 优化视角/力度
ANGLES = ["aggressive", "conservative", "bottleneck_focused"]

# N = i×j = 15 个组合（保证多样性，无需随机）
ALL_COMBOS: List[Tuple[str, str]] = [
    (op, angle) for op in OPERATORS for angle in ANGLES
]


@dataclass
class SearchNode:
    """
    MCTS 树中的一个节点，记录从 root 到此节点的累积改动。

    patches: 从 root 到此节点的 patch 列表（按顺序），每个 patch = {
        target_file, target_type, target_name, code
    }
    """

    node_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    generation: int = 0
    parent: Optional[SearchNode] = field(default=None, repr=False)

    # 到达此节点所使用的 (operator, angle) 组合
    op: Optional[str] = None
    angle: Optional[str] = None

    # 从 root 到此节点的累积 patch 集合（包含所有祖先改动）
    patches: List[Dict] = field(default_factory=list)

    # 沙盒执行结果
    score: float = float("-inf")
    metrics: Dict = field(default_factory=dict)
    output_log: str = ""

    # 子节点（beam search 展开后挂载）
    children: List[SearchNode] = field(default_factory=list)

    # ── 搜索空间辅助 ─────────────────────────────────────────

    def tried_combos(self) -> Set[Tuple[str, str]]:
        """沿祖先链收集已尝试过的 (op, angle) 组合。"""
        tried: Set[Tuple[str, str]] = set()
        node: Optional[SearchNode] = self
        while node is not None:
            if node.op and node.angle:
                tried.add((node.op, node.angle))
            node = node.parent
        return tried

    def remaining_combos(
        self, global_tried: Optional[Set[Tuple[str, str]]] = None
    ) -> List[Tuple[str, str]]:
        """
        返回在此节点祖先链中 + 全局历史中均未尝试的 (op, angle) 组合。
        供 enumerate 模式使用。
        """
        tried = self.tried_combos()
        if global_tried:
            tried = tried | global_tried
        return [combo for combo in ALL_COMBOS if combo not in tried]

    # ── 展示 ────────────────────────────────────────────────

    def summary(self) -> str:
        op_str = f"{self.op}×{self.angle}" if self.op else "root"
        score_str = f"{self.score:.4f}" if self.score != float("-inf") else "N/A"
        return f"[{self.node_id}] gen={self.generation} op={op_str} score={score_str}"

    def delta_str(self) -> str:
        """相对父节点的分数变化字符串。"""
        if self.parent is None or self.parent.score == float("-inf"):
            return ""
        d = self.score - self.parent.score
        sign = "+" if d >= 0 else ""
        return f"({sign}{d:.2%})"
