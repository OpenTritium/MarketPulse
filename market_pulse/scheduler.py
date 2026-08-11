"""常驻采集调度器：由 API 进程生命周期托管。"""

from __future__ import annotations

import asyncio
import logging
import re

from .config import Config
from .embed import Embedder
from .pipeline import run_collect

DEFAULT_COLLECT_INTERVAL_SECONDS = 30 * 60
DEFAULT_COLLECT_DELAY_SECONDS = 30
_DURATION_PATTERN = re.compile(r"(?P<amount>\d+)(?P<unit>[smh])")


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
        try:
            stats = await run_collect(cfg, embedder=embedder)
            log.info("采集完成: %s", stats)
        except Exception:
            log.exception("本轮采集失败")
        await asyncio.sleep(interval_seconds)
