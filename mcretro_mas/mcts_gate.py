"""
Human Gate — open-agents channel 模式的人工确认门。

每一代搜索结束后，engine 阻塞在 wait_for_decision()；
外部调用方展示结果后，通过 send_decision() 注入用户决策，engine 继续。

Decision 结构：
{
    "action":          "continue" | "select" | "rollback" | "architecture" | "stop",
    "next_mode":       "llm" | "enumerate",          # 下一代使用的展开模式
    "selected_ids":    ["node_id1", ...] | None,     # action=="select" 时指定 frontier 节点
}
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from mcretro_mas.mcts_node import ALL_COMBOS, SearchNode


class HumanGate:
    """
    基于 asyncio.Queue 的单向通道（调用方 → engine）。

    用法：
        gate = HumanGate()
        gate.create_channel("gate_gen_1")

        # engine 侧（await）
        decision = await gate.wait_for_decision("gate_gen_1")

        # 调用方侧（可在另一个协程或线程中执行）
        await gate.send_decision("gate_gen_1", {"action": "continue", "next_mode": "llm"})

        gate.close_channel("gate_gen_1")
    """

    def __init__(self) -> None:
        self._channels: Dict[str, asyncio.Queue] = {}

    def create_channel(self, channel_id: str) -> None:
        self._channels[channel_id] = asyncio.Queue(maxsize=1)

    async def wait_for_decision(self, channel_id: str) -> Dict[str, Any]:
        """阻塞等待用户决策（engine 侧调用）。"""
        return await self._channels[channel_id].get()

    async def send_decision(self, channel_id: str, decision: Dict[str, Any]) -> None:
        """注入用户决策（调用方侧调用）。"""
        await self._channels[channel_id].put(decision)

    def close_channel(self, channel_id: str) -> None:
        self._channels.pop(channel_id, None)

    # ── 展示辅助 ────────────────────────────────────────────

    @staticmethod
    def display_generation_summary(
        generation: int,
        mode: str,
        total_expanded: int,
        static_fail_count: int,
        sandbox_fail_count: int,
        improved: List[SearchNode],
        remaining_space: int,
    ) -> None:
        """向终端打印本代搜索摘要，供用户决策使用。"""
        improved_sorted = sorted(improved, key=lambda n: -n.score)

        print(f"\n{'═' * 62}")
        print(f"  Generation {generation}   [mode: {mode}]")
        print(f"{'─' * 62}")
        print(f"  展开分支:       {total_expanded}")
        print(f"  静态检查失败:   {static_fail_count}  → 已丢弃")
        print(f"  沙盒执行失败:   {sandbox_fail_count}  → 已丢弃")
        print(f"  有效改善:       {len(improved_sorted)}")
        print(f"  剩余搜索空间:   {remaining_space} / {len(ALL_COMBOS)} 个组合")

        if improved_sorted:
            print(f"\n  Top candidates:")
            for i, node in enumerate(improved_sorted[:5], 1):
                print(
                    f"    #{i}  [{node.node_id}]  "
                    f"{node.op:<16}×{node.angle:<20}  "
                    f"score={node.score:.4f}  {node.delta_str()}"
                )
        else:
            print("\n  ⚠️  本代无改善分支。")

        print(f"\n{'─' * 62}")
        print("  Actions : [continue] [select <id1,id2,...>] [rollback] [architecture] [stop]")
        print("  Next mode: [llm] [enumerate]")
        print(f"{'═' * 62}")

    @staticmethod
    def display_arch_escalation(generation: int) -> None:
        print(f"\n{'═' * 62}")
        print(f"  ⚠️  Generation {generation}: 全部分支静态检查失败")
        print("  当前架构存在结构性问题，建议升级到 Architecture Redesign 层。")
        print(f"{'─' * 62}")
        print("  Actions: [architecture] [stop]")
        print(f"{'═' * 62}")

    @staticmethod
    def parse_cli_input(raw: str) -> Dict[str, Any]:
        """
        将终端用户输入解析为标准 decision dict。
        示例输入：
            "continue llm"
            "select a1b2,c3d4 enumerate"
            "rollback"
            "architecture"
            "stop"
        """
        parts = raw.strip().lower().split()
        if not parts:
            return {"action": "continue", "next_mode": "llm"}

        action = parts[0]
        next_mode = "llm"
        selected_ids: Optional[List[str]] = None

        if action == "select" and len(parts) >= 2:
            selected_ids = parts[1].split(",")
            if len(parts) >= 3:
                next_mode = parts[2] if parts[2] in ("llm", "enumerate") else "llm"
        elif action == "continue":
            if len(parts) >= 2 and parts[1] in ("llm", "enumerate"):
                next_mode = parts[1]

        return {
            "action": action if action in ("continue", "select", "rollback", "architecture", "stop") else "continue",
            "next_mode": next_mode,
            "selected_ids": selected_ids,
        }
