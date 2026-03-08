"""
input_utils — 外部数据接入工具函数

所有函数均为 async def（涉及 I/O），返回 ToolResult 子类。
用于 input/*.json 配置声明的数据源适配器：

    http_get     — 通过 HTTP GET 请求获取外部数据
    http_post    — 通过 HTTP POST 请求提交并获取数据
    read_file    — 读取本地文件内容
    read_json    — 读取本地 JSON 文件并解析为 dict
    read_channel — 从命名通道阻塞读取一条数据（长期运行节点透传）

调用方式（通过 input/*.json 配置，在流水线 / 图步骤中使用 type: "input"）：

    {
      "id": "fetch",
      "type": "input",
      "ref": "http_get",
      "params": { "url": "https://api.example.com/data" }
    }

也可直接调用（适合单元测试）：

    from utils.input_utils import http_get
    result = await http_get("https://api.example.com/data")
    result.body           # 响应正文字符串
    result.status_code    # HTTP 状态码
    result.model_dump()   # → dict
"""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path
from typing import Any

import httpx

from ._base import ToolResult


# ── 返回值类型定义 ─────────────────────────────────────────────────────────────

class HttpGetResult(ToolResult):
    """http_get() 的返回值。"""
    status_code: int          # HTTP 状态码
    body: str                 # 响应正文（文本）
    content_type: str         # Content-Type 响应头


class HttpPostResult(ToolResult):
    """http_post() 的返回值。"""
    status_code: int          # HTTP 状态码
    body: str                 # 响应正文（文本）
    content_type: str         # Content-Type 响应头


class ReadFileResult(ToolResult):
    """read_file() 的返回值。"""
    content: str              # 文件内容字符串
    path: str                 # 规范化后的绝对路径
    encoding: str             # 实际使用的编码
    size: int                 # 文件字节数


class ReadJsonResult(ToolResult):
    """read_json() 的返回值。"""
    data: dict[str, Any]      # 解析后的 JSON 对象
    path: str                 # 规范化后的绝对路径
    size: int                 # 文件字节数


# ── 工具函数 ──────────────────────────────────────────────────────────────────

