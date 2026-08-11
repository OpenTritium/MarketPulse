"""采集入口：python -m market_pulse.cli [--sources a,b,c]。

非敏感配置通过 CLI 覆盖；密钥（`LLM_API_KEY` / `WIGOLO_TOKEN`）
只从环境变量或 `.env` 读取。embedding 模型固定为项目数据契约的一部分。
"""

from __future__ import annotations

import asyncio
import json
import logging

import typer

from market_pulse.config import build_config
from market_pulse.pipeline import run_collect
from market_pulse.sources import SOURCES

app = typer.Typer(help="市场情绪采集：wigolo 抓取 → LLM 分析 → Turso 入库")


def _filter_sources(sources_arg: str) -> list[dict[str, str]]:
    names = list(
        dict.fromkeys(name.strip() for name in sources_arg.split(",") if name.strip())
    )
    missing = set(names) - set(SOURCES)
    if missing:
        raise typer.BadParameter(f"未知源: {sorted(missing)}")
    return [{"name": name, "url": SOURCES[name]} for name in names]


@app.command()
def collect(
    sources: str | None = typer.Option(
        None, "--sources", "-s", help="逗号分隔的源名子集（默认全部）"
    ),
    db: str = typer.Option(
        "file:data/market.db",
        "--db",
        help="本地 file: 路径或远程 libsql:// URL（token 带在 URL 上）",
    ),
    llm_base_url: str = typer.Option(
        "https://opencode.ai/zen/go/v1", "--llm-base-url", help="OpenAI 兼容端点"
    ),
    llm_model: str = typer.Option(
        "deepseek-v4-flash", "--llm-model", help="LLM 模型名"
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
    )
    selected = _filter_sources(sources) if sources else None
    stats = asyncio.run(run_collect(cfg, selected))
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    app()
