"""采集源配置：A 股/中国宏观新闻与公告源（name → url）。

内容分类交给 LLM（提取时自行判断），此处只维护源清单。
注：
- 中国经济网 (ce.cn) 已移除——nginx 层 403 反爬，stealth 模式也无法绕过。
- 证监会 (csrc.gov.cn)、统计局 (stats.gov.cn)、央行 (pbc.gov.cn) 已移除——
  实测 wigolo 抓取这些域名的页面后进程僵死，待 wigolo 修复后再考虑加回。
- 东方财富快讯已移除——news.eastmoney.com/kx/ 页面已失效（404）。
- 新华社、澎湃新闻已移除——实测长期 0 产出（列表提取/相关性过滤为空）。
"""

from __future__ import annotations

SOURCES: dict[str, str] = {
    # 快讯/资讯类
    "财联社电报": "https://www.cls.cn/telegraph",
    "华尔街见闻": "https://www.wallstreetcn.com/live",
    "第一财经": "https://www.yicai.com/",
    "新浪财经7x24": "https://finance.sina.com.cn/7x24/",
    "金十数据": "https://www.jin10.com/",
    "证券时报": "https://www.stcn.com/",
    "智通财经": "https://www.zhitongcaijing.com/",
    # 国际视角（VPS 大陆网络可达）
    "汇通网": "https://www.fx678.com/",
    "新浪美股": "https://finance.sina.com.cn/stock/usstock/",
}
