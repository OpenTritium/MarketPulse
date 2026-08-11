"""存储层回归测试：UTC、分页和批量事务。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from market_pulse.config import EMBEDDING_DIM, build_config
from market_pulse.db import Report, Store
from market_pulse.timestamps import format_utc_timestamp


def _vector(index: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[index] = 1.0
    return vector


def _report(index: int) -> Report:
    return Report(
        url=f"https://example.test/{index}",
        source="test",
        title=f"title {index}",
        published_at=None,
        raw_text=f"body {index}",
        summary=f"summary {index}",
        headline=f"headline {index}",
        sentiment=0.2,
        impact=0.3,
        factors={"policy": 0.2},
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    instance = Store(build_config(turso_url=f"file:{tmp_path / 'market.db'}"))
    try:
        yield instance
    finally:
        instance.close()


def test_store_migrates_legacy_utc_timestamps(tmp_path: Path) -> None:
    database = tmp_path / "market.db"
    first = Store(build_config(turso_url=f"file:{database}"))
    try:
        with first.batch():
            _, _ = first.merge_or_create(_report(1), _vector(0))
        first.conn.execute(
            "UPDATE events SET first_seen_at = ?, last_seen_at = ?",
            ("2026-08-10 12:00:00", "2026-08-10 12:00:00"),
        )
        first.conn.execute(
            "UPDATE reports SET collected_at = ?, published_at = ?",
            ("2026-08-10 12:00:00", "2026-08-10T20:00:00+08:00"),
        )
        first.conn.commit()
    finally:
        first.close()

    migrated = Store(build_config(turso_url=f"file:{database}"))
    try:
        event_time = migrated.conn.execute(
            "SELECT first_seen_at FROM events"
        ).fetchone()[0]
        report_row = migrated.conn.execute(
            "SELECT collected_at, published_at FROM reports"
        ).fetchone()
        report_time, published_at = report_row
        assert event_time == "2026-08-10T12:00:00.000000Z"
        assert report_time == "2026-08-10T12:00:00.000000Z"
        assert published_at == "2026-08-10T12:00:00.000000Z"
    finally:
        migrated.close()


def test_store_normalizes_report_published_at(store: Store) -> None:
    report = _report(1)
    report.published_at = "2026-08-10T20:00:00+08:00"
    with store.batch():
        _, _ = store.merge_or_create(report, _vector(0))
    published_at = store.conn.execute("SELECT published_at FROM reports").fetchone()[0]
    assert published_at == "2026-08-10T12:00:00.000000Z"


def test_sentiment_window_uses_utc_datetimes(store: Store) -> None:
    with store.batch():
        _, _ = store.merge_or_create(_report(1), _vector(0))
    now = datetime.now(UTC)
    recent = format_utc_timestamp(now - timedelta(hours=1))
    older = format_utc_timestamp(now - timedelta(days=2))
    store.conn.execute("UPDATE events SET first_seen_at = ?", (recent,))
    assert len(store.sentiment_rows_since(now - timedelta(hours=2))) == 1
    store.conn.execute("UPDATE events SET first_seen_at = ?", (older,))
    assert store.sentiment_rows_since(now - timedelta(hours=2)) == []


def test_timeline_returns_total_across_pages(store: Store) -> None:
    with store.batch():
        for index in range(3):
            _, _ = store.merge_or_create(_report(index), _vector(index))
    total, events = store.timeline(limit=1, offset=1)
    assert total == 3
    assert len(events) == 1


def test_savepoint_keeps_other_batch_writes(store: Store) -> None:
    with store.batch():
        _, _ = store.merge_or_create(_report(1), _vector(0))
        with pytest.raises(ValueError), store.savepoint():
            _, _ = store.merge_or_create(_report(2), [0.0])
        _, _ = store.merge_or_create(_report(3), _vector(1))
    total, _ = store.timeline()
    assert total == 2
