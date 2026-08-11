"""情感查询 API：时间线、情绪聚合与语义检索。

应用生命周期在单一进程内启动采集调度器；HTTP 查询和采集共享
embedding 模型缓存，但使用各自的 SQLite 连接。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request

from market_pulse.config import Config, build_config
from market_pulse.db import Store
from market_pulse.embed import Embedder
from market_pulse.factors import factor_descriptions
from market_pulse.scheduler import (
    DEFAULT_COLLECT_DELAY_SECONDS,
    DEFAULT_COLLECT_INTERVAL_SECONDS,
    run_scheduler,
)
from market_pulse.timestamps import format_utc_timestamp


@dataclass(frozen=True)
class Runtime:
    """一个 API 进程持有的共享运行时资源。"""

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
        app_instance.state.runtime = Runtime(store=store, embedder=embedder)
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
        version="0.2.0",
        lifespan=_lifespan_factory(
            config,
            collect_interval_seconds=collect_interval_seconds,
            collect_delay_seconds=collect_delay_seconds,
        ),
    )
    application.include_router(router)
    return application


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


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/timeline")
def timeline(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """事件时间线：按出现时间倒序，每条带来源数组。"""
    total, rows = _runtime(request).store.timeline(limit, offset)
    return {"total": total, "events": rows}


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
        lambda: {"count": 0, "sum": 0.0, "pos": 0, "neg": 0}
    )
    for row in _rows(_runtime(request).store, since):
        key = _bucket_key(row["at"], window)
        bucket = buckets[key]
        bucket["count"] += 1
        if row["sentiment"] is not None:
            bucket["sum"] += row["sentiment"]
            if row["sentiment"] > 0.05:
                bucket["pos"] += 1
            elif row["sentiment"] < -0.05:
                bucket["neg"] += 1
    series = [
        {
            "bucket": key,
            "avg_sentiment": round(bucket["sum"] / bucket["count"], 3),
            "count": bucket["count"],
            "positive_ratio": round(bucket["pos"] / bucket["count"], 3),
            "negative_ratio": round(bucket["neg"] / bucket["count"], 3),
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


app = create_app()
