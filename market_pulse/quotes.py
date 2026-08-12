"""zzshare 行情客户端：股票列表查询 + 标的代码匹配 + 日线 K 线。

鉴权用自定义请求头 sdk-key（非 Bearer），token 从配置（ZZSHARE_TOKEN 环境
变量）读取。股票列表 5 个交易所全量缓存 24h；lookup 供 LLM 工具查询真实
存在的代码，杜绝编造 ts_code。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .config import Config

ZZSHARE_BASE = "https://api.zizizaizai.com"
_EXCHANGES = {"SS": "SH", "KSH": "SH", "SZ": "SZ", "GEM": "SZ", "BJ": "BJ"}
_STOCK_CACHE_TTL = 24 * 3600

# 常见指数/跨境标的（不在 A 股股票列表内，静态映射）
COMMON_INDICES: dict[str, str] = {
    "上证指数": "000001.SH",
    "深证成指": "399001.SZ",
    "创业板指": "399006.SZ",
    "沪深300": "000300.SH",
    "科创50": "000688.SH",
    "中证500": "000905.SH",
    "中证1000": "000852.SH",
    "上证50": "000016.SH",
    "恒生指数": "HSI",
    "恒生科技": "HSTECH",
    "纳斯达克": "IXIC",
    "纳斯达克100": "NDX",
    "标普500": "INX",
    "道琼斯": "DJI",
}


class QuoteError(RuntimeError):
    pass


class QuoteClient:
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg: Config = cfg
        self._headers: dict[str, str] = (
            {"sdk-key": cfg.zzshare_token} if cfg.zzshare_token else {}
        )
        self._stocks: list[dict[str, str]] = []
        self._stocks_loaded_at: float = 0.0
        self._client: httpx.AsyncClient = httpx.AsyncClient(timeout=15)

    async def __aenter__(self) -> QuoteClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.aclose()

    async def _stock_list(self) -> list[dict[str, str]]:
        """全市场股票列表（5 交易所），24h 缓存。"""
        now = time.monotonic()
        if self._stocks and now - self._stocks_loaded_at < _STOCK_CACHE_TTL:
            return self._stocks
        stocks: list[dict[str, str]] = []
        for exchange, suffix in _EXCHANGES.items():
            response = await self._client.get(
                f"{ZZSHARE_BASE}/v3/open/stocks/list",
                params={"exchange": exchange, "list_status": "L", "format": "records"},
                headers=self._headers,
            )
            _ = response.raise_for_status()
            body = response.json()
            items = body.get("data", {})
            if isinstance(items, dict):
                items = items.get("list") or items.get("records") or []
            for item in items or []:
                code = str(item.get("code", "")).zfill(6)
                name = str(item.get("name", "")).strip()
                if code and name:
                    stocks.append({"ts_code": f"{code}.{suffix}", "name": name})
        self._stocks = stocks
        self._stocks_loaded_at = now
        return stocks

    async def lookup(self, name: str) -> str:
        """按名称查真实存在的 ts_code；精确 > 包含；指数/跨境走静态表。"""
        target = name.strip()
        if not target:
            return ""
        if target in COMMON_INDICES:
            return COMMON_INDICES[target]
        try:
            stocks = await self._stock_list()
        except Exception:
            return ""
        # 精确匹配（含全称变体：去掉空格/单位后缀）
        for stock in stocks:
            if stock["name"] == target:
                return stock["ts_code"]
        for key, value in COMMON_INDICES.items():
            if key in target or target in key:
                return value
        # 包含匹配：名称关键词命中
        for stock in stocks:
            if target in stock["name"]:
                return stock["ts_code"]
        return ""

    async def kline(self, ts_code: str, days: int = 120) -> list[dict[str, Any]]:
        """日线 K 线（最近 N 个交易日），转换为前端图表格式。"""
        from datetime import UTC, datetime, timedelta

        end = datetime.now(UTC).date()
        try:
            days = max(1, min(int(days), 500))
            start_offset = int(days * 1.6) + 10  # 交易日约 60%，留余量
        except (TypeError, ValueError):
            days, start_offset = 120, 202
        start = end - timedelta(days=start_offset)
        response = await self._client.get(
            f"{ZZSHARE_BASE}/v3/market/kline/day/{ts_code}",
            params={
                "get_type": "range",
                "start_date": start.strftime("%Y%m%d"),
                "end_date": end.strftime("%Y%m%d"),
            },
            headers=self._headers,
        )
        _ = response.raise_for_status()
        body = response.json()
        if body.get("code") != 200:
            raise QuoteError(
                f"zzshare 返回异常: {body.get('message', body.get('code'))}"
            )
        data = body.get("data") or {}
        items = data.get("list") or []

        def _num(value: Any, default: float = 0.0) -> float:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return default
            return number if number == number else default

        kline = [
            {
                "date": str(item["trade_date"]),
                "open": _num(item.get("open")),
                "high": _num(item.get("high")),
                "low": _num(item.get("low")),
                "close": _num(item.get("close")),
                "volume": _num(item.get("volume")),
            }
            for item in items
        ]
        return sorted(kline, key=lambda k: k["date"])

    async def kline_minute(self, ts_code: str, days: int = 1) -> list[dict[str, Any]]:
        """最近 N 个交易日的分钟 K 线（1min，9:31-15:00 每天 240 根，北京时区）。

        trade_time 为北京时间 YYYYMMDDHHMM，转换为 UTC 秒时间戳
        （lightweight-charts 的 UTCTimestamp 语义），date 字段即秒整数。
        """
        from datetime import UTC, datetime, timedelta

        try:
            days = max(1, min(int(days), 5))
        except (TypeError, ValueError):
            days = 1
        limit = days * 240
        response = await self._client.get(
            f"{ZZSHARE_BASE}/v3/market/kline/minute/{ts_code}",
            params={"limit": limit},
            headers=self._headers,
        )
        _ = response.raise_for_status()
        body = response.json()
        if body.get("code") != 200:
            raise QuoteError(
                f"zzshare 返回异常: {body.get('message', body.get('code'))}"
            )
        data = body.get("data") or {}
        items = data.get("list") or []
        utc_offset = timedelta(hours=8)

        def _num(value: Any, default: float = 0.0) -> float:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return default
            return number if number == number else default

        kline = []
        for item in items:
            raw = str(item.get("trade_time") or "")
            try:
                when = (
                    datetime.strptime(raw, "%Y%m%d%H%M").replace(tzinfo=UTC)
                    - utc_offset
                )
                ts = int(when.timestamp())
            except (ValueError, OverflowError, OSError):
                continue
            kline.append(
                {
                    "date": ts,
                    "open": _num(item.get("open")),
                    "high": _num(item.get("high")),
                    "low": _num(item.get("low")),
                    "close": _num(item.get("close")),
                    "volume": _num(item.get("vol")),
                }
            )
        return sorted(kline, key=lambda k: k["date"])
