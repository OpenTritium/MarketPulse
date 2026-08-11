"""配置：非敏感配置走参数（CLI 传入或代码默认值），密钥只从环境变量读。

数据流：
  - collect.py：CLI 参数（唯一覆盖入口）→ build_config()
  - api.py：build_config() 全默认
  - 密钥：真实环境变量优先，.env 文件兜底（systemd 无 shell 环境场景）
"""

from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import dotenv_values

_DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"


@dataclass(frozen=True)
class Config:
    turso_url: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    wigolo_url: str
    wigolo_token: str
    embedding_model: str
    embedding_dim: int


def build_config(
    *,
    turso_url: str = "file:data/market.db",
    llm_base_url: str = "https://opencode.ai/zen/go/v1",
    llm_model: str = "deepseek-v4-flash",
    wigolo_url: str = "http://127.0.0.1:3333/mcp",
    embedding_model: str = "BAAI/bge-small-zh-v1.5",
    embedding_dim: int = 512,
) -> Config:
    """构造配置：非敏感字段用参数（默认值），密钥从环境变量/.env 读取。"""
    return Config(
        turso_url=turso_url,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        wigolo_url=wigolo_url,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        llm_api_key=_secret("LLM_API_KEY"),
        wigolo_token=_secret("WIGOLO_TOKEN"),
    )


def _secret(name: str) -> str:
    """读取密钥：真实环境变量优先，.env 兜底（如 systemd 场景）。"""
    value = os.environ.get(name)
    if value:
        return value.strip()
    return str((dotenv_values(_DOTENV_PATH) or {}).get(name) or "").strip()
