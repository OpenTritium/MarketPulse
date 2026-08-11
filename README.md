# Market Pulse · 市场情绪采集服务

通过 wigolo MCP 采集 A 股/中国宏观新闻，LLM 生成摘要与情绪分，本地向量模型入库 libSQL，提供 FastAPI 查询接口。API 与后台采集调度器同进程运行，一个容器全包。

## 部署

前置：wigolo MCP 服务运行在宿主机 `127.0.0.1:3333`；`.env` 需包含 `LLM_API_KEY`（密钥只从环境变量读）。

```bash
cp .env.example .env   # 填 LLM_API_KEY，按需填 WIGOLO_TOKEN、HTTP(S)_PROXY

docker build -t market-pulse .
# Linux 用 host network 直连宿主机 MCP；data/models 挂卷持久化
docker run -d --name market-pulse \
  --restart unless-stopped \
  --network host \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/models:/models" \
  market-pulse \
  --collect-interval 30m \
  --collect-delay 30s

curl 'http://127.0.0.1:8000/health'   # {"status":"ok"}
docker logs -f market-pulse
```

常用启动参数（时长后缀支持 `s` / `m` / `h`）：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--collect-interval` | `30m` | 采集轮次间隔，必须 > 0 |
| `--collect-delay` | `30s` | 启动后首轮延迟；`0s` 立即采集 |

> 固定单 worker：增加 worker 会启动重复的后台采集器。首轮启动会自动下载 embedding 模型（约 100MB，需要代理时配置 `HTTPS_PROXY`）。

## API

| 接口 | 说明 |
| --- | --- |
| `GET /health` | 健康检查 |
| `GET /timeline?limit=50&offset=0` | 事件时间线（倒序，带来源数组） |
| `GET /events/{event_id}` | 事件详情 + 多来源报道列表 |
| `GET /sentiment/overview` | 情绪总览：24h 均值、7 日均值与趋势差 |
| `GET /sentiment/timeseries?window=day&hours=72` | 情绪时序：均值/计数/正负占比，按小时或天分桶 |
| `GET /sentiment/factors?hours=72` | 因子分解：各因子平均情绪与覆盖率 |
| `GET /search?q=降息&k=10` | 语义检索：按事件摘要向量近邻 |

```bash
curl 'http://127.0.0.1:8000/timeline?limit=5'
curl 'http://127.0.0.1:8000/sentiment/overview'
curl 'http://127.0.0.1:8000/search?q=央行+降准'
```

## 常见问题

**如何采集？**
每轮采集：并发抓取各源列表页 → LLM 提取条目（每源最多 20 条最新）→ 抓取正文 → 数据库去重 → LLM 分析（相关判定 + 摘要 + 情绪 + 因子）→ 批量向量化 → 单事务入库。源清单集中在 `market_pulse/sources.py`（财联社、华尔街见闻、央行/证监会/统计局公告、新华社等，全部为财经/监管/宏观机构）。

**如何保证及时性？**
后台调度器每 30 分钟自动跑一轮（`--collect-interval` 可调）；抓取与 LLM 分析各 4 路并发；JS 渲染站点（财联社、华尔街见闻等）自动开启渲染。`--collect-delay 0s` 可让容器启动即采集。

**如何存储？**
libSQL（SQLite 兼容），默认本地单文件 `data/market.db`（WAL 模式，挂卷即持久化）。连接串直接透传给 libsql：可填本地 `file:` 路径，也可填远程 `libsql://` / `https://` URL（CLI 采集命令 `--db` 可传，token 需带在 URL 上如 `?authToken=...`；远程需服务端支持向量索引）。三张表：`events`（事件、情绪、因子聚合）、`reports`（原文与内容指纹）、`event_embeddings`（512 维向量 + 向量索引）。时间戳统一 RFC 3339 UTC。

**如何避免重复入库？**
两层去重：① 精确去重——`reports` 表 `UNIQUE(url, content_hash)`，指纹 = sha256(URL+标题+正文)，入库前先查库，重复报道直接跳过、不调 LLM；② 语义合并——48 小时窗口内向量相似度达标（cosine < 0.25）的报道视为同一事件合并，情绪与因子按报道数滚动平均。

**如何避免采集到无关财经的新闻？**
两道闸：源头——源清单只收录财经/监管/宏观机构；判定——LLM 按「从严」标准判断相关性（文化、体育、娱乐、生活方式、普通公司事务一律判为不相关），不相关的报道跳过入库和向量化，不污染查询结果。

**如何节省 token？**
去重前置（重复报道不调 LLM）；正文截断（列表页 30K/正文 20K 字符，分析 prompt 只取前 6000 字）；每源每轮最多 20 条；LLM 关闭 thinking（`reasoning_effort=none`），输出压缩为 ≤30 字标题 + ≤120 字摘要；不相关报道不 embedding 不入库。embedding 用本地 `BAAI/bge-small-zh-v1.5`，不走 API。
