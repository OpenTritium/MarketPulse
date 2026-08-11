# 构建阶段：canary bun 编译单二进制（静态资源经 text import 内嵌）
FROM oven/bun:canary AS builder

WORKDIR /app

COPY package.json bun.lock ./
RUN bun install --frozen-lockfile

COPY server.ts tsconfig.json assets.d.ts ./
COPY public/ public/
RUN bun build --compile --minify --outfile /app/market-pulse-web server.ts

# 运行阶段：产物仅依赖 glibc（无 ICU/libstdc++），distroless base 即最小运行环境
FROM gcr.io/distroless/base-debian13

COPY --from=builder /app/market-pulse-web /usr/local/bin/market-pulse-web

ENV PORT=8443
EXPOSE 8443

CMD ["/usr/local/bin/market-pulse-web"]
