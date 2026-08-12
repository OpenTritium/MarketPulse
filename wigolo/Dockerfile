# wigolo on Bun — 预载 Chromium + 中文 embedding 模型
#
# 动机：node 版 wigolo 在 2G VPS 高负载下事件循环僵死；bun 版用 bun 运行时
# 替代 node（better-sqlite3 通过 bun:sqlite shim 兼容；sqlite-vec 向量扩展
# 降级禁用——采集只用 fetch，不需要 RAG 向量检索）。
#
# 构建机需要网络（拉镜像/npm 包/Chromium/模型）；运行时零下载。
# ---------- Build stage ----------
FROM oven/bun:canary AS build

WORKDIR /app

# 安装 wigolo（--ignore-scripts：better-sqlite3 的 node-gyp postinstall 在
# bun 下无意义——我们用 bun:sqlite shim 覆盖；playwright 浏览器下面手动装）
RUN bun add --ignore-scripts --no-progress wigolo@0.2.1

# better-sqlite3 → bun:sqlite shim（bun 暂不支持 better-sqlite3 N-API）
COPY shim/better-sqlite3/ node_modules/better-sqlite3/

# Chromium 浏览器（预载进镜像，运行时零下载）
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/wigolo-preload/browsers
RUN bunx playwright install chromium \
    && rm -rf /opt/wigolo-preload/browsers/ffmpeg-* 2>/dev/null || true

# 中文 embedding 模型：用 bun 下载（oven/bun 镜像无 curl），解压 stage
# （不经 fastembed 运行时下载；目录名按 wigolo 硬编码的
# fast-bge-small-en-v1.5 改名——原工程同款 trick）
RUN bun -e "await Bun.write('/tmp/model.tar.gz', await (await fetch('https://storage.googleapis.com/qdrant-fastembed/fast-bge-small-zh-v1.5.tar.gz')).arrayBuffer())" \
    && mkdir -p /opt/wigolo-seed/fastembed \
    && tar -xzf /tmp/model.tar.gz -C /tmp \
    && mv /tmp/fast-bge-small-zh-v1.5 /opt/wigolo-seed/fastembed/fast-bge-small-en-v1.5 \
    && rm /tmp/model.tar.gz \
    && ls /opt/wigolo-seed/fastembed/fast-bge-small-en-v1.5

# 清理体积：类型声明/文档/CUDA 原生库/非当前平台二进制（对照原 node 版工程）
RUN find /app/node_modules -type f \( -name "*.md" -o -name "*.ts" -o -name "*.map" \) -delete 2>/dev/null || true \
    && rm -rf \
        /app/node_modules/onnxruntime-node/bin/napi-v3/darwin \
        /app/node_modules/onnxruntime-node/bin/napi-v3/win32 \
        /app/node_modules/onnxruntime-node/bin/napi-v3/linux/arm64 \
        /app/node_modules/onnxruntime-web \
        /app/node_modules/onnxruntime-node \
        /app/node_modules/onnxruntime-node/bin/napi-v3/linux/x64/libonnxruntime_providers_cuda.so \
        /app/node_modules/onnxruntime-node/bin/napi-v3/linux/x64/libonnxruntime_providers_tensorrt.so \
        /app/node_modules/@napi-rs/canvas-linux-x64-musl \
        /app/node_modules/@napi-rs/canvas-linux-arm64-gnu \
        /app/node_modules/@napi-rs/canvas-linux-arm64-musl \
        /app/node_modules/@napi-rs/keyring-linux-x64-musl \
        /app/node_modules/@napi-rs/keyring-linux-arm64-gnu \
        /app/node_modules/@napi-rs/keyring-linux-arm64-musl \
        /app/node_modules/@img/sharp-linuxmusl-x64 \
        /app/node_modules/@img/sharp-linuxmusl-arm64 \
        /app/node_modules/@img/sharp-linux-arm64 \
        /app/node_modules/@img/sharp-libvips-linuxmusl-x64 \
        /app/node_modules/@img/sharp-libvips-linuxmusl-arm64 \
        /app/node_modules/@img/sharp-libvips-linux-arm64 \
    && find /app/node_modules/wreq-js/rust -type f ! -name 'wreq-js.linux-x64-gnu.node' -delete 2>/dev/null || true

# ---------- Runtime stage ----------
FROM oven/bun:canary

# Chromium 的 OS 依赖（bun 镜像基于 Debian，apt 直接装；无需 ldd 闭包）
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libasound2t64 libatk1.0-0t64 libatk-bridge2.0-0t64 \
        libatspi2.0-0t64 libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 \
        libpango-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 libharfbuzz0b libfontconfig1 \
        libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 \
        libxrandr2 libxtst6 libgl1 libegl1 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

ENV NODE_ENV=production \
    WIGOLO_RUNTIME_DIR=/runtime \
    WIGOLO_DATA_DIR=/runtime/data \
    HOME=/runtime/home \
    TMPDIR=/runtime/tmp \
    XDG_CACHE_HOME=/runtime/home/.cache \
    WIGOLO_SEARCH=core \
    WIGOLO_RERANKER=onnx \
    PLAYWRIGHT_BROWSERS_PATH=/opt/wigolo-preload/browsers

WORKDIR /app
COPY --from=build /app /app
COPY --from=build /opt/wigolo-preload /opt/wigolo-preload
COPY --from=build /opt/wigolo-seed /opt/wigolo-seed

# bun 版 entrypoint：重置 runtime 卷 + symlink 模型 + 启动 wigolo
# --smol：低内存模式；--production：生产模式运行
COPY entrypoint.js /wigolo-entry.js

LABEL org.opencontainers.image.title="wigolo-bun" \
      org.opencontainers.image.description="Wigolo 0.2.1 on Bun with Chromium and Chinese embedding model preloaded"

ENTRYPOINT ["bun", "--smol", "--production", "/wigolo-entry.js"]
CMD ["mcp"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["bun", "-e", "fetch('http://127.0.0.1:3333/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
