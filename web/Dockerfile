# 构建阶段：canary bun 编译单二进制（静态资源经 text import 内嵌）
FROM oven/bun:canary AS builder

WORKDIR /app

COPY package.json bun.lock ./
RUN bun install --frozen-lockfile

COPY server.ts tsconfig.json assets.d.ts ./
COPY public/ public/
RUN bun build --compile --minify --outfile /app/market-pulse-web server.ts

# 运行阶段：编译产物自包含，只需 glibc 基础镜像
FROM debian:bookworm-slim

COPY --from=builder /app/market-pulse-web /usr/local/bin/market-pulse-web

ENV PORT=8443
EXPOSE 8443

CMD ["market-pulse-web"]
