FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# --- Frontend build (runs inside the image — `docker compose up -d --build` is now one step) ---
# 1) Dependency layer: only re-runs `npm install` when package.json changes.
#    DEBIAN_FRONTEND=noninteractive silences the harmless debconf frontend warnings.
COPY frontend/package*.json ./frontend/
RUN DEBIAN_FRONTEND=noninteractive apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && cd /app/frontend && npm install \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
# 2) Source + build. vite.config.ts already writes output to /app/app/static/dist
#    (the dir FastAPI serves), so no extra `cp` is needed — that was the build break.
COPY frontend ./frontend
RUN cd /app/frontend && npm run build

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
