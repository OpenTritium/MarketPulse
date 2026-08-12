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
from .quotes import QuoteClient

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
- related_symbols: 相关标的数组，每项为对象 {name, ts_code, type}：
  - name: 标的名（公司名/指数名/商品名）
  - ts_code: A 股代码（带交易所后缀 .SH/.SZ/.BJ，如 000001.SZ、600519.SH）；
    指数用指数代码（上证指数 000001.SH、深证成指 399001.SZ、创业板指 399006.SZ、
    沪深300 000300.SH）；商品/外汇等无 A 股代码时留空字符串
  - type: 枚举 stock（股票）/ index（指数）/ commodity（商品/其他）
  最多 5 个；无法确定代码的标的不编造 ts_code，留空
  - **产业/行业事件（产品发布、首航/开通航线、产能扩张、价格变动、政策利好某行业等）
    应识别产业链上的上市公司（核心供应商/受益公司/同业龙头），即使新闻正文未点名；
    如大飞机首航 → 中航西飞、洪都航空等机身/部段供应商；
    事件主体未上市时（如央企、合资公司），更要找产业链公司而非留空

**打分依据约束（必须遵守）**：
1. sentiment 必须依据新闻中的**事实方向**打分，禁止凭印象或平均主义：
   - 明确利好（政策支持/资金流入/业绩超预期/监管放宽）→ 正分，幅度与利好强度匹配
   - 明确利空（处罚/下滑/风险事件/监管收紧）→ 负分，幅度与利空强度匹配
   - 中性/数据型快讯（无方向性）→ 0 附近（|sentiment| ≤ 0.15）
   - 不要用 0.5/-0.5 这种习惯性默认值；分数应能区分强弱（0.3 与 0.8 代表不同量级的利好）
2. impact 按**影响广度与持续性**分档：
   - 0.8~1.0：宏观/系统性（央行政策、重大监管、地缘冲突、系统性风险）
   - 0.5~0.8：行业级（行业政策、龙头业绩、重要数据）
   - 0.2~0.5：个股/局部（单公司事件、小范围波动）
   - 0~0.2：噪音/快讯（无实质影响）
3. 同一新闻的 sentiment 与 factors 方向必须一致（如 sentiment 为负则涉及的因子分也为负）。
"""

# 因子列表从 factors.py 派生，避免两处维护
_FACTOR_LIST = " / ".join(f"{k} {v}" for k, v in factor_descriptions().items())


class RelatedSymbol(BaseModel):
    """相关标的：名称 + A 股 ts_code（如 000001.SZ）+ 类型。"""

    name: str = ""
    ts_code: str = ""  # 股票/指数代码（交易所后缀 .SH/.SZ/.BJ）；商品等无代码留空
    type: str = "stock"  # stock | index | commodity


class AnalysisResult(BaseModel):
    headline: str = ""
    summary: str = ""
    sentiment: float = Field(default=0.0)
    impact: float = Field(default=0.0)
    relevant: bool = True
    factors: dict[str, float] = Field(default_factory=dict)
    related_symbols: list[RelatedSymbol] = Field(default_factory=list)


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
        self._quotes: QuoteClient | None = None

    def _quote_client(self) -> QuoteClient:
        """惰性创建行情客户端（仅 analyze 的工具路径使用）。"""
        if self._quotes is None:
            self._quotes = QuoteClient(self.cfg)
        return self._quotes

    def _agent(self, output_type: Any, instructions: str, retries: int) -> Agent:
        """构造单次调用 Agent（共享模型配置），注册标的代码查询工具。"""

        async def lookup_symbol(name: str) -> str:
            """按名称查询真实存在的 A 股 ts_code（带交易所后缀，如 000001.SZ）；查不到返回空字符串。"""
            return await self._quote_client().lookup(name)

        return Agent(
            self._model,
            output_type=PromptedOutput(output_type),
            model_settings=self._SETTINGS,
            retries=retries,
            instructions=instructions + "\n只输出 JSON，不要任何其他文字。",
            tools=[lookup_symbol],
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
            related_symbols=out.related_symbols[:10],
        )

    async def extract_items(
        self, page_text: str, base_url: str, limit: int = 10
    ) -> list[NewsItem]:
        """从列表页 markdown 提取条目；失败由采集层按源记录。"""
        agent = self._agent(
            _NewsItemList,
            f"""从页面内容中提取新闻/公告条目（JSON 对象，字段 items 为数组）：
1. 只提取页面顶部最近的 {limit} 条
2. title 为条目标题；url 必须是完整 URL（相对路径补全为 {base_url} 域名下）
3. published_at 仅在页面给出可明确解析的带时区 ISO 8601 时间时填写；统一输出 RFC 3339 UTC（`Z`）格式，否则空字符串
4. content：页面本身含正文则填（≤800字），只有链接则空字符串
5. 跳过导航、广告、页脚等非内容条目""",
            retries=2,
        )
        result = await agent.run(page_text[:20000])
        wrapper = result.output
        items = wrapper.items if isinstance(wrapper, _NewsItemList) else []
        return [item for item in items if item.title and item.url]
