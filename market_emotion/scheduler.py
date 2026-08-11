#!/usr/bin/env python3
"""常驻采集调度器（容器内运行）：按间隔全量采集，防重叠。

环境变量：
  COLLECT_INTERVAL  采集间隔秒数（默认 1800 = 30 分钟）
  COLLECT_DELAY     启动后首次采集延迟秒数（默认 30，等 API 就绪）
"""

from __future__ import annotations
import asyncio
import logging
import os
from market_emotion.config import build_config
from market_emotion.pipeline import run_collect


async def _loop() -> None:
    log = logging.getLogger("scheduler")
    try:
        interval = int(os.environ.get("COLLECT_INTERVAL", "1800"))
        delay = int(os.environ.get("COLLECT_DELAY", "30"))
    except ValueError:
        interval, delay = 1800, 30  # 非法配置回退默认值
    running = False
    log.info("调度器启动：间隔 %ss，首次延迟 %ss", interval, delay)
    await asyncio.sleep(delay)
    while True:
        if not running:
            running = True
            try:
                stats = await run_collect(build_config())
                log.info("采集完成: %s", stats)
            except Exception:  # noqa: BLE001 - 单轮失败不影响后续
                log.exception("本轮采集失败")
            finally:
                running = False
        await asyncio.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_loop())
