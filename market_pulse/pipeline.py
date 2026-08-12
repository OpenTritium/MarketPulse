"""采集编排：并发抓取与 LLM 分析，串行批量 embedding 和入库。

外部网络调用使用受限并发；去重、向量合并和 SQLite 写入始终由单一
Collector 顺序执行，避免 SQLite 竞争并保证每轮只提交一次事务。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from .config import Config
from .db import Report, Store
from .embed import Embedder
from .llm import AnalysisResult, Analyzer, NewsItem
from .mcp import MCPError, WigoloMCP
from .sources import SOURCES

_log = logging.getLogger("collect")

_JS_SITES = ("cls.cn", "wallstreetcn", "eastmoney")
_WAIT_SITES = {"eastmoney": 4000}
_MAX_ITEMS_PER_SOURCE = 20
_FETCH_CONCURRENCY = 4
_ANALYSIS_CONCURRENCY = 4

Source = dict[str, str]
Stats = dict[str, int]


@dataclass(frozen=True)
class SourceListing:
    source: Source
    items: list[NewsItem]
    failed: bool
    fetch_failed: bool


@dataclass(frozen=True)
class FetchedItem:
    source: Source
    item: NewsItem
    content: str


@dataclass(frozen=True)
class AnalyzedItem:
    fetched: FetchedItem
    analysis: AnalysisResult


def _empty_stats() -> Stats:
    return {
        "listed": 0,
        "new": 0,
        "merged": 0,
        "duplicate": 0,
        "skipped": 0,
        "failed": 0,
        "source_fetch_failed": 0,
    }


def _extract_page_text(result: Any) -> str | None:
    """从 wigolo fetch 结果提取可分析文本。"""
    if isinstance(result, str):
        return result[:30000]
    if isinstance(result, dict):
        for key in ("markdown", "content", "text", "page"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value[:30000]
    return None


class Collector:
    def __init__(self, cfg: Config, *, embedder: Embedder | None = None):
        super().__init__()
        self.cfg: Config = cfg
        self.store: Store = Store(cfg)
        self.analyzer: Analyzer = Analyzer(cfg)
        self.embedder: Embedder = embedder or Embedder(cfg)
        self._fetch_semaphore: asyncio.Semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)
        self._analysis_semaphore: asyncio.Semaphore = asyncio.Semaphore(
            _ANALYSIS_CONCURRENCY
        )

    # ---- 外部 I/O：受限并发 ----

    async def _fetch_page(
        self, mcp: WigoloMCP, url: str, max_chars: int, label: str
    ) -> str | None:
        """抓取页面文本，单次失败返回 None 让调用方记录对应统计。"""
        arguments: dict[str, Any] = {"max_content_chars": max_chars}
        if any(site in url for site in _JS_SITES):
            arguments["render_js"] = "always"
        for site, wait_ms in _WAIT_SITES.items():
            if site in url:
                arguments["actions"] = [{"type": "wait", "ms": wait_ms}]

        try:
            async with self._fetch_semaphore:
                result = await mcp.fetch(url, **arguments)
        except MCPError as exc:
            _log.warning("[%s] 抓取失败 %s: %s", label, url, exc)
            return None
        return _extract_page_text(result)

    async def _fetch_source_listing(
        self, mcp: WigoloMCP, source: Source
    ) -> SourceListing:
        """并发获取并提取一个来源的列表页。"""
        page_text = await self._fetch_page(mcp, source["url"], 30000, source["name"])
        if not page_text:
            return SourceListing(
                source=source,
                items=[],
                failed=True,
                fetch_failed=True,
            )

        try:
            async with self._analysis_semaphore:
                items = await self.analyzer.extract_items(
                    page_text, source["url"], limit=_MAX_ITEMS_PER_SOURCE
                )
        except Exception:  # noqa: BLE001 - 单源提取失败不终止本轮
            _log.exception("[%s] 列表页提取失败", source["name"])
            return SourceListing(
                source=source,
                items=[],
                failed=True,
                fetch_failed=False,
            )
        return SourceListing(
            source=source,
            items=items[:_MAX_ITEMS_PER_SOURCE],
            failed=False,
            fetch_failed=False,
        )

    async def _fetch_item(
        self, mcp: WigoloMCP, source: Source, item: NewsItem
    ) -> FetchedItem | None:
        """获取一篇报道正文；列表页已有正文时避免重复抓取。"""
        content = item.content or await self._fetch_page(
            mcp, item.url, 20000, source["name"]
        )
        if not content:
            return None
        return FetchedItem(source=source, item=item, content=content)

    async def _analyze_item(self, fetched: FetchedItem) -> AnalyzedItem | None:
        """并发分析一篇已去重报道。"""
        try:
            async with self._analysis_semaphore:
                analysis = await self.analyzer.analyze(
                    fetched.item.title, fetched.content
                )
        except Exception:  # noqa: BLE001 - 单条分析失败不终止本轮
            _log.exception(
                "[%s] 条目分析失败 %s", fetched.source["name"], fetched.item.url
            )
            return None
        return AnalyzedItem(fetched=fetched, analysis=analysis)

    # ---- 串行阶段：去重、embedding、SQLite 写入 ----

    def _reserve_new_reports(
        self, fetched_items: list[FetchedItem], stats_by_source: dict[str, Stats]
    ) -> list[FetchedItem]:
        """串行执行数据库去重，再决定需要调用 LLM 的报道。"""
        pending: list[FetchedItem] = []
        for fetched in fetched_items:
            stats = stats_by_source[fetched.source["name"]]
            if self.store.reserve_report(
                fetched.item.url, fetched.item.title, fetched.content
            ):
                pending.append(fetched)
            else:
                stats["duplicate"] += 1
        return pending

    async def _store_analyzed_reports(
        self, analyzed_items: list[AnalyzedItem], stats_by_source: dict[str, Stats]
    ) -> None:
        """批量生成向量，并在单个 SQLite 事务中顺序写入。"""
        relevant_items: list[AnalyzedItem] = []
        for analyzed in analyzed_items:
            stats = stats_by_source[analyzed.fetched.source["name"]]
            if analyzed.analysis.relevant:
                relevant_items.append(analyzed)
            else:
                stats["skipped"] += 1

        if not relevant_items:
            return

        texts = [
            item.analysis.summary or item.fetched.item.title for item in relevant_items
        ]
        vectors = await asyncio.to_thread(self.embedder.embed_many, texts)
        with self.store.batch():
            for analyzed, vector in zip(relevant_items, vectors, strict=True):
                source = analyzed.fetched.source
                stats = stats_by_source[source["name"]]
                report = Report(
                    url=analyzed.fetched.item.url,
                    source=source["name"],
                    title=analyzed.fetched.item.title,
                    published_at=analyzed.fetched.item.published_at or None,
                    raw_text=analyzed.fetched.content,
                    summary=analyzed.analysis.summary,
                    headline=analyzed.analysis.headline,
                    sentiment=analyzed.analysis.sentiment,
                    impact=analyzed.analysis.impact,
                    factors=analyzed.analysis.factors,
                    related_symbols=[s.model_dump() for s in analyzed.analysis.related_symbols],
                )
                try:
                    with self.store.savepoint():
                        _, outcome = self.store.merge_or_create(report, vector)
                except Exception:  # noqa: BLE001 - 回滚单条后继续本批次
                    _log.exception("[%s] 条目入库失败 %s", source["name"], report.url)
                    stats["failed"] += 1
                    continue

                stats[outcome] += 1
                _log.info(
                    "[%s] %s 事件 %s sentiment=%s",
                    source["name"],
                    {"new": "新建", "merged": "合并", "duplicate": "跳过"}[outcome],
                    report.title[:40],
                    analyzed.analysis.sentiment,
                )

    # ---- 整轮 ----

    async def run(self, sources: list[Source] | None = None) -> dict[str, Any]:
        selected_sources = sources or [
            {"name": name, "url": url} for name, url in SOURCES.items()
        ]
        stats_by_source = {
            source["name"]: _empty_stats() for source in selected_sources
        }
        started_at = time.monotonic()
        try:
            async with WigoloMCP(self.cfg) as mcp:
                listings = await asyncio.gather(
                    *(
                        self._fetch_source_listing(mcp, source)
                        for source in selected_sources
                    )
                )
                fetch_jobs: list[tuple[Source, NewsItem]] = []
                for listing in listings:
                    stats = stats_by_source[listing.source["name"]]
                    if listing.failed:
                        stats["failed"] += 1
                        if listing.fetch_failed:
                            stats["source_fetch_failed"] += 1
                        continue
                    stats["listed"] = len(listing.items)
                    fetch_jobs.extend((listing.source, item) for item in listing.items)

                fetched_results = await asyncio.gather(
                    *(
                        self._fetch_item(mcp, source, item)
                        for source, item in fetch_jobs
                    )
                )
                fetched_items: list[FetchedItem] = []
                for (source, _), fetched in zip(
                    fetch_jobs, fetched_results, strict=True
                ):
                    if fetched is not None:
                        fetched_items.append(fetched)
                    else:
                        stats_by_source[source["name"]]["failed"] += 1

                pending_items = self._reserve_new_reports(
                    fetched_items, stats_by_source
                )
                analyzed_results = await asyncio.gather(
                    *(self._analyze_item(item) for item in pending_items)
                )
                analyzed_items: list[AnalyzedItem] = []
                for pending, analyzed in zip(
                    pending_items, analyzed_results, strict=True
                ):
                    if analyzed is None:
                        stats_by_source[pending.source["name"]]["failed"] += 1
                    else:
                        analyzed_items.append(analyzed)

                await self._store_analyzed_reports(analyzed_items, stats_by_source)
        finally:
            self.store.close()

        totals: dict[str, Any] = {
            "sources": len(selected_sources),
            "listed": 0,
            "new": 0,
            "merged": 0,
            "duplicate": 0,
            "skipped": 0,
            "failed": 0,
            "source_fetch_failed": 0,
            "duration_s": round(time.monotonic() - started_at, 1),
        }
        for source in selected_sources:
            stats = stats_by_source[source["name"]]
            for key, value in stats.items():
                totals[key] += value
            _log.info(
                "[%s] 列表 %s 新增 %s 合并 %s 重复 %s 跳过 %s 失败 %s",
                source["name"],
                stats["listed"],
                stats["new"],
                stats["merged"],
                stats["duplicate"],
                stats["skipped"],
                stats["failed"],
            )
        return totals


async def run_collect(
    cfg: Config,
    sources: list[Source] | None = None,
    *,
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    """执行一轮采集。"""
    return await Collector(cfg, embedder=embedder).run(sources)
