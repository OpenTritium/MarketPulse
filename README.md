# Market Emotion · 市场情绪采集服务

通过 wigolo MCP 采集 A 股/中国宏观新闻（财联社电报、央行/证监会/统计局公告等 17 个源），LLM 生成摘要+情感分+市场因子，本地 fastembed 向量化，存储到 Turso（libSQL 原生向量），提供情感聚合查询 API。

**设计原则：轻量**（无 agent 框架、无 ORM、依赖 6 个、~1300 行代码）。

## 架构

```text
docker compose (api + collector) ──▶ scheduler.py (30min 常驻) ──▶ collect.py
                            │ 1. wigolo MCP fetch 列表页（JSON-RPC over HTTP，~90 行自研客户端）
                            │ 2. LLM 提取条目 {title, url, published_at, content}
                            │ 3. 去重：url + content_hash（内容变化 → 新版本）
                            │ 4. LLM 结构化输出 {summary, sentiment, factors, related_symbols}
                            │ 5. fastembed (bge-small-zh-v1.5, 512 维) → 摘要向量
                            │ 6. libsql 写入 articles + article_embeddings
                            ▼
                    Turso 本地文件库 (file:data/market.db) ◀──── api.py (FastAPI :8000)
                        /health /sentiment/overview|timeseries|factors
                        /articles /articles/{url}/versions /search
```

## 快速开始

```bash
uv venv .venv --python 3.13
uv pip install -e ".[dev]"
cp .env.example .env   # 填入密钥（LLM_API_KEY / WIGOLO_TOKEN）
export https_proxy=http://127.0.0.1:7890  # opencode/HF 需要代理

# 采集（首次会下载 embedding 模型 ~90MB）
.venv/bin/python collect.py --sources 财联社电报
.venv/bin/python collect.py --db file:test.db --llm-model deepseek-v4-flash  # CLI 覆盖非敏感配置

# 查询 API
.venv/bin/uvicorn api:app --port 8000
curl 'localhost:8000/sentiment/overview'
curl 'localhost:8000/search?q=降准'
```

## 配置

**密钥只进环境变量**（无 CLI 入口）：`LLM_API_KEY` / `WIGOLO_TOKEN`（环境变量优先，`.env` 文件兜底）

**非敏感配置**：默认值内联在代码中，CLI 参数是唯一覆盖入口：`--db` / `--llm-base-url` / `--llm-model` / `--embedding-model`

```bash
.venv/bin/python collect.py --db file:test.db --llm-model deepseek-v4-flash
```

## 部署（Docker + uv）

```bash
cp .env.example .env   # 填入密钥

docker compose up -d --build
curl localhost:8000/health          # API 健康检查
curl localhost:8000/sentiment/overview  # 情绪总览
docker compose logs -f collector    # 采集调度日志
```

- **双容器**：`api`（uvicorn 常驻，HEALTHCHECK /health）+ `collector`（scheduler.py 常驻，每 30 分钟全量采集，`COLLECT_INTERVAL` 可调）
- **uv 构建**：uv.lock 锁依赖（`uv sync --frozen` 保证与开发环境一致）、`UV_COMPILE_BYTECODE=1` 预编译、`/root/.cache/uv` 构建缓存挂载、pyproject+lock 分层缓存
- **非 root**：entrypoint chown 数据卷后 gosu 降权到 appuser（uid 1000，对齐宿主机）
- **network_mode: host**：宿主机网络——wigolo MCP (127.0.0.1:3333) 与代理 (127.0.0.1:7890) 直通
- **数据卷**：`./data`（数据库，WAL 模式多进程安全）+ `./models`（embedding 模型缓存，首次运行下载 ~90MB）
- 密钥从 `.env` 注入（env_file），不进入镜像
- 手动采集：`docker compose exec collector python -m market_emotion.cli -s 财联社电报`

## 数据模型（事件时间线）

- `events`：**合并后的事件实体**（时间线条目）——uuid7 主键（时间有序，插入即按时间排）；同源/跨源报道同一事件时自动合并（向量相似度 + 48h 时间窗），`sentiment`/`factors` 滚动平均，`last_seen_at` 更新
- `reports`：报道明细——同一事件的多个来源，`source` 数组从这聚合（`UNIQUE (url, content_hash)` 去重）
- `event_embeddings`：事件摘要向量（F32_BLOB 512 维 + `libsql_vector_idx` + `vector_top_k`）——合并检索 + 语义搜索

## 情绪查询

按市场配置情绪因子（`market_emotion/factors.py`，加市场不改代码）：

| 市场 | 因子 |
|---|---|
| a-share | policy 政策 / liquidity 流动性 / macro 宏观 / regulation 监管 / industry 行业 |
| us | fed / macro / earnings / tech / geopolitics |
| crypto | etf / regulation / macro / adoption |

API 三层下钻：`/sentiment/overview`（总览+趋势）→ `/sentiment/timeseries`（时序）→ `/sentiment/factors`（因子分解）→ `/articles`（原始新闻）。

## 技术选型要点

- **Pydantic AI + fastmcp（半 harness）**：MCP 层用 fastmcp（官方生态），LLM 层用 pydantic-ai Agent + PromptedOutput（schema 验证 + 自动重试）；确定性流水线无需 agent 循环，编排层保持显式
- **中转端点约束**：opencode 中转 thinking 模式拒绝 tool_choice、不支持 response_format=json_schema，实测后确定 `openai_reasoning_effort=none` + PromptedOutput 方案
- **libsql-experimental**：唯一支持本地嵌入式向量搜索的官方 Python 客户端；官方新推 pyturso 实测不支持 `libsql_vector_idx`

## 已知限制

- 列表页提取依赖 LLM，每源每轮限 20 条（`sources.py` 可调）
- 快讯型源（财联社）的 url 每次刷新稳定，去重有效；公告型源列表页结构差异需按需调 prompt
- embedding 维度锁死 512：换模型需清空 `article_embeddings` 重建