async def http_get(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> HttpGetResult:
    """通过 HTTP GET 请求获取外部数据。

    Args:
        url:     目标 URL，必须包含协议前缀（如 https://）。
        headers: 附加请求头，默认为空。
        timeout: 请求超时秒数，默认 10 秒。

    Returns:
        HttpGetResult:
            .status_code   HTTP 状态码
            .body          响应正文字符串
            .content_type  Content-Type 响应头

    Raises:
        httpx.TimeoutException: 请求超时。
        httpx.HTTPStatusError:  响应状态码 >= 400（需调用方自行 raise_for_status）。

    Example:
        >>> r = await http_get("https://httpbin.org/get")
        >>> r.status_code
        200
        >>> r.content_type
        'application/json'
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url, headers=headers or {})
    return HttpGetResult(
        status_code=response.status_code,
        body=response.text,
        content_type=response.headers.get("content-type", ""),
    )


async def http_post(
    url: str,
    body: str | dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> HttpPostResult:
    """通过 HTTP POST 请求提交数据并获取响应。

    Args:
        url:     目标 URL。
        body:    请求正文；dict 自动序列化为 JSON 并设置 Content-Type。
        headers: 附加请求头。
        timeout: 请求超时秒数，默认 10 秒。

    Returns:
        HttpPostResult:
            .status_code   HTTP 状态码
            .body          响应正文字符串
            .content_type  Content-Type 响应头

    Example:
        >>> r = await http_post("https://httpbin.org/post", body={"key": "value"})
        >>> r.status_code
        200
    """
    req_headers = dict(headers or {})
    content: str | None = None
    json_body: dict | None = None

    if isinstance(body, dict):
        json_body = body
    elif isinstance(body, str):
        content = body

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            url,
            content=content,
            json=json_body,
            headers=req_headers,
        )
    return HttpPostResult(
        status_code=response.status_code,
        body=response.text,
        content_type=response.headers.get("content-type", ""),
    )


async def read_file(
    path: str,
    encoding: str = "utf-8",
) -> ReadFileResult:
    """异步读取本地文件内容。

    Args:
        path:     文件路径（绝对路径或相对路径；相对路径以当前工作目录为基准）。
        encoding: 文件编码，默认 UTF-8。

    Returns:
        ReadFileResult:
            .content   文件内容字符串
            .path      规范化后的绝对路径
            .encoding  实际使用的编码
            .size      文件字节数

    Raises:
        FileNotFoundError: 文件不存在时抛出。
        UnicodeDecodeError: 编码不匹配时抛出。

    Example:
        >>> r = await read_file("prompts/chat/system.txt")
        >>> r.content[:20]
        '你是一个智能助手...'
        >>> r.size
        128
    """
    p = Path(path).resolve()

    def _read() -> tuple[str, int]:
        raw = p.read_bytes()
        return raw.decode(encoding), len(raw)

    content, size = await asyncio.to_thread(_read)
    return ReadFileResult(content=content, path=str(p), encoding=encoding, size=size)


async def read_json(
    path: str,
) -> ReadJsonResult:
    """异步读取本地 JSON 文件并解析为 dict。

    Args:
        path: JSON 文件路径。

    Returns:
        ReadJsonResult:
            .data   解析后的 JSON 对象（顶层必须是 object）
            .path   规范化后的绝对路径
            .size   文件字节数

    Raises:
        FileNotFoundError: 文件不存在。
        json.JSONDecodeError: JSON 格式错误。
        TypeError: JSON 顶层不是 object（如是 array）时抛出。

    Example:
        >>> r = await read_json("configs/extract_user.json")
        >>> r.data["model"]
        'gpt-4o-mini'
    """
    p = Path(path).resolve()

    def _read() -> tuple[dict, int]:
        raw = p.read_bytes()
        data = _json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise TypeError(f"JSON 顶层必须是 object，实际为 {type(data).__name__}（文件: {path}）")
        return data, len(raw)

    data, size = await asyncio.to_thread(_read)
    return ReadJsonResult(data=data, path=str(p), size=size)


class ChannelResult(ToolResult):
    """read_channel() 的返回值。"""
    data: Any           # 从通道接收到的业务数据（原样传递，类型由发送方决定）
    channel_id: str     # 来源通道 ID


async def read_channel(
    channel_id: str,
    timeout: float | None = None,
) -> ChannelResult:
    """从命名通道阻塞读取一条数据，用于长期运行节点接收外部透传数据。

    图内节点声明此步骤后将挂起等待，直到调用方通过 send_to_channel() 推送数据
    或通过 close_channel() 关闭通道。

    Args:
        channel_id: 通道唯一标识（需由调用方预先调用 create_channel() 创建）。
        timeout:    等待超时秒数，None 表示永久等待（默认）。

    Returns:
        ChannelResult:
            .data        调用方推送的原始数据（任意类型）
            .channel_id  来源通道 ID

    Raises:
        ChannelClosedError:  通道被 close_channel() 关闭，无更多数据。
        KeyError:            通道不存在（未调用 create_channel）。
        asyncio.TimeoutError: 等待超时（仅当 timeout 不为 None 时）。

    Example:
        # 调用方：
        create_channel("ch_001")
        await send_to_channel("ch_001", {"text": "hello"})

        # 图内步骤（input/read_channel）会返回：
        # ChannelResult(data={"text": "hello"}, channel_id="ch_001")
        # 下游步骤通过 receive.data 访问
    """
    from ._channel import receive_from_channel
    data = await receive_from_channel(channel_id, timeout=timeout)
    return ChannelResult(data=data, channel_id=channel_id)
