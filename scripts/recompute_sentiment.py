"""历史事件情绪重算：用新 prompt 重评历史报道，按 impact 加权聚合。

背景：reports 表早期不存报道级 sentiment/impact/factors（只有事件级
简单平均结果）。本脚本读取历史报道的 raw_text（zlib 压缩存储，自动
解压），用最新 LLM prompt（打分依据约束）重新评分，然后按 impact
加权聚合回写 events（与新采集逻辑一致）。

用法：
    python -m scripts.recompute_sentiment

注意：会消耗 LLM token（每篇报道一次分析调用）。
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

from market_pulse.config import build_config
from market_pulse.db import Store, try_json
from market_pulse.llm import Analyzer
from market_pulse.scheduler import collector_status


def _safe_float(value: Any, default: float = 0.0) -> float:
    """容错数值转换：脏数据（None/非数字/NaN/Inf）回退默认值。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


async def _reanalyze_reports(store: Store) -> list[tuple[str, dict[str, Any]]]:
    """对尚无报道级分数的历史报道重跑 LLM 分析（新 prompt）。"""
    rows = store.conn.execute(
        """SELECT id, url, title, raw_text FROM reports
           WHERE sentiment IS NULL OR impact IS NULL"""
    ).fetchall()
    analyzer = Analyzer(store.cfg)
    results: list[tuple[str, dict[str, Any]]] = []
    semaphore = asyncio.Semaphore(4)

    async def work(report_id: str, title: str, raw: Any) -> None:
        if isinstance(raw, bytes):
            try:
                text = zlib_decompress(raw)
            except Exception:
                text = ""
        else:
            text = raw or ""
        if not text.strip():
            return
        async with semaphore:
            try:
                analysis = await analyzer.analyze(title, text)
            except Exception as exc:
                print(f"  [warn] 报道 {report_id} 重评失败: {exc}")
                return
        results.append(
            (
                report_id,
                {
                    "sentiment": analysis.sentiment,
                    "impact": analysis.impact,
                    "factors": analysis.factors,
                },
            )
        )

    _ = await asyncio.gather(*(work(rid, title, raw) for rid, _url, title, raw in rows))
    return results


def zlib_decompress(raw: bytes) -> str:
    import zlib

    return zlib.decompress(raw).decode(errors="replace")


def _aggregate(rows: list[tuple[Any, Any, Any]]) -> tuple[float, float, float, dict[str, float]]:
    """按 impact 加权聚合 sentiment/factors，impact 取最大值。"""
    impact_sum = 0.0
    weighted_sentiment = 0.0
    weighted_factors: dict[str, float] = {}
    max_impact = 0.0
    for sentiment, impact, factors_json in rows:
        impact = _safe_float(impact)
        max_impact = max(max_impact, impact)
        impact_sum += impact
        sentiment = _safe_float(sentiment)
        if impact > 0:
            weighted_sentiment += sentiment * impact
        for name, value in (try_json(factors_json) or {}).items():
            if isinstance(value, int | float):
                weighted_factors[name] = (
                    weighted_factors.get(name, 0.0) + _safe_float(value) * impact
                )
    if impact_sum > 0:
        return (
            weighted_sentiment / impact_sum,
            max_impact,
            impact_sum,
            {k: v / impact_sum for k, v in weighted_factors.items()},
        )
    count = len(rows) or 1
    return (
        sum(_safe_float(r[0]) for r in rows) / count,
        max_impact,
        impact_sum,
        {},
    )


def recompute(store: Store) -> dict[str, int]:
    """用 reports 现有报道级分数聚合所有事件（无分数时跳过）。"""
    events = store.conn.execute("SELECT id FROM events").fetchall()
    updated = 0
    skipped = 0
    with store.batch():
        for (event_id,) in events:
            rows = store.conn.execute(
                """SELECT sentiment, impact, factors FROM reports
                   WHERE event_id = ?""",
                (event_id,),
            ).fetchall()
            if not rows:
                skipped += 1
                continue
            if any(r[0] is None or r[1] is None for r in rows):
                skipped += 1  # 仍有未重评报道，等重评后再聚合
                continue
            sentiment, impact, impact_sum, factors = _aggregate(rows)
            store.conn.execute(
                """UPDATE events SET sentiment = ?, impact = ?, factors = ?, impact_sum = ?
                   WHERE id = ?""",
                (
                    round(sentiment, 4),
                    round(impact, 4),
                    json.dumps(
                        {k: round(v, 4) for k, v in factors.items()},
                        ensure_ascii=False,
                    )
                    if factors
                    else None,
                    round(impact_sum, 4),
                    event_id,
                ),
            )
            updated += 1
    return {"updated": updated, "skipped": skipped}


async def main() -> None:
    store = Store(build_config())
    try:
        print("== 1/3 重评历史报道（新 prompt）==")
        scores = await _reanalyze_reports(store)
        print(f"   重评 {len(scores)} 篇报道")
        for report_id, score in scores:
            store.conn.execute(
                """UPDATE reports SET sentiment = ?, impact = ?, factors = ?
                   WHERE id = ?""",
                (
                    score["sentiment"],
                    score["impact"],
                    json.dumps(score["factors"], ensure_ascii=False)
                    if score["factors"]
                    else None,
                    report_id,
                ),
            )
        store.conn.commit()
        print("== 2/3 按 impact 加权聚合事件 ==")
        result = recompute(store)
        print(f"   更新 {result['updated']} 个事件，跳过 {result['skipped']} 个")
    finally:
        store.close()


if __name__ == "__main__":
    collector_status.running = False  # 脚本独立运行，与调度器无关
    asyncio.run(main())
