"""
input_utils — 外部数据接入工具函数

所有函数均为 async def（涉及 I/O），返回 ToolResult 子类。
用于 input/*.json 配置声明的数据源适配器：

    http_get                 — 通过 HTTP GET 请求获取外部数据
    http_post                — 通过 HTTP POST 请求提交并获取数据
    read_file                — 读取本地文件内容
    read_json                — 读取本地 JSON 文件并解析为 dict
    read_channel             — 从命名通道阻塞读取一条数据（长期运行节点透传）
    pip_install_requirements — 安装 requirements.txt 中声明的第三方依赖
    human_gate               — 通用人工决策关卡：打印上下文后阻塞等待 channel 注入决策

沙盒相关接口已迁移至 utils.sandbox，此处保留向后兼容的重导出：
    SandboxResult / sandbox_run — 从 utils.sandbox 重导出

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
import subprocess
import sys
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


# ── pip_install_requirements ───────────────────────────────────────────────────

class PipInstallResult(ToolResult):
    """pip_install_requirements() 的返回值。"""
    ok: bool
    packages: list[str]   # requirements.txt 中声明的包名列表
    stdout: str
    stderr: str
    error: str


async def pip_install_requirements(
    requirements_file: str,
    timeout: float = 120.0,
) -> PipInstallResult:
    """读取 requirements.txt 并通过 pip 安装其中声明的第三方依赖。

    安装到当前 Python 解释器环境（与沙盒 subprocess 共享），因此只需在
    arch_then_mcts 图中运行一次，后续所有沙盒 trial 均可复用。

    Args:
        requirements_file: requirements.txt 的绝对路径。
        timeout:           pip install 执行超时秒数，默认 120。

    Returns:
        PipInstallResult:
            .ok        安装是否成功（requirements.txt 不存在或为空视为成功）
            .packages  requirements.txt 中声明的包名列表
            .stdout    pip 标准输出
            .stderr    pip 标准错误
            .error     失败原因（ok=False 时）
    """
    req_path = Path(requirements_file)

    if not req_path.exists():
        return PipInstallResult(ok=True, packages=[], stdout="", stderr="",
                                error="requirements.txt 不存在，跳过安装")

    content = req_path.read_text(encoding="utf-8").strip()
    if not content:
        return PipInstallResult(ok=True, packages=[], stdout="", stderr="",
                                error="requirements.txt 为空，跳过安装")

    packages = [line.strip() for line in content.splitlines() if line.strip()]

    def _install() -> tuple[bool, str, str]:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", *packages, "--quiet"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return proc.returncode == 0, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return False, "", "pip install 超时"
        except Exception as e:
            return False, "", str(e)

    ok, stdout, stderr = await asyncio.to_thread(_install)
    return PipInstallResult(
        ok=ok,
        packages=packages,
        stdout=stdout,
        stderr=stderr,
        error="" if ok else f"pip install 失败: {stderr[:500]}",
    )


# ── sandbox_run（重导出，保持向后兼容）────────────────────────────────────────────

from utils.sandbox import SandboxResult, sandbox_run  # noqa: F401


# ── human_gate ─────────────────────────────────────────────────────────────────

class HumanGateResult(ToolResult):
    """human_gate() 的返回值。"""
    data: dict   # 调用方通过 channel 注入的原始决策数据


async def human_gate(
    channel_id: str,
    context_text: str = "",
    label: str = "Human Gate",
) -> HumanGateResult:
    """通用人工决策关卡：打印可选上下文后，阻塞等待外部通过 channel 注入决策数据。

    调用方须在图启动前创建通道，并在合适时机通过 ``send_to_channel(channel_id, decision)``
    注入任意 dict 作为决策。图内此步骤完成后，下游步骤可通过 ``步骤id.data`` 访问决策内容。

    Args:
        channel_id:   预先由 create_channel() 创建的通道 ID。
        context_text: 可选的上下文说明文本，打印供人工参考（不影响阻塞逻辑）。
        label:        显示在分隔线上的标签名，默认 "Human Gate"。

    Returns:
        HumanGateResult:
            .data   调用方注入的决策 dict（内容由业务自定义）

    Example:
        # 图 JSON 中声明：
        # {
        #   "id": "review",
        #   "type": "input",
        #   "ref": "human_gate",
        #   "long_running": true,
        #   "params": {
        #     "channel_id": "{{gate_channel_id}}",
        #     "context_text": "{{summary_text}}",
        #     "label": "架构审核"
        #   }
        # }
        #
        # 调用方：
        # create_channel("gate_ch_001")
        # task = asyncio.create_task(run_graph(...))
        # await send_to_channel("gate_ch_001", {"action": "approve"})
        # result = await task
    """
    from ._channel import receive_from_channel

    print(f"\n{'═' * 60}")
    print(f"  {label}")
    if context_text:
        print(f"{'─' * 60}")
        print(context_text)
    print(f"{'─' * 60}")
    print(f"  等待决策 ... send_to_channel({channel_id!r}, {{...}})")
    print(f"{'═' * 60}")

    data = await receive_from_channel(channel_id)
    if not isinstance(data, dict):
        data = {"value": data}
    return HumanGateResult(data=data)
