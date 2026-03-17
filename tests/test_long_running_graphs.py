"""
tests/test_long_running_graphs.py
──────────────────────────────────
20 个长期运行图集成测试。

所有测试均不依赖 LLM API，仅使用 tool / input 步骤：
  - echo      (tools/echo.json)       ← 原样返回文本
  - truncate  (tools/truncate.json)   ← 截断文本
  - render_template (tools/)          ← 模板渲染
  - read_channel (input/)             ← 通道读取（long_running）

运行方式：
    python -m pytest tests/test_long_running_graphs.py -v
    # 或直接运行：
    python tests/test_long_running_graphs.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

# ── 确保从项目根目录导入 ────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import (
    ChannelClosedError,
    channel_exists,
    close_channel,
    create_channel,
    list_channels,
    run_graph,
    run_graph_stream,
    send_to_channel,
    validate_all_configs,
    validate_long_running_propagation,
)
from utils import channel_registry as _registry


# ── 测试辅助 ──────────────────────────────────────────────────────────────────

def _cleanup():
    """清除所有残留通道，保证测试隔离。"""
    _registry.clear()


# ── 内联图配置工厂 ─────────────────────────────────────────────────────────────

def _echo_graph() -> dict:
    """read_channel → echo 的基础长期运行图。"""
    return {
        "name": "test_echo",
        "long_running": True,
        "variables": ["channel_id"],
        "steps": [
            {
                "id": "recv",
                "type": "input",
                "ref": "read_channel",
                "params": {"channel_id": "{{channel_id}}"},
            },
            {
                "id": "out",
                "type": "tool",
                "ref": "echo",
                "depends_on": ["recv"],
                "inputs": {"text": "recv.data"},
            },
        ],
    }


def _truncate_graph() -> dict:
    """read_channel → truncate 的基础长期运行图。"""
    return {
        "name": "test_truncate",
        "long_running": True,
        "variables": ["channel_id"],
        "steps": [
            {
                "id": "recv",
                "type": "input",
                "ref": "read_channel",
                "params": {"channel_id": "{{channel_id}}"},
            },
            {
                "id": "out",
                "type": "tool",
                "ref": "truncate",
                "depends_on": ["recv"],
                "inputs": {"text": "recv.data", "max_chars": 20},
            },
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
# 测试类
# ══════════════════════════════════════════════════════════════════════════════

class TestLongRunningGraphs(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        _cleanup()

    async def asyncTearDown(self):
        _cleanup()

    # ── 01: 基本长期运行图（单条字符串消息）─────────────────────────────────────

    async def test_01_basic_single_message(self):
        """从通道接收一条字符串，经 echo 原样返回。"""
        ch = "ch_01"
        create_channel(ch)
        task = asyncio.create_task(run_graph(_echo_graph(), {"channel_id": ch}))
        await asyncio.sleep(0)
        await send_to_channel(ch, "hello open-agents")
        result = await task

        self.assertEqual(result.steps["out"]["text"], "hello open-agents")
        self.assertEqual(result.steps["out"]["length"], len("hello open-agents"))

    # ── 02: 图启动后阻塞等待（时序验证）───────────────────────────────────────

    async def test_02_graph_waits_for_data(self):
        """图启动后在通道处阻塞，数据到达后才继续。"""
        ch = "ch_02"
        create_channel(ch)
        task = asyncio.create_task(run_graph(_echo_graph(), {"channel_id": ch}))

        await asyncio.sleep(0.01)
        self.assertFalse(task.done(), "图应在等待通道数据，尚未完成")

        await send_to_channel(ch, "delayed data")
        result = await task
        self.assertEqual(result.steps["out"]["text"], "delayed data")

    # ── 03: 关闭通道触发 ChannelClosedError ────────────────────────────────────

    async def test_03_close_channel_propagates_error(self):
        """关闭通道时，阻塞中的图节点收到 ChannelClosedError。"""
        ch = "ch_03"
        create_channel(ch)
        task = asyncio.create_task(run_graph(_echo_graph(), {"channel_id": ch}))
        await asyncio.sleep(0.01)
        close_channel(ch)

        with self.assertRaises(ChannelClosedError):
            await task

    # ── 04: 数据在图启动前预先写入通道 ──────────────────────────────────────────

    async def test_04_data_preloaded_before_graph_starts(self):
        """通道在图启动前已有数据，图启动后立即读取，无需等待。"""
        ch = "ch_04"
        create_channel(ch)
        await send_to_channel(ch, "preloaded message")

        result = await run_graph(_echo_graph(), {"channel_id": ch})
        self.assertEqual(result.steps["out"]["text"], "preloaded message")

    # ── 05: 两个顺序通道读取 ──────────────────────────────────────────────────

    async def test_05_two_sequential_channel_reads(self):
        """图含两个顺序 read_channel 步骤，依次读取两条消息。"""
        ch = "ch_05"
        config = {
            "name": "two_reads",
            "long_running": True,
            "variables": ["channel_id"],
            "steps": [
                {"id": "r1", "type": "input", "ref": "read_channel",
                 "params": {"channel_id": "{{channel_id}}"}},
                {"id": "r2", "type": "input", "ref": "read_channel",
                 "depends_on": ["r1"],
                 "params": {"channel_id": "{{channel_id}}"}},
                {"id": "out1", "type": "tool", "ref": "echo",
                 "depends_on": ["r1"], "inputs": {"text": "r1.data"}},
                {"id": "out2", "type": "tool", "ref": "echo",
                 "depends_on": ["r2"], "inputs": {"text": "r2.data"}},
            ],
        }
        create_channel(ch)
        task = asyncio.create_task(run_graph(config, {"channel_id": ch}))
        await asyncio.sleep(0)
        await send_to_channel(ch, "first")
        await send_to_channel(ch, "second")
        result = await task

        self.assertEqual(result.steps["r1"]["data"], "first")
        self.assertEqual(result.steps["r2"]["data"], "second")
        self.assertEqual(result.steps["out1"]["text"], "first")
        self.assertEqual(result.steps["out2"]["text"], "second")

    # ── 06: 长期运行节点作为中间节点（非终端）──────────────────────────────────

    async def test_06_long_running_as_intermediate_node(self):
        """read_channel 为中间节点，下游仍有 truncate 工具步骤。"""
        ch = "ch_06"
        create_channel(ch)
        task = asyncio.create_task(
            run_graph(_truncate_graph(), {"channel_id": ch})
        )
        await asyncio.sleep(0)
        await send_to_channel(ch, "a very long message that should be truncated here")
        result = await task

        self.assertTrue(result.steps["out"]["was_truncated"])
        self.assertLessEqual(len(result.steps["out"]["text"]), 20)

    # ── 07: 嵌套字段访问（inputs 连线多级访问）────────────────────────────────

    async def test_07_nested_field_access_via_inputs(self):
        """inputs 连线支持 receive.data.key 多级嵌套访问（bug 修复验证）。"""
        ch = "ch_07"
        config = {
            "name": "nested_access",
            "long_running": True,
            "variables": ["channel_id"],
            "steps": [
                {"id": "recv", "type": "input", "ref": "read_channel",
                 "params": {"channel_id": "{{channel_id}}"}},
                {"id": "out", "type": "tool", "ref": "echo",
                 "depends_on": ["recv"],
                 # 嵌套访问：recv.data.message（需要 bug 修复后才正确）
                 "inputs": {"text": "recv.data.message"}},
            ],
        }
        create_channel(ch)
        task = asyncio.create_task(run_graph(config, {"channel_id": ch}))
        await asyncio.sleep(0)
        await send_to_channel(ch, {"message": "nested field works!"})
        result = await task

        self.assertEqual(result.steps["out"]["text"], "nested field works!")

    # ── 08: 三级嵌套字段访问 ──────────────────────────────────────────────────

    async def test_08_three_level_nested_field_access(self):
        """inputs 连线支持三级嵌套 recv.data.user.name。"""
        ch = "ch_08"
        config = {
            "name": "deep_nested",
            "long_running": True,
            "variables": ["channel_id"],
            "steps": [
                {"id": "recv", "type": "input", "ref": "read_channel",
                 "params": {"channel_id": "{{channel_id}}"}},
                {"id": "out", "type": "tool", "ref": "echo",
                 "depends_on": ["recv"],
                 "inputs": {"text": "recv.data.user.name"}},
            ],
        }
        create_channel(ch)
        task = asyncio.create_task(run_graph(config, {"channel_id": ch}))
        await asyncio.sleep(0)
        await send_to_channel(ch, {"user": {"name": "三级嵌套测试"}})
        result = await task

        self.assertEqual(result.steps["out"]["text"], "三级嵌套测试")

    # ── 09: 通道传输 dict 数据，channel_id 字段可访问 ─────────────────────────

    async def test_09_channel_metadata_accessible(self):
        """ChannelResult 的 channel_id 字段可通过 inputs 连线访问。"""
        ch = "ch_09"
        config = {
            "name": "meta_access",
            "long_running": True,
            "variables": ["channel_id"],
            "steps": [
                {"id": "recv", "type": "input", "ref": "read_channel",
                 "params": {"channel_id": "{{channel_id}}"}},
                {"id": "out", "type": "tool", "ref": "echo",
                 "depends_on": ["recv"],
                 "inputs": {"text": "recv.channel_id"}},
            ],
        }
        create_channel(ch)
        task = asyncio.create_task(run_graph(config, {"channel_id": ch}))
        await asyncio.sleep(0)
        await send_to_channel(ch, "any data")
        result = await task

        self.assertEqual(result.steps["out"]["text"], ch)

    # ── 10: 并发两个独立通道（互不干扰）────────────────────────────────────────

    async def test_10_concurrent_independent_channels(self):
        """两个图并发运行，各使用独立通道，互不干扰。"""
        ch_a, ch_b = "ch_10a", "ch_10b"
        create_channel(ch_a)
        create_channel(ch_b)

        task_a = asyncio.create_task(run_graph(_echo_graph(), {"channel_id": ch_a}))
        task_b = asyncio.create_task(run_graph(_echo_graph(), {"channel_id": ch_b}))
        await asyncio.sleep(0)

        await send_to_channel(ch_a, "message for A")
        await send_to_channel(ch_b, "message for B")

        result_a, result_b = await asyncio.gather(task_a, task_b)
        self.assertEqual(result_a.steps["out"]["text"], "message for A")
        self.assertEqual(result_b.steps["out"]["text"], "message for B")

    # ── 11: 通道关闭后 channel_exists 返回 False ────────────────────────────────

    async def test_11_channel_exists_lifecycle(self):
        """通道生命周期：create → exists=True → send → close → exists=False。"""
        ch = "ch_11"
        self.assertFalse(channel_exists(ch))

        create_channel(ch)
        self.assertTrue(channel_exists(ch))

        await send_to_channel(ch, "data")
        self.assertTrue(channel_exists(ch))

        close_channel(ch)
        self.assertFalse(channel_exists(ch))

    # ── 12: list_channels 反映活跃通道 ──────────────────────────────────────────

    async def test_12_list_channels_reflects_active(self):
        """list_channels() 动态反映当前活跃通道列表。"""
        ch_x, ch_y = "ch_12x", "ch_12y"
        create_channel(ch_x)
        create_channel(ch_y)
        active = list_channels()
        self.assertIn(ch_x, active)
        self.assertIn(ch_y, active)

        close_channel(ch_x)
        active2 = list_channels()
        self.assertNotIn(ch_x, active2)
        self.assertIn(ch_y, active2)

    # ── 13: 关闭后重建同名通道 ────────────────────────────────────────────────

    async def test_13_recreate_channel_after_close(self):
        """关闭通道后可用同一 ID 重新创建，并成功运行图。"""
        ch = "ch_13"
        # 第一轮：创建 → 关闭
        create_channel(ch)
        close_channel(ch)
        self.assertFalse(channel_exists(ch))

        # 第二轮：重建同名通道并运行图
        create_channel(ch)
        self.assertTrue(channel_exists(ch))

        task = asyncio.create_task(run_graph(_echo_graph(), {"channel_id": ch}))
        await asyncio.sleep(0)
        await send_to_channel(ch, "after recreate")
        result = await task
        self.assertEqual(result.steps["out"]["text"], "after recreate")

    # ── 14: 重复创建同名通道抛出 ValueError ─────────────────────────────────────

    async def test_14_duplicate_channel_raises_error(self):
        """重复创建同名通道时抛出 ValueError。"""
        ch = "ch_14"
        create_channel(ch)
        with self.assertRaises(ValueError):
            create_channel(ch)

    # ── 15: 图的 final 结果为终端节点输出 ────────────────────────────────────────

    async def test_15_graph_final_is_terminal_output(self):
        """单终端节点的图，result.final 等于该终端节点的输出。"""
        ch = "ch_15"
        create_channel(ch)
        task = asyncio.create_task(run_graph(_echo_graph(), {"channel_id": ch}))
        await asyncio.sleep(0)
        await send_to_channel(ch, "final test")
        result = await task

        # echo_graph 只有 "out" 是终端节点
        self.assertEqual(result.final, result.steps["out"])
        self.assertEqual(result.final["text"], "final test")

    # ── 16: 从文件加载实际图配置（graphs/channel_echo.json）────────────────────

    async def test_16_load_real_graph_from_file(self):
        """从 graphs/channel_echo.json 加载并执行标准长期运行图。"""
        from utils import run_graph_from_file

        ch = "ch_16"
        create_channel(ch)
        task = asyncio.create_task(
            run_graph_from_file("graphs/channel_echo.json", {"channel_id": ch})
        )
        await asyncio.sleep(0)
        await send_to_channel(ch, "from real file graph")
        result = await task

        self.assertEqual(result.steps["echo_out"]["text"], "from real file graph")

    # ── 17: 流式运行长期运行图（run_graph_stream）──────────────────────────────

    async def test_17_stream_long_running_graph(self):
        """run_graph_stream 流式运行含通道的图，终端节点 token 正确 yield。"""
        ch = "ch_17"
        create_channel(ch)

        tokens: list[tuple[str, str]] = []

        async def consume():
            async for step_id, token in run_graph_stream(
                _echo_graph(), {"channel_id": ch}
            ):
                tokens.append((step_id, token))

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.01)
        await send_to_channel(ch, "stream me")
        await task

        self.assertTrue(len(tokens) > 0)
        step_ids = {t[0] for t in tokens}
        self.assertIn("out", step_ids)

    # ── 18: 验证器 — 检测缺失的 long_running 标记 ─────────────────────────────

    async def test_18_validator_detects_missing_long_running(self):
        """validate_long_running_propagation 检测含长期运行步骤但顶层未标记的配置。"""
        bad_config = {
            "name": "bad_config",
            "long_running": False,  # ← 错误：含 read_channel 但未声明
            "variables": ["channel_id"],
            "steps": [
                {"id": "recv", "type": "input", "ref": "read_channel",
                 "params": {"channel_id": "{{channel_id}}"}},
            ],
        }
        errors = validate_long_running_propagation(bad_config)
        self.assertGreater(len(errors), 0)
        self.assertTrue(any("recv" in e for e in errors))

    # ── 19: 验证器 — 合规配置无报错 ─────────────────────────────────────────────

    async def test_19_validator_passes_compliant_config(self):
        """validate_long_running_propagation 对合规配置返回空列表。"""
        good_config = {
            "name": "good_config",
            "long_running": True,  # ← 正确声明
            "variables": ["channel_id"],
            "steps": [
                {"id": "recv", "type": "input", "ref": "read_channel",
                 "params": {"channel_id": "{{channel_id}}"}},
                {"id": "out", "type": "tool", "ref": "echo",
                 "depends_on": ["recv"], "inputs": {"text": "recv.data"}},
            ],
        }
        errors = validate_long_running_propagation(good_config)
        self.assertEqual(errors, [], f"合规配置不应有违规：{errors}")

    # ── 20: validate_all_configs 扫描全项目，现有配置均合规 ───────────────────

    async def test_20_validate_all_configs_no_violations(self):
        """扫描项目所有图/流水线配置，验证无 long_running 传播违规。"""
        violations = validate_all_configs()
        if violations:
            lines = []
            for path, errs in violations.items():
                for e in errs:
                    lines.append(f"  [{path}] {e}")
            self.fail("发现 long_running 传播违规：\n" + "\n".join(lines))


# ══════════════════════════════════════════════════════════════════════════════
# 直接运行入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
