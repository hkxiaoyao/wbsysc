# ===== 构建阶段：前端 =====
FROM node:20-alpine AS frontend
WORKDIR /ui
# 先拷依赖清单，利用层缓存（vite.config.js 的 outDir=../app/static/dist）
COPY admin-ui/package.json admin-ui/pnpm-lock.yaml ./
RUN corepack enable && corepack prepare pnpm@10.34.5 --activate \
    && pnpm install --frozen-lockfile
# 拷源码 + vite 配置 → 构建产物输出到 /ui/../app/static/dist 即 /app/static/dist
COPY admin-ui/ ./
COPY admin-ui/vite.config.js ./
RUN mkdir -p /app/static && pnpm run build
# 产物在 /app/static/dist

# ===== 运行阶段：后端 =====
FROM python:3.11-slim AS runtime
# 时区 + 最小运行时依赖
ENV TZ=Asia/Shanghai PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN sed -i 's@deb.debian.org@mirrors.tuna.tsinghua.edu.cn@g' /etc/apt/sources.list.d/debian.sources 2>/dev/null \
    && apt-get update && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# 后端代码
COPY app/ ./app/
COPY sql/ ./sql/
# 前端构建产物
COPY --from=frontend /app/static/dist ./app/static/dist

# 非 root 运行
RUN useradd -r -u 1000 appuser && chown -R appuser /app
USER appuser

EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fs http://localhost:8001/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
# gunicorn/uvicorn 生产建议单进程+多worker uvloop；MCP session 需粘性，单 worker 最稳
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", \
     "--proxy-headers", "--forwarded-allow-ips", "172.16.0.0/12", \
     "--workers", "1", "--no-access-log"]
