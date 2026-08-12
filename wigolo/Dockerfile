# syntax=docker/dockerfile:1

# Build stage: Node 24 (Debian 13 trixie). Install wigolo from npm, install
# Chromium's OS libraries, then warm up the browser suite and the embedding
# model so the final distroless image needs no lazy downloads at runtime.
FROM node:26-trixie-slim AS build

USER root

ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    WIGOLO_DATA_DIR=/data \
    PLAYWRIGHT_BROWSERS_PATH=/opt/wigolo-preload/browsers \
    WIGOLO_SEARCH=core \
    WIGOLO_RERANKER=none

WORKDIR /app

# Native packages normally use prebuilt binaries. Keep a source-build fallback
# for networks where their GitHub release downloads time out. All build-stage
# Debian packages use the Tsinghua TUNA mirror.
RUN sed -i \
    -e 's|http://deb.debian.org/debian-security|http://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
    -e 's|http://deb.debian.org/debian|http://mirrors.tuna.tsinghua.edu.cn/debian|g' \
    /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

# Install the current pnpm release from npmmirror, then use the same mirror for
# dependency resolution and better-sqlite3's prebuilt binary.
RUN npm install -g --no-audit --no-fund \
    --registry=https://registry.npmmirror.com pnpm@11.21.0
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    PNPM_CONFIG_REGISTRY=https://registry.npmmirror.com \
    PNPM_CONFIG_NODE_LINKER=hoisted \
    PNPM_CONFIG_DANGEROUSLY_ALLOW_ALL_BUILDS=true \
    npm_config_better_sqlite3_binary_host_mirror=https://npmmirror.com/mirrors/better-sqlite3 \
    ONNXRUNTIME_NODE_INSTALL_CUDA=skip \
    pnpm add --prod --save-exact wigolo@0.2.1

# Fetch the Chinese embedding model (bge-small-zh-v1.5, 512 dims) and stage it
# under the model directory wigolo 0.2.1 hardcodes (fast-bge-small-en-v1.5).
# fastembed serves whatever files it finds in that directory without checking
# the model identity, so the hardcoded English model selection transparently
# loads the Chinese model. Our content is mixed Chinese/English; the English
# BGE model has no Chinese vocabulary coverage.
RUN mkdir -p /data \
    && node --input-type=module -e "import { FlagEmbedding, EmbeddingModel } from 'fastembed'; await FlagEmbedding.init({ model: EmbeddingModel.BGESmallZH, cacheDir: '/data/fastembed', showDownloadProgress: false });" \
    && mv /data/fastembed/fast-bge-small-zh-v1.5 /data/fastembed/fast-bge-small-en-v1.5 \
    && ls /data/fastembed/fast-bge-small-en-v1.5

# The vector index schema and the warmup shape check hardcode 384 dims, but
# the Chinese model emits 512. Provider model selection stays untouched; the
# staged directory rename above already makes it load the Chinese model.
RUN sed -i 's/float\[384\]/float[512]/' /app/node_modules/wigolo/dist/cache/migrations/runner.js \
    && sed -i 's/this\.dim = 384/this.dim = 512/' /app/node_modules/wigolo/dist/embedding/fastembed-provider.js

