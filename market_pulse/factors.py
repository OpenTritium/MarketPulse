"""市场情绪因子（单市场：A股/中国宏观）。

采集时 LLM 按此列表给新闻打因子分（-1~1），/sentiment/factors 聚合展示。
"""

from __future__ import annotations

_FACTORS: dict[str, str] = {
    "policy": "政策面（央行/证监会/发改委动作）",
    "liquidity": "流动性（逆回购/资金面/利率）",
    "macro": "宏观数据（GDP/CPI/PMI/社融）",
    "regulation": "监管（处罚/问询/新规）",
    "industry": "行业动态（板块/产业链）",
    "disaster": "自然灾害（地震/洪水/台风/干旱/森林火灾）",
    "public": "公共安全事件（食品/药品安全、航空/生产事故）",
    "geopolitics": "地缘政治（战争/武装冲突/军事行动/地缘紧张）",
    "trade": "贸易政策（出口禁令/出口管制/制裁/实体清单/关税）",
}


def factor_descriptions() -> dict[str, str]:
    """因子名 → 中文描述。"""
    return dict(_FACTORS)
