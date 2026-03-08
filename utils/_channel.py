"""
_channel.py — 运行时数据通道注册表

为长期运行的图 / 流水线节点提供外部数据注入能力。
图内的 input/read_channel 步骤通过 channel_id 阻塞等待；
调用方持有同一 channel_id，可在图运行期间随时向通道推送数据，
实现图外 → 图内的运行时透传。

━━ 生命周期 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 1. 调用方：创建通道
    from utils import create_channel, send_to_channel, close_channel
    create_channel("ch_001")

    # 2. 启动图（通道 ID 通过 variables 传入）
    task = asyncio.create_task(
        run_graph(config, variables={"channel_id": "ch_001"})
    )

    # 3. 图内步骤（input/read_channel）阻塞等待：
    #    { "id": "recv", "type": "input", "ref": "read_channel",
    #      "params": { "channel_id": "{{channel_id}}" } }

    # 4. 调用方推送数据，图内节点继续执行
    await send_to_channel("ch_001", {"user_input": "第一轮"})
    await send_to_channel("ch_001", {"user_input": "第二轮"})

    # 5. 关闭通道，图内节点感知 ChannelClosedError 并退出
    close_channel("ch_001")

━━ 注意 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    - channel_id 为进程内全局唯一标识，调用方自行确保唯一性
    - 同一 channel_id 不可重复创建（需先 close_channel 再 create_channel）
    - asyncio.Queue 与事件循环绑定，不适用于跨进程 / 跨线程场景
    - 通道关闭后，正在等待的节点将收到 ChannelClosedError
"""

from __future__ import annotations

import asyncio
from typing import Any


# ── 全局通道注册表 ─────────────────────────────────────────────────────────────

_registry: dict[str, asyncio.Queue] = {}

# 通道关闭哨兵（私有对象，不可能与业务数据相同）
_CLOSED = object()


# ── 异常 ─────────────────────────────────────────────────────────────────────

class ChannelClosedError(RuntimeError):
    """通道已被 close_channel() 关闭，无更多数据可读。"""


# ── 通道管理 API ──────────────────────────────────────────────────────────────

def create_channel(channel_id: str, maxsize: int = 0) -> str:
    """创建一个命名通道，返回 channel_id。

    Args:
        channel_id: 通道唯一标识，图内步骤通过此 ID 阻塞读取数据。
        maxsize:    队列容量上限，0 表示无限制（默认）。

    Returns:
        channel_id（方便链式调用）。

    Raises:
        ValueError: channel_id 已存在时抛出。

    Example:
        >>> ch = create_channel("user_input_001")
        >>> await send_to_channel(ch, {"text": "hello"})
    """
    if channel_id in _registry:
        raise ValueError(
            f"通道 '{channel_id}' 已存在，请先调用 close_channel() 释放后再创建"
        )
    _registry[channel_id] = asyncio.Queue(maxsize=maxsize)
    return channel_id


async def send_to_channel(channel_id: str, data: Any) -> None:
    """向通道推送一条数据，供图内节点读取。

    若队列已满（maxsize > 0），将阻塞直到有空位。

    Args:
        channel_id: 目标通道 ID（必须已由 create_channel 创建）。
        data:       任意 Python 对象，原样传递给图内节点。

    Raises:
        KeyError: 通道不存在时抛出。

    Example:
        >>> await send_to_channel("user_input_001", {"text": "第二轮输入"})
    """
    await _get_queue(channel_id).put(data)


async def receive_from_channel(
    channel_id: str, timeout: float | None = None
) -> Any:
    """从通道读取一条数据（供 input/read_channel 内部调用）。

    阻塞直到有数据可用、超时或通道关闭。

    Args:
        channel_id: 来源通道 ID。
        timeout:    等待超时秒数，None 表示永久等待（默认）。

    Returns:
        通道中的下一条业务数据。

    Raises:
        ChannelClosedError:  通道已被 close_channel() 关闭。
        KeyError:            通道不存在。
        asyncio.TimeoutError: 等待超时（仅当 timeout 不为 None 时）。
    """
    q = _get_queue(channel_id)
    if timeout is not None:
        item = await asyncio.wait_for(q.get(), timeout=timeout)
    else:
        item = await q.get()

    if item is _CLOSED:
        # 将哨兵放回队列，让其他同样在等待的协程也能感知到关闭
        q.put_nowait(_CLOSED)
        raise ChannelClosedError(f"通道 '{channel_id}' 已关闭，无更多数据")

    return item


def close_channel(channel_id: str) -> None:
    """关闭通道，向队列写入 EOF 哨兵并从注册表中移除。

    图内正在等待该通道的节点将收到 ChannelClosedError，
    应捕获该异常并优雅退出或返回默认值。

    Args:
        channel_id: 要关闭的通道 ID。

    Raises:
        KeyError: 通道不存在时抛出。

    Example:
        >>> close_channel("user_input_001")
    """
    q = _get_queue(channel_id)
    # 先放入哨兵，再删除注册表项
    # 已持有 q 引用的 receive_from_channel 协程仍可从队列中取到哨兵
    q.put_nowait(_CLOSED)
    del _registry[channel_id]


def channel_exists(channel_id: str) -> bool:
    """检查通道是否存在（已创建且未关闭）。

    Example:
        >>> channel_exists("user_input_001")
        True
    """
    return channel_id in _registry


def list_channels() -> list[str]:
    """列出当前所有活跃通道的 ID 列表。

    Example:
        >>> list_channels()
        ['user_input_001', 'sensor_feed_002']
    """
    return list(_registry.keys())


# ── 内部工具 ─────────────────────────────────────────────────────────────────

def _get_queue(channel_id: str) -> asyncio.Queue:
    q = _registry.get(channel_id)
    if q is None:
        raise KeyError(
            f"通道 '{channel_id}' 不存在，请先调用 create_channel() 创建"
        )
    return q
