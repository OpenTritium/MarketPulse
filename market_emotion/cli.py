#!/usr/bin/env python3
"""采集入口（systemd timer 调用）：python collect.py [--sources a,b,c]

非敏感配置的默认值内联在此（CLI 参数是唯一覆盖入口）；
密钥（LLM_API_KEY / WIGOLO_TOKEN）只从环境变量读取，无 CLI 入口。

输出 JSON 统计到 stdout（journald 自动收集）。
"""

from __future__ import annotations
import asyncio
import json
import logging
import typer
from market_emotion.config import build_config
from market_emotion.pipeline import run_collect
from market_emotion.sources import SOURCES

app = typer.Typer(help="市场情绪采集：wigolo 抓取 → LLM 分析 → Turso 入库")


def _filter_sources(sources_arg: str) -> list[dict[str, str]]:
    names = {s.strip() for s in sources_arg.split(",") if s.strip()}
    missing = names - set(SOURCES)
    if missing:
        raise typer.BadParameter(f"未知源: {sorted(missing)}")
    return [{"name": n, "url": SOURCES[n]} for n in names]


@app.command()
def collect(
    sources: str | None = typer.Option(
        None, "--sources", "-s", help="逗号分隔的源名子集（默认全部）"
    ),
    db: str = typer.Option("file:data/market.db", "--db", help="本地库路径"),
    llm_base_url: str = typer.Option(
        "https://opencode.ai/zen/go/v1", "--llm-base-url", help="OpenAI 兼容端点"
    ),
    llm_model: str = typer.Option(
        "deepseek-v4-flash", "--llm-model", help="LLM 模型名"
    ),
    embedding_model: str = typer.Option(
        "BAAI/bge-small-zh-v1.5", "--embedding-model", help="embedding 模型"
    ),
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = build_config(
        turso_url=db,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        embedding_model=embedding_model,
    )
    selected = _filter_sources(sources) if sources else None
    stats = asyncio.run(run_collect(cfg, selected))
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    app()
