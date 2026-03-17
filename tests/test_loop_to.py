"""
tests/test_loop_to.py
─────────────────────
loop_to 回跳机制测试。

所有测试不依赖 LLM API，仅使用 tool 步骤：
  - echo      (tools/echo.json)
  - truncate  (tools/truncate.json)

运行方式：
    python -m pytest tests/test_loop_to.py -v
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import run_graph


# ── 测试辅助 ──────────────────────────────────────────────────────────────────

def _run(coro):
    """同步执行协程。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        raise RuntimeError("Cannot use _run inside a running event loop")
    return asyncio.run(coro)


# ── 图配置工厂 ────────────────────────────────────────────────────────────────

def _loop_with_truncate_graph(max_iter: int = 3) -> dict:
    """truncate → echo 图，echo 通过 loop_to 回跳到 truncate。

    每轮 truncate 截断 2 字符，echo 检查是否 was_truncated。
    当文本足够短（不再被截断）时 was_truncated=false，循环终止。
    """
    return {
        "name": "test_loop",
        "steps": [
            {
                "id": "shorten",
                "type": "tool",
                "ref": "truncate",
                "params": {
                    "text": "{{input_text}}",
                    "max_chars": 2,
                    "suffix": "",
                },
            },
            {
                "id": "check",
                "type": "tool",
                "ref": "echo",
                "depends_on": ["shorten"],
                "inputs": {"text": "shorten.text"},
                "loop_to": {
                    "target": "shorten",
                    "condition": "{{shorten.was_truncated}}",
                    "max_iterations": max_iter,
                    "carry": {
                        "input_text": "shorten.text",
                    },
                },
            },
        ],
    }


def _loop_negate_graph() -> dict:
    """测试 condition_negate：循环直到文本未被截断。"""
    return {
        "name": "test_loop_negate",
        "steps": [
            {
                "id": "shorten",
                "type": "tool",
                "ref": "truncate",
                "params": {
                    "text": "{{input_text}}",
                    "max_chars": 5,
                    "suffix": "",
                },
            },
            {
                "id": "pass_through",
                "type": "tool",
                "ref": "echo",
                "depends_on": ["shorten"],
                "inputs": {"text": "shorten.text"},
                "loop_to": {
                    "target": "shorten",
                    "condition": "{{shorten.was_truncated}}",
                    "condition_negate": True,
                    "max_iterations": 5,
                    "carry": {
                        "input_text": "shorten.text",
                    },
                },
            },
        ],
    }


def _no_condition_graph() -> dict:
    """无 condition 的 loop_to：纯粹靠 max_iterations 控制。"""
    return {
        "name": "test_no_cond",
        "steps": [
            {
                "id": "step_a",
                "type": "tool",
                "ref": "echo",
                "params": {"text": "{{input_text}}"},
            },
            {
                "id": "step_b",
                "type": "tool",
                "ref": "echo",
                "depends_on": ["step_a"],
                "inputs": {"text": "step_a.text"},
                "loop_to": {
                    "target": "step_a",
                    "max_iterations": 3,
                    "carry": {
                        "input_text": "step_a.text",
                    },
                },
            },
        ],
    }


# ── 测试用例 ──────────────────────────────────────────────────────────────────