# Chromium's OS libraries (required before the warmup smoke test can launch),
# plus binutils for stripping debug symbols out of the staged runtime files.
RUN apt-get update \
    && /app/node_modules/.bin/playwright install-deps chromium \
    && apt-get install -y --no-install-recommends binutils \
    && rm -rf /var/lib/apt/lists/*

# Install Chromium and its headless shell before warmup. Call runWarmup directly
# so browser, reranker, or embedding failures stop the build instead of being
# converted to a successful CLI exit code.
RUN mkdir -p /data /opt/wigolo-preload \
    && /app/node_modules/.bin/playwright install chromium \
    && node --input-type=module -e "import { runWarmup } from '/app/node_modules/wigolo/dist/cli/warmup.js'; const result = await runWarmup(['--embeddings', '--reranker', '--no-searxng', '--plain']); const failures = ['playwright', 'reranker', 'embeddings'].filter((name) => result[name] !== 'ok'); if (failures.length) throw new Error('warmup failed for: ' + failures.join(', ') + ' (' + JSON.stringify(result) + ')');" \
    && node -e "const { chromium } = require('playwright'); chromium.launch({ headless: true }).then((browser) => browser.close()).catch((error) => { console.error(error); process.exitCode = 1; })"

# Drop npm noise (docs, types, sourcemaps) and Playwright's FFmpeg: wigolo
# never records video, so these files only inflate the image.
RUN find /app/node_modules -type f \( -name "*.md" -o -name "*.ts" -o -name "*.map" \) -delete \
    && rm -rf /opt/wigolo-preload/browsers/ffmpeg-*

# Keep only the target platform's native binaries. CPU-only inference means the
# CUDA/TensorRT providers (hundreds of MB) are dropped; macOS, Windows, ARM,
# and musl artifacts are dead weight in a Linux image.
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
    amd64) \
    rm -rf \
    /app/node_modules/onnxruntime-node/bin/napi-v3/darwin \
    /app/node_modules/onnxruntime-node/bin/napi-v3/win32 \
    /app/node_modules/onnxruntime-node/bin/napi-v3/linux/arm64 \
    /app/node_modules/onnxruntime-node/bin/napi-v3/linux/x64/libonnxruntime_providers_cuda.so \
    /app/node_modules/onnxruntime-node/bin/napi-v3/linux/x64/libonnxruntime_providers_tensorrt.so \
    /app/node_modules/@napi-rs/canvas-linux-x64-musl \
    /app/node_modules/@napi-rs/canvas-linux-arm64-gnu \
    /app/node_modules/@napi-rs/canvas-linux-arm64-musl \
    /app/node_modules/@napi-rs/keyring-linux-x64-musl \
    /app/node_modules/@napi-rs/keyring-linux-arm64-gnu \
    /app/node_modules/@napi-rs/keyring-linux-arm64-musl \
    /app/node_modules/@img/sharp-linuxmusl-x64 \
    /app/node_modules/@img/sharp-libvips-linuxmusl-x64; \
    find /app/node_modules/wreq-js/rust -type f ! -name 'wreq-js.linux-x64-gnu.node' -delete \
    ;; \
    arm64) \
    rm -rf \
    /app/node_modules/onnxruntime-node/bin/napi-v3/darwin \
    /app/node_modules/onnxruntime-node/bin/napi-v3/win32 \
    /app/node_modules/onnxruntime-node/bin/napi-v3/linux/x64 \
    /app/node_modules/onnxruntime-node/bin/napi-v3/linux/arm64/libonnxruntime_providers_cuda.so \
    /app/node_modules/onnxruntime-node/bin/napi-v3/linux/arm64/libonnxruntime_providers_tensorrt.so \
    /app/node_modules/@napi-rs/canvas-linux-arm64-musl \
    /app/node_modules/@napi-rs/canvas-linux-x64-gnu \
    /app/node_modules/@napi-rs/canvas-linux-x64-musl \
    /app/node_modules/@napi-rs/keyring-linux-arm64-musl \
    /app/node_modules/@napi-rs/keyring-linux-x64-gnu \
    /app/node_modules/@napi-rs/keyring-linux-x64-musl \
    /app/node_modules/@img/sharp-linuxmusl-arm64 \
    /app/node_modules/@img/sharp-linux-arm64 \
    /app/node_modules/@img/sharp-libvips-linuxmusl-arm64 \
    /app/node_modules/@img/sharp-libvips-linux-arm64; \
    find /app/node_modules/wreq-js/rust -type f ! -name 'wreq-js.linux-arm64-gnu.node' -delete \
    ;; \
    *) \
    echo "unsupported target architecture: ${TARGETARCH:-amd64}" >&2; \
    exit 1 \
    ;; \
    esac

# Stage Chromium's runtime libraries (ldd closure plus the dlopen'd NSS/GL/font
# set) so the distroless final stage can copy them without a package manager.
RUN set -eux; \
    LIBDIR="$(case "${TARGETARCH:-amd64}" in amd64) echo x86_64-linux-gnu ;; arm64) echo aarch64-linux-gnu ;; esac)"; \
    CHROMIUM_BIN=/opt/wigolo-preload/browsers/chromium-1223/chrome-linux64/chrome; \
    HEADLESS_SHELL_BIN=/opt/wigolo-preload/browsers/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell; \
    test -x "$CHROMIUM_BIN"; \
    test -x "$HEADLESS_SHELL_BIN"; \
    mkdir -p /opt/runtime-deps; \
    ldd "$CHROMIUM_BIN" > /tmp/chromium.ldd; \
    ldd "$HEADLESS_SHELL_BIN" > /tmp/chromium-headless-shell.ldd; \
    awk '/=> \//{print $3} /^\/lib\/ld-linux/{print $1}' /tmp/chromium.ldd /tmp/chromium-headless-shell.ldd | sort -u \
    | while read -r f; do [ -e "$f" ] && cp -aL --parents "$f" /opt/runtime-deps/; done; \
    rm -f /tmp/chromium.ldd /tmp/chromium-headless-shell.ldd; \
    if [ -d /opt/runtime-deps/lib ]; then mkdir -p /opt/runtime-deps/usr/lib && cp -a /opt/runtime-deps/lib/. /opt/runtime-deps/usr/lib/ && rm -rf /opt/runtime-deps/lib; fi; \
    find "/usr/lib/$LIBDIR" "/lib/$LIBDIR" -maxdepth 1 \( -type f -o -type l \) \( \
    -name 'libnss3*' -o -name 'libnspr4*' -o -name 'libnssutil3*' -o -name 'libsmime3*' \
    -o -name 'libplds4*' -o -name 'libplc4*' -o -name 'libasound*' -o -name 'libcups*' \
    -o -name 'libatk*' -o -name 'libatspi*' -o -name 'libdbus*' -o -name 'libgbm*' \
    -o -name 'libdrm*' -o -name 'libglib*' -o -name 'libgmodule*' -o -name 'libgobject*' \
    -o -name 'libpango*' -o -name 'libcairo*' -o -name 'libgdk_pixbuf*' -o -name 'libharfbuzz*' \
    -o -name 'libfontconfig*' -o -name 'libfreetype*' -o -name 'libexpat*' -o -name 'libx11*' \
    -o -name 'libxcb*' -o -name 'libxkbcommon*' -o -name 'libwayland*' -o -name 'libX*' \
    -o -name 'libGL*' -o -name 'libEGL*' -o -name 'libglapi*' -o -name 'libvulkan*' \
    -o -name 'libgles*' -o -name 'libssl*' -o -name 'libcrypto*' -o -name 'libz3*' \
    -o -name 'libedit*' -o -name 'libxml2*' -o -name 'libavahi*' \
    -o -name 'libpng*' -o -name 'libjpeg*' -o -name 'libwebp*' \
    -o -name 'libbz2*' -o -name 'liblzma*' -o -name 'libzstd*' -o -name 'libunwind*' \
    -o -name 'libbrotli*' -o -name 'libffi*' -o -name 'libpcre2*' -o -name 'libgraphite2*' \
    -o -name 'libfribidi*' -o -name 'libthai*' -o -name 'libdatrie*' -o -name 'libpixman*' \
    -o -name 'libxshmfence*' -o -name 'libxfixes*' -o -name 'libxext*' -o -name 'libxrender*' \
    -o -name 'libxrandr*' -o -name 'libxcomposite*' -o -name 'libxdamage*' -o -name 'libxi*' \
    -o -name 'libxtst*' -o -name 'libxcursor*' -o -name 'libxau*' -o -name 'libxdmcp*' \
    -o -name 'libxt*' -o -name 'libsm*' -o -name 'libice*' -o -name 'libz.so*' \
    -o -name 'libudev*' -o -name 'libexif*' -o -name 'liblcms*' -o -name 'libopenjp2*' \
    -o -name 'libjbig*' -o -name 'libtiff*' -o -name 'libgif*' -o -name 'libnss*' \
    -o -name 'libfido2*' -o -name 'libspeex*' -o -name 'libopus*' -o -name 'libsndfile*' \
    -o -name 'libsoftokn*' -o -name 'libfreebl*' -o -name 'libnssckbi*' -o -name 'libnsssysinit*' -o -name 'libssl3*' \
    -o -name 'libsqlite3*' \
    \) -exec cp -aL --parents {} /opt/runtime-deps/ \;; \
    if [ -d "/usr/lib/$LIBDIR/nss" ]; then mkdir -p "/opt/runtime-deps/usr/lib/$LIBDIR" && cp -aL "/usr/lib/$LIBDIR/nss/." "/opt/runtime-deps/usr/lib/$LIBDIR/nss/"; fi; \
    mkdir -p /opt/runtime-deps/usr/share /opt/runtime-deps/etc; \
    cp -a /usr/share/fonts/. /opt/runtime-deps/usr/share/fonts/; \
    cp -a /etc/fonts/. /opt/runtime-deps/etc/fonts/; \
    find /opt/runtime-deps -type f \( -name "*.so" -o -name "*.so.*" \) -exec strip --strip-unneeded {} + 2>/dev/null || true; \
    strip --strip-unneeded /opt/wigolo-preload/browsers/chromium-1223/chrome-linux64/chrome 2>/dev/null || true; \
    strip --strip-unneeded /opt/wigolo-preload/browsers/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell 2>/dev/null || true

# Runtime stage: minimal distroless Node 24 image - no shell or package
# manager. Compose mounts a disk-backed volume at /runtime and makes the root
# filesystem read-only. The entrypoint wipes that volume on every start.
FROM gcr.io/distroless/nodejs26-debian13:nonroot AS runtime

ENV NODE_ENV=production \
    NODE_OPTIONS="--max-old-space-size=128" \
    GLIBC_TUNABLES="glibc.malloc.arena_max=2:glibc.malloc.trim_threshold=65536:glibc.malloc.mmap_threshold=65536" \
    WIGOLO_RUNTIME_DIR=/runtime \
    WIGOLO_DATA_DIR=/runtime/data \
    HOME=/runtime/home \
    TMPDIR=/runtime/tmp \
    XDG_CACHE_HOME=/runtime/home/.cache \
    WIGOLO_SEARCH=core \
    WIGOLO_RERANKER=onnx \
    PLAYWRIGHT_BROWSERS_PATH=/opt/wigolo-preload/browsers

# Docker copies /runtime ownership to a newly created named volume, allowing
# the nonroot entrypoint to reset it without running as root. Precreating the
# secret mountpoint prevents Docker from adding it to the writable layer.
USER root
RUN ["/nodejs/bin/node", "-e", "const fs=require('node:fs');fs.mkdirSync('/runtime',{recursive:true});fs.chownSync('/runtime',65532,65532);fs.chmodSync('/runtime',0o700);fs.mkdirSync('/run/secrets',{recursive:true});fs.chmodSync('/run/secrets',0o755)"]

WORKDIR /app
COPY --from=build --chown=nonroot:nonroot /app /app
COPY --from=build --chown=nonroot:nonroot /opt/wigolo-preload /opt/wigolo-preload
# distroless uses usrmerge, so runtime libraries live under /usr; /lib and /etc
# are symlinks that must not be copied over. Fonts go to the standard location.
COPY --from=build --chown=nonroot:nonroot /opt/runtime-deps/usr/ /usr/
COPY --from=build --chown=nonroot:nonroot /opt/runtime-deps/etc/fonts /etc/fonts

# Immutable preloaded models stay in the image layer. The entrypoint links
# them into WIGOLO_DATA_DIR, so restarts neither download nor copy ~187MB.
COPY --from=build --chown=0:0 /data /opt/wigolo-seed

COPY --chown=0:0 entrypoint.js /wigolo-entry.js

LABEL org.opencontainers.image.title="wigolo-preloaded" \
    org.opencontainers.image.description="Wigolo on Node 26 distroless with Chromium and the embedding model preloaded"

USER nonroot
ENTRYPOINT ["/nodejs/bin/node", "--optimize-for-size", "/wigolo-entry.js"]
CMD ["mcp"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["/nodejs/bin/node", "-e", "fetch('http://127.0.0.1:3333/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
