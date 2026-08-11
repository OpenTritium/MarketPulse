"""API 契约测试：信封形状、游标分页、错误结构与请求 ID。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from market_pulse.api import create_app
from market_pulse.config import build_config
from market_pulse.db import Store


def _insert_event(store: Store, event_id: str, title: str, first_seen_at: str) -> None:
    store.conn.execute(
        """INSERT INTO events (id, title, first_seen_at, last_seen_at, report_count)
           VALUES (?, ?, ?, ?, 1)""",
        (event_id, title, first_seen_at, first_seen_at),
    )
    store.conn.commit()


def _store_for(tmp_path: Path) -> Store:
    return Store(build_config(turso_url=f"file:{tmp_path / 'market.db'}"))


def test_create_app_health_and_shutdown(tmp_path: Path) -> None:
    config = build_config(turso_url=f"file:{tmp_path / 'market.db'}")
    application = create_app(
        cfg=config,
        collect_interval_seconds=3600,
        collect_delay_seconds=3600,
    )

    with TestClient(application) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-Id"]


def test_timeline_cursor_pagination(tmp_path: Path) -> None:
    config = build_config(turso_url=f"file:{tmp_path / 'market.db'}")
    application = create_app(
        cfg=config,
        collect_interval_seconds=3600,
        collect_delay_seconds=3600,
    )
    with TestClient(application) as client:
        store = _store_for(tmp_path)
        # 乱序插入：first_seen_at 倒序应为 c → b → a（id 决胜键同时间戳）
        _insert_event(store, "evt-a", "A", "2026-08-01T00:00:00.000000Z")
        _insert_event(store, "evt-b", "B", "2026-08-02T00:00:00.000000Z")
        _insert_event(store, "evt-c", "C", "2026-08-02T00:00:00.000000Z")
        store.close()

    with TestClient(application) as client:
        page1 = client.get("/timeline", params={"limit": 2})
        body1 = page1.json()
        assert page1.status_code == 200
        assert body1["total"] == 3
        assert [e["id"] for e in body1["events"]] == ["evt-c", "evt-b"]
        assert body1["has_more"]
        assert body1["starting_after"] == "evt-b"

        page2 = client.get(
            "/timeline",
            params={"limit": 2, "starting_after": body1["starting_after"]},
        )
        body2 = page2.json()
        assert [e["id"] for e in body2["events"]] == ["evt-a"]
        assert body2["has_more"] is False
        assert body2["starting_after"] is None
        # 翻页期间插入新事件，游标不受影响、不重复
        store = _store_for(tmp_path)
        _insert_event(store, "evt-d", "D", "2026-08-03T00:00:00.000000Z")
        store.close()
        page3 = client.get(
            "/timeline",
            params={"limit": 2, "starting_after": body1["starting_after"]},
        )
        assert [e["id"] for e in page3.json()["events"]] == ["evt-a"]


def test_timeline_invalid_cursor(tmp_path: Path) -> None:
    config = build_config(turso_url=f"file:{tmp_path / 'market.db'}")
    application = create_app(
        cfg=config,
        collect_interval_seconds=3600,
        collect_delay_seconds=3600,
    )
    with TestClient(application) as client:
        response = client.get("/timeline", params={"starting_after": "missing"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["param"] is None
    assert error["request_id"]


def test_error_envelope_404_and_422(tmp_path: Path) -> None:
    config = build_config(turso_url=f"file:{tmp_path / 'market.db'}")
    application = create_app(
        cfg=config,
        collect_interval_seconds=3600,
        collect_delay_seconds=3600,
    )
    with TestClient(application) as client:
        not_found = client.get("/events/nope")
        validation = client.get("/timeline", params={"limit": 0})
        blank_query = client.get("/search", params={"q": "   "})

    assert not_found.status_code == 404
    error = not_found.json()["error"]
    assert error["type"] == "not_found_error"
    assert error["message"] == "事件不存在"
    assert error["request_id"]
    assert not_found.headers["X-Request-Id"] == error["request_id"]

    assert validation.status_code == 422
    error = validation.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["param"] == "limit"

    assert blank_query.status_code == 422
    error = blank_query.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["request_id"]
