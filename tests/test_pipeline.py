"""采集编排的失败统计和并发边界回归测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from types import TracebackType
from typing import Any, ClassVar, Self, cast

import pytest

from market_pulse import pipeline
from market_pulse.config import EMBEDDING_DIM, Config, build_config
from market_pulse.db import Report
from market_pulse.embed import Embedder
from market_pulse.llm import AnalysisResult, NewsItem
from market_pulse.mcp import MCPError


class FakeStore:
    """只实现编排层需要的串行存储接口。"""

    instances: ClassVar[list[Self]] = []

    def __init__(self, cfg: Config) -> None:
        del cfg
        self.closed: bool = False
        self.write_count: int = 0
        type(self).instances.append(self)

    def close(self) -> None:
        self.closed = True

    def reserve_report(self, url: str, title: str, content: str) -> bool:
        del url, title, content
        return True

    @contextmanager
    def batch(self) -> Generator[None]:
        yield

    @contextmanager
    def savepoint(self) -> Generator[None]:
        yield

    def merge_or_create(
        self, report: Report, embedding: list[float]
    ) -> tuple[str, str]:
        del report, embedding
        self.write_count += 1
        return f"event-{self.write_count}", "new"


class FakeEmbedder:
    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * EMBEDDING_DIM for _ in texts]


class BaseMCP:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback


class FailingSourceMCP(BaseMCP):
    def __init__(self, cfg: Config) -> None:
        del cfg

    async def fetch(self, url: str, **_: Any) -> str:
        if "fetch-failure" in url:
            raise MCPError("source is unavailable")
        return "listing that cannot be parsed"


class FailingListingAnalyzer:
    def __init__(self, cfg: Config) -> None:
        del cfg

    async def extract_items(
        self, page_text: str, base_url: str, limit: int = 10
    ) -> list[NewsItem]:
        del page_text, base_url, limit
        raise RuntimeError("invalid listing response")

    async def analyze(self, title: str, content: str) -> AnalysisResult:
        del title, content
        raise AssertionError("analysis should not run after listing extraction failure")


class TrackingMCP(BaseMCP):
    instance: ClassVar[Self | None] = None

    def __init__(self, cfg: Config) -> None:
        del cfg
        self.active: int = 0
        self.max_active: int = 0
        type(self).instance = self

    async def fetch(self, url: str, **_: Any) -> str:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            return "listing" if url.endswith("/listing") else "article body"
        finally:
            self.active -= 1


class TrackingAnalyzer:
    instance: ClassVar[Self | None] = None

    def __init__(self, cfg: Config) -> None:
        del cfg
        self.active: int = 0
        self.max_active: int = 0
        type(self).instance = self

    async def _work(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
        finally:
            self.active -= 1

    async def extract_items(
        self, page_text: str, base_url: str, limit: int = 10
    ) -> list[NewsItem]:
        del page_text, limit
        await self._work()
        return [NewsItem(title=base_url, url=f"{base_url}/article")]

    async def analyze(self, title: str, content: str) -> AnalysisResult:
        del title, content
        await self._work()
        return AnalysisResult(
            headline="headline",
            summary="summary",
            sentiment=0.2,
            impact=0.3,
            relevant=True,
        )


def _collector(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mcp: type[BaseMCP],
    analyzer: type[Any],
) -> pipeline.Collector:
    FakeStore.instances.clear()
    monkeypatch.setattr(pipeline, "Store", FakeStore)
    monkeypatch.setattr(pipeline, "WigoloMCP", mcp)
    monkeypatch.setattr(pipeline, "Analyzer", analyzer)
    cfg = build_config(turso_url="file:unused.db")
    return pipeline.Collector(
        cfg, embedder=cast(Embedder, cast(object, FakeEmbedder()))
    )


def test_source_fetch_and_listing_extraction_failures_are_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = _collector(
        monkeypatch, mcp=FailingSourceMCP, analyzer=FailingListingAnalyzer
    )

    stats = asyncio.run(
        collector.run(
            [
                {"name": "fetch", "url": "https://fetch-failure.test/listing"},
                {"name": "extract", "url": "https://extract-failure.test/listing"},
            ]
        )
    )

    assert stats["listed"] == 0
    assert stats["failed"] == 2
    assert stats["source_fetch_failed"] == 1
    assert len(FakeStore.instances) == 1
    assert FakeStore.instances[0].closed


def test_network_and_llm_work_respect_their_independent_concurrency_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = _collector(monkeypatch, mcp=TrackingMCP, analyzer=TrackingAnalyzer)
    sources = [
        {"name": f"source-{index}", "url": f"https://source.test/{index}/listing"}
        for index in range(5)
    ]

    stats = asyncio.run(collector.run(sources))

    mcp = TrackingMCP.instance
    analyzer = TrackingAnalyzer.instance
    assert mcp is not None
    assert analyzer is not None
    assert mcp.max_active == 1  # _FETCH_CONCURRENCY=1：串行抓取防 OOM
    assert analyzer.max_active == 4  # _ANALYSIS_CONCURRENCY 独立
    assert stats["new"] == len(sources)
    assert stats["failed"] == 0
    assert FakeStore.instances[0].closed
