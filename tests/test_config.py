"""配置构建测试：非敏感字段走 CLI 覆盖，密钥走环境变量。"""

from __future__ import annotations

from market_pulse.config import build_config


def test_wigolo_url_default() -> None:
    assert build_config().wigolo_url == "http://127.0.0.1:3333/mcp"


def test_wigolo_url_cli_override() -> None:
    cfg = build_config(wigolo_url="http://10.0.0.5:4444/mcp")
    assert cfg.wigolo_url == "http://10.0.0.5:4444/mcp"
