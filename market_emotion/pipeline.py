"""采集编排（async）：源列表 → wigolo 抓取 → LLM 提取/分析 → embedding → 入库。

单进程顺序执行，每步独立容错（单条目失败不影响整轮）。
"""

from __future__ import annotations
import logging
import time
from typing import Any
from .config import Config
from .db import Report, Store
from .embed import Embedder
from .llm import AnalysisResult, Analyzer, NewsItem
from .mcp import MCPError, WigoloMCP
from .sources import SOURCES

_log = logging.getLogger("collect")

# JS 渲染站点需要 render_js=always
_JS_SITES = ("cls.cn", "wallstreetcn", "eastmoney")

# 渲染后还需等待数据加载的站点（URL 片段 → 等待毫秒）
_WAIT_SITES = {"eastmoney": 4000}

# 每源每轮条目上限
_MAX_ITEMS_PER_SOURCE = 20


def _extract_page_text(result: Any) -> str | None:
    """从 wigolo fetch 结果提取可分析文本。"""
    if isinstance(result, str):
        return result[:30000]
    if isinstance(result, dict):
        for key in ("markdown", "content", "text", "page"):
            v = result.get(key)
            if isinstance(v, str) and v.strip():
                return v[:30000]
    return None


class Collector:
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg: Config = cfg
        self.store: Store = Store(cfg)
        self.analyzer: Analyzer = Analyzer(cfg)
        self.embedder: Embedder = Embedder(cfg)  # 惰性加载（首次用才下载模型）

    # ---- 抓取 ----

    async def _fetch_page(
        self, mcp: WigoloMCP, url: str, max_chars: int, label: str
    ) -> str | None:
        """抓取页面文本。JS 渲染站点自动启用 render_js，数据异步加载的站点加等待。"""
        kw: dict[str, Any] = {"max_content_chars": max_chars}
        if any(s in url for s in _JS_SITES):
            kw["render_js"] = "always"
        for site, ms in _WAIT_SITES.items():
            if site in url:
                kw["actions"] = [{"type": "wait", "ms": ms}]
        try:
            result = await mcp.fetch(url, **kw)
        except MCPError as e:
            _log.warning("[%s] 抓取失败 %s: %s", label, url, e)
            return None
        return _extract_page_text(result)

    # ---- 单源处理 ----

    async def _process_source(
        self, mcp: WigoloMCP, source: dict[str, Any]
    ) -> dict[str, int]:
        stats = {
            "listed": 0,
            "new": 0,
            "merged": 0,
            "duplicate": 0,
            "skipped": 0,
            "failed": 0,
        }
        text = await self._fetch_page(mcp, source["url"], 30000, source["name"])
        if not text:
            return stats
        items = await self.analyzer.extract_items(text, base_url=source["url"])
        stats["listed"] = len(items)
        for it in items[:_MAX_ITEMS_PER_SOURCE]:
            content = it.content or await self._fetch_page(
                mcp, it.url, 20000, source["name"]
            )
            if not content:
                stats["failed"] += 1
                continue
            try:
                outcome = await self._process_report(it, content, source)
                stats[outcome] += 1  # new / merged / duplicate
            except Exception:  # noqa: BLE001 - 单条失败不中断整轮
                _log.exception("[%s] 条目处理失败 %s", source["name"], it.url)
                stats["failed"] += 1
        return stats

    async def _process_report(
        self, item: NewsItem, content: str, source: dict[str, Any]
    ) -> str:
        """前置去重 → LLM 分析 → 相关性过滤 → embedding → 合并或新建。

        返回 new/merged/duplicate/skipped。"""
        # 1. 前置去重：已分析过的 URL+内容直接跳过（省 LLM token）
        if self.store.is_known(item.url, item.title, content):
            return "duplicate"
        # 2. LLM 分析（含 relevant 判断）
        analysis: AnalysisResult = await self.analyzer.analyze(item.title, content)
        # 3. 相关性过滤：对市场无影响的新闻不存储（省 embedding + 存储）
        if not analysis.relevant:
            return "skipped"
        vec = self.embedder.embed(analysis.summary or item.title)
        report = Report(
            url=item.url,
            source=source["name"],
            title=item.title,
            published_at=item.published_at or None,
            raw_text=content,
            summary=analysis.summary,
            headline=analysis.headline,
            sentiment=analysis.sentiment,
            impact=analysis.impact,
            factors=analysis.factors,
            related_symbols=analysis.related_symbols,
        )
        _, outcome = self.store.merge_or_create(report, vec)
        _log.info(
            "[%s] %s 事件 %s sentiment=%s",
            source["name"],
            {
                "new": "新建",
                "merged": "合并",
                "duplicate": "跳过",
                "skipped": "跳过(无关)",
            }[outcome],
            item.title[:40],
            analysis.sentiment,
        )
        return outcome

    # ---- 整轮 ----

    async def run(self, sources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        sources = sources or [{"name": n, "url": u} for n, u in SOURCES.items()]
        totals = {
            "sources": len(sources),
            "new": 0,
            "merged": 0,
            "duplicate": 0,
            "skipped": 0,
            "failed": 0,
            "duration_s": 0.0,
        }
        t0 = time.time()
        async with WigoloMCP(self.cfg) as mcp:
            for source in sources:
                st = await self._process_source(mcp, source)
                for k in ("new", "merged", "duplicate", "skipped", "failed"):
                    totals[k] += st[k]
                _log.info(
                    "[%s] 列表 %s 新增 %s 重复 %s 失败 %s",
                    source["name"],
                    st["listed"],
                    st["new"],
                    st["duplicate"],
                    st["failed"],
                )
        self.store.close()
        totals["duration_s"] = round(time.time() - t0, 1)
        return totals


async def run_collect(
    cfg: Config, sources: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return await Collector(cfg).run(sources)
