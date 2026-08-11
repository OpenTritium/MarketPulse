FROM ghcr.io/astral-sh/uv:0.12.3-python3.13-trixie-slim AS builder

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY market_pulse/ market_pulse/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
    && find /app/.venv -type d -name __pycache__ -prune -exec rm -rf {} + \
    && find /app/.venv/lib/python3.13/site-packages \
    -type d \( -name test -o -name tests \) -prune -exec rm -rf {} +

FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home appuser \
    && mkdir -p /app/data /models \
    && chown appuser:appuser /app/data /models

WORKDIR /app

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh", "python", "-m", "market_pulse.server"]

ENV PATH="/app/.venv/bin:$PATH" \
    FASTEMBED_CACHE_PATH=/models \
    HF_HUB_DISABLE_XET=1 \
    HOME=/models \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["--collect-interval", "30m", "--collect-delay", "30s"]
