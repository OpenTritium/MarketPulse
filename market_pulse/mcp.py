"""wigolo MCP 客户端：基于 fastmcp（pydantic-ai 的 MCP 底座）。

替代手写 JSON-RPC：session 管理、SSE/streamable-http、重连由 fastmcp 处理。
使用方式（async）：
    async with WigoloMCP(cfg) as mcp:
        result = await mcp.fetch("https://...", max_content_chars=30000)
"""

from __future__ import annotations

import json
from typing import Any, Self

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from .config import Config


class MCPError(RuntimeError):
    pass


def _parse_text(result: Any) -> Any:
    """CallToolResult → 解析后的 JSON（wigolo 工具返回 JSON 文本）。"""
    content = getattr(result, "content", None) or []
    texts = [c.text for c in content if getattr(c, "type", "") == "text" and c.text]
    joined = "\n".join(texts).strip()
    if not joined:
        return None
    try:
        return json.loads(joined)
    except (ValueError, TypeError):
        return joined


class WigoloMCP:
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg: Config = cfg
        headers = (
            {"Authorization": "Bearer " + cfg.wigolo_token}
            if cfg.wigolo_token
            else None
        )
        self._transport: StreamableHttpTransport = StreamableHttpTransport(
            cfg.wigolo_url, headers=headers
        )
        self._client: Client[StreamableHttpTransport] = Client(
            self._transport, name="wigolo"
        )

    async def __aenter__(self) -> Self:
        _ = await self._client.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.__aexit__(*args)

    async def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        try:
            result = await self._client.call_tool(tool, arguments)
        except Exception as e:
            raise MCPError(f"工具 {tool} 调用失败: {e}") from e
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            raise MCPError(f"工具 {tool} 执行失败: {result}")
        return _parse_text(result)

    async def fetch(self, url: str, **kw: Any) -> Any:
        return await self._call("fetch", {"url": url, **kw})
