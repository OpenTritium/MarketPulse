"""LLM 层：pydantic-ai Agent + PromptedOutput 结构化输出。

关键约束（实测）：
  - opencode 中转的 deepseek-v4-flash 在 thinking 模式下拒绝 tool_choice
    → model_settings 必须关 reasoning（openai_reasoning_effort=none）
  - 中转不支持 response_format=json_schema（"unavailable now"）
    → 结构化输出只能用 PromptedOutput（纯 prompt + 客户端解析验证）
"""

from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.output import PromptedOutput
from pydantic_ai.providers.openai import OpenAIProvider
from .config import Config
from .factors import factor_descriptions

_SYSTEM_PROMPT = """你是专业市场新闻分析助手。你的任务是把新闻文本压缩为结构化情绪数据。

字段说明：
- headline: 一句话标题（≤30字，保留关键数字）
- summary: 中文摘要（≤120字，保留关键数字、主体、动作）
- sentiment: 对市场整体的情绪，-1.0（极度利空）~ 1.0（极度利多），0 为中性
- impact: 影响强度 0~1（越接近 1 越是重要新闻）
- relevant: 该新闻是否对 A 股/中国宏观市场情绪有潜在影响。**判定从严**：
  以下情况一律 false：文化/艺术/体育/娱乐/生活方式报道、人物特写/个人故事、
  地方民生服务信息、政策宣讲/活动宣传、无市场影响的普通公司事务。
  仅当新闻涉及政策、资金、宏观经济、行业/公司业绩与经营、监管、
  灾害事故（自然灾害/安全事故/公共卫生事件）、地缘政治、贸易政策时才是 true。
- factors: 因子 → 情绪分（-1~1）。只填新闻确实涉及的因子，无关的不填
- related_symbols: 相关标的（股票代码/指数名/商品名/公司名），无则空数组
"""

# 因子列表从 factors.py 派生，避免两处维护
_FACTOR_LIST = " / ".join(f"{k} {v}" for k, v in factor_descriptions().items())


class AnalysisResult(BaseModel):
    headline: str = ""
    summary: str = ""
    sentiment: float = Field(default=0.0)
    impact: float = Field(default=0.0)
    relevant: bool = True
    factors: dict[str, float] = Field(default_factory=dict)
    related_symbols: list[str] = Field(default_factory=list)


class NewsItem(BaseModel):
    title: str
    url: str
    published_at: str = ""
    content: str = ""


class _NewsItemList(BaseModel):
    items: list[NewsItem] = Field(default_factory=list)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class Analyzer:
    """LLM 分析器：摘要 + 情感 + 因子（pydantic-ai Agent，单次调用）。"""

    _SETTINGS: OpenAIChatModelSettings = OpenAIChatModelSettings(
        openai_reasoning_effort="none"
    )

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg: Config = cfg
        self._model: OpenAIChatModel = OpenAIChatModel(
            cfg.llm_model,
            provider=OpenAIProvider(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key),
        )

    def _agent(self, output_type: Any, instructions: str, retries: int) -> Agent:
        """构造单次调用 Agent（共享模型配置）。"""
        return Agent(
            self._model,
            output_type=PromptedOutput(output_type),
            model_settings=self._SETTINGS,
            retries=retries,
            instructions=instructions + "\n只输出 JSON，不要任何其他文字。",
        )

    async def analyze(self, title: str, text: str) -> AnalysisResult:
        """分析一篇新闻。"""
        user = f"""新闻标题：{title}

新闻正文：
{text[:6000]}

可用因子：{_FACTOR_LIST}"""
        agent = self._agent(
            AnalysisResult,
            _SYSTEM_PROMPT,
            retries=3,
        )
        result = await agent.run(user)
        out = (
            result.output
            if isinstance(result.output, AnalysisResult)
            else AnalysisResult()
        )
        known = set(factor_descriptions())
        return AnalysisResult(
            headline=out.headline[:80],
            summary=out.summary[:500],
            sentiment=_clamp(out.sentiment, -1.0, 1.0),
            impact=_clamp(out.impact, 0.0, 1.0),
            relevant=out.relevant,
            factors={
                k: _clamp(v, -1.0, 1.0) for k, v in out.factors.items() if k in known
            },
            related_symbols=[str(s) for s in out.related_symbols][:10],
        )

    async def extract_items(
        self, page_text: str, base_url: str, limit: int = 10
    ) -> list[NewsItem]:
        """从列表页 markdown 提取条目。失败返回空列表。"""
        agent = self._agent(
            _NewsItemList,
            f"""从页面内容中提取新闻/公告条目（JSON 对象，字段 items 为数组）：
1. 只提取页面顶部最近的 {limit} 条
2. title 为条目标题；url 必须是完整 URL（相对路径补全为 {base_url} 域名下）
3. published_at 页面给出则保留，否则空字符串
4. content：页面本身含正文则填（≤800字），只有链接则空字符串
5. 跳过导航、广告、页脚等非内容条目""",
            retries=2,
        )
        try:
            result = await agent.run(page_text[:20000])
            wrapper = result.output
            items = wrapper.items if isinstance(wrapper, _NewsItemList) else []
        except Exception:  # noqa: BLE001 - 提取失败不中断采集
            return []
        return [it for it in items if it.title and it.url]
