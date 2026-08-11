"""情感查询 API：时间线 → 情绪总览 → 时序 → 因子分解 → 语义检索。

启动：uvicorn api:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from fastapi import FastAPI, HTTPException, Query
from market_emotion.config import build_config
from market_emotion.db import Store, try_json
from market_emotion.embed import Embedder
from market_emotion.factors import factor_descriptions

app = FastAPI(title="Market Emotion API", version="0.2.0")
_cfg = build_config()
_store = Store(_cfg)
_embedder = Embedder(_cfg)  # 惰性加载（首次 /search 时下载模型）


def _rows(since: datetime) -> list[dict[str, Any]]:
    """取过滤后的 (first_seen_at, sentiment, factors) 行，Python 侧聚合。"""
    rows = _store.conn.execute(
        "SELECT first_seen_at, sentiment, factors FROM events WHERE first_seen_at >= ?",
        (since.isoformat(),),
    ).fetchall()
    return [{"at": r[0], "sentiment": r[1], "factors": try_json(r[2])} for r in rows]


def _bucket_key(at: str, window: str) -> str:
    """按窗口生成分组键：hour → 'YYYY-MM-DD HH:00'，day → 'YYYY-MM-DD'。"""
    dt = datetime.fromisoformat(at)
    if window == "hour":
        return dt.strftime("%Y-%m-%d %H:00")
    return dt.strftime("%Y-%m-%d")


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/timeline")
def timeline(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """事件时间线：按出现时间倒序，每条带 sources 来源数组。"""
    rows = _store.timeline(limit, offset)
    return {"total": len(rows), "events": rows}


@app.get("/events/{event_id}")
def event_detail(event_id: str) -> dict[str, Any]:
    """事件详情 + 多来源报道列表。"""
    event = _store.event_detail(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    return event


@app.get("/sentiment/overview")
def overview() -> dict[str, Any]:
    """情绪总览：最新 24h 均值 + 7 日趋势（单次查询 14 天数据）。"""
    now = datetime.now(UTC)
    by_time = [(r["at"], r["sentiment"]) for r in _rows(now - timedelta(days=14))]
    c24 = (now - timedelta(hours=24)).isoformat()
    c7 = (now - timedelta(days=7)).isoformat()
    s24 = [v for at, v in by_time if v is not None and at >= c24]
    s7 = [v for at, v in by_time if v is not None and at >= c7]
    s_prev = [v for at, v in by_time if v is not None and at < c7]
    avg7 = _avg(s7)
    avg_prev = _avg(s_prev)
    trend = (
        round(avg7 - avg_prev, 3) if avg7 is not None and avg_prev is not None else None
    )
    return {
        "generated_at": now.isoformat(),
        "latest_24h": _avg(s24),
        "count_24h": sum(1 for at, _ in by_time if at >= c24),
        "avg_7d": avg7,
        "trend_7d": trend,
    }


@app.get("/sentiment/timeseries")
def timeseries(
    window: str = Query("day", pattern="^(hour|day)$"),
    hours: int = Query(72, ge=1, le=24 * 30),
) -> dict[str, Any]:
    """情绪时间序列：AVG + 计数 + 正负占比，按 hour/day 分桶。"""
    since = datetime.now(UTC) - timedelta(hours=hours)
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "sum": 0.0, "pos": 0, "neg": 0}
    )
    for r in _rows(since):
        key = _bucket_key(r["at"], window)
        b = buckets[key]
        b["count"] += 1
        if r["sentiment"] is not None:
            b["sum"] += r["sentiment"]
            if r["sentiment"] > 0.05:
                b["pos"] += 1
            elif r["sentiment"] < -0.05:
                b["neg"] += 1
    series = [
        {
            "bucket": key,
            "avg_sentiment": round(b["sum"] / b["count"], 3),
            "count": b["count"],
            "positive_ratio": round(b["pos"] / b["count"], 3),
            "negative_ratio": round(b["neg"] / b["count"], 3),
        }
        for key, b in sorted(buckets.items())
    ]
    return {"window": window, "series": series}


@app.get("/sentiment/factors")
def factors(
    hours: int = Query(72, ge=1, le=24 * 30),
) -> dict[str, Any]:
    """因子分解：每个因子的平均情绪 + 覆盖事件数 + 权重。"""
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = _rows(since)
    agg: dict[str, dict[str, float]] = defaultdict(lambda: {"sum": 0.0, "n": 0})
    for r in rows:
        for k, v in (r["factors"] or {}).items():
            if isinstance(v, (int, float)):
                agg[k]["sum"] += v
                agg[k]["n"] += 1
    total = len(rows) or 1
    result = []
    for name, desc in factor_descriptions().items():
        a = agg.get(name)
        result.append(
            {
                "factor": name,
                "description": desc,
                "avg": round(a["sum"] / a["n"], 3) if a and a["n"] else None,
                "events": a["n"] if a else 0,
                "coverage": round(a["n"] / total, 3) if a else 0.0,
            }
        )
    return {"hours": hours, "factors": result}


@app.get("/search")
def search(
    q: str = Query(min_length=1, max_length=200),
    k: int = Query(10, ge=1, le=50),
) -> dict[str, Any]:
    """语义检索：查询 → embedding → 事件向量最近邻。"""
    if not q.strip():
        raise HTTPException(status_code=422, detail="q 不能为空")
    return {
        "query": q,
        "results": _store.search_similar(_embedder.embed(q), k=k),
    }
