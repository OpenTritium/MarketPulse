FROM python:3.13-slim

# uv 二进制直接拷入（无需运行时/守护进程）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# onnxruntime（fastembed 依赖）需要 libgomp1；gosu 用于 root→appuser 降权
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 分层缓存：pyproject+lock 不变则依赖层被 Docker 缓存命中
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_COMPILE_BYTECODE=1 uv sync --frozen --no-install-project --no-dev

# 再拷代码装项目自身
COPY market_emotion/ market_emotion/
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_COMPILE_BYTECODE=1 uv sync --frozen --no-dev

# 非 root 运行（uid 1000 对齐宿主机数据卷权限）
RUN useradd -u 1000 -m appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

# entrypoint：root 启动 → 修复数据卷所有权 → gosu 降权执行 CMD
# （bind mount 的目录由 docker 以 root 创建，容器启动时必须 chown）
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# fastembed 模型缓存落到 /models 卷（首次运行下载 ~90MB）
ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/models

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uvicorn", "market_emotion.api:app", "--host", "0.0.0.0", "--port", "8000"]
