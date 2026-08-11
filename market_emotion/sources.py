"""采集源配置：A股/中国宏观新闻与公告源（name → url）。

内容分类交给 LLM（提取时自行判断），此处只维护源清单。
注：中国经济网 (ce.cn) 已移除——nginx 层 403 反爬，stealth 模式也无法绕过。
"""

from __future__ import annotations

SOURCES: dict[str, str] = {
    # 快讯/资讯类
    "财联社电报": "https://www.cls.cn/telegraph",
    "华尔街见闻": "https://www.wallstreetcn.com/live",
    "东方财富快讯": "https://news.eastmoney.com/kx/",
    "第一财经": "https://www.yicai.com/",
    # 央行公告（货币政策类在货币政策司，法规类在条法司）
    "央行-公开市场业务公告": "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/125469/index.html",
    "央行-公开市场业务交易公告": "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/125475/index.html",
    "央行-买断式逆回购公告": "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/5492845/index.html",
    "央行-条法司规范性文件": "https://www.pbc.gov.cn/tiaofasi/144941/3581332/index.html",
    "央行-货币政策司利率政策": "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/125463/index.html",
    # 证监会公告（URL 模式 csrc.gov.cn/csrc/c{编号}/common_list.shtml）
    "证监会-信息公开": "http://www.csrc.gov.cn/csrc/c100033/common_list.shtml",
    "证监会-公告列表": "http://www.csrc.gov.cn/csrc/c101954/common_list.shtml",
    "证监会-行政处罚": "http://www.csrc.gov.cn/csrc/c101971/zfxxgk_zdgk.shtml",
    "证监会-风险警示": "http://www.csrc.gov.cn/csrc/c106299/common_list.shtml",
    "证监会-投资者保护": "http://www.csrc.gov.cn/csrc/c100210/common_list.shtml",
    # 统计局公告
    "统计局-最新发布与解读": "https://www.stats.gov.cn/sj/zxfb/",
    "统计局-通知公告": "https://www.stats.gov.cn/sj/tzgg/",
    # 社会/灾害（突发事件对市场情绪有直接冲击时才相关）
    # 注：中国地震台网、应急管理部已移除——金融中心不在地震高风险区，灾害应对类公告对市场情绪价值低
    "新华社": "https://www.news.cn/",
    "澎湃新闻": "https://www.thepaper.cn/",
}
