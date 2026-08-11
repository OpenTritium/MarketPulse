"""API 工厂与生命周期回归测试。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from market_pulse.api import create_app
from market_pulse.config import build_config


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
