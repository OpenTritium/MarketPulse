"""采集源配置：A股/中国宏观新闻与公告源（name → url）。

内容分类交给 LLM（提取时自行判断），此处只维护源清单。
注：
- 中国经济网 (ce.cn) 已移除——nginx 层 403 反爬，stealth 模式也无法绕过。
- 证监会 (csrc.gov.cn)、统计局 (stats.gov.cn)、央行 (pbc.gov.cn) 已移除——
  实测 wigolo 抓取这些域名的页面（列表页或文章页）后进程僵死（返回后崩溃），
  待 wigolo 修复后再考虑加回。
"""

from __future__ import annotations

SOURCES: dict[str, str] = {
    # 快讯/资讯类
    "财联社电报": "https://www.cls.cn/telegraph",
    "华尔街见闻": "https://www.wallstreetcn.com/live",
    "东方财富快讯": "https://news.eastmoney.com/kx/",
    "第一财经": "https://www.yicai.com/",
    # 社会/灾害（突发事件对市场情绪有直接冲击时才相关）
    # 注：中国地震台网、应急管理部已移除——金融中心不在地震高风险区，灾害应对类公告对市场情绪价值低
    "新华社": "https://www.news.cn/",
    "澎湃新闻": "https://www.thepaper.cn/",
}
