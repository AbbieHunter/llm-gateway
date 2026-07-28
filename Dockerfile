FROM python:3.12-slim

# Avoid interactive debconf prompts during apt installs. Set via ENV (not a RUN
# prefix) so it persists into dpkg postinst sub-scripts and fully silences the
# harmless "unable to initialize frontend" warnings on Debian trixie.
ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# 构建参数：包索引镜像源。默认官方源（本地构建不变）；
# 生产部署(国内服务器)由 compose 注入阿里云/npmmirror 提速。
ARG PIP_INDEX=https://pypi.org/simple/
ARG NPM_REGISTRY=https://registry.npmjs.org/
# apt 源(装 nodejs/npm 用)。默认官方 deb.debian.org；生产换阿里云 Debian 镜像。
ARG APT_MIRROR=deb.debian.org

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i ${PIP_INDEX}

# --- Frontend build (runs inside the image — `docker compose up -d --build` is one step) ---
# 1) Dependency layer: the slow one (apt + npm install). Depends ONLY on
#    package.json, so it is cached unless frontend deps actually change.
COPY frontend/package*.json ./frontend/
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/*.sources /etc/apt/sources.list 2>/dev/null; \
    apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && cd /app/frontend && npm install --registry ${NPM_REGISTRY} \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
# 2) Source + build. vite.config.ts already writes output to /app/app/static/dist
#    (the dir FastAPI serves), so no extra `cp` is needed — that was the build break.
COPY frontend ./frontend
# vite.config.ts outDir is ../app/static/dist — ensure /app/app exists so the
# build can write there before the backend source is copied in.
RUN mkdir -p /app/app && cd /app/frontend && npm run build

# Backend source is placed LAST on purpose: it changes most often, and we do NOT
# want an edit to app/*.py to invalidate (and re-run) the slow frontend layers
# above. With this order a backend-only change rebuilds only these two COPYs.
COPY app ./app
COPY scripts ./scripts

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
