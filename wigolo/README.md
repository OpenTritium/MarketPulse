# wigolo on Bun — 构建与部署说明

用 bun 运行时替代 node 运行 wigolo 0.2.1（解决 2G VPS 上 node 版事件循环
僵死问题）。better-sqlite3 用 bun:sqlite shim 兼容；sqlite-vec 向量扩展降级
禁用（采集只用 fetch，不需要 RAG 向量检索）。

## 构建

```bash
# canary（默认）
docker build -t wigolo-bun:latest .
# stable（canary 出现 N-API 回归时可切换）
docker build --build-arg BUN_TAG=1.3.14 -t wigolo-bun:latest .
```

构建机需要网络：拉 oven/bun 镜像、npm 安装 wigolo（376 包）、下载
Chromium（~150MB）与中文 embedding 模型（~55MB，GCS）。

## 部署（VPS，rootless podman + quadlet）

```bash
docker save wigolo-bun:latest | zstd -19 > wigolo-bun.tar.zstd
scp wigolo-bun.tar.zstd user@vps:/home/tritium/deploy/
# VPS 上：
podman load -i wigolo-bun.tar.zstd
# quadlet 的 wigolo.container 里 Image 改为 docker.io/library/wigolo-bun:latest
systemctl --user daemon-reload && systemctl --user restart wigolo
```

## shim 原理（shim/better-sqlite3/）

bun 暂不支持 better-sqlite3（N-API，issue #4290），用 bun:sqlite 实现兼容层：

- `@name` 命名参数 SQL → `$name`（bun 语法），绑定对象键加 `$` 前缀
- `pragma()` 返回数组（better-sqlite3 语义）
- `exec("")` 是 no-op（better-sqlite3 行为；bun 抛错）
- `loadExtension()` no-op（sqlite-vec 无法加载，wigolo 已容忍降级）
- `transaction()` 用 BEGIN/COMMIT/ROLLBACK 模拟

## 已知问题

- **bun canary 的 wreq-js（Rust N-API）网络层**：VPS 上 http fetch 可能
  挂起（本地 stable bun 正常）——如遇此问题，用 `--build-arg BUN_TAG=1.3.14`
  构建 stable 版验证。
- **2G 内存下 playwright 渲染**：chromium 渲染重页面可能 Page crash
  （renderer 内存不足），wigolo 快速失败不僵死，采集会跳过失败源。
- embedding/向量搜索降级（sqlite-vec 未加载），不影响 fetch 采集链路。
