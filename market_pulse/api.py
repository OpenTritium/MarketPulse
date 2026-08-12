"""情感查询 API：时间线、情绪聚合与语义检索。

应用生命周期在单一进程内启动采集调度器；HTTP 查询和采集共享
embedding 模型缓存，但使用各自的 SQLite 连接。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast, override

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from market_pulse.config import Config, build_config
from market_pulse.db import Store
from market_pulse.embed import Embedder
from market_pulse.factors import factor_descriptions
from market_pulse.quotes import QuoteClient, QuoteError
from market_pulse.scheduler import (
    DEFAULT_COLLECT_DELAY_SECONDS,
    DEFAULT_COLLECT_INTERVAL_SECONDS,
    collector_status,
    run_scheduler,
)
from market_pulse.timestamps import format_utc_timestamp

_log = logging.getLogger("api")


@dataclass(frozen=True)
class Runtime:
    """一个 API 进程持有的共享运行时资源。"""

    cfg: Config
    store: Store
    embedder: Embedder


def _runtime(request: Request) -> Runtime:
    return cast(Runtime, request.app.state.runtime)


router = APIRouter()


def _lifespan_factory(
    config: Config,
    *,
    collect_interval_seconds: int,
    collect_delay_seconds: int,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncGenerator[None]:
        store = Store(config)
        embedder = Embedder(config)
        app_instance.state.runtime = Runtime(cfg=config, store=store, embedder=embedder)
        scheduler_task = asyncio.create_task(
            run_scheduler(
                config,
                embedder=embedder,
                interval_seconds=collect_interval_seconds,
                delay_seconds=collect_delay_seconds,
            ),
            name="collector",
        )
        try:
            yield
        finally:
            _ = scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task
            store.close()

    return lifespan


def create_app(
    *,
    cfg: Config | None = None,
    collect_interval_seconds: int = DEFAULT_COLLECT_INTERVAL_SECONDS,
    collect_delay_seconds: int = DEFAULT_COLLECT_DELAY_SECONDS,
) -> FastAPI:
    """创建 API，并把调度时长固定在该应用实例上。"""
    if collect_interval_seconds <= 0:
        raise ValueError("采集间隔必须大于 0 秒")
    if collect_delay_seconds < 0:
        raise ValueError("首次采集延迟不能为负数")

    config = cfg or build_config()

    application = FastAPI(
        title="Market Pulse API",
        version="0.3.0",
        lifespan=_lifespan_factory(
            config,
            collect_interval_seconds=collect_interval_seconds,
            collect_delay_seconds=collect_delay_seconds,
        ),
    )
    application.include_router(router)
    _register_error_handlers(application)
    _register_request_id_middleware(application)
    return application


_STATUS_TO_ERROR_TYPE = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    409: "conflict_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
}


def _error_body(
    *, type_: str, message: str, request_id: str, param: str | None = None
) -> dict[str, Any]:
    return {
        "error": {
            "type": type_,
            "message": message,
            "param": param,
            "request_id": request_id,
        }
    }


def _request_id(request: Request) -> str:
    return cast(str, getattr(request.state, "request_id", "")) or ""


def _register_error_handlers(application: FastAPI) -> None:
    """统一错误信封：{error: {type, message, param, request_id}}。"""

    def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # 用基类判断：fastapi.HTTPException 与路由未匹配抛的 starlette 类都覆盖
        status_code = (
            exc.status_code if isinstance(exc, StarletteHTTPException) else 500
        )
        message = (
            str(exc.detail)
            if isinstance(exc, StarletteHTTPException)
            else "服务器内部错误"
        )
        return JSONResponse(
            status_code=status_code,
            content=_error_body(
                type_=_STATUS_TO_ERROR_TYPE.get(status_code, "api_error"),
                message=message,
                request_id=_request_id(request),
            ),
            headers={"X-Request-Id": _request_id(request)},
        )

    def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        errors = exc.errors() if isinstance(exc, RequestValidationError) else []
        first = errors[0] if errors else {}
        param = ".".join(str(part) for part in first.get("loc", [])[1:]) or None
        message = "；".join(
            f"{'.'.join(str(p) for p in e.get('loc', [])[1:]) or e.get('loc', [''])[0]}: {e.get('msg', '')}"
            for e in errors[:5]
        )
        return JSONResponse(
            status_code=422,
            content=_error_body(
                type_="invalid_request_error",
                message=message,
                param=param,
                request_id=_request_id(request),
            ),
            headers={"X-Request-Id": _request_id(request)},
        )

    def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = _request_id(request)
        _log.exception("未处理异常 request_id=%s", request_id, exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=_error_body(
                type_="api_error",
                message="服务器内部错误",
                request_id=request_id,
            ),
            headers={"X-Request-Id": request_id},
        )

    application.add_exception_handler(HTTPException, http_exception_handler)
    # FastAPI 0.141 中 fastapi.HTTPException 是 starlette.HTTPException 的子类；
    # 路由未匹配抛的是 starlette 类实例，两个键都要注册才覆盖所有 404。
    application.add_exception_handler(StarletteHTTPException, http_exception_handler)
    application.add_exception_handler(
        RequestValidationError, validation_exception_handler
    )
    application.add_exception_handler(Exception, unhandled_exception_handler)


class _RequestIdMiddleware(BaseHTTPMiddleware):
    """透传或生成 X-Request-Id，并写入响应头供排障关联。"""

    @override
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response


def _register_request_id_middleware(application: FastAPI) -> None:
    application.add_middleware(_RequestIdMiddleware)


def _rows(store: Store, since: datetime) -> list[dict[str, Any]]:
    """读取 UTC 时间窗口内的情绪聚合行。"""
    return store.sentiment_rows_since(since)


def _bucket_key(at: datetime, window: str) -> str:
    """按窗口生成 UTC 分组键。"""
    if window == "hour":
        return at.strftime("%Y-%m-%d %H:00")
    return at.strftime("%Y-%m-%d")


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


async def _probe_wigolo(cfg: Config) -> dict[str, Any]:
    """对 wigolo MCP 做一次轻量 initialize 探活。"""
    headers = {"Accept": "application/json, text/event-stream"}
    if cfg.wigolo_token:
        headers["Authorization"] = f"Bearer {cfg.wigolo_token}"
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                cfg.wigolo_url,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "market-pulse-status", "version": "1"},
                    },
                },
            )
        return {
            "reachable": response.status_code == 200,
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    except Exception:
        return {"reachable": False, "latency_ms": None}


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    """服务状态汇总：应用/数据库/wigolo/采集器可观测性。"""
    runtime = _runtime(request)
    try:
        row = runtime.store.conn.execute("SELECT COUNT(*) FROM events").fetchone()
        events = int(row[0]) if row else 0
    except Exception:
        events = None
    return {
        "app": "ok",
        "events": events,
        "wigolo": await _probe_wigolo(runtime.cfg),
        "collector": {
            "running": collector_status.running,
            "last_run_at": collector_status.last_run_at,
            "last_error": collector_status.last_error,
            "last_result": collector_status.last_result,
        },
    }


@router.get("/timeline")
def timeline(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    starting_after: str | None = Query(
        None, description="上一页最后一条事件的 ID（游标分页）"
    ),
) -> dict[str, Any]:
    """事件时间线：按出现时间倒序，每条带来源数组。"""
    try:
        total, rows = _runtime(request).store.timeline(limit, starting_after)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="starting_after 指向的事件不存在"
        ) from None
    has_more = len(rows) > limit
    return {
        "total": total,
        "events": rows[:limit],
        "has_more": has_more,
        "starting_after": rows[limit - 1]["id"] if has_more else None,
    }


@router.get("/events/{event_id}")
def event_detail(event_id: str, request: Request) -> dict[str, Any]:
    """事件详情及多来源报道列表。"""
    event = _runtime(request).store.event_detail(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    return event


@router.get("/sentiment/overview")
def overview(request: Request) -> dict[str, Any]:
    """情绪总览：最新 24 小时均值和连续两个 7 日窗口的趋势差。"""
    now = datetime.now(UTC)
    store = _runtime(request).store
    by_time = [
        (row["at"], row["sentiment"]) for row in _rows(store, now - timedelta(days=14))
    ]
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)
    latest_24h = [
        value for at, value in by_time if value is not None and at >= cutoff_24h
    ]
    current_7d = [
        value for at, value in by_time if value is not None and at >= cutoff_7d
    ]
    previous_7d = [
        value for at, value in by_time if value is not None and at < cutoff_7d
    ]
    current_average = _avg(current_7d)
    previous_average = _avg(previous_7d)
    trend = (
        round(current_average - previous_average, 3)
        if current_average is not None and previous_average is not None
        else None
    )
    return {
        "generated_at": format_utc_timestamp(now),
        "latest_24h": _avg(latest_24h),
        "count_24h": sum(1 for at, _ in by_time if at >= cutoff_24h),
        "avg_7d": current_average,
        "trend_7d": trend,
    }


@router.get("/sentiment/timeseries")
def timeseries(
    request: Request,
    window: str = Query("day", pattern="^(hour|day)$"),
    hours: int = Query(72, ge=1, le=24 * 30),
) -> dict[str, Any]:
    """情绪时间序列：均值、计数和正负占比，按 UTC hour/day 分桶。"""
    since = datetime.now(UTC) - timedelta(hours=hours)
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "sum": 0.0, "with_sentiment": 0, "pos": 0, "neg": 0}
    )
    for row in _rows(_runtime(request).store, since):
        key = _bucket_key(row["at"], window)
        bucket = buckets[key]
        bucket["count"] += 1
        if row["sentiment"] is not None:
            bucket["sum"] += row["sentiment"]
            bucket["with_sentiment"] += 1
            if row["sentiment"] > 0.05:
                bucket["pos"] += 1
            elif row["sentiment"] < -0.05:
                bucket["neg"] += 1
    series = [
        {
            "bucket": key,
            "avg_sentiment": (
                round(bucket["sum"] / bucket["with_sentiment"], 3)
                if bucket["with_sentiment"]
                else None
            ),
            "count": bucket["count"],
            "positive_ratio": round(bucket["pos"] / bucket["with_sentiment"], 3)
            if bucket["with_sentiment"]
            else 0.0,
            "negative_ratio": round(bucket["neg"] / bucket["with_sentiment"], 3)
            if bucket["with_sentiment"]
            else 0.0,
        }
        for key, bucket in sorted(buckets.items())
    ]
    return {"window": window, "series": series}


@router.get("/sentiment/factors")
def factors(
    request: Request,
    hours: int = Query(72, ge=1, le=24 * 30),
) -> dict[str, Any]:
    """因子分解：每个因子的平均情绪、覆盖事件数和覆盖率。"""
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = _rows(_runtime(request).store, since)
    aggregates: dict[str, dict[str, float]] = defaultdict(
        lambda: {"sum": 0.0, "count": 0}
    )
    for row in rows:
        for name, value in (row["factors"] or {}).items():
            if isinstance(value, int | float):
                aggregates[name]["sum"] += value
                aggregates[name]["count"] += 1
    total = len(rows) or 1
    result = []
    for name, description in factor_descriptions().items():
        aggregate = aggregates.get(name)
        result.append(
            {
                "factor": name,
                "description": description,
                "avg": (
                    round(aggregate["sum"] / aggregate["count"], 3)
                    if aggregate and aggregate["count"]
                    else None
                ),
                "events": aggregate["count"] if aggregate else 0,
                "coverage": (
                    round(aggregate["count"] / total, 3) if aggregate else 0.0
                ),
            }
        )
    return {"hours": hours, "factors": result}


@router.get("/search")
def search(
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    k: int = Query(10, ge=1, le=50),
) -> dict[str, Any]:
    """语义检索：查询转 embedding 后执行事件近邻检索。"""
    if not q.strip():
        raise HTTPException(status_code=422, detail="q 不能为空")
    runtime = _runtime(request)
    return {
        "query": q,
        "results": runtime.store.search_similar(runtime.embedder.embed(q), k=k),
    }


@router.get("/quote/kline")
async def quote_kline(
    request: Request,
    ts_code: str = Query(
        pattern=r"^[0-9A-Z.]{1,12}$",
        description="标的代码（带交易所后缀），如 000001.SZ / 600519.SH",
    ),
    days: int = Query(120, ge=10, le=500),
) -> dict[str, Any]:
    """标的日线 K 线（zzshare 行情源代理）。"""
    runtime = _runtime(request)
    if not runtime.cfg.zzshare_token:
        raise HTTPException(status_code=503, detail="行情服务未配置（ZZSHARE_TOKEN）")
    async with QuoteClient(runtime.cfg) as client:
        try:
            kline = await client.kline(ts_code.upper(), days)
        except (httpx.HTTPError, QuoteError) as exc:
            raise HTTPException(status_code=502, detail=f"行情源错误: {exc}") from exc
    return {"ts_code": ts_code.upper(), "kline": kline}


app = create_app()