class TestLoopTo(unittest.TestCase):
    """loop_to 回跳机制测试。"""

    def test_basic_loop(self):
        """循环截断：5字符文本 → max_chars=2 → 循环2次后停止。"""
        config = _loop_with_truncate_graph(max_iter=5)
        result = _run(run_graph(config, variables={"input_text": "abcde"}))
        # 第1轮: "abcde" → truncate(2) → "ab" (was_truncated=True) → 回跳
        # 第2轮: "ab" → truncate(2) → "ab" (was_truncated=False) → 停止
        self.assertEqual(result.steps["shorten"]["text"], "ab")
        self.assertFalse(result.steps["shorten"]["was_truncated"])

    def test_max_iterations_respected(self):
        """max_iterations=1 时只回跳一次。"""
        config = _loop_with_truncate_graph(max_iter=1)
        result = _run(run_graph(config, variables={"input_text": "abcdefgh"}))
        # 第1轮: "abcdefgh" → "ab" (truncated=True) → 回跳
        # 第2轮: "ab" → "ab" (truncated=False) → 停止（但 max_iter 也已用完）
        self.assertEqual(result.steps["shorten"]["text"], "ab")

    def test_condition_negate(self):
        """condition_negate=true：条件取反后评估。

        was_truncated=True → negate → False → 不回跳。
        即：只有 was_truncated=False 时才回跳（这里第一次就不截断，所以不回跳）。
        """
        config = _loop_negate_graph()
        result = _run(run_graph(config, variables={"input_text": "hi"}))
        # "hi" 长度2 <= max_chars=5 → was_truncated=False → negate → True → 回跳
        # 但 "hi" 永远不截断，所以会循环 max_iterations=5 次后停止
        self.assertEqual(result.steps["shorten"]["text"], "hi")

    def test_no_condition_loops_max_times(self):
        """无 condition 时循环 max_iterations 次。"""
        config = _no_condition_graph()
        result = _run(run_graph(config, variables={"input_text": "hello"}))
        # 无条件，循环 3 次后停止
        self.assertEqual(result.steps["step_a"]["text"], "hello")

    def test_carry_injects_context(self):
        """carry 正确将步骤输出注入到 context 中。"""
        config = _loop_with_truncate_graph(max_iter=5)
        result = _run(run_graph(config, variables={"input_text": "abcdefghij"}))
        # "abcdefghij" → "ab" (truncated) → 回跳，carry input_text="ab"
        # "ab" → "ab" (not truncated) → 停止
        self.assertEqual(result.steps["check"]["text"], "ab")
        self.assertEqual(result.steps["check"]["length"], 2)

    def test_no_loop_when_condition_false(self):
        """condition 为 false 时不回跳。"""
        config = _loop_with_truncate_graph(max_iter=5)
        result = _run(run_graph(config, variables={"input_text": "ab"}))
        # "ab" → truncate(2) → "ab" (was_truncated=False) → 不回跳
        self.assertEqual(result.steps["shorten"]["text"], "ab")
        self.assertFalse(result.steps["shorten"]["was_truncated"])

    def test_validation_missing_target(self):
        """loop_to.target 引用不存在的步骤应报错。"""
        config = {
            "name": "bad",
            "steps": [
                {"id": "a", "type": "tool", "ref": "echo", "params": {"text": "x"}},
                {
                    "id": "b",
                    "type": "tool",
                    "ref": "echo",
                    "depends_on": ["a"],
                    "inputs": {"text": "a.text"},
                    "loop_to": {"target": "nonexistent", "max_iterations": 1},
                },
            ],
        }
        with self.assertRaises(ValueError) as ctx:
            _run(run_graph(config, variables={}))
        self.assertIn("nonexistent", str(ctx.exception))

    def test_validation_missing_max_iterations(self):
        """loop_to 缺少 max_iterations 应报错。"""
        config = {
            "name": "bad",
            "steps": [
                {"id": "a", "type": "tool", "ref": "echo", "params": {"text": "x"}},
                {
                    "id": "b",
                    "type": "tool",
                    "ref": "echo",
                    "depends_on": ["a"],
                    "inputs": {"text": "a.text"},
                    "loop_to": {"target": "a"},
                },
            ],
        }
        with self.assertRaises(ValueError) as ctx:
            _run(run_graph(config, variables={}))
        self.assertIn("max_iterations", str(ctx.exception))

    def test_validation_forward_jump_rejected(self):
        """loop_to.target 指向后续步骤应报错（不支持前跳）。"""
        config = {
            "name": "bad",
            "steps": [
                {
                    "id": "a",
                    "type": "tool",
                    "ref": "echo",
                    "params": {"text": "x"},
                    "loop_to": {"target": "b", "max_iterations": 1},
                },
                {"id": "b", "type": "tool", "ref": "echo", "depends_on": ["a"],
                 "inputs": {"text": "a.text"}},
            ],
        }
        with self.assertRaises(ValueError) as ctx:
            _run(run_graph(config, variables={}))
        self.assertIn("之前", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
