/**
 * Market Pulse Web：静态前端 + /api 反代，单二进制部署。
 *
 * 静态资源通过 text import 在 bun build --compile 时内嵌进可执行文件，
 * 产物只需一个文件；/api/* 转发到后端 FastAPI。
 *
 * 环境变量：
 *   BACKEND_URL  后端地址（默认 http://127.0.0.1:8000）
 *   PORT         监听端口（默认 8443）
 */
import indexHtml from "./public/index.html" with { type: "text" };
import appJs from "./public/app.js" with { type: "text" };
import styleCss from "./public/style.css" with { type: "text" };

const BACKEND_URL = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";
const PORT = Number(process.env.PORT ?? 8443);

const ASSETS: Record<string, { body: string; contentType: string }> = {
  "/": { body: String(indexHtml), contentType: "text/html; charset=utf-8" },
  "/index.html": { body: String(indexHtml), contentType: "text/html; charset=utf-8" },
  "/app.js": { body: appJs, contentType: "text/javascript; charset=utf-8" },
  "/style.css": { body: styleCss, contentType: "text/css; charset=utf-8" },
};

const IDEMPOTENT_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

async function proxyApi(request: Request, url: URL): Promise<Response> {
  // 剥掉 /api 前缀再转发到后端
  const upstream = new URL(
    url.pathname.replace(/^\/api/, "") + url.search,
    BACKEND_URL,
  );
  const headers = new Headers(request.headers);
  headers.delete("host");
  const response = await fetch(upstream, {
    method: request.method,
    headers,
    body: IDEMPOTENT_METHODS.has(request.method) ? undefined : request.body,
  });
  return new Response(response.body, {
    status: response.status,
    headers: response.headers,
  });
}

const server = Bun.serve({
  port: PORT,
  async fetch(request) {
    let url: URL;
    try {
      url = new URL(request.url);
    } catch {
      return new Response("Bad Request", { status: 400 });
    }
    if (url.pathname.startsWith("/api/")) {
      try {
        return await proxyApi(request, url);
      } catch (error) {
        console.error(`[proxy] 后端不可达 ${BACKEND_URL}:`, error);
        return Response.json(
          {
            error: {
              type: "api_error",
              message: `后端不可达：${BACKEND_URL}`,
              param: null,
              request_id: null,
            },
          },
          { status: 502 },
        );
      }
    }
    const asset = ASSETS[url.pathname];
    if (!asset) return new Response("Not Found", { status: 404 });
    return new Response(asset.body, {
      headers: { "Content-Type": asset.contentType },
    });
  },
});

console.log(`market-pulse-web listening on :${server.port} → ${BACKEND_URL}`);
