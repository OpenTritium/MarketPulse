"""QuoteClient 单元测试：分钟 K 线的 UTC 转换与容错。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from market_pulse.config import Config
from market_pulse.quotes import QuoteClient


class FakeResponse:
    def __init__(self, body: dict[str, Any], status: int = 200) -> None:
        self._body: dict[str, Any] = body
        self.status_code: int = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._body


class FakeClient:
    """替换 QuoteClient._client 的假 httpx 客户端。"""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body: dict[str, Any] = body
        self.calls: list[str] = []

    async def get(
        self, url: str, params: dict[str, Any] | None = None, headers: Any = None
    ) -> FakeResponse:
        del headers
        self.calls.append(f"{url}?{params or {}}")
        return FakeResponse(self.body)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> QuoteClient:
    cfg = Config(
        turso_url="",
        llm_base_url="",
        llm_api_key="",
        llm_model="",
        wigolo_url="",
        wigolo_token="",
        zzshare_token="token",
    )
    quote = QuoteClient(cfg)
    monkeypatch.setattr(quote, "_client", FakeClient({"code": 200, "data": {}}))
    return quote


def _fake(client: QuoteClient) -> FakeClient:
    # 运行时已由 fixture 替换为 FakeClient；私有访问为测试意图
    return cast(FakeClient, client._client)  # type: ignore[reportPrivateUsage, reportInvalidCast]


def _minute_bar(trade_time: str, close: float = 6.0) -> dict[str, Any]:
    return {
        "code": "000800.SZ",
        "trade_time": trade_time,
        "open": close,
        "close": close,
        "high": close,
        "low": close,
        "vol": 1000,
    }


def test_kline_minute_converts_bj_to_utc_seconds(client: QuoteClient) -> None:
    """trade_time（北京 YYYYMMDDHHMM）→ UTC 秒时间戳。"""
    _fake(client).body = {
        "code": 200,
        "data": {
            "list": [
                _minute_bar("202608120931"),
                _minute_bar("202608121500"),
            ]
        },
    }
    kline = asyncio.run(client.kline_minute("000800.SZ"))
    assert len(kline) == 2
    # 9:31 北京 = 1:31 UTC 同日；15:00 北京 = 7:00 UTC
    assert datetime.fromtimestamp(kline[0]["date"], UTC) == datetime(
        2026, 8, 12, 1, 31, tzinfo=UTC
    )
    assert datetime.fromtimestamp(kline[1]["date"], UTC) == datetime(
        2026, 8, 12, 7, 0, tzinfo=UTC
    )
    assert kline[0]["close"] == 6.0


def test_kline_minute_skips_invalid_trade_time(client: QuoteClient) -> None:
    """非法 trade_time 行被跳过而非崩溃。"""
    _fake(client).body = {
        "code": 200,
        "data": {"list": [_minute_bar("202608120931"), _minute_bar("bad-time")]},
    }
    kline = asyncio.run(client.kline_minute("000800.SZ"))
    assert len(kline) == 1
    assert kline[0]["date"] == kline[0]["date"]  # 非 NaN


def test_kline_minute_clamps_days(client: QuoteClient) -> None:
    """days 超界被 clamp 到 [1, 5]，limit 随之计算。"""
    fake = _fake(client)
    fake.body = {"code": 200, "data": {"list": [_minute_bar("202608120931")]}}
    _ = asyncio.run(client.kline_minute("000800.SZ", days=99))
    assert "'limit': 1200" in fake.calls[0]
    _ = asyncio.run(client.kline_minute("000800.SZ", days=0))
    assert "'limit': 240" in fake.calls[1]


def test_kline_days_clamped(client: QuoteClient) -> None:
    """日线 days 超界被 clamp（500 上限）。"""
    _fake(client).body = {
        "code": 200,
        "data": {
            "list": [
                {
                    "trade_date": "20260812",
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                }
            ]
        },
    }
    kline = asyncio.run(client.kline("000800.SZ", days=9999))
    assert len(kline) == 1
    assert kline[0]["date"] == "20260812"
