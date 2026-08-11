"""HTTP API 启动入口：显式配置采集调度时长。"""

from __future__ import annotations

import logging

import typer
import uvicorn

from .api import create_app
from .config import build_config
from .scheduler import parse_duration_seconds


def _parse_duration_option(value: str, *, option_name: str, allow_zero: bool) -> int:
    try:
        return parse_duration_seconds(value, allow_zero=allow_zero)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint=option_name) from exc


def main(
    collect_interval: str = typer.Option(
        "30m",
        "--collect-interval",
        help="采集间隔；使用 s、m 或 h 后缀，例如 30m 或 2h（必须大于 0）",
    ),
    collect_delay: str = typer.Option(
        "30s",
        "--collect-delay",
        help="首次采集延迟；使用 s、m 或 h 后缀，例如 30s（允许 0s）",
    ),
    host: str = typer.Option("0.0.0.0", "--host", help="HTTP 监听地址"),
    port: int = typer.Option(8000, "--port", min=1, max=65535, help="HTTP 端口"),
    wigolo_url: str | None = typer.Option(
        None, "--wigolo-url", help="wigolo MCP 监听地址（默认 127.0.0.1:3333/mcp）"
    ),
) -> None:
    """启动单 worker API 与同进程采集调度器。"""
    interval_seconds = _parse_duration_option(
        collect_interval, option_name="--collect-interval", allow_zero=False
    )
    delay_seconds = _parse_duration_option(
        collect_delay, option_name="--collect-delay", allow_zero=True
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        create_app(
            cfg=build_config(wigolo_url=wigolo_url),
            collect_interval_seconds=interval_seconds,
            collect_delay_seconds=delay_seconds,
        ),
        host=host,
        port=port,
        workers=1,
    )


if __name__ == "__main__":
    typer.run(main)
