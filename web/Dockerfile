# 运行阶段：nginx:alpine 托管静态资源 + /api 同源反代。
# PORT 与 BACKEND_URL 经官方 20-envsubst-on-templates 在容器启动时注入模板。
FROM nginx:alpine

# 静态资源（nginx 以 root 启动 master，worker 降权；读取无碍）
COPY public/ /usr/share/nginx/html/

# 站点配置模板：启动时由 entrypoint 用环境变量替换 $PORT / $BACKEND_URL
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template

# 默认值保持与旧 Bun 二进制一致（监听 8443，反代 127.0.0.1:8000）
ENV PORT=8443 \
    BACKEND_URL=http://127.0.0.1:8000

EXPOSE 8443

# nginx 官方镜像默认 entrypoint（docker-entrypoint.sh）会自动执行
# /docker-entrypoint.d/20-envsubst-on-templates.sh，将 templates/*.template
# 渲染到 /etc/nginx/conf.d/，然后启动 nginx。
