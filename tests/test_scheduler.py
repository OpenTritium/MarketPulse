"""调度器生命周期回归测试。"""

from __future__ import annotations

import asyncio

import pytest

from market_pulse.config import build_config
from market_pulse.scheduler import parse_duration_seconds, run_scheduler


def test_parse_duration_seconds() -> None:
    assert parse_duration_seconds("30s", allow_zero=False) == 30
    assert parse_duration_seconds("2m", allow_zero=False) == 120
    assert parse_duration_seconds("1h", allow_zero=False) == 3600
    assert parse_duration_seconds("0s", allow_zero=True) == 0


@pytest.mark.parametrize("value", ["1800", "-1s", "1d", ""])
def test_parse_duration_seconds_rejects_invalid_suffix(value: str) -> None:
    with pytest.raises(ValueError):
        _ = parse_duration_seconds(value, allow_zero=False)


def test_parse_duration_seconds_rejects_zero_interval() -> None:
    with pytest.raises(ValueError, match="大于 0"):
        _ = parse_duration_seconds("0m", allow_zero=False)


def test_scheduler_cancels_while_waiting_for_initial_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del monkeypatch

    async def cancel_scheduler() -> None:
        task = asyncio.create_task(
            run_scheduler(
                build_config(),
                interval_seconds=3600,
                delay_seconds=3600,
            ),
            name="scheduler-test",
        )
        await asyncio.sleep(0)
        _ = task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

    asyncio.run(cancel_scheduler())
