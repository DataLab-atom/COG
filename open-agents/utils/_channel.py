"""Async channel mechanism for Human Gate support.

Usage:
    create_channel(channel_id)              # open a channel
    await send_to_channel(channel_id, data) # inject a decision from outside
    data = await receive_from_channel(...)  # block until data arrives
    close_channel(channel_id)               # cleanup
"""
import asyncio
from typing import Any

_channels: dict[str, asyncio.Queue] = {}


def create_channel(channel_id: str) -> None:
    """Open a new one-shot channel."""
    _channels[channel_id] = asyncio.Queue(maxsize=1)


async def send_to_channel(channel_id: str, data: Any) -> None:
    """Push a decision into the channel (non-blocking if slot is free)."""
    q = _channels.get(channel_id)
    if q is None:
        raise KeyError(f"Channel '{channel_id}' does not exist.")
    await q.put(data)


async def receive_from_channel(channel_id: str) -> Any:
    """Block until a decision arrives, then return it."""
    q = _channels.get(channel_id)
    if q is None:
        raise KeyError(f"Channel '{channel_id}' does not exist.")
    return await q.get()


def close_channel(channel_id: str) -> None:
    """Remove the channel after use."""
    _channels.pop(channel_id, None)
