"""wigolo REST 客户端：绕开 MCP streamable-http 会话在长负载下的僵死。

实测结论：MCP 会话内大量 tools/call 后 wigolo 事件循环永久阻塞（探活无响应，
重启才恢复）；而 REST /v1/fetch 连续 100+ 请求稳定。pipeline 只使用 fetch，
直接调用 REST 端点，响应 markdown 字段与采集层文本提取兼容。

使用方式（async）：
    async with WigoloMCP(cfg) as mcp:
        result = await mcp.fetch("https://...", max_content_chars=30000)
"""

from __future__ import annotations

import asyncio
from typing import Any, Self

import httpx

from .config import Config

_CALL_TIMEOUT_SECONDS = 120.0


class MCPError(RuntimeError):
    pass


class WigoloMCP:
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg: Config = cfg
        # wigolo REST 端点：/mcp → /v1（如 http://127.0.0.1:3333/mcp）
        self._rest_base: str = cfg.wigolo_url.removesuffix("/mcp") + "/v1"
        self._headers: dict[str, str] = (
            {"Authorization": "Bearer " + cfg.wigolo_token} if cfg.wigolo_token else {}
        )
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=_CALL_TIMEOUT_SECONDS
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.aclose()

    async def fetch(self, url: str, **kw: Any) -> Any:
        """抓取页面并返回 JSON；kw 透传 max_content_chars / render_js / actions。

        wigolo 忙碌时返回 429（并发槽满），指数退避重试最多 3 次。
        """
        payload: dict[str, Any] = {"url": url, "timeoutMs": 90000}
        if kw.get("max_content_chars"):
            payload["max_content_chars"] = kw["max_content_chars"]
        if kw.get("render_js"):
            payload["render_js"] = kw["render_js"]
        if kw.get("actions"):
            payload["actions"] = kw["actions"]
        for attempt in range(3):
            try:
                response = await asyncio.wait_for(
                    self._client.post(
                        f"{self._rest_base}/fetch",
                        json=payload,
                        headers=self._headers,
                    ),
                    timeout=_CALL_TIMEOUT_SECONDS,
                )
            except TimeoutError as exc:
                raise MCPError(
                    f"fetch 超时（>{_CALL_TIMEOUT_SECONDS:.0f}s）: {url}"
                ) from exc
            except httpx.HTTPError as exc:
                raise MCPError(f"fetch 调用失败: {exc}") from exc
            if response.status_code == 429 and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if response.status_code >= 400:
                raise MCPError(f"fetch 失败 HTTP {response.status_code}: {url}")
            return response.json()
