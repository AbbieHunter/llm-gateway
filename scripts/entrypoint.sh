#!/bin/sh
# Gateway process entrypoint.
# Multi-worker (UVICORN_WORKERS>1) is only safe with a shared DB (Postgres).
# SQLite + multiple workers would corrupt / lock the single file — refuse and fall back.
# --timeout-graceful-shutdown: drain in-flight requests on SIGTERM (rolling deploys).
set -eu

WORKERS="${UVICORN_WORKERS:-1}"
DB_URL="${DATABASE_URL:-sqlite+aiosqlite:///./data/gateway.db}"
GRACE="${UVICORN_GRACEFUL_TIMEOUT_SEC:-30}"

case "$DB_URL" in
  sqlite*)
    if [ "$WORKERS" -gt 1 ]; then
      echo "WARN: UVICORN_WORKERS=$WORKERS with SQLite is unsafe; forcing workers=1." >&2
      echo "WARN: Switch DATABASE_URL to postgresql+asyncpg://... before raising workers." >&2
      WORKERS=1
    fi
    ;;
esac

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 \
  --workers "$WORKERS" \
  --timeout-graceful-shutdown "$GRACE"
