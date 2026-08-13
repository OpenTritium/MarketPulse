"""常驻采集调度器：由 API 进程生命周期托管。"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import Config
from .embed import Embedder
from .pipeline import run_collect

DEFAULT_COLLECT_INTERVAL_SECONDS = 30 * 60
DEFAULT_COLLECT_DELAY_SECONDS = 30
_COLLECT_TIMEOUT_SECONDS = 1500  # 30m 周期内给串行采集留 25 分钟
_DURATION_PATTERN = re.compile(r"(?P<amount>\d+)(?P<unit>[smh])")


@dataclass
class CollectorStatus:
    """采集器运行状态（供 /status 端点观测）。"""

    running: bool = False
    last_run_at: str | None = None
    last_result: dict[str, Any] | None = field(default=None)
    last_error: str | None = None


collector_status = CollectorStatus()


def parse_duration_seconds(value: str, *, allow_zero: bool) -> int:
    """解析带 ``s``、``m`` 或 ``h`` 后缀的时长并返回秒数。"""
    match = _DURATION_PATTERN.fullmatch(value.strip().lower())
    if match is None:
        raise ValueError("时长必须以 s、m 或 h 结尾，例如 30s、30m 或 2h")

    try:
        seconds = int(match.group("amount"))
    except ValueError as exc:
        raise ValueError("时长数值无效") from exc
    unit = match.group("unit")
    if unit == "m":
        seconds *= 60
    elif unit == "h":
        seconds *= 3600
    if seconds == 0 and not allow_zero:
        raise ValueError("时长必须大于 0")
    return seconds


async def run_scheduler(
    cfg: Config,
    *,
    embedder: Embedder | None = None,
    interval_seconds: int = DEFAULT_COLLECT_INTERVAL_SECONDS,
    delay_seconds: int = DEFAULT_COLLECT_DELAY_SECONDS,
) -> None:
    """周期执行采集，取消时立即退出，不留下未受管控的后台任务。"""
    if interval_seconds <= 0:
        raise ValueError("采集间隔必须大于 0 秒")
    if delay_seconds < 0:
        raise ValueError("首次采集延迟不能为负数")

    log = logging.getLogger("scheduler")
    log.info("调度器启动：间隔 %ss，首次延迟 %ss", interval_seconds, delay_seconds)

    await asyncio.sleep(delay_seconds)
    while True:
        collector_status.running = True
        try:
            stats = await asyncio.wait_for(
                run_collect(cfg, embedder=embedder), timeout=_COLLECT_TIMEOUT_SECONDS
            )
            collector_status.last_run_at = datetime.now(UTC).isoformat()
            collector_status.last_result = stats
            collector_status.last_error = None
            log.info("采集完成: %s", stats)
        except TimeoutError:
            # MCP 会话异常时 fastmcp 可能静默挂起，总超时兜底放弃本轮
            collector_status.last_error = "采集超时（>900s）"
            log.warning("本轮采集超时（>900s），放弃本轮，等待下一轮")
        except Exception as exc:
            collector_status.last_error = repr(exc)
            log.exception("本轮采集失败")
        finally:
            collector_status.running = False
        await asyncio.sleep(interval_seconds)
